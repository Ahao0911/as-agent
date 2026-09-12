"""
盘前提示生成器 — 当天交易计划卡（不是晨报，是可执行计划）

闭环位置:
    昨日盘后推演 →(读取 forecast_store) 今日盘前计划 → 保存 report_store
    → 收盘后 review_flow 读取本报告做"盘前计划验证"

用法:
    python3 utils/pre_market_flow.py                    # 标准盘前
    python3 utils/pre_market_flow.py --mode simple      # 简单盘前
    python3 utils/pre_market_flow.py --mode deep        # 深度盘前
    python3 utils/pre_market_flow.py --date 2026-08-03  # 指定交易日（默认下一个工作日）

数据截止原则: 只用生成时间前已公开数据；缺失标注"数据缺失"，禁止编造。
推送规则:     默认只保存不推送（auto_push=false），推送由上层 skill 按用户明确指令执行。
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.data_router import load_snapshot, build_market_snapshot, get_news, get_kline_df
from utils.forecast_store import load_latest_forecast, save_forecast, load_forecast_by_date
from utils.report_store import save_report
from utils.trade_journal import get_observations, error_pattern_stats

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")


def _load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"持仓": [], "自选池": []}


def _next_trade_day():
    """下一个工作日（简单规则：跳过周末；节假日需人工注意）"""
    d = datetime.now()
    while d.weekday() >= 5:  # 周六5 周日6
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _get_fx_cny():
    """美元兑人民币（akshare中国银行外汇牌价），失败返回None
    注意：中行牌价按100外币计价（674.48 = 6.7448元/美元），需÷100"""
    try:
        import akshare as ak
        from utils.data_router import _clear_proxy
        _clear_proxy()
        df = ak.currency_boc_sina(symbol="美元", start_date="20200101", end_date="20500101")
        if df is not None and len(df):
            last = df.iloc[-1]
            buy = float(last.get("中行汇买价", 0))
            return {"美元兑人民币": round(buy / 100, 4), "日期": str(last.get("日期", ""))}
    except Exception:
        pass
    return None


def _get_commodity():
    """重要商品（黄金/原油，akshare乐咕宏观），失败返回None"""
    out = {}
    try:
        import akshare as ak
        from utils.data_router import _clear_proxy
        _clear_proxy()
        gold = ak.macro_cons_gold()
        if gold is not None and len(gold):
            last = gold.iloc[-1]
            out["黄金"] = f"库存{last.get('总库存')}吨 总价值{float(last.get('总价值', 0))/1e8:.0f}亿 (COMEX, {last.get('日期')})"
    except Exception:
        pass
    try:
        import akshare as ak
        from utils.data_router import _clear_proxy
        _clear_proxy()
        oil = ak.macro_cons_oil()
        if oil is not None and len(oil):
            last = oil.iloc[-1]
            # 列名兼容：数据/值/总库存等
            val = last.get("总库存") or last.get("数据") or last.get("值")
            out["原油"] = f"{val} ({last.get('日期', '')})"
    except Exception:
        pass
    return out or None


def _clean_us_text(s):
    """清理美股表格文本：去markdown表格线，压缩空白"""
    if not s:
        return s
    lines = []
    for ln in str(s).split("\n"):
        ln = ln.strip()
        if not ln or set(ln) <= {"|", "-", " "}:
            continue
        ln = ln.replace("|", " ").strip()
        if ln:
            lines.append(" ".join(ln.split()))
    return "；".join(lines)[:400]


def _parse_us_market(us_value, depth=0):
    """美股iFinD返回解析：逐层剥嵌套JSON/列表/字符串，提取关键指数收盘/涨跌幅"""
    if depth > 8 or us_value is None:
        return "数据缺失"
    # 字符串：尝试JSON解析，否则原文
    if isinstance(us_value, str):
        s = us_value.strip()
        if s.startswith(("{", "[")):
            try:
                return _parse_us_market(json.loads(s), depth + 1)
            except Exception:
                return _clean_us_text(s[:400])
        return _clean_us_text(s[:400])
    # 列表：取第一个元素
    if isinstance(us_value, list):
        return _parse_us_market(us_value[0] if us_value else None, depth + 1)
    # dict：按优先级找 answer > text > content[0].text > data
    if isinstance(us_value, dict):
        for key in ("answer", "text", "summary"):
            if key in us_value and us_value[key]:
                v = us_value[key]
                if isinstance(v, str) and v.startswith(("{", "[")):
                    try:
                        return _parse_us_market(json.loads(v), depth + 1)
                    except Exception:
                        return v[:200]
                return _parse_us_market(v, depth + 1)
        if "content" in us_value and isinstance(us_value["content"], list) and us_value["content"]:
            return _parse_us_market(us_value["content"][0], depth + 1)
        if "result" in us_value:
            return _parse_us_market(us_value["result"], depth + 1)
        if "data" in us_value:
            return _parse_us_market(us_value["data"], depth + 1)
        return _clean_us_text(str(us_value))[:400]
    return _clean_us_text(str(us_value))[:400]


def _market_value_status():
    """活跃市值状态(指南针App数据+CSV历史,含择时信号)"""
    try:
        from utils.data_router import get_market_value_with_meta
        return get_market_value_with_meta()
    except Exception:
        pass
    try:
        from utils.market_value import get_active_market_value
        mv = get_active_market_value()
        if mv:
            return {"value": mv, "source": mv.get("数据源", "CSV")}
    except Exception:
        pass
    return None


def build_pre_snapshot():
    """盘前快照：复用当日缓存快照（盘前实为昨日收盘数据）+ 汇率商品补充"""
    snap = load_snapshot()
    if not snap:
        try:
            snap = build_market_snapshot()
        except Exception as e:
            snap = {"生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "错误": str(e)[:120]}
    snap["汇率"] = _get_fx_cny()
    snap["商品"] = _get_commodity()
    snap["活跃市值"] = _market_value_status()
    return snap


def analyze_watch_stock(code, name=""):
    """标的级分析（黄白线+砖型图+单针+B1），复用 daily_review.check_stock"""
    try:
        from utils.daily_review import check_stock
        r = check_stock(code, name)
        return r
    except Exception as e:
        return {"代码": code, "名称": name, "错误": str(e)[:80]}


def risk_level(snap, regime):
    """
    盘前风险等级: 低/中/较高/高 + 仓位建议
    依据: 活跃市值趋势 + 市场宽度 + 黄白线(大盘参考辅助) + 海外
    """
    try:
        mv = snap.get("活跃市值")
        mv_val = mv.get("value") if isinstance(mv, dict) else mv
        mv_yi = (mv_val or {}).get("活跃市值", 0) if isinstance(mv_val, dict) else 0
        mv_trend = (mv_val or {}).get("趋势", "") if isinstance(mv_val, dict) else ""
    except Exception:
        mv_yi = 0
        mv_trend = ""
    try:
        sent = snap["情绪"]["value"]
        width = sent.get("上涨家数", 0) / max(1, sent.get("上涨家数", 0) + sent.get("下跌家数", 0)) * 100
    except Exception:
        width = 50
    try:
        amt = snap["成交额"]["value"].get("沪深合计", 0)
    except Exception:
        amt = 0

    # 活跃市值判断: 看趋势方向与择时信号(2026-08-10改版,不看绝对值)
    #   图形买点体系: 4%阳线入场/ -2.3%离场, 趋势上行=增量资金进场
    mv_trend_up = "上行" in mv_trend or "进场" in mv_trend
    mv_trend_down = "回落" in mv_trend or "离场" in mv_trend
    mv_high = mv_trend_up          # 趋势上行=可积极
    mv_low = mv_trend_down         # 趋势下行=防守
    width_high = width >= 55
    amt_high = amt >= 15000

    if mv_high and width_high and amt_high:
        return {"风险等级": "低", "仓位区间": "5-7成", "进攻": True, "搬砖": True, "等待确认": False}
    if mv_high and (width_high or amt_high):
        return {"风险等级": "中", "仓位区间": "3-5成", "进攻": False, "搬砖": True, "等待确认": False}
    if mv_low or width < 45:
        return {"风险等级": "高", "仓位区间": "0-3成", "进攻": False, "搬砖": False, "等待确认": True}
    return {"风险等级": "较高", "仓位区间": "3成内", "进攻": False, "搬砖": False, "等待确认": True}


def scenarios(snap, prev_forecast=None):
    """
    三情景（基准/强势/弱势，概率总和100%）
    以昨日盘后推演为基线，叠加盘前隔夜信息微调
    """
    try:
        sent = snap["情绪"]["value"]
        width = sent.get("上涨家数", 0) / max(1, sent.get("上涨家数", 0) + sent.get("下跌家数", 0)) * 100
        zt = sent.get("涨停", 0)
    except Exception:
        width, zt = 50, 0
    try:
        amt = snap["成交额"]["value"].get("沪深合计", 0)
    except Exception:
        amt = 0
    try:
        kospi = snap["韩股"]["value"]
        kospi_chg = kospi.get("涨跌幅", 0) if kospi else 0
    except Exception:
        kospi_chg = 0

    # 基线概率（可被 prev_forecast 覆盖）
    prev_probs = None
    if prev_forecast and prev_forecast.get("预测"):
        scen = prev_forecast["预测"].get("三情景") or prev_forecast["预测"].get("情景")
        if scen:
            prev_probs = {s.get("情景", "").replace("情景", ""): s.get("概率", 0) for s in scen}

    if width > 65 and amt > 20000:
        base, strong, weak = 40, 40, 20
        base_cond = "宽度高位+量能充足，延续概率大"
    elif width > 45 and amt > 15000:
        base, strong, weak = 50, 25, 25
        base_cond = "结构性行情，震荡为主"
    else:
        base, strong, weak = 40, 15, 45
        base_cond = "宽度弱/量能不足，防守为主"

    # 韩股隔夜异动修正
    if kospi_chg <= -1.5:
        strong = max(5, strong - 8)
        weak = min(70, weak + 8)
        base = 100 - strong - weak
    elif kospi_chg >= 1.5:
        strong = min(70, strong + 8)
        weak = max(5, weak - 8)
        base = 100 - strong - weak

    return {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "数据截止": "前一交易日A股收盘 + 韩股截至当前 + 美股上一交易日（若数据缺失则注明）",
        "三情景": [
            {
                "情景": "基准情景", "概率": base,
                "核心依据": base_cond,
                "触发条件": "高开不追，回踩黄白线企稳或平开震荡，宽度不恶化",
                "失效条件": "开盘30分钟内指数跌破昨日低点",
                "对应仓位": "按风险等级建议仓位执行",
                "交易策略": "以持仓管理为主，等候选池触发条件再开新仓",
            },
            {
                "情景": "强势情景", "概率": strong,
                "核心依据": f"宽度{width:.0f}%+涨停{zt}家+韩股{kospi_chg:+.1f}%",
                "触发条件": "放量高开不回补缺口、主线板块共振、宽度>60%",
                "失效条件": "高开低走翻绿或放量滞涨",
                "对应仓位": "可提高至上限（活跃市值配合时）",
                "交易策略": "只做计划内A级候选，不在竞价直接追高",
            },
            {
                "情景": "弱势情景", "概率": weak,
                "核心依据": "宽度或量能不足/海外转弱",
                "触发条件": "低开低走、活跃市值下降、跌停家数增多",
                "失效条件": "缩量企稳、宽度快速修复",
                "对应仓位": "降至3成内，只做确定性B1",
                "交易策略": "持仓破位按计划止损，不开新仓",
            },
        ],
        "概率校验": f"{base}+{strong}+{weak}=100",
    }


def watch_review(positions):
    """持仓逐只复查（黄白线/砖型/B1/支撑失效/建议）"""
    result = []
    for p in positions:
        code = p.get("代码", "")
        if not code or "待确认" in code:
            result.append({"代码": code, "名称": p.get("名称"), "结论": "代码待确认，跳过"})
            continue
        r = analyze_watch_stock(code, p.get("名称", ""))
        r["成本"] = p.get("成本")
        r["止损"] = p.get("止损")
        r["计划动作"] = p.get("动作")
        result.append(r)
    return result


def candidate_pool(watch_pool):
    """候选池分级：A接近完成/B需确认/C仅观察（个股用黄白线+砖型；ETF用均线+量能）"""
    result = []
    for c in watch_pool:
        code = c.get("代码", "")
        r = analyze_watch_stock(code, c.get("名称", ""))
        if "错误" in r:
            result.append({"代码": code, "名称": c.get("名称"), "级别": "C", "理由": r["错误"]})
            continue
        if r.get("类型") == "ETF":
            bull = r.get("均线多头", False)
            vol_ok = r.get("量能比", 0) >= 1.2
            dev = r.get("偏离MA20", 99)
            if bull and vol_ok and -3 < dev < 5:
                grade, why = "A", "均线多头+放量+贴近MA20，条件接近完成"
            elif bull and vol_ok:
                grade, why = "B", "均线多头+放量但位置偏高，等回踩"
            elif bull:
                grade, why = "B", "均线多头但量能不足，等放量确认"
            else:
                grade, why = "C", "均线空头（MA20<MA60），仅观察"
        else:
            bull = r.get("多头", False)
            brick_ok = r.get("翻红", False)
            dev = r.get("偏离黄线", 99)
            if bull and brick_ok and -5 < dev < 5:
                grade, why = "A", "多头+翻红+贴近黄线，条件接近完成"
            elif bull and brick_ok:
                grade, why = "B", "多头+翻红但位置偏高，等回踩"
            elif bull:
                grade, why = "B", "多头但砖型绿柱，等翻红确认"
            else:
                grade, why = "C", "空头区间，仅观察"
        r["级别"] = grade
        r["入选理由"] = why
        r["原入选理由"] = c.get("入选理由")
        result.append(r)
    return result


def forbidden_items():
    """今日禁止事项：基于行为观察统计，最多3条"""
    stats = error_pattern_stats()
    obs = get_observations(7)
    patterns = []
    # 优先取近7天有记录的
    from collections import Counter
    cnt = Counter(o.get("行为", "") for o in obs)
    # 映射到可执行禁止条款
    rule_map = {
        "追高": "不在黄白线未确认时提前入场，不因竞价高开直接追涨",
        "提前入场": "开盘30分钟内不冲动开仓，等黄白线/宽度确认",
        "仓位过大": "单笔仓位不超过计划上限",
        "止损迟疑": "触发止损价立即执行，不犹豫",
        "计划外交易": "不开仓前计划外的标的",
        "冲动加仓": "不进行计划外补仓，做T不演变成被动加仓",
        "报复性交易": "不因上一笔亏损提高下一笔仓位",
        "恐慌卖出": "不在恐慌低点割肉，按计划位执行",
        "频繁操作": "减少无效操作频率",
        "过早卖出": "盈利持仓不因盘中波动过早卖出",
        "单日打满": "T+1品种单日建仓不超过计划仓位50%，禁止当日打满（2026-08-02新增）",
        "逆势加仓": "价格连续创新低时禁止加仓，加仓必须等企稳确认（2026-08-02新增）",
        "下跌博反弹": "下跌趋势博反弹只做试探仓(≤计划1/3)，止损3%即撤，不越跌越买（2026-08-02新增）",
    }
    for k, v in cnt.items():
        if v >= 1 and k in rule_map and rule_map[k] not in patterns:
            patterns.append(rule_map[k])
        if len(patterns) >= 3:
            break
    # 兜底：stats里高频的
    for p, n in stats.items():
        if isinstance(n, int) and n >= 2 and len(patterns) < 3:
            for k, rule in rule_map.items():
                if k in p and rule not in patterns:
                    patterns.append(rule)
    return patterns[:3] or ["不因单日行情改变既定计划"]


def build_execution_card(risk, scenarios_data, positions, watch_pool):
    """今日执行卡"""
    strong = scenarios_data["三情景"][1]
    weak = scenarios_data["三情景"][2]
    # 首要观察指标
    watch_items = ["标的自身趋势（个股黄白线/ETF均线20/60）", "成交额是否>1.5万亿", "市场宽度是否>55%"]
    return {
        "建议总仓位": risk["仓位区间"],
        "风险等级": risk["风险等级"],
        "首要观察指标": watch_items,
        "允许交易的条件": "个股黄白线多头+砖型翻红 / ETF均线多头+放量 + 计划内A级候选",
        "必须减仓的条件": f"弱势情景触发（{weak['触发条件']}）→ 降至3成内",
        "强势情景应对": f"若{strong['触发条件']}，仅加计划内标的，不追竞价",
        "今日主线观察": "活跃市值方向 + 板块均线20/60 + 前日主线持续性",
        "今日最大风险": weak["触发条件"],
        "需要避免的个人错误": forbidden_items(),
    }


def render_report(mode, snap, risk, scen, watch_rev, cand, forbid, card, prev_forecast):
    """渲染报告（md文本 + 结构化dict）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_cut = "前一交易日A股收盘 / 韩股截至当前 / 美股上一交易日（缺失项标注）"

    md = []
    # ── 第一行: 活跃市值择时结论(指南针App, 用户每日补充) ──
    mv = snap.get("活跃市值")
    if mv:
        mv_val = mv.get("value") or mv
        if isinstance(mv_val, dict) and mv_val.get("活跃市值"):
            lv = mv_val.get("择时级别", "未知")
            sig = mv_val.get("择时信号", "")
            trend = mv_val.get("趋势", "")
            md.append("╔══════════════════════════════════════════════════════╗")
            md.append(f"║ 📡 今日择时: 活跃市值 {mv_val['活跃市值']:.0f} ({mv_val.get('涨跌幅', 0):+.2f}%)  [{lv}]")
            md.append(f"║    信号: {sig}")
            md.append(f"║    趋势: {trend}")
            md.append("╚══════════════════════════════════════════════════════╝")
        else:
            mv_legacy = mv_val.get("活跃市值") if isinstance(mv_val, dict) else None
            md.append(f"- 活跃市值：{mv_legacy if mv_legacy is not None else '数据缺失'}（{mv_val.get('日期', '') if isinstance(mv_val, dict) else ''}）")
    else:
        md.append("╔══════════════════════════════════════════════════════╗")
        md.append("║ 📡 今日择时: 活跃市值【待补充】— 请从指南针App查看后告诉我 ║")
        md.append("╚══════════════════════════════════════════════════════╝")
    md.append("")

    md.append("╔══════════════════════════════════════╗")
    md.append("║  今日一句话计划（见下方风险等级与情景） ║")
    md.append("╚══════════════════════════════════════╝")

    # 一句话计划（简单模式核心）
    one_line = (f"风险{risk['风险等级']}，建议仓位{risk['仓位区间']}；"
                f"基准{scen['三情景'][0]['概率']}%/强势{scen['三情景'][1]['概率']}%/弱势{scen['三情景'][2]['概率']}%；"
                f"首要条件=个股黄白线多头+砖型翻红/ETF均线多头+放量，弱势触发即降仓防守")
    md.append(f"▶ 一句话计划：{one_line}")
    md.append("")

    md.append("## 一、盘前风险等级")
    md.append(f"- 风险等级：**{risk['风险等级']}**")
    md.append(f"- 建议仓位区间：{risk['仓位区间']}")
    md.append(f"- 适合主动进攻：{'✓' if risk['进攻'] else '✗'}　适合搬砖做T：{'✓' if risk['搬砖'] else '✗'}　适合等待确认：{'✓' if risk['等待确认'] else '✗'}")
    md.append("")

    md.append("## 二、隔夜市场摘要（数据截止：报告生成时已公开）")
    try:
        us = snap["美股"]["value"]
        md.append(f"- 美股：{_parse_us_market(us)}")
    except Exception:
        md.append("- 美股：数据缺失")
    try:
        kospi = snap["韩股"]["value"]
        md.append(f"- 韩国KOSPI：{kospi.get('收盘')}（{kospi.get('涨跌幅'):+.2f}%，{kospi.get('日期','')}）" if kospi else "- 韩国KOSPI：数据缺失")
    except Exception:
        md.append("- 韩国KOSPI：数据缺失")
    fx = snap.get("汇率")
    if fx:
        md.append(f"- 美元兑人民币：{fx.get('美元兑人民币', '数据缺失')}（{fx.get('日期', '')}）")
    else:
        md.append("- 美元兑人民币：数据缺失")
    com = snap.get("商品")
    if com:
        for k, v in com.items():
            md.append(f"- {k}：{v}")
    else:
        md.append("- 商品：数据缺失")
    # 隔夜新闻（妙想mx优先 → iFinD兜底，2026-08-03接入）
    news = []
    news_source = "妙想mx"
    for kw in ["A股 政策", "央行 流动性"]:
        try:
            r = get_news(kw, days=1, size=3)
            if isinstance(r, dict) and r.get("status") == "OK" and r.get("source") == "mx妙想":
                news_source = "妙想mx"
                # 妙想返回的是markdown文本，直接截取要点
                text = r.get("text", "")
                for line in text.splitlines():
                    if line.strip().startswith(("---", "日期:", "搜索")):
                        continue
                    if line.strip().startswith(("1.", "2.", "3.")) and len(line) > 10:
                        news.append(line.strip()[:80])
                    elif line.strip() and len(line) > 20 and not line.startswith(("**", "|", "*查询")):
                        news.append(line.strip()[:80])
            elif isinstance(r, str) and r and "为空" not in r:
                from utils.review_flow import _parse_news_text
                news.extend(_parse_news_text(r))
            elif isinstance(r, dict) and "text" in r:
                from utils.review_flow import _parse_news_text
                news.extend(_parse_news_text(r.get("text", "")))
        except Exception:
            continue
    if news:
        md.append(f"- 隔夜要闻（{news_source}）：")
        for n in news[:3]:
            md.append(f"  ▸ {n}")
    else:
        md.append("- 隔夜要闻：暂无（妙想/iFinD不可用，需人工补充）")
    md.append("")

    md.append("## 三、三种盘中情景")
    for s in scen["三情景"]:
        md.append(f"### {s['情景']}（{s['概率']}%）")
        md.append(f"- 核心依据：{s['核心依据']}")
        md.append(f"- 触发条件：{s['触发条件']}")
        md.append(f"- 失效条件：{s['失效条件']}")
        md.append(f"- 对应仓位：{s['对应仓位']}")
        md.append(f"- 交易策略：{s['交易策略']}")
        md.append("")
    md.append(f"概率校验：{scen['概率校验']}")
    md.append("")

    md.append("## 四、开盘后观察清单")
    obs_list = [
        ("标的自身趋势", "个股黄白线多头/ETF均线多头+放量", "个股跌破黄线/ETF跌破MA20", "空头区禁止开仓"),
        ("成交额", ">1.5万亿维持活跃", "<1.2万亿萎缩", "降仓"),
        ("市场宽度", "上涨家数>55%", "<40%", "降仓防守"),
        ("主线板块", "前日主线延续/新主线出现", "主线散乱无领涨", "降低开仓意愿"),
        ("高位股反馈", "高位股不炸板", "炸板率上升", "规避追高"),
    ]
    for name, confirm, neg, impact in obs_list:
        md.append(f"- **{name}**：确认={confirm}｜否定={neg}｜仓位影响={impact}")
    md.append("")

    md.append("## 五、持仓复查")
    if watch_rev:
        md.append("| 标的 | 收盘 | 趋势 | 量能/砖型 | 偏离 | 止损 | 建议 |")
        md.append("|------|------|------|-----------|------|------|------|")
        for r in watch_rev:
            if "结论" in r and "错误" not in r and "代码待确认" in str(r.get("结论")):
                md.append(f"| {r['代码']} {r['名称']} | - | - | - | - | - | {r['结论']} |")
                continue
            if "错误" in r:
                md.append(f"| {r['代码']} {r['名称']} | - | - | - | - | - | 数据错误 |")
                continue
            if r.get("类型") == "ETF":
                dual = "★均线多头" if r.get("均线多头") else "✗均线空头"
                md.append(f"| {r['代码']} {r['名称']} | {r.get('收盘')} | {dual} | {r.get('量能')}({r.get('量能比')}x) | MA20{r.get('偏离MA20')}% | {r.get('止损','-')} | {r.get('结论')} |")
            else:
                dual = "★多头" if r.get("多头") else "✗空头"
                md.append(f"| {r['代码']} {r['名称']} | {r.get('收盘')} | {dual} | {r.get('砖型')} | 黄线{r.get('偏离黄线')}% | {r.get('止损','-')} | {r.get('结论')} |")
    else:
        md.append("- 无持仓记录")
    md.append("")

    md.append("## 六、今日候选观察池")
    if cand:
        md.append("| 代码 | 名称 | 级别 | 入选理由 | 尚未满足/触发条件 |")
        md.append("|------|------|------|----------|------------------|")
        for c in cand:
            md.append(f"| {c['代码']} {c['名称']} | **{c['级别']}** | {c.get('入选理由')} | 触发={c.get('结论')} |")
    else:
        md.append("- 无候选池记录")
    md.append("")

    md.append("## 七、今日禁止事项")
    for i, f in enumerate(forbid, 1):
        md.append(f"{i}. {f}")
    md.append("")

    md.append("## 八、今日执行卡")
    md.append(f"- 建议总仓位：**{card['建议总仓位']}**")
    md.append(f"- 风险等级：{card['风险等级']}")
    md.append(f"- 首要观察指标：{' / '.join(card['首要观察指标'])}")
    md.append(f"- 允许交易的条件：{card['允许交易的条件']}")
    md.append(f"- 必须减仓的条件：{card['必须减仓的条件']}")
    md.append(f"- 今日最大风险：{card['今日最大风险']}")
    md.append("")

    if mode == "deep" and prev_forecast:
        md.append("## 九、与昨日推演对比")
        prev = prev_forecast.get("预测", {})
        prev_scen = prev.get("三情景") or prev.get("情景")
        if prev_scen:
            md.append(f"- 昨日（{prev_forecast.get('生成时间','')}）推演：")
            for s in prev_scen[:3]:
                md.append(f"  ▸ {s.get('情景')} {s.get('概率')}%")
        md.append(f"- 今日调整：概率已按盘前数据修正（见三情景）")
        md.append("")

    md.append(f"---")
    md.append(f"生成时间：{now}　数据截止：{data_cut}　模式：{mode}")
    md.append("⚠ 本报告为观察计划，非买卖指令；所有价格/偏离为盘面数据，止损以用户确认为准")

    report = {
        "生成时间": now,
        "模式": mode,
        "数据截止": data_cut,
        "一句话计划": one_line,
        "风险等级": risk,
        "隔夜市场": {
            "美股": snap.get("美股", {}).get("value") if isinstance(snap.get("美股"), dict) else None,
            "韩股": snap.get("韩股", {}).get("value") if isinstance(snap.get("韩股"), dict) else None,
            "汇率": snap.get("汇率"),
            "商品": snap.get("商品"),
            "活跃市值": snap.get("活跃市值"),
            "新闻": news[:3] if news else [],
        },
        "三情景": scen,
        "观察清单": obs_list,
        "持仓复查": watch_rev,
        "候选池": cand,
        "禁止事项": forbid,
        "执行卡": card,
        "昨日推演对比": (prev_forecast.get("预测", {}) if mode == "deep" else None),
    }
    return "\n".join(md), report


def generate_pre_market(mode="standard", date_str=""):
    """
    盘前提示主入口
    mode: simple / standard / deep
    date_str: 报告对应交易日（默认下一个工作日）
    """
    date_str = date_str or _next_trade_day()
    snap = build_pre_snapshot()

    # 昨日盘后推演（闭环起点）
    prev_forecast = load_latest_forecast(ftype="post_market")

    wl = _load_watchlist()
    positions = wl.get("持仓", [])
    watch_pool = wl.get("自选池", [])

    # 简单模式：跳过逐票分析
    if mode == "simple":
        risk = risk_level(snap, None)
        scen = scenarios(snap, prev_forecast)
        watch_rev, cand = [], []
    else:
        risk = risk_level(snap, None)
        scen = scenarios(snap, prev_forecast)
        watch_rev = watch_review(positions)
        cand = candidate_pool(watch_pool) if watch_pool else []

    forbid = forbidden_items()
    card = build_execution_card(risk, scen, positions, watch_pool)

    md_text, report = render_report(mode, snap, risk, scen, watch_rev, cand, forbid, card, prev_forecast)

    # 保存报告 + 预测历史
    paths = save_report("pre_market", report, md_text, date_str)
    save_forecast(date_str, "pre_market", {"三情景": scen["三情景"], "一句话计划": report["一句话计划"]},
                  source=f"盘前提示{date_str}")

    return report, paths


def main():
    parser = argparse.ArgumentParser(description="盘前提示")
    parser.add_argument("--mode", choices=["simple", "standard", "deep"], default="standard")
    parser.add_argument("--date", default="", help="报告对应交易日 YYYY-MM-DD")
    parser.add_argument("--quiet", action="store_true", help="只输出保存路径")
    args = parser.parse_args()

    report, paths = generate_pre_market(args.mode, args.date)
    if args.quiet:
        print(f"✓ 盘前提示已保存: {paths['md']}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
