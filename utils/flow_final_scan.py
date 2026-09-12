# -*- coding: utf-8 -*-
"""
B2 胜率冲刺 + B2/单针收益转正 专项扫描（2026-09-12）
====================================================
用户目标: B2 胜率 50%+ / B2·单针收益率为正 / 大框架不变微调找最优。

B2 未试维度（逐项收紧，W 系列）:
  W0  现状: 5日窗+反包+不破B1低点+涨4%+量1.5x昨+J<55+无大上影
  W1  量比基准改为「5日均量的2倍」(知识库"倍量柱"=相对均量, 非相对昨日)
  W2  涨幅加上限 4%~7%（排除追涨停/大长阳, 它们次日均值回归风险大）
  W3  J<40（收紧 KDJ 位置）
  W4  W1+W2+W3 全收紧

单针变体（N 系列）:
  N0  现状（长期≥85）
  N1  + 缩量超跌（单针日 量 < 5日均量, 排除放量假超跌）
  N2  + 量比收紧（长期≥85 & 量<5日均量×0.7, 更深的缩量）

每变体输出 ALL/IS/OOS 三元组（胜率/交易数/盈亏比/平均单笔/收益）。
"""
import os
import sys
import json
import time
import socket
import sqlite3
import multiprocessing as mp
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
socket.setdefaulttimeout(20)

from utils.backtest import compute_signals, backtest_stock, make_exit_rules
from utils.backtest_data import DB_PATH, get_benchmark
from utils.flow_env_compare import (load_df, universe_from_cache, stats_v1,
                                    _exit_dates_from_hold, IS_RATIO, IS_TAIL_TRIM,
                                    START, OOS_MIN_TRADES)
from utils.indicators import kdj_j, white_line, yellow_line
from utils.vol_price import heavy_volume_bar

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")
B2_VARIANTS = ["W0", "W1", "W2", "W3", "W4"]
N_VARIANTS = ["N0", "N1", "N2"]
B2_DESC = {
    "W0": "现状(5日窗+反包+不破B1低点+涨4%+量1.5x昨+J<55)",
    "W1": "W0 + 量比基准改5日均量×2.0(倍量柱本义)",
    "W2": "W0 + 涨幅上限7%(4~7%, 排除追涨停)",
    "W3": "W0 + J<40(收紧KDJ)",
    "W4": "W1+W2+W3 全收紧",
}
N_DESC = {
    "N0": "单针现状(长期≥85)",
    "N1": "+ 缩量超跌(量<5日均量)",
    "N2": "+ 深度缩量(量<5日均量×0.7)",
}

_CONN = None
_RULES = None


def _init():
    global _CONN, _RULES
    _CONN = sqlite3.connect(DB_PATH, timeout=60)
    _RULES = make_exit_rules()


def _b2_w(df, sig, w):
    """B2 W 系列信号重算。"""
    s = dict(sig)
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)
    bull = white_line(c) > yellow_line(c)
    chg = c.pct_change() * 100
    k, d, j = kdj_j(h, df["low"].astype(float), c)
    upper_shadow = h - pd.concat([o, c], axis=1).max(axis=1)
    entity = (c - o).abs()
    b1_prev = s["B1"].rolling(5, min_periods=1).max().shift(1)\
        .fillna(False).astype(bool)
    engulf = ((c > o) & (c.shift(1) < o.shift(1))
              & (o <= c.shift(1)) & (c >= o.shift(1)))
    low = df["low"].astype(float)
    b1_low_ff = low.where(s["B1"]).ffill(limit=5)
    not_break_b1 = c >= b1_low_ff

    base = (b1_prev & (chg > 4) & (v > v.shift(1) * 1.5) & (j < 55)
            & (upper_shadow < entity) & (entity > 0) & bull & engulf
            & not_break_b1)

    v5ma = v.rolling(5).mean().shift(1)
    if w == "W0":
        s["B2"] = base
    elif w == "W1":
        s["B2"] = base & (v >= v5ma * 2.0)
    elif w == "W2":
        s["B2"] = base & (chg <= 7)
    elif w == "W3":
        s["B2"] = base & (j < 40)
    elif w == "W4":
        s["B2"] = base & (v >= v5ma * 2.0) & (chg <= 7) & (j < 40)
    return s


def _needle_n(df, sig, n):
    """单针 N 系列信号重算。"""
    s = dict(sig)
    v = df["volume"].astype(float)
    v5ma = v.rolling(5).mean()
    if n == "N0":
        return s
    if n == "N1":
        s["单针"] = s["单针"] & (v < v5ma)
    elif n == "N2":
        s["单针"] = s["单针"] & (v < v5ma * 0.7)
    return s


def _work(code):
    try:
        df = load_df(_CONN, code)
        if df is None or len(df) < 120:
            return []
        sig0 = compute_signals(df)
        rows = []
        for w in B2_VARIANTS:
            s = _b2_w(df, sig0, w)
            try:
                res = backtest_stock(s, df, rules=_RULES)
            except Exception:
                continue
            for tr in res.get("B2", []):
                rows.append({"var": w, "strat": "B2",
                             "entry": str(pd.Timestamp(tr["日期"]) + pd.Timedelta(days=1))[:10],
                             "pnl": tr["收益"], "hold": int(tr.get("持有", 10))})
        for n in N_VARIANTS:
            s = _needle_n(df, sig0, n)
            try:
                res = backtest_stock(s, df, rules=_RULES)
            except Exception:
                continue
            for tr in res.get("单针", []):
                rows.append({"var": n, "strat": "单针",
                             "entry": str(pd.Timestamp(tr["日期"]) + pd.Timedelta(days=1))[:10],
                             "pnl": tr["收益"], "hold": int(tr.get("持有", 10))})
        return rows
    except Exception:
        return []


def run(limit=0, workers=8, tag=""):
    os.makedirs(OUT_DIR, exist_ok=True)
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    bench = get_benchmark(start=START)
    cal = [str(d)[:10] for d in bench["date"]]
    sp_idx = int(len(cal) * IS_RATIO)
    split_date, is_lo = cal[sp_idx], cal[max(sp_idx - IS_TAIL_TRIM, 0)]
    print(f"[split] {split_date} (IS≤{is_lo})")

    all_vars = B2_VARIANTS + N_VARIANTS
    t0 = time.time()
    buckets = {v: [] for v in all_vars}
    done = 0
    with mp.Pool(processes=workers, initializer=_init) as pool:
        for rows in pool.imap_unordered(_work, codes, chunksize=20):
            done += 1
            for r in rows:
                buckets[r["var"]].append(r)
            if done % 1000 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(codes)}  {el/60:.1f}m", flush=True)
    print(f"[run] 完成 {done} 只, 耗时 {(time.time()-t0)/60:.1f} 分钟")

    def seg(trades):
        tr = _exit_dates_from_hold(list(trades))
        is_t = [t for t in tr if t["entry"] < split_date and t["entry"] < is_lo]
        oos_t = [t for t in tr if t["entry"] >= split_date]
        out = {"ALL": stats_v1(tr), "IS": stats_v1(is_t), "OOS": stats_v1(oos_t)}
        for k in ("IS", "OOS"):
            if 0 < out[k]["n"] < OOS_MIN_TRADES:
                out[k]["insufficient"] = True
        return out

    res = {v: seg(buckets[v]) for v in all_vars}
    payload = {"version": "final_scan", "generated_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "results": res}
    jp = os.path.join(OUT_DIR, f"final_scan_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 88)
    print("B2 胜率冲刺（W 系列）—— 目标 50%")
    print("=" * 88)
    for w in B2_VARIANTS:
        r = res[w]
        for seg_k in ("ALL", "IS", "OOS"):
            d = r.get(seg_k, {})
            flag = " ⚠样本不足" if d.get("insufficient") else ""
            print(f"{w:<4}{seg_k:<5} n={d.get('n',0):>7}  胜率={d.get('win_rate',0):>5.1f}%  "
                  f"盈亏比={d.get('pl_ratio',0) or 0:>5.2f}  平均单笔={d.get('avg_pnl',0):>6.3f}%  "
                  f"收益={d.get('sum_pnl_pct',0):>9.1f}%{flag}")
        print("-" * 88)
    print("\n" + "=" * 88)
    print("单针收益优化（N 系列）")
    print("=" * 88)
    for n in N_VARIANTS:
        r = res[n]
        for seg_k in ("ALL", "IS", "OOS"):
            d = r.get(seg_k, {})
            flag = " ⚠样本不足" if d.get("insufficient") else ""
            print(f"{n:<4}{seg_k:<5} n={d.get('n',0):>7}  胜率={d.get('win_rate',0):>5.1f}%  "
                  f"盈亏比={d.get('pl_ratio',0) or 0:>5.2f}  平均单笔={d.get('avg_pnl',0):>6.3f}%  "
                  f"收益={d.get('sum_pnl_pct',0):>9.1f}%{flag}")
        print("-" * 88)
    print("=" * 88)
    mp_ = os.path.join(OUT_DIR, f"final_scan_{tag}.md")
    with open(mp_, "w", encoding="utf-8") as f:
        f.write(f"# B2 胜率冲刺 + 单针收益优化（{datetime.now():%Y-%m-%d %H:%M}）\n\n")
        for title, vs, desc in (("B2 W 系列（胜率冲刺）", B2_VARIANTS, B2_DESC),
                                ("单针 N 系列（收益优化）", N_VARIANTS, N_DESC)):
            f.write(f"## {title}\n\n| 变体 | 定义 | 段 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | 收益% |\n"
                    f"|---|---|---|---:|---:|---:|---:|---:|\n")
            for v in vs:
                r = res[v]
                for seg_k in ("ALL", "IS", "OOS"):
                    d = r.get(seg_k, {})
                    flag = " ⚠" if d.get("insufficient") else ""
                    f.write(f"| {v} | {desc[v]} | {seg_k} | {d.get('n',0)} | "
                            f"{d.get('win_rate',0):.1f}% | {d.get('pl_ratio',0) or 0:.2f} | "
                            f"{d.get('avg_pnl',0):.3f} | {d.get('sum_pnl_pct',0):.1f}{flag} |\n")
            f.write("\n")
    print(f"报告: {mp_}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="B2 胜率冲刺 + 单针收益优化")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    run(limit=a.limit, workers=a.workers, tag=a.tag)


if __name__ == "__main__":
    main()
