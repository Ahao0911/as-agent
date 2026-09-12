"""
Ahao Stock Agent · 每日午间模拟买入(midday_sim)
每天 11:30 执行: 收集消息 → 妙想选股 → 本地指标验证 → 按战法分组模拟买入 → 止损检查 → 报告

数据源(用户指定妙想优先): 妙想新闻/选股 → 本地K线验证(腾讯/akshare兜底)
用法:
    python utils/midday_sim.py                # 执行今日午间模拟
    python utils/midday_sim.py --dry-run      # 只出候选不买入
"""
import sys
import os
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.sim_portfolio import (load_account, init_account, save_account, buy, sell,
                                 check_all_stops, status, GROUPS,
                                 single_position_cap, position_amount)
from utils.strategy_config import CONFIG  # 图形买点体系唯一参数源(仓位/止损口径)
from utils.mx_data import query_news, call as mx_call

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
MX_OUTPUT = os.path.join(DATA_DIR, "mx_output")

# 满仓全抡: 每组单只买入金额 = 组预算的 50% (~12.5万), 单组最多同时持有 2 只
# 单组两只打满 25 万预算; 四组都有信号即满仓 100 万 (2026-08-20 用户确认)
# 口径统一自 图形买点体系参数源(strategy_config), 不再本地硬编码
BUY_RATIO = CONFIG.BUY_RATIO
GROUP_BUDGET = CONFIG.GROUP_BUDGET
MAX_HOLD_PER_GROUP = 2


def _get_realtime_price(code):
    """腾讯实时报价(web.ifzq.gtimg.cn),返回最新价;失败返回 None
    返回格式: v_sh600519="1~贵州茅台~600519~最新价~昨收~今开~..." 价格=第4字段(index 3)
    """
    import requests
    prefix = "sh" if str(code).startswith(("5", "6", "9")) else "sz"
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={prefix}{code}", timeout=10)
        body = r.text
        if '="' in body:
            body = body.split('="', 1)[1]
        fields = body.split("~")
        if len(fields) > 3:
            return float(fields[3])
    except Exception:
        pass
    return None

# 战法分组 → 候选查询词(已废弃:选股走 screen_by_code 纯代码,此处仅留档)
# 深V组 = 单针下30(洗盘补票),非独立形态
GROUP_QUERIES = {
    "B1组": ["今天KDJ的J值超跌到低位且缩量的股票"],
    "砖型组": ["今日放量上涨且创20日新高的股票", "今日阳线放量站上5日均线的股票"],
    "单针组": ["单针下20:短期线≤20且长期线≥85(回调买点;门槛见 CONFIG.NEEDLE_LONG_MIN)"],
    "深V组": ["单针下30:短期线≤30且长期线≤80(洗盘补票)"],
}

# 本地指标对候选打分分组(在 _verify_local 内按 group 不同判定)
def _group_match(code, cand):
    """用本地K线判定候选最匹配的战法分组,返回 (group, score, note)"""
    if code.startswith(("4", "8", "92")):  # 北交所/新三板,数据源不支持
        return None, 0, "北交所不支持"
    from utils.screen_bull import fetch_kline
    try:
        df = fetch_kline(code, bars=250)
        if df is None or len(df) < 60:
            return None, 0, "K线不足"
    except Exception as e:
        return None, 0, f"K线失败:{str(e)[:40]}"

    from utils.indicators import analyze_dual_line, kdj_j, ma
    from utils.brick import analyze_brick_patterns
    last = df.iloc[-1]
    results = {}

    # 个股才用黄白线/砖型/单针; ETF 只给均线组
    is_etf = code.startswith(("5", "1", "15", "56", "58")) and not code.startswith(("60", "00", "30"))
    if is_etf:
        ma20 = ma(df["close"], 20).iloc[-1]
        ma60 = ma(df["close"], 60).iloc[-1]
        bull = ma20 > ma60 and last["close"] > ma20
        if bull:
            return "砖型组", 3, "ETF均线多头"
        return None, 0, "ETF均线空头"

    try:
        dual = analyze_dual_line(df)
        bull = dual.get("多头区间")
        j = dual.get("J值", 99)
        # B系战法组(B1/B2/B3 同一波段): B1超跌 / B2放量突破 / B3加速主升
        # v2(2026-09-12): B2/B3 **不再作独立买入信号** —— B2 只作 B1 的加仓位、B3 只作持有节点
        #   依据：全市场回测 B2 独立口径交易胜率仅 31.5%；口径源 CONFIG.sim_rules()
        #   回滚：CONFIG.SIM_RULES_VERSION = 1 即恢复 v1（B2/B3 计分）
        _v2 = CONFIG.SIM_RULES_VERSION >= 2
        b1_s = 0; b1_n = []
        if bull: b1_s += 3; b1_n.append("多头")
        if j <= 13: b1_s += 3; b1_n.append(f"B1:J{j:.0f}≤13")
        elif j <= 20: b1_s += 2; b1_n.append(f"J{j:.0f}")
        if cand.get("缩量") == "符合" or cand.get("量比", 0) < 1:
            b1_s += 1; b1_n.append("缩量")
        # B2: 放量阳线突破黄线/20日前高(多头环境)
        try:
            c_now = float(df["close"].iloc[-1]); c_prev = float(df["close"].iloc[-2])
            o_now = float(df["open"].iloc[-1])
            vol = df["volume"]; v_now = float(vol.iloc[-1]); v_prev = float(vol.iloc[-2])
            yl_val = float(dual.get("黄线", 0) or 0)
            hh20 = float(df["high"].iloc[-21:-1].max()) if len(df) > 21 else c_now
            vol_ratio = v_now / v_prev if v_prev > 0 else 0
            if bull and (c_now > yl_val > 0 or c_now > hh20) and c_now >= o_now and vol_ratio >= 1.5:
                if _v2:
                    b1_n.append("B2:放量突破(仅加仓用·不计独立买点)")
                else:
                    b1_s += 3; b1_n.append("B2:放量突破")
            c_5ago = float(df["close"].iloc[-6])
            if bull and j >= 80 and c_now > c_5ago * 1.08 and v_now > float(vol.iloc[-20:].mean()):
                if _v2:
                    b1_n.append(f"B3:J{j:.0f}加速(持有节点·不计独立买点)")
                else:
                    b1_s += 3; b1_n.append(f"B3:J{j:.0f}加速")
        except Exception:
            pass
        results["B1组"] = (b1_s, ";".join(b1_n) or f"J{j:.0f}")

        # 砖型: 绿翻强红 / 红柱增强
        brick = analyze_brick_patterns(df, None)
        if isinstance(brick, dict):
            bs = str(brick.get("状态", ""))
            if brick.get("绿翻强红"):
                results["砖型组"] = (6, "绿翻强红")
            elif brick.get("今日翻红") or "红柱" in bs:
                results["砖型组"] = (3, "红柱增强:" + bs[:8])
            else:
                results["砖型组"] = (0, bs[:10])

        # 单针下20(回调买点): 短期线≤20 & 长期线≥CONFIG.NEEDLE_LONG_MIN
        from utils.needle20 import needle20_lines
        nl = needle20_lines(df)
        s_ = float(nl["短期"].iloc[-1]); l_ = float(nl["长期"].iloc[-1])
        if s_ <= 20 and l_ >= CONFIG.NEEDLE_LONG_MIN:
            results["单针组"] = (4, f"单针下20:短{s_:.0f}≤20/长{l_:.0f}≥{CONFIG.NEEDLE_LONG_MIN}")
        else:
            results["单针组"] = (0, f"短{s_:.0f}>20或长{l_:.0f}<{CONFIG.NEEDLE_LONG_MIN}")

        # 单针下30(洗盘补票,即深V): 短期线≤30 & 长期线≤80
        if s_ <= 30 and l_ <= 80:
            results["深V组"] = (4, f"单针下30:短{s_:.0f}≤30/长{l_:.0f}≤80")
        else:
            results["深V组"] = (0, f"短{s_:.0f}>30或长{l_:.0f}>80")
    except Exception as e:
        return None, 0, f"指标异常:{str(e)[:40]}"

    # 取得分最高的组(需≥3)
    best = max(results.items(), key=lambda x: x[1][0])
    g, (s, n) = best
    if s >= 3:
        return g, s, n
    return None, s, n


def _parse_xuangu_csv(query):
    """解析妙想xuangu输出CSV(列名带时间戳,模糊匹配),返回 [{代码,名称,最新价,涨跌幅,量比,KDJ低位,缩量,...}]"""
    import glob
    import pandas as pd
    qname = "".join(c if c.isalnum() or c in "_-" else "_" for c in query[:20])
    files = sorted(glob.glob(os.path.join(MX_OUTPUT, f"mx_xuangu_{qname}*.csv")),
                   key=os.path.getmtime)
    if not files:
        return []
    try:
        df = pd.read_csv(files[-1], encoding="utf-8-sig")
    except Exception:
        df = pd.read_csv(files[-1], encoding="utf-8")

    def col(*keys):
        """按关键词模糊找列名"""
        for c in df.columns:
            cs = str(c)
            if all(k.lower() in cs.lower() for k in keys):
                return c
        return None

    c_code = col("代码"); c_name = col("名称")
    c_price = col("最新价"); c_chg = col("涨跌幅")
    c_vr = col("量比"); c_j = col("kdj") or col("J值")
    c_low = col("KDJ低位") or col("低位"); c_shrink = col("缩量")
    c_to = col("换手率")
    if not c_code:
        return []

    def f(row, c, default=0.0):
        if not c:
            return default
        v = row.get(c)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    rows = []
    for _, r in df.iterrows():
        code = str(r.get(c_code, "")).zfill(6)
        if not code or code == "000000":
            continue
        rows.append({
            "代码": code,
            "名称": str(r.get(c_name, "")) if c_name else "",
            "最新价": f(r, c_price),
            "涨跌幅": f(r, c_chg),
            "量比": f(r, c_vr),
            "J值": f(r, c_j),
            "KDJ低位": str(r.get(c_low, "")) if c_low else "",
            "缩量": str(r.get(c_shrink, "")) if c_shrink else "",
            "换手率": f(r, c_to),
        })
    return rows[:5]  # 每组最多取5只候选


def _verify_local(code, group):
    """
    本地指标验证,判断是否符合战法
    返回: {ok, score, note}
    """
    from utils.screen_bull import fetch_kline
    try:
        df = fetch_kline(code, bars=250)
        if df is None or len(df) < 60:
            return {"ok": False, "score": 0, "note": "K线不足"}
    except Exception as e:
        return {"ok": False, "score": 0, "note": f"K线失败:{str(e)[:40]}"}

    from utils.indicators import analyze_dual_line, kdj_j, ma
    from utils.brick import analyze_brick_patterns
    last = df.iloc[-1]
    score = 0
    notes = []
    is_etf = code.startswith(("5", "1", "15", "56", "58")) and not code.startswith(("60", "00", "30"))

    try:
        if is_etf:
            ma20 = ma(df["close"], 20).iloc[-1]
            ma60 = ma(df["close"], 60).iloc[-1]
            bull = ma20 > ma60 and last["close"] > ma20
            if bull:
                score += 3; notes.append("ETF均线多头")
            else:
                notes.append("ETF均线空头")
        else:
            dual = analyze_dual_line(df)
            bull = dual.get("多头区间")
            if bull:
                score += 3; notes.append("黄白线多头")
            else:
                notes.append("黄白线空头")
            j = dual.get("J值", 99)
            if j <= 13:
                score += 3; notes.append(f"J值{j:.0f}≤13(B1)")
            elif j <= 20:
                score += 2; notes.append(f"J值{j:.0f}低位")
            # 砖型图
            brick = analyze_brick_patterns(df, None)
            bs = brick.get("最新状态", "") if isinstance(brick, dict) else ""
            if "翻红" in bs or "红柱" in bs:
                score += 2; notes.append("砖型翻红")
            elif "绿" in bs:
                notes.append("砖型绿柱")
    except Exception as e:
        notes.append(f"指标异常:{str(e)[:30]}")

    # 战法针对性加分
    if group == "B1组" and ("J值" in str(notes) and "≤13" in str(notes)):
        score += 2
    if group == "砖型组" and "砖型翻红" in str(notes):
        score += 3
    if group == "深V组":
        chg5 = (last["close"] / df["close"].iloc[-6] - 1) * 100
        if last["low"] < df["close"].iloc[-2] * 0.98 and last["close"] > last["open"]:
            score += 3; notes.append("深V形态")
        else:
            notes.append(f"5日{chg5:+.1f}%")

    return {"ok": score >= 3, "score": score, "note": ";".join(notes)}


def main(dry_run=False):
    acc = load_account()
    today = datetime.now().strftime("%Y-%m-%d")
    report = []
    report.append("═" * 64)
    report.append(f"📊 模拟盘午间操作 {today} (数据:妙想优先)")
    report.append("═" * 64)

    # 1. 收集消息
    try:
        news = query_news("今日A股市场 热点 主线 板块 消息", days=1, size=5)
        report.append(f"📰 今日消息: {news.get('output','')[:150].strip()}")
    except Exception as e:
        report.append(f"📰 消息获取失败: {e}")

    # 2. 选股 + 买入(纯代码条件选股,不依赖自然语言)
    buys = []
    from utils.sim_screener import screen_by_code
    try:
        group_picks = screen_by_code(top_n=12)
        total = sum(len(v) for v in group_picks.values())
        report.append(f"  📥 代码选股: 全市场快照粗筛 → K线指标精筛, 命中 {total} 只")
    except Exception as e:
        group_picks = {g: [] for g in GROUPS}
        report.append(f"  ⚠ 代码选股失败: {str(e)[:80]}")

    # 2.3 买入 · 满仓全抡(2026-08-20 用户确认, 配比不变 + 资金尽量打光):
    #   第一轮: 每组信号最优1只, 每只 = 组预算50%(12.5万), 单组最多2只 —— 配比不变
    #   第二轮: 若组内还有第2个候选信号且持仓<2只, 补买第2只(仍12.5万/只) —— 配比不变
    #   第三轮: 若现金仍富余(部分组无信号/少信号), 让渡给已买入股票加仓(可突破单组
    #           25万预算 allow_overflow, 但只加仓已有持仓、不新开第三只), 尽量打光现金
    bought_pool = []  # [(group, pick)] 实际第一/二轮买入的股票(用于第三轮加仓)
    for round_i in (1, 2):
        for group in GROUPS:
            picks = group_picks.get(group, [])
            g = acc["分组"][group]
            if round_i == 2:
                # 第二轮: 只处理有第2个候选、且组内持仓<2只的组
                if len(picks) < 2 or len(g["持仓"]) >= MAX_HOLD_PER_GROUP:
                    continue
                pick = picks[1]
            else:
                if not picks:
                    report.append(f"  [{group}] 无符合信号候选(没信号不硬买)")
                    continue
                if len(g["持仓"]) >= MAX_HOLD_PER_GROUP:
                    report.append(f"  [{group}] 已达持仓上限({MAX_HOLD_PER_GROUP}只),跳过")
                    continue
                pick = picks[0]
            if pick["最新价"] <= 0:
                continue
            # 金额 = 组预算50%, 但不超过组内剩余预算(保证不超25万组预算)
            used = sum(p["成本价"] * p["数量"] for p in g["持仓"].values())
            amount = int(min(CONFIG.scale_in_amount(GROUP_BUDGET), GROUP_BUDGET - used))
            if amount < 100 * 2:
                continue
            # 买入价: 优先腾讯实时价, 避免 xuangu 与 K线价差
            rp = _get_realtime_price(pick["代码"])
            buy_price = rp if rp else pick["最新价"]
            is_add = pick["代码"] in g["持仓"]
            r_buy = buy(acc, group, pick["代码"], pick["名称"], buy_price,
                        amount=amount, reason=f"午间模拟:{pick['note']}",
                        date_str=today)
            # 标签: 已在持仓则为加仓, 否则按轮次显示买入/补买
            if is_add:
                tag = "加仓"
            elif round_i == 2:
                tag = "补买"
            else:
                tag = "买入"
            report.append(f"  [{group}] {tag}{pick['代码']}{pick['名称']}(分{pick['score']}:{pick['note']}) → {r_buy['msg']}")
            if r_buy["ok"]:
                buys.append(pick)
                bought_pool.append((group, pick))

    # 2.4 第三轮 · 让渡加仓: 现金仍富余(>10万)时, 让渡给已买入股票加仓
    #   v1: 无差别加倍(仅受现金/敞口约束) | v2(2026-09-12): **半仓追加**(≤已建仓额×ADD_ON_RATIO)
    if bought_pool and acc["现金"] > 100000:
        # 按信号分从高到低排序
        pool = sorted(bought_pool, key=lambda x: -x[1].get("score", 0))
        reserve = CONFIG.RESERVE_CASH  # 保留现金口径统一自 图形买点体系参数源
        usable = acc["现金"] - reserve
        if usable > 0:
            per_stock = int(usable / len(pool) / 100) * 100  # 每只加仓金额(按百取整)
            for group, pick in pool:
                if per_stock <= 0 or acc["现金"] <= reserve:
                    break
                g = acc["分组"][group]
                code = pick["代码"]
                if code not in g["持仓"]:
                    continue  # 只对已买入的股票加仓
                # 按该股剩余敞口空间截断, 使报告申购金额与实际成交金额一致
                # (避免"报告说买12万、实际成交5万"的口径错位)
                headroom = single_position_cap(acc, group) - position_amount(acc, group, code)
                caps = [per_stock, acc["现金"] - reserve, headroom]
                if CONFIG.SIM_RULES_VERSION >= 2:
                    # v2 半仓追加: 加仓额 ≤ 该股已建仓额 × ADD_ON_RATIO(0.5)
                    # 依据: 全量 3737 笔加仓腿 +1.604%/笔; 加倍会把整笔胜率 82.5% 拉到 63.8%
                    # 回滚: SIM_RULES_VERSION = 1 → 撤销该上限(恢复 v1 无差别加倍)
                    caps.append(position_amount(acc, group, code) * CONFIG.ADD_ON_RATIO)
                amount = int(min(caps))
                if amount < 100 * 2:
                    continue
                rp = _get_realtime_price(code)
                buy_price = rp if rp else pick["最新价"]
                r_buy = buy(acc, group, code, pick["名称"], buy_price,
                            amount=amount, reason=f"午间全抡加仓:{pick['note']}",
                            date_str=today, allow_overflow=True)
                if r_buy["ok"]:
                    report.append(f"  [{group}] 全抡加仓{pick['代码']}{pick['名称']} → {r_buy['msg']}")
                    buys.append(pick)
                else:
                    report.append(f"  [{group}] 全抡加仓 {pick['代码']} 失败: {r_buy['msg']}")
            if acc["现金"] <= reserve + 10000:
                report.append(f"  💰 满仓全抡: 现金打光至 {acc['现金']:.0f} 元")
            else:
                report.append(f"  💰 满仓全抡: 现金剩余 {acc['现金']:.0f} 元(按手取整残差)")

    # 3. 止损检查(腾讯实时价)
    sold = []
    for g_name, g in acc["分组"].items():
        for code, p in list(g["持仓"].items()):
            cur = _get_realtime_price(code)
            if cur and cur <= p["止损价"]:
                r = sell(acc, g_name, code, cur, reason="止损(自动)")
                sold.append(r)
                report.append(f"  ⚠ {g_name} {p['名称']} 触发止损@{cur} (止损{p['止损价']})")
    if not sold:
        report.append("  ✅ 无持仓触发止损")

    if not dry_run and (buys or sold):
        save_account(acc)

    report.append("")
    report.append(status(acc))
    report.append("")

    # 4. 报告落盘 (dry-run 不覆盖正式报告, 用独立 _dryrun 文件名)
    os.makedirs(REPORT_DIR, exist_ok=True)
    suffix = "_dryrun" if dry_run else ""
    fp = os.path.join(REPORT_DIR, f"sim_{today}{suffix}.md")
    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    report.append(f"📄 报告: {fp}")
    print("\n".join(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="每日午间模拟买入")
    parser.add_argument("--dry-run", action="store_true", help="只出候选不买入")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
