"""
盘后复盘主流程（Orchestrator）
数据快照→市场环境识别→指标战法复盘→个人操作复盘→行情多维复盘→明日情景推演→报告

设计要点（GPT建议落地）:
- 市场数据只抓取一次（build_market_snapshot），各模块复用
- 评价决策不评价结果：六项评分30分制，盈亏单独展示不计入
- 明日推演是三情景树不是猜涨跌
- 无操作时跳过逐笔评分
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.data_router import (build_market_snapshot, load_snapshot,
                               get_kline_df, get_news)
from utils.trade_journal import (get_today_trades, has_today_trades,
                                 add_observation, error_pattern_stats)
from utils.forecast_store import (save_forecast, load_forecast_by_date,
                                  load_latest_forecast, verify_forecast)
from utils.report_store import save_report, load_report
from utils.indicators import analyze_dual_line, analyze_trend
from utils.brick import brick_signal
from utils.needle20 import needle20_signal
from utils.lower_shadow import lower_shadow_signal
from utils.battle import analyze_nodes, analyze_red_fat_green_thin
from utils.turnover import turnover_signal
from utils.brick_carry import carry_signal

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def parse_quote_table(value):
    """
    兼容两种快照格式，统一解析为 [{名称, 最新价, 涨跌幅}, ...]:
    1. 结构化列表: [{"名称": ..., "涨跌幅": ...}, ...]
    2. 妙想/iFinD 文本表格: {"text": "| 名称 | 价格 | 涨跌幅 |"}
    解析失败返回空列表（调用方按"数据缺失"处理）。
    """
    import re
    if isinstance(value, list):
        return [d for d in value if isinstance(d, dict)]
    text = ""
    if isinstance(value, dict):
        text = value.get("text") or value.get("output") or ""
    elif isinstance(value, str):
        text = value
    if not text:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|") if c.strip()]
        if len(cells) < 2:
            continue
        name = cells[0]
        # 智能定位涨跌幅单元格（妙想两种列序: |名称|价格|涨跌幅| 或 |名称|涨跌幅|最新价|）
        chg_idx, chg = None, None
        for i, c in enumerate(cells[1:], start=1):
            m = re.match(r"^[+-]?\d+(?:\.\d+)?%$", c)
            if m:
                chg_idx, chg = i, float(c.rstrip("%"))
                break
        if chg is None:
            continue
        # 价格 = 涨跌幅单元格旁边的非%数字单元格
        price = "-"
        for i in (chg_idx - 1, chg_idx + 1):
            if 0 < i < len(cells):
                c = cells[i]
                if c and re.match(r"^[+-]?\d[\d,\.]*万?$", c):
                    price = c
                    break
        rec = {"名称": name, "涨跌幅": chg, "最新价": price}
        m_price = re.search(r"-?\d+(?:\.\d+)?", str(price).replace(",", "").replace("万", ""))
        if m_price:
            rec["价格"] = float(m_price.group())
        out.append(rec)
    return out


# ===================== 1. 数据快照 =====================

def market_snapshot():
    """市场快照（复用缓存，只抓一次）"""
    snap = load_snapshot()
    if snap:
        return snap
    return build_market_snapshot()


# ===================== 2. 市场环境识别 =====================

def market_regime(snap):
    """
    市场环境识别：趋势/震荡/退潮/修复
    依据: 涨跌家数占比、成交额、指数涨跌
    """
    result = {}
    try:
        idx_raw = snap["指数"]["value"]
        idx = parse_quote_table(idx_raw)
        up = sum(1 for d in idx if d.get("涨跌幅", 0) > 0)
        total = len(idx)
        up_ratio = up / total if total else 0

        sent = snap["情绪"]["value"]
        up_cnt = sent.get("上涨家数", 0)
        down_cnt = sent.get("下跌家数", 0)
        width = up_cnt / max(1, up_cnt + down_cnt) * 100  # 市场宽度

        amt = snap["成交额"]["value"]
        amount_yi = amt.get("沪深合计", 0)

        # 判断
        if width > 65:
            regime = "普涨/趋势行情"
        elif width > 50:
            regime = "结构性偏强"
        elif width > 40:
            regime = "结构性分化（震荡）"
        else:
            regime = "退潮/防守"

        result = {
            "市场宽度": round(width, 1),
            "成交额(亿)": amount_yi,
            "环境": regime,
            "结论": {
                "普涨/趋势行情": "可积极参与B1/B2，仓位可拉高",
                "结构性偏强": "精选主线，仓位5-7成",
                "结构性分化（震荡）": "搬砖为主，仓位半仓内",
                "退潮/防守": "防守为主，仓位3成内",
            }.get(regime, "观望"),
        }
    except Exception as e:
        result = {"环境": "识别失败", "错误": str(e)[:80]}
    return result


# ===================== 3. 指标战法复盘 =====================

def stock_analysis(code, bars=250):
    """单票全指标分析（统一路由取数，缓存+腾讯/mootdx）
    ETF用均线20/60+量能（图形买点体系不适用）；个股用黄白线+砖型+单针+B1"""
    df, meta = get_kline_df(code, bars=bars)
    if df is None or len(df) < 60:
        return {"错误": "数据不足", "数据源": meta.get("source") if meta else None}
    from utils.daily_review import is_etf
    if is_etf(code):
        close = df['close'].iloc[-1]
        ma20 = df['close'].rolling(20).mean().iloc[-1]
        ma60 = df['close'].rolling(60).mean().iloc[-1]
        vol = df['volume'].iloc[-1]
        vol_ma20 = df['volume'].iloc[-21:-1].mean()
        vol_ratio = vol / vol_ma20 if vol_ma20 else 1.0
        bull = bool(ma20 > ma60)
        return {
            "代码": code,
            "类型": "ETF",
            "数据源": meta.get("source"),
            "均线": {
                "MA20": float(ma20), "MA60": float(ma60),
                "多头": bull,
                "偏离MA20": float(round((close - ma20) / ma20 * 100, 1)) if ma20 else 0,
                "量能比": float(round(vol_ratio, 2)),
                "量能": "放量" if vol_ratio >= 1.5 else "缩量" if vol_ratio <= 0.7 else "平量",
                "收阳": bool(close >= df['open'].iloc[-1]),
            },
            "df": df,  # 供评分模块使用
        }
    dual = analyze_dual_line(df)
    brick = brick_signal(df, dual)
    needle = needle20_signal(df, dual)
    needle_pattern = lower_shadow_signal(df, lookback=60)
    battle = analyze_nodes(df, dual, brick, needle, needle_pattern=needle_pattern)
    carry = carry_signal(df, dual, needle)
    return {
        "代码": code,
        "类型": "个股",
        "数据源": meta.get("source"),
        "黄白线": dual,
        "砖型图": brick,
        "单针下20": needle,
        "B1战法": battle,
        "搬砖": carry,
        "df": df,  # 供评分模块使用
    }


def strategy_review(snap, watch_codes=None):
    """
    指标与战法复盘（大盘级别）
    watch_codes: 自选股列表，逐只分析
    """
    review = {}
    # 大盘环境
    review["环境"] = market_regime(snap)

    # 自选股逐只
    review["个股"] = {}
    for code in (watch_codes or []):
        review["个股"][code] = stock_analysis(code)
    return review


# ===================== 4. 个人操作复盘（六项评分） =====================

def score_trade(trade, analysis):
    """
    单笔操作六项评分（0-5分/项，总分30）
    评价决策不评价结果！盈亏单独展示
    个股用黄白线+砖型图；ETF用均线20/60+量能
    返回含"细则": {项名: {得分, 说明:[]}} 供报告逐项展示
    """
    df = analysis.get("df") if analysis else None
    is_etf = analysis.get("类型") == "ETF"
    dual = analysis.get("黄白线", {}) if analysis else {}
    brick = analysis.get("砖型图", {}) if analysis else {}
    needle = analysis.get("单针下20", {}) if analysis else {}
    battle = analysis.get("B1战法", {}) if analysis else {}
    ma = analysis.get("均线", {}) if analysis else {}

    scores = {}
    notes = []
    # 细则: 每项评分独立收集说明
    detail = {k: [] for k in ["方向前提", "战法匹配", "入场位置", "仓位管理", "执行纪律"]}
    def _add(key, text):
        notes.append(text)
        detail[key].append(text)

    # 1. 方向前提（个股黄白线多头？ETF均线多头？）
    if is_etf:
        bull = ma.get("多头", False)
        if trade["方向"] == "buy":
            scores["方向前提"] = 5 if bull else 1
            if not bull:
                _add("方向前提", f"✗ 均线空头（MA20 {ma.get('MA20', '?')} < MA60 {ma.get('MA60', '?')}）买入，违反硬前提")
            else:
                _add("方向前提", f"✓ 均线多头（MA20 {ma.get('MA20', '?')} > MA60 {ma.get('MA60', '?')}）")
        else:
            scores["方向前提"] = 3  # 卖出方向不强约束
            _add("方向前提", "卖出操作，方向前提中性（3/5）")
    else:
        bull = dual.get("多头区间", False)
        if trade["方向"] == "buy":
            scores["方向前提"] = 5 if bull else 1
            if not bull:
                _add("方向前提", "✗ 黄白线空头区间买入，违反硬前提")
            else:
                _add("方向前提", "✓ 黄白线多头区间")
        else:
            scores["方向前提"] = 3  # 卖出方向不强约束
            _add("方向前提", "卖出操作，方向前提中性（3/5）")

    # 2. 战法匹配（是否有战法依据）
    has_strategy = bool(trade.get("操作理由"))
    b1_signal = battle.get("节点", []) if battle else []
    if has_strategy and "B1" in b1_signal:
        scores["战法匹配"] = 5
        _add("战法匹配", "✓ 有操作理由 + B1节点信号")
    elif has_strategy:
        scores["战法匹配"] = 4
        _add("战法匹配", "✓ 有操作理由（需人工核对战法）")
    else:
        scores["战法匹配"] = 0
        _add("战法匹配", "✗ 无操作理由记录（计划外交易）")

    # 3. 入场位置（追高？低位？）
    if trade["方向"] == "buy":
        if is_etf:
            dev20 = ma.get("偏离MA20", 99)
            vol_ratio = ma.get("量能比", 1.0)
            if -3 <= dev20 <= 5:
                scores["入场位置"] = 4
                _add("入场位置", f"✓ 贴近MA20（偏离{dev20:+.1f}%）")
            elif dev20 < -3:
                scores["入场位置"] = 3
                _add("入场位置", f"○ 低于MA20（偏离{dev20:+.1f}%，位置偏低）")
            else:
                scores["入场位置"] = 1
                notes.append(f"✗ 高位追入（偏离MA20 {dev20:+.1f}%）")
            if vol_ratio >= 1.2:
                scores["入场位置"] = min(5, scores["入场位置"] + 1)
                _add("入场位置", f"✓ 放量确认（量能比{vol_ratio:.1f}x）")
        else:
            j_val = dual.get("J值", 50)
            if j_val <= 20:
                scores["入场位置"] = 5
                _add("入场位置", f"✓ 低位（J={j_val:.0f}）")
            elif j_val <= 60:
                scores["入场位置"] = 3
                _add("入场位置", f"○ 中位（J={j_val:.0f}）")
            else:
                scores["入场位置"] = 1
                _add("入场位置", f"✗ 高位追入（J={j_val:.0f}）")
            if brick and brick.get("绿翻强红"):
                scores["入场位置"] = min(5, scores["入场位置"] + 1)
                _add("入场位置", "✓ 绿翻强红动量确认")
    else:
        scores["入场位置"] = 3
        _add("入场位置", "卖出操作位置中性（3/5）")

    # 4. 仓位管理（未知则中性）
    shares = trade.get("手数", 0)
    if shares > 0:
        price = trade.get("成交均价", 0)
        # ETF以份为单位（1份=1份），股票以手为单位（1手=100股）
        unit = trade.get("单位", "手")
        shares_unit = trade.get("股数", shares * 100)
        amount = shares_unit * price
        unit_label = "份" if unit == "份" else "手"
        scores["仓位管理"] = 3
        _add("仓位管理", f"金额约{amount:.0f}元（{shares}{unit_label}×{price}，相对总资金比例需人工确认）")
    else:
        scores["仓位管理"] = 1
        _add("仓位管理", "✗ 未记录仓位信息")

    # 5. 执行纪律（计划内还是计划外）
    scores["执行纪律"] = 3
    if trade.get("计划外"):
        scores["执行纪律"] = 0
        _add("执行纪律", "✗ 计划外交易")
    else:
        _add("执行纪律", "○ 需确认是否按计划执行")

    # ⚠ 2026-08-13 用户要求: 撤掉「止损/止盈计划」展示(原"风险计划"评分项)。
    #    止损优先人工输入,报告中不再展示"✓止损X + 止盈Y / ✗无止损止盈计划"。
    #    评分由6项30分改为5项25分;trade_journal 中计划止损/止盈字段仍保留(数据不删)。
    total = sum(scores.values())
    return {
        "评分": scores,
        "细则": detail,
        "总分": total,
        "满分": 25,  # 2026-08-13: 撤掉"风险计划"评分项后 5项25分
        "说明": notes,
    }


def trade_review(trades):
    """全部操作复盘"""
    if not trades:
        return {"有操作": False, "提示": "今日无操作，跳过逐笔评分"}
    reviews = []
    for t in trades:
        code = t.get("代码", "")
        analysis = stock_analysis(code) if code else None
        r = score_trade(t, analysis)
        r["交易"] = {
            "代码": code, "名称": t.get("名称"), "方向": t.get("方向"),
            "手数": t.get("手数"), "价格": t.get("成交均价"),
            "理由": t.get("操作理由"),
        }
        reviews.append(r)
    return {"有操作": True, "逐笔": reviews}


# ===================== 4.5 持仓总览（现价/市值/浮盈浮亏/总仓位） =====================

ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "real_account.json")


def get_total_asset():
    """实盘总资产: data/real_account.json 优先 → 无则 None(报告标待补充)"""
    try:
        if os.path.exists(ACCOUNT_FILE):
            with open(ACCOUNT_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("总资产") or d.get("total_asset")
    except Exception:
        pass
    return None


def _latest_close(analysis):
    """取最新收盘价(昨日收盘, 盘中为实时)"""
    try:
        df = analysis.get("df")
        if df is not None and len(df) > 0:
            return float(df.iloc[-1]["close"])
    except Exception:
        pass
    return None


def position_overview():
    """
    持仓总览: 每只持仓现价/市值/浮盈浮亏(元/%) + 汇总市值 + 总仓位
    现价来自 K线最新收盘; 总资产来自 data/real_account.json, 无则 None
    """
    try:
        from utils.trade_journal import get_positions_list
        positions = get_positions_list()
    except Exception:
        return {"可用": False, "提示": "持仓数据读取失败"}

    if not positions:
        return {"可用": False, "提示": "当前无持仓"}

    rows = []
    total_mv = 0.0
    total_cost = 0.0
    for code, p in positions.items():
        shares = p.get("持仓数", 0)
        cost_avg = p.get("成本均价", 0)
        unit = p.get("单位", "手")
        # 展示单位: 股票"手"(100股) / ETF"份"
        if unit == "手":
            display_qty = f"{shares / 100:.0f}手" if shares % 100 == 0 else f"{shares}股"
        else:
            display_qty = f"{shares:.0f}份"
        analysis = stock_analysis(code) if code else None
        price = _latest_close(analysis)
        mv = (price or cost_avg) * shares
        cost_total = cost_avg * shares
        pnl = mv - cost_total
        pnl_pct = (price / cost_avg - 1) * 100 if price and cost_avg else None
        rows.append({
            "代码": code, "名称": p.get("名称", ""), "持仓": display_qty,
            "成本均价": round(cost_avg, 4) if cost_avg else None,
            "现价": round(price, 4) if price else None,
            "市值": round(mv, 2),
            "浮盈": round(pnl, 2),
            "浮盈%": round(pnl_pct, 2) if pnl_pct is not None else None,
            "已实现盈亏": round(p.get("已实现盈亏", 0), 2),
        })
        total_mv += mv
        total_cost += cost_total

    total_asset = get_total_asset()
    return {
        "可用": True,
        "逐仓": rows,
        "总市值": round(total_mv, 2),
        "总成本": round(total_cost, 2),
        "总浮盈": round(total_mv - total_cost, 2),
        "总浮盈%": round((total_mv / total_cost - 1) * 100, 2) if total_cost else None,
        "总资产": total_asset,
        "总仓位": round(total_mv / total_asset * 100, 1) if total_asset else None,
    }


# ===================== 5. 行情多维复盘 =====================

def _parse_news_text(text):
    """解析iFinD新闻返回（兼容列表/dict/嵌套JSON字符串/Python字面量）"""
    if isinstance(text, list):
        return [item.get("资讯标题", "")[:60] for item in text if isinstance(item, dict)][:3]
    if isinstance(text, dict):
        if isinstance(text.get("data"), list):
            return [item.get("资讯标题", "")[:60] for item in text["data"] if isinstance(item, dict)][:3]
        return [str(text.get("answer", ""))[:60]] if text.get("answer") else []
    if isinstance(text, str):
        # Python 字面量列表（如 "[{'资讯标题': ...}]"）
        if text.strip().startswith("["):
            try:
                d = eval(text, {"__builtins__": {}})
                return [str(x.get("资讯标题", ""))[:60] for x in d if isinstance(x, dict)][:3]
            except Exception:
                pass
        try:
            d = json.loads(text)
            return _parse_news_text(d)
        except Exception:
            return [text[:60]]
    return []


def _news_fresh(text, max_age_days=1):
    """
    新闻新鲜度过滤（2026-08-13 修复: 昨日/旧闻混入今日报告）。
    文本中出现的中文日期(YYYY年M月D日 / M月D日)若早于最近 max_age_days 天 → 判旧丢弃。
    无日期信息 → 保留(无法判断,宁多勿少)。含"今日/今天"或今天日期 → 保留。
    """
    import re
    if not text:
        return False
    today = datetime.now()
    today_str = today.strftime("%m月%d日")
    if "今日" in text or "今天" in text or today_str in text:
        return True
    for m in re.finditer(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text):
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if (today - d).days > max_age_days:
                return False
        except ValueError:
            continue
    for m in re.finditer(r"(?<![\d年])(\d{1,2})月(\d{1,2})日", text):
        try:
            d = datetime(today.year, int(m.group(1)), int(m.group(2)))
            if (today - d).days > max_age_days:
                return False
        except ValueError:
            continue
    return True


def _news_dedup_with_yesterday(news_list, max_age_days=1):
    """
    新鲜度过滤 + 与昨日报告消息面去重（2026-08-13 修复）。
    返回: 过滤后的新闻列表;若过滤后为空,补一个提示项。
    """
    fresh = [n for n in news_list if _news_fresh(n, max_age_days)]
    # 与昨日报告去重
    try:
        from utils.report_store import load_report
        yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        yest_r = load_report("post_market", yest)
        if yest_r:
            yest_news = (yest_r.get("content", {}).get("多维复盘", {}).get("消息面") or "")
            if isinstance(yest_news, str):
                yest_news = [yest_news]
            for y in yest_news:
                # 昨日消息面可能很长,取前40字匹配今日项
                key = str(y)[:40]
                fresh = [n for n in fresh if str(n)[:40] != key]
    except Exception:
        pass
    return fresh if fresh else ["（今日无新增重要新闻，请人工补充）"]


def multi_dim_review(snap, news_keywords=None):
    """行情多维度：技术/资金/情绪/板块/消息/海外"""
    review = {}
    # 技术面（指数黄白线状态）
    try:
        idx = parse_quote_table(snap["指数"]["value"])
        tech = []
        for d in idx[:4]:
            tech.append(f"{d.get('名称', '?')} {d.get('涨跌幅', 0):+.2f}% 振幅{d.get('振幅', 0)}%")
        review["技术面"] = tech if tech else "数据缺失"
    except Exception:
        review["技术面"] = "数据缺失"

    # 资金面（成交额）
    try:
        amt = snap["成交额"]["value"]
        review["资金面"] = f"沪深成交{amt['沪深合计']:.0f}亿（沪{amt['沪市']:.0f}/深{amt['深市']:.0f}）"
    except Exception:
        review["资金面"] = "数据缺失"

    # 情绪面
    try:
        sent = snap["情绪"]["value"]
        review["情绪面"] = (f"涨{sent['上涨家数']}/跌{sent['下跌家数']}，"
                            f"涨停{sent['涨停']}/跌停{sent['跌停']}，"
                            f"活跃度{sent['活跃度']}%")
    except Exception:
        review["情绪面"] = "数据缺失"

    # 板块面
    try:
        sec = snap["板块"]["value"]
        tops = sec.get("概念涨幅", [])[:5]
        review["板块面"] = " → ".join(f"{s['板块']}+{s['涨幅']}%" for s in tops) if tops else "数据缺失"
    except Exception:
        review["板块面"] = "数据缺失"

    # 消息面（新闻）——2026-08-13: 妙想查询词内嵌当日日期 + 新鲜度过滤 + 与昨日去重
    today = datetime.now().strftime("%Y-%m-%d")
    news_list = []
    for kw in (news_keywords or ["A股 政策", "央行 流动性", "AI 科技"]):
        try:
            # 妙想是自然语言模型: 查询词带"今日日期"引导返回当天新闻,避免旧闻
            r = get_news(f"{kw} 今日{today}最新消息", days=1, size=3)
            if isinstance(r, str) and r and "为空" not in r:
                news_list.extend(_parse_news_text(r))
            elif isinstance(r, dict) and r.get("source") == "mx妙想":
                # 妙想返回markdown文本，提取要点
                text = r.get("text", "")
                for line in text.splitlines():
                    s = line.strip()
                    if s.startswith(("1.", "2.", "3.")) and len(s) > 10:
                        news_list.append(s[:60])
                    elif s and len(s) > 20 and not s.startswith(("**", "|", "*查询", "---", "日期:")):
                        news_list.append(s[:60])
            elif isinstance(r, dict):
                text = r.get("data", {}).get("result", {}).get("content", [{}])[0].get("text", "")
                if text and "为空" not in text:
                    news_list.extend(_parse_news_text(text))
        except Exception:
            continue
    # 去重 + 新鲜度过滤 + 昨日去重
    seen = set()
    dedup = []
    for n in news_list:
        if n and n not in seen:
            seen.add(n)
            dedup.append(n)
    review["消息面"] = _news_dedup_with_yesterday(dedup[:8])

    # 海外市场
    try:
        us_list = parse_quote_table(snap["美股"]["value"])
        rows = []
        for d in us_list:
            rows.append(f"{d.get('名称', '?')} {d.get('最新价', '-')} {d.get('涨跌幅', 0):+.2f}%")
        review["美股"] = "；".join(rows[:8]) if rows else "（美股数据解析失败，见快照原始返回）"
    except Exception:
        review["美股"] = "数据缺失"

    try:
        kospi = snap["韩股"]["value"]
        review["韩国KOSPI"] = f"{kospi.get('收盘')}（{kospi.get('涨跌幅'):+.2f}%）" if kospi else "数据缺失"
    except Exception:
        review["韩国KOSPI"] = "数据缺失"

    return review


# ===================== 6. 明日情景推演 =====================

def forecast(snap, regime):
    """
    明日三情景推演（不是猜涨跌，是条件树）
    依据: 黄白线/宽度/成交额/情绪/板块/海外
    """
    base = {}
    try:
        sent = snap["情绪"]["value"]
        width = sent.get("上涨家数", 0) / max(1, sent.get("上涨家数", 0) + sent.get("下跌家数", 0)) * 100
        zt = sent.get("涨停", 0)
        amt = snap["成交额"]["value"].get("沪深合计", 0)
    except Exception:
        width, zt, amt = 50, 0, 0

    kospi = snap["韩股"]["value"]
    kospi_chg = kospi.get("涨跌幅", 0) if kospi else 0

    # 三情景概率初判（基于规则，供LLM细化）
    if width > 65 and amt > 20000:
        base_p, strong_p, weak_p = 40, 40, 20
        base_cond = "市场宽度高位+量能充足，延续概率大"
    elif width > 45 and amt > 15000:
        base_p, strong_p, weak_p = 50, 25, 25
        base_cond = "结构性行情，量能中等，震荡为主"
    else:
        base_p, strong_p, weak_p = 40, 15, 45
        base_cond = "宽度弱/量能不足，防守为主"

    return {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "数据截止": "A股收盘 + 韩股当日 + 美股上一交易日",
        "三情景": [
            {
                "情景": "基准情景",
                "概率": base_p,
                "核心依据": base_cond,
                "触发条件": "黄白线保持、宽度不恶化、成交额不明显萎缩",
                "失效条件": "指数跌破关键位或宽度快速恶化",
                "仓位建议": "维持当前仓位，不加不减",
                "重点观察": ["黄白线方向", "成交额变化", "主线板块持续性"],
            },
            {
                "情景": "强势情景",
                "概率": strong_p,
                "核心依据": f"宽度{width:.0f}%+涨停{zt}家+韩股{kospi_chg:+.1f}%",
                "触发条件": "指数放量突破关键位，主线板块共振",
                "失效条件": "放量滞涨或冲高回落",
                "仓位建议": "可提高至上限（活跃市值配合时）",
                "重点观察": ["突破量能", "涨停家数", "北向/资金流向"],
            },
            {
                "情景": "弱势情景",
                "概率": weak_p,
                "核心依据": "宽度或量能不足/海外转弱",
                "触发条件": "黄白线转弱、活跃市值下降、海外风险资产下跌",
                "失效条件": "缩量企稳、宽度修复",
                "仓位建议": "降至3成内，只做确定性B1",
                "重点观察": ["黄白线是否死叉", "跌停家数", "KOSPI/美股期指"],
            },
        ],
        "明日最重要观察": ["黄白线方向", "成交额（>=2万亿为活跃）", "主线板块是否切换"],
        "置信度": "中等（基于规则初判，LLM可细化）",
    }


# ===================== 6.5 盘前计划验证（闭环核心） =====================

def pre_market_verification(snap):
    """
    盘前计划验证：读取今日盘前提示 → 判断哪个情景命中 → 逐项验证观察条件
    返回: dict {盘前计划存在?, 命中情景, 验证详情, 误差来源, 校准}
    """
    today = datetime.now().strftime("%Y-%m-%d")
    result = {
        "盘前计划存在": False,
        "命中情景": None,
        "验证详情": [],
        "误差来源": [],
        "盘前仓位建议": None,
        "实际建议": None,
    }

    # 1. 读取今日盘前报告
    pre = load_report("pre_market", today)
    if pre is None:
        # 没有今日盘前，尝试用最近一份（标注日期）
        from utils.report_store import load_latest_report
        latest = load_latest_report("pre_market")
        if latest is None:
            result["说明"] = "无盘前计划记录，跳过验证"
            return result
        result["说明"] = f"未找到{today}盘前报告，用最近一份（{latest['path'].split('/')[-1]}）验证"
        pre = latest
    content = pre.get("content", pre)
    result["盘前计划存在"] = True

    # 2. 提取盘前三情景
    scen = content.get("三情景", {})
    scenarios = scen.get("三情景", []) if isinstance(scen, dict) else scen
    if not scenarios:
        result["说明"] = "盘前报告无三情景，跳过验证"
        return result

    # 3. 今日实际行情指标
    try:
        idx = parse_quote_table(snap["指数"]["value"])
        main_idx = next((d for d in idx if "上证" in d.get("名称", "")), idx[0] if idx else None)
        actual_chg = main_idx.get("涨跌幅", 0) if main_idx else 0
    except Exception:
        actual_chg = 0
    try:
        sent = snap["情绪"]["value"]
        width = sent.get("上涨家数", 0) / max(1, sent.get("上涨家数", 0) + sent.get("下跌家数", 0)) * 100
    except Exception:
        width = 50
    try:
        amt = snap["成交额"]["value"].get("沪深合计", 0)
    except Exception:
        amt = 0

    # 4. 匹配情景（规则：涨幅+宽度+量能 → 命中哪个）
    if actual_chg >= 1.0 and width > 55 and amt > 15000:
        hit = "强势情景"
    elif actual_chg <= -1.0 or (width < 40 and actual_chg < 0):
        hit = "弱势情景"
    else:
        hit = "基准情景"
    result["命中情景"] = hit

    # 5. 逐项验证观察条件（对照盘前观察清单/触发失效条件）
    check_items = [
        ("指数方向", f"{actual_chg:+.2f}%",
         "符合基准情景（-1%~+1%震荡）" if hit == "基准情景"
         else "符合强势情景（放量上涨）" if hit == "强势情景" else "符合弱势情景（下跌）"),
        ("市场宽度", f"{width:.0f}%",
         ">55%宽" if width > 55 else "40-55%中性" if width >= 40 else "<40%弱"),
        ("成交额", f"{amt:.0f}亿",
         ">1.5万亿活跃" if amt > 15000 else "1.2-1.5万亿正常" if amt > 12000 else "<1.2万亿萎缩"),
    ]
    for name, actual, verdict in check_items:
        result["验证详情"].append({"观察项": name, "实际": actual, "判定": verdict})

    # 6. 误差来源（盘前概率 vs 实际）
    probs = {s.get("情景"): s.get("概率", 0) for s in scenarios}
    if hit == "强势情景" and probs.get("强势情景", 0) <= 20:
        result["误差来源"].append("盘前低估强势概率，偏保守")
    elif hit == "弱势情景" and probs.get("弱势情景", 0) <= 20:
        result["误差来源"].append("盘前低估弱势风险，仓位建议偏乐观")
    elif hit == "基准情景" and (probs.get("强势情景", 0) >= 50 or probs.get("弱势情景", 0) >= 50):
        result["误差来源"].append("盘前倾向极端情景，实际走震荡")
    if not result["误差来源"]:
        result["误差来源"].append("盘前概率分配基本合理")

    # 7. 校准落库（供 forecast_store 统计命中率）
    try:
        verify_forecast(today, hit, actual_chg,
                        note=f"宽度{width:.0f}% 成交{amt:.0f}亿 上证{actual_chg:+.2f}%")
    except Exception:
        pass

    # 8. 仓位建议对比（兼容风险等级为字符串的情况）
    _risk = content.get("风险等级", {})
    _exec = content.get("执行卡", {}) or {}
    if isinstance(_risk, dict):
        result["盘前仓位建议"] = _risk.get("仓位区间")
    elif isinstance(_risk, str):
        import re
        _m = re.search(r"([0-9]+\s*[-~至]\s*[0-9]+成|[0-9]+成内|[0-9]+\.[0-9]+成|[0-9]+成)", _risk)
        result["盘前仓位建议"] = _m.group(1) if _m else _risk
    else:
        result["盘前仓位建议"] = None
    if not result["盘前仓位建议"]:
        result["盘前仓位建议"] = _exec.get("建议总仓位") if isinstance(_exec, dict) else None
    result["实际建议"] = f"宽度{width:.0f}%→" + ("5-7成" if width > 55 and amt > 15000 else "3-5成" if width >= 40 else "3成内")
    return result


# ===================== 6.8 昨日推演验证（自我升级闭环, 2026-08-13 新增） =====================

def yesterday_forecast_verification(snap):
    """
    昨日推演验证（自我升级）：
    读取昨日盘后生成、针对"今日"的推演 → 对照今日实际盘面
    输出: 昨日预测(三情景概率+方向)、今日实际、命中情景、逐观察项验证、偏差、升级点
    并落库验证(挂到昨日生成的那条记录上),累计命中率供统计。
    """
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    result = {
        "昨日推演": None, "方向": None, "命中情景": None,
        "验证详情": [], "偏差": [], "升级点": [],
        "实际涨跌幅": 0, "市场宽度": 0, "成交额": 0, "历史命中率": None,
    }

    # 1. 取昨日生成的、针对今日的推演（forecast_history: 预测日期==今天 && post_market && 生成时间<今天）
    fc_rec = None
    try:
        from utils.forecast_store import _load_all
        for r in _load_all():
            if (r.get("预测日期") == today_str and r.get("类型") == "post_market"
                    and r.get("生成时间", "") < f"{today_str} 00:00:00"):
                fc_rec = r
                break
    except Exception:
        pass
    if fc_rec is None:
        # fallback: 昨日报告文件里的"明日推演"(规则版)
        try:
            from utils.report_store import load_report
            yest = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            yest_r = load_report("post_market", yest)
            if yest_r:
                fc = yest_r.get("content", {}).get("明日推演", {})
                if fc:
                    fc_rec = {"预测": fc, "来源": f"昨日报告{yest}"}
        except Exception:
            pass
    if fc_rec is None:
        result["说明"] = "无昨日推演记录，跳过验证"
        return result

    pred = fc_rec.get("预测", {}) or {}
    scenarios = pred.get("三情景", []) or []
    result["昨日推演"] = [{"情景": s.get("情景"), "概率": s.get("概率"),
                          "核心依据": (s.get("核心依据") or "")[:40]} for s in scenarios]
    result["方向"] = pred.get("方向")
    result["昨日观察"] = pred.get("明日最重要观察", [])

    # 2. 今日实际
    try:
        idx = parse_quote_table(snap["指数"]["value"])
        main_idx = next((d for d in idx if "上证" in d.get("名称", "")), idx[0] if idx else None)
        actual_chg = main_idx.get("涨跌幅", 0) if main_idx else 0
    except Exception:
        actual_chg = 0
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
    result["实际涨跌幅"], result["市场宽度"], result["成交额"] = actual_chg, round(width, 1), amt

    # 3. 命中情景（与盘前验证同规则）
    if actual_chg >= 1.0 and width > 55 and amt > 15000:
        hit = "强势情景"
    elif actual_chg <= -1.0 or (width < 40 and actual_chg < 0):
        hit = "弱势情景"
    else:
        hit = "基准情景"
    result["命中情景"] = hit

    # 4. 逐观察项验证（昨日"明日最重要观察"；缺失则用默认三项）
    obs = result["昨日观察"] or ["黄白线方向", "成交额变化", "主线板块是否切换"]
    for o in obs:
        if "黄白线" in o or "方向" in o:
            verdict = "转弱" if actual_chg < -0.3 else "企稳" if actual_chg < 0.3 else "走强"
            result["验证详情"].append({"观察项": o, "实际": f"指数{actual_chg:+.2f}%→{verdict}",
                                        "命中": verdict == "走强"})
        elif "成交额" in o:
            active = amt >= 20000
            result["验证详情"].append({"观察项": o, "实际": f"{amt:.0f}亿(活跃线2万亿)",
                                        "命中": active})
        elif "主线" in o or "板块" in o:
            sec = snap["板块"]["value"] if isinstance(snap.get("板块", {}).get("value"), dict) else {}
            tops = (sec or {}).get("概念涨幅", [])[:3]
            chain = "、".join(str(t["板块"]) for t in tops) if tops else "数据缺失"
            result["验证详情"].append({"观察项": o, "实际": f"领涨:{chain}", "命中": None})
        else:
            result["验证详情"].append({"观察项": o, "实际": "待人工核对", "命中": None})

    # 5. 偏差分析（规则初判, LLM可细化）
    probs = {s.get("情景"): s.get("概率", 0) for s in scenarios}
    top_scen = max(scenarios, key=lambda s: s.get("概率", 0)) if scenarios else {}
    if top_scen.get("情景") != hit:
        result["偏差"].append(
            f"昨日最高概率为{top_scen.get('情景')}({probs.get(top_scen.get('情景'), 0)}%),"
            f"实际命中{hit}(概率{probs.get(hit, 0)}%),概率分配有偏差")
    if result.get("方向") and (("多" in str(result["方向"]) and actual_chg < -0.3)
                               or ("空" in str(result["方向"]) and actual_chg > 0.3)):
        result["偏差"].append(f"昨日方向「{result['方向']}」与今日实际{actual_chg:+.2f}%相悖")
    if not result["偏差"]:
        result["偏差"].append("昨日概率分配与方向判断基本正确")

    # 6. 升级点（规则版提示, LLM可细化）
    result["升级点"] = [
        "普涨次日(宽度>65%)易均值回归,次日强势概率应下调",
        "海外(韩股/美股)映射不等于A股主线,需结合量能与宽度验证",
        "量能未放大的普涨=存量博弈,弱势概率应上调",
    ]

    # 7. 落库验证（挂到昨日记录,不重复挂）
    try:
        from utils.forecast_store import verify_forecast, calibration_stats
        verify_forecast(today_str, ftype="post_market", hit_scenario=hit, actual_chg=actual_chg,
                        note=f"昨日推演验证:宽度{width:.0f}% 成交{amt:.0f}亿 上证{actual_chg:+.2f}%",
                        generated_before=f"{today_str} 00:00:00")
        result["历史命中率"] = calibration_stats()
    except Exception:
        pass
    return result


# ===================== 报告生成 =====================

def generate_report(mode="standard"):
    """
    盘后复盘主入口
    mode: simple / standard / deep
    闭环: 盘前计划验证 → 市场复盘 → 操作复盘 → 明日推演 → 落盘(报告+预测)
    """
    snap = market_snapshot()
    trades = get_today_trades()
    today = datetime.now().strftime("%Y-%m-%d")

    report = {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "模式": mode,
        "日期": today,
        "活跃市值": snap.get("活跃市值"),  # 指南针App数据(用户每日补充) + CSV历史趋势
        "数据源快照": {k: v.get("source") for k, v in snap.items() if isinstance(v, dict) and "source" in v},
    }

    # 0. 昨日推演验证（自我升级闭环, 2026-08-13 新增）
    report["昨日推演验证"] = yesterday_forecast_verification(snap)

    # 0.5 盘前计划验证（闭环核心，simple模式也保留）
    report["盘前计划验证"] = pre_market_verification(snap)

    # 环境 + 指标复盘
    regime = market_regime(snap)
    report["市场环境"] = regime

    # 行情多维
    report["多维复盘"] = multi_dim_review(snap)

    # 操作复盘
    report["操作复盘"] = trade_review(trades)
    report["持仓总览"] = position_overview()

    # 行为观察统计
    report["错误模式统计"] = error_pattern_stats()

    # 明日推演
    fc = forecast(snap, regime)
    report["明日推演"] = fc

    # 预测落盘：把明日推演写入 forecast_history（供次日盘前读取）
    try:
        save_forecast(today, "post_market",
                      {"三情景": fc["三情景"], "明日最重要观察": fc.get("明日最重要观察", [])},
                      source=f"盘后复盘{today}")
    except Exception as e:
        report["预测落盘"] = f"失败: {e}"

    # 报告落盘（md + json）
    try:
        md_text = render_markdown(report)
        paths = save_report("post_market", report, md_text, today)
        report["报告路径"] = paths
    except Exception as e:
        report["报告落盘"] = f"失败: {e}"

    return report


def render_markdown(report):
    """盘后复盘 Markdown 渲染（给人读，遵守可读性规范）"""
    md = []
    # ── 第一行: 活跃市值择时结论(指南针App, 用户每日补充) ──
    mv = report.get("活跃市值") or {}
    mv_val = mv.get("value") if isinstance(mv, dict) else mv
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
        md.append("╔══════════════════════════════════════════════════════╗")
        md.append("║ 📡 今日择时: 活跃市值【待补充】— 请从指南针App查看后告诉我 ║")
        md.append("╚══════════════════════════════════════════════════════╝")
    md.append("")
    md.append("╔══════════════════════════════════════╗")
    md.append("║  今日一句话结论（见各模块要点）        ║")
    md.append("╚══════════════════════════════════════╝")
    md.append(f"▶ 生成时间：{report['生成时间']}　模式：{report['模式']}")
    md.append("")

    # 一、昨日推演验证（自我升级, 2026-08-13 新增）
    yv = report.get("昨日推演验证", {})
    md.append("## 一、昨日推演验证（自我升级）")
    if yv.get("昨日推演"):
        scen_str = " / ".join(f"{s['情景']}{s['概率']}%" for s in yv["昨日推演"])
        line = f"- 昨日推演：{scen_str}"
        if yv.get("方向"):
            line += f"，方向「{yv['方向']}」"
        md.append(line)
        md.append(f"- 今日实际：上证{yv.get('实际涨跌幅', 0):+.2f}% 宽度{yv.get('市场宽度', 0):.0f}% "
                  f"成交{yv.get('成交额', 0):.0f}亿 → 命中 **{yv.get('命中情景')}**")
        for item in yv.get("验证详情", []):
            flag = "✅" if item.get("命中") is True else "❌" if item.get("命中") is False else "○"
            md.append(f"  ▸ {item['观察项']}：实际{item['实际']} {flag}")
        for b in yv.get("偏差", []):
            md.append(f"- 偏差：{b}")
        up = yv.get("升级点", [])
        if up:
            md.append("- 升级点：")
            for u in up:
                md.append(f"  · {u}")
        hl = yv.get("历史命中率")
        if hl and hl.get("方向命中率") is not None:
            md.append(f"- 累计校准：已验证{hl.get('已验证数')}/{hl.get('总预测数')}条，"
                      f"方向命中率{hl.get('方向命中率')}%（{hl.get('方向命中')}）")
    else:
        md.append(f"- {yv.get('说明', '无昨日推演记录')}")
    md.append("")

    # 二、盘前计划验证
    pv = report.get("盘前计划验证", {})
    md.append("## 二、盘前计划验证")
    if pv.get("盘前计划存在"):
        md.append(f"- 盘前计划：✓ 存在（{pv.get('说明', '今日盘前')}）")
        md.append(f"- 命中情景：**{pv.get('命中情景')}**")
        for item in pv.get("验证详情", []):
            md.append(f"  ▸ {item['观察项']}：实际{item['实际']} → {item['判定']}")
        md.append(f"- 误差来源：{'；'.join(pv.get('误差来源', []))}")
        md.append(f"- 仓位建议：盘前{pv.get('盘前仓位建议')} vs 实际{pv.get('实际建议')}")
    else:
        md.append(f"- {pv.get('说明', '无盘前计划')}")
    md.append("")

    # 三、市场环境
    env = report.get("市场环境", {})
    md.append("## 三、整体市场复盘")
    md.append(f"- 市场宽度：{env.get('市场宽度')}%　成交额：{env.get('成交额(亿)')}亿")
    md.append(f"- 环境定性：**{env.get('环境')}**　结论：{env.get('结论')}")
    md.append("")

    # 四、多维复盘
    md.append("## 四、核心指标与多维复盘")
    multi = report.get("多维复盘", {})
    for k in ["技术面", "资金面", "情绪面", "板块面", "消息面", "美股", "韩国KOSPI"]:
        v = multi.get(k)
        if v is None:
            continue
        if isinstance(v, list):
            md.append(f"- {k}：{'；'.join(str(x) for x in v[:5])}")
        else:
            md.append(f"- {k}：{v}")
    md.append("")

    # 五、操作复盘（含评分细则逐项）
    md.append("## 五、个人操作复盘")
    op = report.get("操作复盘", {})
    if op.get("有操作"):
        for r in op.get("逐笔", []):
            t = r.get("交易", {})
            direction = "买入" if t.get("方向") == "buy" else "卖出"
            unit_label = "份" if t.get("单位") == "份" else "手"
            md.append(f"### {t.get('名称') or t.get('代码')} {direction} {t.get('手数')}{unit_label} @{t.get('价格')}")
            md.append(f"- 操作理由：{t.get('理由') or '未记录'}")
            md.append(f"- 总分：**{r.get('总分')}/{r.get('满分')}**（评价决策，不评价结果）")
            detail = r.get("细则", {})
            scores = r.get("评分", {})
            # 细则逐项: 得分 + 说明
            md.append("")
            md.append("| 评分项 | 得分 | 细则 |")
            md.append("| --- | --- | --- |")
            for item in ["方向前提", "战法匹配", "入场位置", "仓位管理", "执行纪律"]:
                sc = scores.get(item, "-")
                lines = detail.get(item, []) or ["（无说明）"]
                md.append(f"| {item} | {sc}/5 | {'；'.join(lines)} |")
            md.append("")
            # 完整说明（保留原有平铺）
            for n in r.get("说明", []):
                md.append(f"  {n}")
            md.append("")
    else:
        md.append(f"- {op.get('提示', '无操作')}")
    md.append("")

    # 五·补 持仓总览（现价/市值/浮盈浮亏/总仓位）
    md.append("## 五·补、持仓总览")
    po = report.get("持仓总览", {})
    if po.get("可用"):
        md.append("| 代码 | 名称 | 持仓 | 成本均价 | 现价 | 市值(元) | 浮盈(元) | 浮盈% | 已实现盈亏 |")
        md.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in po.get("逐仓", []):
            md.append(f"| {row['代码']} | {row['名称']} | {row['持仓']} | "
                      f"{row['成本均价'] or '-'} | {row['现价'] or '-'} | {row['市值']:,.0f} | "
                      f"{row['浮盈']:+,.0f} | {row['浮盈%'] if row['浮盈%'] is not None else '-'}% | "
                      f"{row['已实现盈亏']:+,.2f} |")
        md.append("")
        md.append(f"- **持仓总市值**：{po['总市值']:,.0f} 元")
        md.append(f"- **总浮盈/浮亏**：{po['总浮盈']:+,.0f} 元（{po['总浮盈%'] if po['总浮盈%'] is not None else '-'}%）")
        if po.get("总资产"):
            md.append(f"- **实盘总资产**：{po['总资产']:,.0f} 元 → **总仓位 {po['总仓位']}%**")
        else:
            md.append("- **实盘总资产**：【待补充】— 请提供后计算总仓位（写入 data/real_account.json）")
    else:
        md.append(f"- {po.get('提示', '无持仓')}")
    md.append("")

    # 六、行为观察统计
    md.append("## 六、行为观察统计")
    err = report.get("错误模式统计", {})
    hits = {k: v for k, v in err.items() if isinstance(v, int) and v > 0 and k != "总观察数"}
    if hits:
        for k, v in hits.items():
            md.append(f"- {k}：{v}次")
    else:
        md.append("- 近30天无显著错误模式")
    md.append("")

    # 七、明日情景推演
    md.append("## 七、明日情景推演")
    fc = report.get("明日推演", {})
    for s in fc.get("三情景", []):
        md.append(f"### {s.get('情景')}（{s.get('概率')}%）")
        md.append(f"- 核心依据：{s.get('核心依据')}")
        md.append(f"- 触发条件：{s.get('触发条件')}")
        md.append(f"- 失效条件：{s.get('失效条件')}")
        md.append(f"- 仓位建议：{s.get('仓位建议')}")
        md.append(f"- 重点观察：{'、'.join(s.get('重点观察', []))}")
        md.append("")
    md.append(f"- 明日最重要观察：{'、'.join(fc.get('明日最重要观察', []))}")
    md.append(f"- 置信度：{fc.get('置信度')}")
    md.append("")
    md.append("---")
    md.append("⚠ 预测基于生成时已公开数据；价格/偏离为盘面数据，止损以用户确认为准")
    return "\n".join(md)


def main():
    parser = argparse.ArgumentParser(description="盘后复盘")
    parser.add_argument("--mode", choices=["simple", "standard", "deep"], default="standard")
    args = parser.parse_args()

    report = generate_report(args.mode)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
