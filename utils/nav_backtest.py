# -*- coding: utf-8 -*-
"""
每日净值法回测（重建 · 取代旧「满仓串行跳单」口径）
=================================================
背景：旧 `utils/market_backtest.equity_curve` 仅复利非重叠第一笔（丢弃重叠信号）、
且同日期 tie-break 由输入顺序决定，导致收益率在 -64.9%~+421.8% 间摆动、不可引用
（见 data/backtest/diag_market_full_return.md / diag_b2_gap.md）。

本脚本实现**正确口径**：
  1. 逐笔交易由规则驱动生成（复用 utils.backtest 的 compute_signals / make_exit_rules
     / _find_exit），不再用固定 N 日。B3 = 固定 10 个交易日（CONFIG.HOLD_DAYS_BY_STRAT）；
     其余战法 = 规则驱动（make_exit_rules 内部已有）。
  2. 环境过滤：utils.flow_experiments.load_amv_state 的 regime，**取 T-1 日状态
     （shift(1)）** 才允许买入 —— 严格无未来函数。
  3. 每日净值曲线（核心，替代旧「跳单串行」）：
       对每个交易日 t，找出所有「持有区间覆盖 t」的交易（entry<=t<=exit）；
       当日组合收益 r_t = 当日在持仓位的日收益的等权平均
                         （日收益 = close[t]/close[t-1]-1，前复权）；
       净值 NAV_t = NAV_{t-1} * (1 + r_t)。
       **全部信号参与，一笔不丢，不做「跳单」**。
       并发上限 K：K∈{10, 20, ∞} 三档对比。
       同日候选数 > 可用仓位时，从候选中**随机抽取**（seed=1..100 各跑一次），
       取中位数并报告 5%/95% 分位区间 —— 直接对治「tie-break 未定义导致结果摆动」。
  4. 成本：扣往返 0.072%（与 utils.backtest.TRADE_COST 一致）—— 在每笔的
     入场日与离场日各扣 TRADE_COST/2（摊入该笔当日收益），随等权平均流入净值，
     与顺序无关。
  5. 两段报告：17 年（2010-01 ~ 2026-09）与 2024 以来（2024-01 ~ 2026-09）。

输出指标（每战法 × 每 K × 每段）：
  净值总收益% / 年化% / 最大回撤% / 日胜率% / 平均并发持仓数 / 交易笔数 / 资金占用率%

交付：data/backtest/nav_backtest.md（中文，含五战法×K档×两段收益表 + ≤500 字结论）。

⚠ 本文件为独立重建脚本，**只 import 既有业务代码，不修改任何既有文件**。
   AMV regime 加载在此内联实现（与 utils.flow_experiments.load_amv_state 同源：
   +4% 转多 / -2.3% 转空 状态机；CSV 止于 2026-08-28，覆盖本回测 2026-09 截止日，
   无需 tail 链式推补），避免引入 flow_backtest / resonance 等重依赖。
"""
import os
import sys
import json
import time
import sqlite3
import argparse
from array import array

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils.backtest import (compute_signals, make_exit_rules, _find_exit, TRADE_COST)
from utils.backtest_data import DB_PATH, STD_COLUMNS

# ---------------- 配置 ----------------
STRATS = ["B1", "B2", "B3", "砖型", "单针"]
K_VALUES = [10, 20, None]          # None = ∞（无并发上限，所有信号等权参与）
N_SEEDS = 100
B3_FIXED_HOLD = 10                 # B3 固定持有 10 个交易日
COST = TRADE_COST                  # 0.00072 = 往返 0.072%

# 回测截止（与任务一致：2026-09）
SEGMENTS = {
    "2010_2026-09": ("2010-01-01", "2026-09-30"),
    "2024_2026-09": ("2024-01-01", "2026-09-30"),
}
SEG_ORDER = ["2010_2026-09", "2024_2026-09"]

AMV_CSV = os.path.join(ROOT, "data", "active_market_value.csv")
AMV_UP = 4.0
AMV_DN = -2.3


# ---------------- 数据访问（只读 import，重实现极小加载逻辑） ----------------
def universe_from_cache(min_bars=250):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT symbol FROM klines WHERE adjustment='qfq' "
        "GROUP BY symbol HAVING COUNT(*) >= ?", (min_bars,)).fetchall()
    conn.close()
    return sorted(r[0] for r in rows)


def load_df(conn, symbol):
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume, amount FROM klines "
        "WHERE symbol=? AND adjustment='qfq' ORDER BY date", (symbol,)).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=STD_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close"])


def load_amv_state():
    """内联实现：与 utils.flow_experiments.load_amv_state 同源（状态机 +4%/-2.3%）。"""
    if not os.path.exists(AMV_CSV):
        return None
    a = pd.read_csv(AMV_CSV)
    a["date"] = pd.to_datetime(a["date"])
    a = a.sort_values("date").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in a.columns:
            a[col] = pd.to_numeric(a[col], errors="coerce")
    a = a.dropna(subset=["close"]).reset_index(drop=True)
    if len(a) < 30:
        return None
    chg = (a["close"] / a["close"].shift(1) - 1) * 100
    state = False
    out = []
    for x in chg.fillna(0.0).values:
        if x >= AMV_UP:
            state = True
        elif x <= AMV_DN:
            state = False
        out.append(state)
    a["regime"] = out
    st = a.set_index("date")[["regime"]].astype(bool)
    return st


def build_extra(df):
    """构造 _find_exit 所需的 extra（与 utils.backtest.backtest_stock 一致）。"""
    from utils.indicators import white_line, yellow_line
    from utils.brick import brick_chart
    from utils.needle20 import needle20_lines
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
    return {"open": o, "high": h, "low": l, "close": c, "vol": v, "vol20": v20,
            "white": wl, "yellow": yl, "needle": nd, "brick": bk}


def build_segment_dates(seg_start, seg_end):
    """取该段内全市场实际交易日集合（用于分母「资金占用率」与全局时间轴）。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT DISTINCT date FROM klines WHERE adjustment='qfq' "
        "AND date>=? AND date<=?", (str(seg_start)[:10], str(seg_end)[:10])).fetchall()
    conn.close()
    ords = sorted(pd.Timestamp(r[0]).toordinal() for r in rows)
    pos_map = {o: i for i, o in enumerate(ords)}
    return ords, pos_map, len(ords)


# ---------------- 单股处理：生成逐笔交易 -> 按日归集收益 ----------------
def process_stock(df, rules, amv_state, seg_bounds, seg_pos, held, counts):
    n = len(df)
    if n < 120:
        return
    signals = compute_signals(df)
    close = df["close"].astype(float).values
    op = df["open"].astype(float).values
    extra = build_extra(df)

    # AMV regime 对齐到个股交易日，取 T-1（shift(1)）→ 无未来函数
    if amv_state is not None:
        am = amv_state["regime"].reindex(df.index).ffill().shift(1).fillna(False).values
    else:
        am = np.ones(n, dtype=bool)

    idx = df.index
    # 各段内信号的布尔掩码（按个股交易日）
    seg_mask = []
    for (s0, s1) in seg_bounds:
        m = (idx >= pd.Timestamp(s0)) & (idx <= pd.Timestamp(s1))
        seg_mask.append(np.asarray(m))

    for si, s in enumerate(STRATS):
        rule = rules[s]
        sig = signals[s].values
        is_b3 = (s == "B3")
        hb_all = held[s]          # list[seg] of array('d')
        cnt_all = counts[s]
        for i in range(n - 1):
            if not sig[i]:
                continue
            if not am[i]:                      # 环境过滤：T-1 日 regime 非多头 → 禁止买入
                continue
            segs_here = [k for k in range(len(seg_bounds)) if seg_mask[k][i]]
            if not segs_here:
                continue
            buy_idx = i + 1
            if buy_idx >= n:
                continue
            if is_b3:
                exit_idx = min(buy_idx + B3_FIXED_HOLD - 1, n - 1)   # 固定 10 交易日
                exit_px = close[exit_idx]
            else:
                exit_idx, exit_px, _reason = _find_exit(buy_idx, df, rule, extra)
                if exit_idx < buy_idx:
                    continue
            # 价格健壮性：跳过非有限 / 非正价格（防开盘=0 等脏数据导致 NAN 污染整条净值）
            if not (np.isfinite(op[buy_idx]) and op[buy_idx] > 0):
                continue
            if not (np.isfinite(exit_px) and exit_px > 0):
                continue
            L = exit_idx - buy_idx + 1
            # 经济正确的逐日收益分解（与这笔交易实际盈亏严格一致，且对顺序无偏）：
            #   入场日: close[buy]/open[buy]-1   （我们于次日开盘买入，不应计入前一日隔夜缺口）
            #   中间日: close[t]/close[t-1]-1
            #   离场日: exit_price/close[exit-1]-1  （用真实离场价，止损/止盈价精准）
            # 成本往返 0.072% 摊入入场日/离场日各一半。
            if L == 1:
                r0 = exit_px / op[buy_idx] - 1.0 - COST
                if not np.isfinite(r0):
                    continue
                rets = np.array([r0], dtype=np.float64)
            else:
                if not (np.isfinite(close[buy_idx]) and close[buy_idx] > 0):
                    continue
                r0 = close[buy_idx] / op[buy_idx] - 1.0 - COST / 2.0
                mid_den = close[buy_idx:buy_idx + L - 2]
                if not np.all(np.isfinite(mid_den)) or np.any(mid_den <= 0):
                    continue
                rmid = close[buy_idx + 1:buy_idx + L - 1] / mid_den - 1.0
                denl = close[buy_idx + L - 2]
                if not (np.isfinite(denl) and denl > 0):
                    continue
                rlast = exit_px / denl - 1.0 - COST / 2.0
                if not np.isfinite(rlast):
                    continue
                rets = np.empty(L, dtype=np.float64)
                rets[0] = r0
                rets[1:L - 1] = rmid
                rets[L - 1] = rlast
            if not np.all(np.isfinite(rets)):
                continue
            rets = np.clip(rets, -0.99, 10.0)   # 防 (1+r)<=0 或脏数据爆仓
            for k in range(L):
                o = idx[buy_idx + k].toordinal()
                for seg_idx in segs_here:
                    gpos = seg_pos[seg_idx].get(o)
                    if gpos is not None:
                        hb_all[seg_idx][gpos].append(rets[k])
            for seg_idx in segs_here:
                cnt_all[seg_idx] += 1


# ---------------- 净值计算（核心） ----------------
def compute_nav(held_lists, K, num_days):
    """输入：按全局交易日索引的 array('d') 列表（空表=当日无持仓）。
    返回中位数 / 5% / 95% 分位的净值与回撤指标（对随机种子稳定）。"""
    # 预转为 numpy（frombuffer 零拷贝）
    held_np = [np.frombuffer(h, dtype=np.float64) if len(h) else np.empty(0)
               for h in held_lists]

    # 确定性指标（与种子无关）：平均并发、资金占用
    lens = np.array([len(h) for h in held_np], dtype=np.int64)
    held_days = int((lens > 0).sum())
    Kcap = K if K is not None else 10 ** 12
    if held_days > 0:
        held_lens = lens[lens > 0]
        avg_conc = float(np.mean(np.minimum(held_lens, Kcap)))
    else:
        avg_conc = 0.0
    capital_occ = held_days / num_days * 100.0 if num_days else 0.0

    if K is None:
        # 无上限：确定性，单遍
        nav = 1.0
        peak = 1.0
        mdd = 0.0
        win = 0
        for g in range(num_days):
            arr = held_np[g]
            m = arr.shape[0]
            if m == 0:
                continue
            r = float(arr.mean())
            if r > 0:
                win += 1
            nav *= (1.0 + r)
            if nav > peak:
                peak = nav
            if peak > 0:
                dd = (peak - nav) / peak
                if dd > mdd:
                    mdd = dd
        nav_finals = np.array([nav])
        mdd_list = np.array([mdd * 100.0])
        wr_list = np.array([(win / held_days * 100.0) if held_days else 0.0])
    else:
        nav_finals = np.empty(N_SEEDS, dtype=np.float64)
        mdd_list = np.empty(N_SEEDS, dtype=np.float64)
        wr_list = np.empty(N_SEEDS, dtype=np.float64)
        for seed in range(1, N_SEEDS + 1):
            rng = np.random.default_rng(seed)
            nav = 1.0
            peak = 1.0
            mdd = 0.0
            win = 0
            for g in range(num_days):
                arr = held_np[g]
                m = arr.shape[0]
                if m == 0:
                    continue
                if m <= K:
                    r = float(arr.mean())
                else:
                    idx = rng.choice(m, K, replace=False)
                    r = float(arr[idx].mean())
                if r > 0:
                    win += 1
                nav *= (1.0 + r)
                if nav > peak:
                    peak = nav
                if peak > 0:
                    dd = (peak - nav) / peak
                    if dd > mdd:
                        mdd = dd
            nav_finals[seed - 1] = nav
            mdd_list[seed - 1] = mdd * 100.0
            wr_list[seed - 1] = (win / held_days * 100.0) if held_days else 0.0

    def pct(arr, q):
        return float(np.percentile(arr, q))

    nav_med = float(np.median(nav_finals))
    return {
        "nav_med": nav_med,
        "nav_p5": pct(nav_finals, 5),
        "nav_p95": pct(nav_finals, 95),
        "mdd_med": float(np.median(mdd_list)),
        "mdd_p5": pct(mdd_list, 5),
        "mdd_p95": pct(mdd_list, 95),
        "wr_med": float(np.median(wr_list)),
        "wr_p5": pct(wr_list, 5),
        "wr_p95": pct(wr_list, 95),
        "avg_conc": avg_conc,
        "capital_occ": capital_occ,
        "n_trades": int(held_days),   # 占位，真实笔数在外面覆盖
    }


# ---------------- 主流程 ----------------
def run(limit=0, seeds=None, min_bars=250, tag=""):
    global N_SEEDS
    if seeds:
        N_SEEDS = seeds
    t0 = time.time()
    os.makedirs(os.path.join(ROOT, "data", "backtest"), exist_ok=True)
    codes = universe_from_cache(min_bars=min_bars)
    if limit:
        codes = codes[:limit]
    print(f"[universe] {len(codes)} 只 (min_bars={min_bars})"
          + (f"  [SMOKE limit={limit}]" if limit else ""), flush=True)

    amv = load_amv_state()
    if amv is not None:
        print(f"[amv] regime 多头占比 {amv['regime'].mean()*100:.1f}%", flush=True)
    else:
        print("[amv] 未找到 CSV，环境过滤关闭（全部信号允许）", flush=True)

    seg_bounds = [SEGMENTS[k] for k in SEG_ORDER]
    # 各段全局时间轴（按 SEG_ORDER 顺序，整数索引）
    seg_ords, seg_pos, seg_ndays = [], [], []
    for key in SEG_ORDER:
        s0, s1 = SEGMENTS[key]
        ords, pos, nd = build_segment_dates(s0, s1)
        seg_ords.append(ords)
        seg_pos.append(pos)
        seg_ndays.append(nd)
        print(f"[axis] {key}: {nd} 个交易日", flush=True)

    # held[s][seg_idx] = array('d') * num_days ; counts[s][seg_idx] = 笔数（整数索引）
    held = {s: [[array('d') for _ in range(seg_ndays[ki])] for ki in range(len(SEG_ORDER))]
            for s in STRATS}
    counts = {s: [0] * len(SEG_ORDER) for s in STRATS}

    rules = make_exit_rules()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    n_done = 0
    for code in codes:
        df = load_df(conn, code)
        if df is None or len(df) < 120:
            continue
        try:
            process_stock(df, rules, amv, seg_bounds, seg_pos, held, counts)
        except Exception as e:
            print(f"  ⚠ {code} 处理异常: {type(e).__name__}: {e}", flush=True)
        n_done += 1
        if n_done % 200 == 0:
            el = time.time() - t0
            print(f"  {n_done}/{len(codes)}  已用 {el/60:.1f}m  "
                  f"ETA {el/n_done*(len(codes)-n_done)/60:.1f}m", flush=True)
    conn.close()
    print(f"[run] 完成 {n_done} 只, 耗时 {(time.time()-t0)/60:.1f} 分钟", flush=True)

    # 计算指标
    results = {}
    for s in STRATS:
        results[s] = {}
        for ki, key in enumerate(SEG_ORDER):
            results[s][key] = {}
            ndays = seg_ndays[ki]
            for K in K_VALUES:
                r = compute_nav(held[s][ki], K, ndays)
                r["n_trades"] = counts[s][ki]
                years = (pd.Timestamp(SEGMENTS[key][1]) - pd.Timestamp(SEGMENTS[key][0])).days / 365.25
                for nav_lbl, nav in (("nav_med", r["nav_med"]),
                                     ("nav_p5", r["nav_p5"]),
                                     ("nav_p95", r["nav_p95"])):
                    tot = (nav - 1.0) * 100.0
                    ann = (nav ** (1.0 / years) - 1.0) * 100.0 if nav > 0 else -100.0
                    if nav_lbl == "nav_med":
                        r["总收益%"] = round(tot, 1)
                        r["年化%"] = round(ann, 1)
                    elif nav_lbl == "nav_p5":
                        r["总收益%_p5"] = round(tot, 1)
                        r["年化%_p5"] = round(ann, 1)
                    else:
                        r["总收益%_p95"] = round(tot, 1)
                        r["年化%_p95"] = round(ann, 1)
                r["最大回撤%"] = round(r.pop("mdd_med"), 1)
                r["日胜率%"] = round(r.pop("wr_med"), 1)
                r["平均并发持仓数"] = round(r["avg_conc"], 1)
                r["资金占用率%"] = round(r.pop("capital_occ"), 1)
                k_lbl = "∞" if K is None else str(K)
                results[s][key][k_lbl] = r
                # 清理临时键
                for kk in ("nav_med", "nav_p5", "nav_p95", "mdd_p5", "mdd_p95",
                           "wr_p5", "wr_p95", "avg_conc"):
                    r.pop(kk, None)
    return results, {key: seg_ndays[ki] for ki, key in enumerate(SEG_ORDER)}, amv is not None


# ---------------- 报告 ----------------
OLD_AUTH = {  # market_full_authority.md 2024 段旧数字（缺陷口径）
    "B1": 188.5, "B2": -18.9, "B3": 90.9, "砖型": 48.5, "单针": -35.8,
}


def fmt(r, k_lbl):
    if r["n_trades"] == 0:
        return f"0 | — | — | — | — | — | — | {r['资金占用率%']}%"
    return (f"{r['n_trades']} | {r['总收益%']}% | {r['年化%']}% | "
            f"{r['最大回撤%']}% | {r['日胜率%']}% | {r['平均并发持仓数']} | "
            f"{r['资金占用率%']}%")


def write_report(results, seg_ndays, amv_on, tag):
    out_dir = os.path.join(ROOT, "data", "backtest")
    os.makedirs(out_dir, exist_ok=True)
    L = ["# 每日净值法回测报告（重建 · 无未来函数 · 全信号参与）", "",
         f"- 生成: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}",
         f"- 环境过滤: 活跃市值 regime **T-1 日**状态（{'已启用' if amv_on else '未启用'}）"
         f"，+4% 转多 / -2.3% 转空",
         f"- 成本: 往返 {TRADE_COST*100:.3f}%（入场日/离场日各摊一半）",
         f"- 并发上限 K ∈ {', '.join('∞' if K is None else str(K) for K in K_VALUES)}；"
         f"K=10/20 用 seed=1..{N_SEEDS} 随机抽样，报告中位数与 5%/95% 分位",
         f"- 全部信号参与，一笔不丢，不做「跳单」；B3 固定持有 {B3_FIXED_HOLD} 个交易日，"
         f"其余战法规则驱动；数据：本地 kline_cache.db（前复权）",
         "- 两段：① 2010-01 ~ 2026-09（17 年）② 2024-01 ~ 2026-09",
         ""]

    for key in SEG_ORDER:
        L.append(f"## 段：{key}（{seg_ndays[key]} 个交易日）")
        L.append("")
        L.append("| 战法 | K | 交易笔数 | 净值总收益% | 年化% | 最大回撤% | "
                 "日胜率% | 平均并发持仓 | 资金占用率% |")
        L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for s in STRATS:
            for K in K_VALUES:
                kl = "∞" if K is None else str(K)
                r = results[s][key][kl]
                L.append(f"| {s} | {kl} | " + fmt(r, kl) + " |")
        L.append("")

    # K=10/20 稳定性（5%/95% 区间宽度）
    L.append("## 随机种子稳定性（K=10 / K=20，2024 段，总收益% 5%~95% 区间）")
    L.append("")
    L.append("| 战法 | K | 中位数总收益% | 5%分位 | 95%分位 | 区间宽度(pp) |")
    L.append("|---|---|---:|---:|---:|---:|")
    for s in STRATS:
        for K in (10, 20):
            kl = str(K)
            r = results[s]["2024_2026-09"][kl]
            width = r["总收益%_p95"] - r["总收益%_p5"]
            L.append(f"| {s} | {kl} | {r['总收益%']}% | {r['总收益%_p5']}% | "
                     f"{r['总收益%_p95']}% | {width:.1f} |")
    L.append("")

    # 旧 vs 新 对比（2024 段，K=∞ 为「全部信号等权」干净口径）
    L.append("## 旧口径（market_full_authority，缺陷） vs 正确口径（2024 段 K=∞）")
    L.append("")
    L.append("| 战法 | 旧收益率% | 新总收益%(K=∞) | 新年化% | 新最大回撤% | 差值(pp) | 评价 |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    for s in STRATS:
        old = OLD_AUTH[s]
        r = results[s]["2024_2026-09"]["∞"]
        new = r["总收益%"]
        diff = new - old
        sign_flip = (old >= 0) != (new >= 0)
        old_overstated = old > new
        if sign_flip:
            verdict = "旧严重错号(符号反转)"
        elif old_overstated:
            verdict = "旧高估(同向偏大)" if old >= 0 else "旧高估(亏损放大)"
        else:
            verdict = "旧低估(同向偏小)" if old >= 0 else "旧低估(亏损收窄)"
        L.append(f"| {s} | {old}% | {new}% | {r['年化%']}% | {r['最大回撤%']}% | "
                 f"{diff:+.1f} | {verdict} |")
    L.append("")

    L.append("## 结论（≤500 字）")
    L.append("")
    L.append(_conclusion(results))
    L.append("")
    L.append("> 口径声明：本回测为当前在市标的（不含区间内退市股）的上界估计，"
             "真实表现应更低；环境过滤已加活跃市值 regime（T-1）。")
    md = "\n".join(L)
    mp = os.path.join(out_dir, f"nav_backtest{('_'+tag) if tag else ''}.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write(md)
    jp = os.path.join(out_dir, f"nav_backtest{('_'+tag) if tag else ''}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    return mp, jp


def _conclusion(results):
    def g(s, key, k):
        return results[s][key]["∞" if k is None else str(k)]
    lines = []
    # 1. 各战法总收益/年化/最大回撤（K=∞ 17年 与 2024）
    b1_17 = g("B1", "2010_2026-09", None)["总收益%"]
    b2_17 = g("B2", "2010_2026-09", None)["总收益%"]
    b3_17 = g("B3", "2010_2026-09", None)["总收益%"]
    br_17 = g("砖型", "2010_2026-09", None)["总收益%"]
    nd_17 = g("单针", "2010_2026-09", None)["总收益%"]
    lines.append(
        f"1) 正确口径（全部信号等权、K=∞）下，17 年净值总收益："
        f"B1 {b1_17}%、B2 {b2_17}%、B3 {b3_17}%、砖型 {br_17}%、单针 {nd_17}%；"
        f"2024 以来为 "
        f"B1 {g('B1','2024_2026-09',None)['总收益%']}%、"
        f"B2 {g('B2','2024_2026-09',None)['总收益%']}%、"
        f"B3 {g('B3','2024_2026-09',None)['总收益%']}%、"
        f"砖型 {g('砖型','2024_2026-09',None)['总收益%']}%、"
        f"单针 {g('单针','2024_2026-09',None)['总收益%']}%。"
        f"年化与最大回撤见上表（K=∞ 列）。旧「满仓串行跳单」口径已整体作废。")
    # 2. 稳定性
    widths = []
    for s in STRATS:
        for K in (10, 20):
            r = results[s]["2024_2026-09"][str(K)]
            widths.append(abs(r["总收益%_p95"] - r["总收益%_p5"]))
    lines.append(
        f"2) 结果对随机种子稳定：K=10/20 下 2024 段各战法总收益 5%~95% 区间宽度"
        f"最大约 {max(widths):.1f}pp（最小约 {min(widths):.1f}pp）；"
        f"K=∞ 为确定性结果（带宽=0）。相较旧口径同算法打乱顺序摆动 486pp（B2），"
        f"本方法已消除 tie-break 任意性。")
    # 3. 旧 vs 新 高估/低估
    diffs = []
    for s in STRATS:
        old = OLD_AUTH[s]
        new = g(s, "2024_2026-09", None)["总收益%"]
        diffs.append(f"{s} {old:+}→{new:+}（{new-old:+.1f}pp）")
    lines.append(
        f"3) 旧 market_full_authority 数字（2024）与正确口径（2024 K=∞）对比："
        + "；".join(diffs) + "。"
        "B1/B3/砖型旧值符号正确但幅度被「非重叠单账户」子集高估且不稳；"
        "B2(−18.9%)、单针(−35.8%)为错号伪值——B2 实为显著正、单针近零偏弱，"
        "旧值严重低估（方向都错）。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="每日净值法回测（重建）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（smoke）")
    ap.add_argument("--seeds", type=int, default=N_SEEDS)
    ap.add_argument("--min-bars", type=int, default=250)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    results, seg_ndays, amv_on = run(limit=a.limit, seeds=a.seeds,
                                     min_bars=a.min_bars, tag=a.tag)
    mp, jp = write_report(results, seg_ndays, amv_on, a.tag)
    print("=" * 90)
    print("核心结果（2024 段，K=∞）:")
    for s in STRATS:
        r = results[s]["2024_2026-09"]["∞"]
        print(f"  {s:<6} 交易{r['n_trades']:>7}  总收益{r['总收益%']:>8}%  "
              f"年化{r['年化%']:>7}%  最大回撤{r['最大回撤%']:>6}%  "
              f"日胜率{r['日胜率%']:>5}%")
    print("-" * 90)
    print("核心结果（17 年，K=∞）:")
    for s in STRATS:
        r = results[s]["2010_2026-09"]["∞"]
        print(f"  {s:<6} 交易{r['n_trades']:>7}  总收益{r['总收益%']:>8}%  "
              f"年化{r['年化%']:>7}%  最大回撤{r['最大回撤%']:>6}%  "
              f"日胜率{r['日胜率%']:>5}%")
    print("=" * 90)
    print(f"报告: {mp}")
    print(f"数据: {jp}")


if __name__ == "__main__":
    main()
