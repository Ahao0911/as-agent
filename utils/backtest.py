# -*- coding: utf-8 -*-
"""
武器库回测框架
============
回测区间 2024-01-01 ~ 今天, 对股票池 × 全战法做信号回测:
  信号日 → 次日开盘买入 → 按【每套战法自己的卖出规则】离场(随盘面走) → 每套战法只出一个收益率

每套战法的离场规则(来源: ima 图形买点体系知识库 B1/B2/B3逻辑图 + modules/*_rules.md + pattern_rules.md):
  B1     : 止损=买入K线最低点或向下3-5个价位; 期待=3个交易日内恢复上涨(3日不涨就走)
          止盈=前高压力位, 放量突破前高→抬高止损继续持有(放飞)
          离场=连续2日收盘跌破白线 / 最多30天
  B2     : 止损=N型前低; 期待=2个交易日内必须大幅拉升(2日不拉就走); 止盈放飞=突破后按白线走
  B3     : 加速主升买点(J值高位钝化+快速拉升); 止损=突破大阳线的中间位置; 止盈=红砖缩短/红翻绿
  砖型   : 止盈=红翻绿(红砖缩短/翻绿即走), 终极止损=白线有效跌破黄线
  单针   : 止损=长期线(红线)跌破60(中期趋势走坏), 离场=白线高位拐头向下/破位
  所有战法: 白线>黄线(多头环境)信号才有效, 空头环境红砖/反弹多为诱多

⚠ 下影线/对子底不是独立战法, 是辅助判断手段(配合主战法用), 不纳入回测

用法:
    python utils/backtest.py                    # 默认股票池20只
    python utils/backtest.py --stocks 601899,600519  # 指定股票
"""
import os
import sys
import json
import argparse
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.indicators import white_line, yellow_line, kdj_j
from utils.data_router import get_daily_bars
from utils.brick import brick_chart
from utils.needle20 import needle20_lines
from utils.strategy_config import CONFIG

START = "2024-01-01"   # 只看24年至今

# 交易成本(用户费率: 万一免五)
COMMISSION = 0.0001   # 佣金万1(1万元收1元), 免最低5元
STAMP_TAX = 0.0005    # 印花税 0.05%(卖出单向)
TRANSFER = 0.00001    # 过户费 万0.1(买卖双向)
TRADE_COST = COMMISSION * 2 + STAMP_TAX + TRANSFER * 2  # 单笔往返 ≈ 0.072%

DEFAULT_POOL = [
    ("601899", "紫金矿业"), ("600519", "贵州茅台"), ("300750", "宁德时代"),
    ("000001", "平安银行"), ("002594", "比亚迪"), ("688981", "中芯国际"),
    ("601012", "隆基绿能"), ("601318", "中国平安"), ("600036", "招商银行"),
    ("600276", "恒瑞医药"), ("002415", "海康威视"), ("002475", "立讯精密"),
    ("603259", "药明康德"), ("600031", "三一重工"), ("601888", "中国中免"),
    ("600309", "万华化学"), ("600660", "福耀玻璃"), ("000725", "京东方A"),
    ("300059", "东方财富"), ("000858", "五粮液"),
]


# ============================================================
# 每套战法自己的卖出规则(随盘面走, 不是全局统一参数)
# 说明: 下影线/对子底是辅助判断手段(配合主战法用), 不是独立战法, 不纳入回测
# ============================================================
def make_exit_rules():
    """
    返回 {战法名: dict}，每套战法绑定自己的离场逻辑。
    各战法的止损/止盈/放飞都在各自的规则里定义, 互不套用。
    仅五套主战法: B1 / B2 / B3 / 砖型 / 单针
    """
    return {
        # B1: 止损=买入K线最低点或向下3-5个价位(知识库原文); 止盈=前高, 放量突破前高→抬高止损放飞;
        #     期待=3个交易日内恢复上涨(3日不涨就走); 离场=连续2日收盘跌破白线 / 最多30天
        "B1": {"kind": "b1", "stop_pct": -0.04, "max_hold": 30,
               "desc": "止损买入K线低点-3~5价位/3日不涨离场/前高止盈/破前高放飞"},
        # B2: N型前低止损, 期待=2日内必须大幅拉升(2日不拉就走), 突破后按白线放飞(右侧确认)
        "B2": {"kind": "b2", "max_hold": 15,
               "desc": "止损N型前低/2日不拉离场/白线放飞(右侧)"},
        # B3: 加速主升(知识库: J值高位钝化+快速拉升; B3是B2后持有段, 非独立追高买点)
        #     止损=突破大阳线的中间位置; 持有=不破B3阳线最低点都可以; 红砖缩短/红翻绿止盈
        "B3": {"kind": "b3", "max_hold": 15,
               "desc": "止损突破阳线中位/不破阳线低点持有/红翻绿止盈"},
        # 砖型: 红翻绿即止盈离场; 白线有效跌破黄线=终极止损
        "砖型": {"kind": "brick", "max_hold": 20,
                 "desc": "红翻绿止盈/白线破黄线止损"},
        # 单针下20: 红线跌破60=中期走坏止损; 白线高位拐头向下离场
        "单针": {"kind": "needle", "max_hold": 15,
                 "desc": "红线跌破60止损/白线高位拐头离场"},
    }


def compute_signals(df):
    """计算各战法逐日信号序列 → {战法名: bool Series}"""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)
    o = df["open"].astype(float)

    wx = white_line(c)
    yx = yellow_line(c)
    k, d, j = kdj_j(h, l, c)

    sig = {}

    # 通用量
    chg = c.pct_change() * 100           # 当日涨跌幅%
    amplitude = (h - l) / c.shift(1) * 100  # 振幅%
    v20 = v.rolling(20).mean()
    # 图形买点体系核心前提: 白线>黄线=多头环境, 信号才有效(空头环境红砖/反弹多为诱多)
    bull = wx > yx

    # B1(图形买点体系十张图+知识库): 涨幅-2%~+1.8% + 振幅<7% + 白线>黄线 + 缩量 + J值≤13(勾到大负值)
    sig["B1"] = (chg >= -2) & (chg <= 1.8) & (amplitude < 7) & bull & (v < v20) & (j <= 13)

    # B2: B1确认阳线(知识库《B2案例买点精讲》: B2是B1之后的确定性买点/波段启动关键信号)
    #     条件: 前10日内出现过B1信号 + 当日涨幅>4% + 放量(≥1.5倍) + J<55 + 无大上影线 + 多头环境
    #     + 反包(2026-09-12 用户确认采纳, 全市场扫描 +5.3pt): 阳线完全覆盖前一根阴线实体
    #       知识库原文: "反包：一根阳线完全覆盖前一根阴线的实体部分, 特别是'灾后重建'形态"
    upper_shadow = h - pd.concat([o, c], axis=1).max(axis=1)
    entity = (c - o).abs()
    # B1 窗口=5日（2026-09-12 用户最终裁决「3~5日最合适」，取5日）
    #   窗口扫描历史: 2日 35.9% / 3日 35.4% / 10日 35.1%（2010起17年数据）
    #   注: 5日含突破条件的旧实验为 30.2%（被突破条件拖累），纯反包版 5 日窗见回测报告
    b1_prev = sig["B1"].rolling(5, min_periods=1).max().shift(1).fillna(False).astype(bool)
    engulf = ((c > o) & (c.shift(1) < o.shift(1))
              & (o <= c.shift(1)) & (c >= o.shift(1)))
    # B2 结构保护（2026-09-12 用户定义）：B1 之后 5 日内不跌破 B1 最低点
    #   （B1 止损位=买入K线最低点；不破=埋伏有效，主力护盘；破了=B1失败，放量阳线不可信）
    b1_low_ff = l.where(sig["B1"]).ffill(limit=5)     # 最近一次 B1 的最低点（5日内有效）
    not_break_b1 = c >= b1_low_ff                      # 收盘未破 B1 最低点
    # W3 定稿（2026-09-12 扫描采纳）: J<40（收紧 KDJ 位置, +2.5pp 三段全升）
    sig["B2"] = (b1_prev & (chg > 4) & (v > v.shift(1) * 1.5) & (j < 40)
                 & (upper_shadow < entity) & (entity > 0) & bull & engulf
                 & not_break_b1)

    # B3: 中继K线（2026-09-12 用户口述定义定稿，实测 2024窗口 688轮 / 60.3% / IS 60.0% / OOS 60.2% 双段一致）
    #     用户定义: 「B3 = B2 后的中继K线(确认延续信号): 出现在B2确认阳线之后;
    #              缩量到半量以内(相比B2那天的量); 小阳线最好, 缩半量的小阴线也可以
    #              (分歧转一致); 主力整理蓄力, 不破B2最低点就继续持有」
    #     定位: B2 持有确认（不破 B2 低点拿住），同时可作加仓参考
    #     对照: 回调加仓版(85.7%)保留于 flow_optimize_v6 X2；追加速旧版(17年−381%)已废
    b2_sig = sig["B2"]
    b2_vol = v.where(b2_sig).ffill(limit=5)      # 最近一次 B2 的量
    b2_low = l.where(b2_sig).ffill(limit=5)      # 最近一次 B2 的最低点
    after_b2 = b2_sig.shift(1).fillna(False).astype(bool)\
        .rolling(3, min_periods=1).max().astype(bool)   # B2 之后 3 日内
    shrink = v <= b2_vol * 0.5                       # 缩量至 B2 量的半量以内
    small = ((c - o).abs() / c.shift(1).replace(0, np.nan)) <= 0.02   # 小K线(实体≤2%)
    not_break = c >= b2_low                          # 不破 B2 最低点
    sig["B3"] = after_b2 & shrink & small & not_break

    # 砖型: 翻红XG(绿翻红) + 多头环境(知识库: 白线<黄线红砖多为诱多, 禁止重仓)
    try:
        b = brick_chart(df)
        sig["砖型"] = b["翻红XG"].fillna(False).astype(bool) & bull
    except Exception:
        sig["砖型"] = pd.Series(False, index=df.index)

    # 单针下20: 短期≤20 且 长期≥CONFIG.NEEDLE_LONG_MIN (白线快速杀到20下方+红线站稳长期线上方=大势多头回调买点)
    try:
        lines = needle20_lines(df)
        sig["单针"] = (lines["短期"] <= 20) & (lines["长期"] >= CONFIG.NEEDLE_LONG_MIN)
    except Exception:
        sig["单针"] = pd.Series(False, index=df.index)

    return sig


def _find_exit(buy_idx, df, rule, extra, first_k=None):
    """
    按战法自己的规则找离场日。
    first_k: 离场检查起始K线索引（默认 buy_idx；当日尾盘买入模式传 buy_idx+1，遵守 T+1）
    返回 (exit_idx, exit_price, exit_reason)
    """
    o = extra["open"]; h = extra["high"]; l = extra["low"]
    c = extra["close"]; wl = extra["white"]; yl = extra["yellow"]
    nd = extra.get("needle"); bk = extra.get("brick")
    n = len(c)
    buy_price = o[buy_idx]
    kind = rule["kind"]
    max_hold = rule.get("max_hold", 15)
    stop_pct = rule.get("stop_pct", -0.04)

    # 信号日(K线索引 buy_idx-1)的最低点 = 结构止损基准
    sig_low = l[buy_idx - 1] if buy_idx - 1 >= 0 else l[buy_idx]
    # 前高: 信号日前60日内最高价(不含信号日)
    look = slice(max(0, buy_idx - 60), buy_idx - 1) if buy_idx > 1 else slice(0, buy_idx)
    prev_high = h[look].max() if buy_idx > 1 else buy_price * 1.2
    prev_high = max(prev_high, buy_price * 1.05)  # 前高至少比买入价高5%, 避免止盈价太低

    for k in range(first_k if first_k is not None else buy_idx, min(buy_idx + max_hold, n)):
        # ---------- 止损优先(各战法自己的结构止损) ----------
        if kind == "b1":
            # 知识库: B1止损 = 买入K线最低点或向下3-5个价位(结构破位为主, 3-5价位是容差)
            # 回测实现: 信号日K线最低点=结构止损位(破位无条件走)
            stop_price = sig_low if sig_low > 0 else buy_price * (1 - 0.03)
        elif kind == "b2":
            # 知识库(2025-12-24底部暴力K线): B2买入跌破买入当日K线的最低点止损
            # B2-2图: N型结构前低(信号日K线最低点≈N型低点)
            stop_price = l[buy_idx - 1] if buy_idx - 1 >= 0 else l[buy_idx]
            stop_price = min(stop_price, buy_price * 0.95)  # 兜底
        elif kind == "b3":
            # 知识库: B3止损 = 突破大阳线的中间位置
            mid = (o[buy_idx - 1] + c[buy_idx - 1]) / 2 if buy_idx > 0 else buy_price * 0.95
            stop_price = mid
        elif kind == "brick":
            # 终极止损: 白线有效跌破黄线 → 无条件离场
            if k > buy_idx and wl[k] < yl[k] and wl[k - 1] < yl[k - 1]:
                return k, c[k], "白线破黄线止损"
            stop_price = buy_price * (1 - 0.05)
        elif kind == "needle":
            # 红线(长期线)跌破60 = 中期趋势走坏
            if k > buy_idx and nd is not None and nd["长期"][k] < 60:
                return k, c[k], "红线跌破60止损"
            stop_price = buy_price * (1 - 0.05)
        else:
            stop_price = buy_price * (1 + stop_pct)

        if l[k] <= stop_price:
            return k, stop_price, "止损"

        # ---------- 止盈/放飞(各战法自己的) ----------
        if kind == "b1":
            # 知识库: B1止损=买入K线最低点或向下3-5个价位; 期待3日内恢复上涨(期待不作为硬离场)
            # 前高是第一目标; 放量突破前高 → 抬高止损继续持有(放飞)
            if h[k] >= prev_high:
                if k > buy_idx and c[k] > prev_high and extra["vol"][k] > extra["vol20"][k]:
                    prev_high = max(prev_high, c[k] * 1.02)   # 突破放飞, 抬高止盈线
                    continue
                return k, prev_high, "前高止盈"
            # 连续2日收盘跌破白线 → 离场
            if k > buy_idx + 1 and c[k] < wl[k] and c[k - 1] < wl[k - 1]:
                return k, c[k], "破白线离场"
        elif kind == "b2":
            # 知识库: B2止损=N型结构前低(买入当日K线最低点), 波段启动信号→持有让利润奔跑
            # 离场: 连续2日收盘破白线(白线牵牛绳) 或 到期
            if k > buy_idx + 1 and c[k] < wl[k] and c[k - 1] < wl[k - 1]:
                return k, c[k], "破白线离场"
        elif kind == "b3":
            # 红砖缩短/红翻绿止盈
            if bk is not None:
                cur_brick = bk["砖型图"][k]
                prev_brick = bk["砖型图"][k - 1] if k > buy_idx else cur_brick
                if cur_brick < prev_brick or bk["翻绿XD"][k]:
                    return k, c[k], "红砖缩短/翻绿止盈"
            if c[k] >= prev_high:
                return k, prev_high, "前高止盈"
        elif kind == "brick":
            # 红翻绿止盈(红砖缩短也算)
            if bk is not None:
                if bk["翻绿XD"][k] or (k > buy_idx and bk["砖型图"][k] < bk["砖型图"][k - 1]):
                    return k, c[k], "红翻绿止盈"
        elif kind == "needle":
            # 白线(短期)高位拐头向下 → 离场
            if nd is not None and k > buy_idx and nd["短期"][k - 1] > 80 and nd["短期"][k] < nd["短期"][k - 1]:
                return k, c[k], "白线高位拐头离场"
            if c[k] >= prev_high:
                return k, prev_high, "前高止盈"

    # 到期强制离场
    exit_idx = min(buy_idx + max_hold - 1, n - 1)
    return exit_idx, c[exit_idx], "到期离场"


def backtest_stock(signals, df, rules=None, entry_mode="next_open"):
    """单只股票回测: 信号日→次日开盘买入→按【该战法自己的卖出规则】离场
    entry_mode:
      next_open  信号日次日开盘买入（旧口径）
      same_close 信号日当日尾盘(14:55)买入 ≈ 以当日收盘价成交（2026-09-12 用户指定）
                 离场检查从次日开始（遵守 T+1，买入当日不可卖）
    """
    if rules is None:
        rules = make_exit_rules()
    o = df["open"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    v = df["volume"].astype(float).values
    v20 = pd.Series(v).rolling(20).mean().values
    wl = white_line(df["close"].astype(float)).values
    yl = yellow_line(df["close"].astype(float)).values

    try:
        ndf = needle20_lines(df)
        nd = {col: ndf[col].values for col in ndf.columns}
    except Exception:
        nd = None
    try:
        bkf = brick_chart(df)
        bk = {col: bkf[col].values for col in bkf.columns}
    except Exception:
        bk = None

    extra = {"open": o, "high": h, "low": l, "close": c, "vol": v, "vol20": v20,
             "white": wl, "yellow": yl, "needle": nd, "brick": bk}

    result = {}
    for strat, sig_series in signals.items():
        rule = rules.get(strat)
        if rule is None:
            continue
        sig = sig_series.values.astype(bool)
        trades = []
        i = 0
        n = len(sig)
        while i < n - 1:
            if sig[i]:
                if entry_mode == "same_close":
                    buy_idx = i
                    buy_price = c[buy_idx]          # 14:55 尾盘价 ≈ 当日收盘
                    if buy_price <= 0:
                        i += 1
                        continue
                    exit_idx, exit_price, reason = _find_exit(
                        buy_idx, df, rule, extra, first_k=buy_idx + 1)  # T+1: 次日才可卖
                else:
                    buy_idx = i + 1
                    if buy_idx >= n:
                        break
                    buy_price = o[buy_idx]
                    if buy_price <= 0:
                        i += 1
                        continue
                    exit_idx, exit_price, reason = _find_exit(buy_idx, df, rule, extra)
                pnl = (exit_price - buy_price) / buy_price - TRADE_COST
                trades.append({"日期": str(df.index[i])[:10], "买入": round(buy_price, 2),
                               "卖出": round(exit_price, 2), "收益": round(pnl * 100, 2),
                               "持有": exit_idx - buy_idx + 1, "原因": reason})
                i = exit_idx + 1
            else:
                i += 1
        result[strat] = trades
    return result


def summarize(trades):
    if not trades:
        return {"次数": 0, "胜率": 0, "平均盈利": 0, "平均亏损": 0, "盈亏比": 0,
                "累计收益%": 0, "平均单笔%": 0}
    gains = [t["收益"] for t in trades]
    wins = [g for g in gains if g > 0]
    losses = [g for g in gains if g <= 0]
    win_rate = len(wins) / len(gains) * 100
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    ratio = round(avg_win / abs(avg_loss), 2) if avg_loss else 0
    return {"次数": len(gains), "胜率": round(win_rate, 1),
            "平均盈利": round(avg_win, 2), "平均亏损": round(avg_loss, 2),
            "盈亏比": ratio, "总收益累加%": round(sum(gains), 2),
            "平均单笔%": round(np.mean(gains), 2)}


def compound_curve(trades, years=None):
    """时间轴模拟 · 满仓复利收益率 + 年化
    真实资金曲线: 一个战法一个账户满仓轮动。按信号日时间轴推进,
    资金空仓时遇到信号就买入, 卖出(信号日+持有天数)后才能买下一笔;
    持仓期间的信号全部跳过。这样 2.5 年内实际能做多少笔就是多少笔。
    years: 回测年数(2024-01-01 ~ 今天 动态计算)
    """
    from datetime import datetime, timedelta
    if years is None:
        try:
            years = max((datetime.now() - datetime(2024, 1, 1)).days / 365.25, 1.0)
        except Exception:
            years = 2.6
    if not trades:
        return {"收益率%": 0.0, "年化%": 0.0, "复利倍数": 1.0, "实际交易": 0}
    sorted_trades = sorted(trades, key=lambda x: x["日期"])
    capital = 1.0
    free_date = None
    done = 0
    for t in sorted_trades:
        try:
            sig_date = datetime.strptime(t["日期"], "%Y-%m-%d")
        except Exception:
            continue
        hold = t.get("持有", 10)
        if free_date is None or sig_date >= free_date:
            capital *= (1 + t["收益"] / 100)
            free_date = sig_date + timedelta(days=hold + 3)
            done += 1
    ret = (capital - 1) * 100
    annual = (capital ** (1 / years) - 1) * 100
    return {"收益率%": round(ret, 1), "年化%": round(annual, 1),
            "复利倍数": round(capital, 3), "实际交易": done}


def run_backtest(stock_pool, rules=None):
    """跑整个股票池 × 全战法, 返回汇总(每个战法一行: 收益率/胜率)"""
    if rules is None:
        rules = make_exit_rules()
    strat_names = list(rules.keys())
    agg = {s: [] for s in strat_names}

    for code, name in stock_pool:
        r = get_daily_bars(code, count=700, purpose="indicator", freshness="any")
        if r.get("status") != "OK" or not r.get("data"):
            print(f"  ⚠ {name}({code}) 数据失败, 跳过")
            continue
        df = pd.DataFrame(r["data"]).set_index("date")
        df.index = pd.to_datetime(df.index)
        df = df[(df.index >= START)]
        if len(df) < 120:
            print(f"  ⚠ {name}({code}) 数据不足{len(df)}根, 跳过")
            continue
        signals = compute_signals(df)
        per_stock = backtest_stock(signals, df, rules=rules)
        for s in strat_names:
            agg[s].extend(per_stock[s])

    report = {}
    for s in strat_names:
        st = summarize(agg[s])
        st.update(compound_curve(agg[s]))
        st["离场规则"] = rules[s]["desc"]
        report[s] = st
    return report


def print_report(report, rules=None):
    if rules is None:
        rules = make_exit_rules()
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    print("═" * 90)
    print(f"武器库回测报告  {START} ~ {today}  (满仓轮动复利 · 每套战法绑定自己的卖出规则)")
    print(f"买入: 信号日次日开盘 · 离场: 见各战法规则")
    print("═" * 90)
    print(f"{'战法':<8}{'信号数':>6}{'胜率':>8}{'盈亏比':>8}{'收益率%':>11}{'年化%':>9}  离场规则")
    print("─" * 90)
    for s, r in report.items():
        if r["次数"] >= 20 and r["盈亏比"] > 1.2 and r["收益率%"] > 0:
            flag = "✅"
        elif r["次数"] >= 20:
            flag = "⚠️"
        elif r["次数"] > 0:
            flag = "🔍"
        else:
            flag = "—"
        print(f"{s:<8}{r['次数']:>6}{r['胜率']:>7.1f}%{r['盈亏比']:>8.2f}{r['收益率%']:>10.1f}%{r['年化%']:>8.1f}%  {flag}  {r.get('离场规则','')}")
    print("═" * 90)
    print(f"交易成本已扣: 佣金万1免五 + 印花税0.05%(卖) + 过户费万0.1(双) = 单笔往返约{TRADE_COST*100:.3f}%")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="武器库回测(每套战法绑定自己的卖出规则)")
    parser.add_argument("--stocks", default="", help="股票代码逗号分隔, 默认池20只")
    parser.add_argument("--save", default="", help="保存报告到 JSON 文件")
    args = parser.parse_args()

    if args.stocks:
        codes = args.stocks.split(",")
        pool = [(c.strip().zfill(6), c.strip().zfill(6)) for c in codes if c.strip()]
    else:
        pool = DEFAULT_POOL

    rules = make_exit_rules()
    print(f"股票池 {len(pool)} 只, 开始回测(区间 {START} ~ 今天)...")
    report = run_backtest(pool, rules=rules)
    print_report(report, rules=rules)

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已保存: {args.save}")
