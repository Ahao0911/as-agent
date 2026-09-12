# -*- coding: utf-8 -*-
"""
B2 专项扫描（2026-09-12）
==========================
用户判断：B2 是「B1 起来那一根」，确定性最高、胜率应最高。实测 32.3% 说明实现错了。

知识库《B2案例买点精讲》原文要点：
- B2 是 B1 之后的确定性买点，波段启动关键信号
- 通常出现在 B1 之后，涨幅大于 4%，且伴随放量
- 形态特征：倍量柱(单根或双根显著放大) + 暴力K线 + 反包(阳线完全覆盖前一根阴线实体，"灾后重建")
- 本地 b1_rules.md：B2 = 突破确认 = 股价突破关键压力位(多空黄线/前高)，量能配合

变体矩阵（V0 现状为基线，逐个叠加缺失形态）:
  V0  现状: b1_prev(10) & chg>4 & v>1.5x昨 & J<55 & 无大上影 & bull
  V1  = V0 + 反包(阳线吞没前阴实体)
  V2  = V0 + 突破(前20日新高 或 上穿黄线)
  V3  = V0 + 反包 + 突破
  V4  = V0(量比2.0) + 反包 + 突破
  V5  = V3 但 B1 窗口收紧至 5 日
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

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")
VARIANTS = ["V0", "V1", "V6", "V7", "V5"]   # V6=2日窗 V7=3日窗 V5=5日窗(对照V1=10日窗)
V_DESC = {
    "V0": "现状(基线): B1后10日+涨4%+量1.5x+J<55+无大上影",
    "V1": "V0 + 反包(阳线吞没前阴实体)",
    "V2": "V0 + 突破(前20日新高 或 上穿黄线)",
    "V3": "V0 + 反包 + 突破",
    "V4": "量比2.0 + 反包 + 突破",
    "V5": "V1(反包) 但 B1 窗口 5 日",
    "V6": "V1(反包) 但 B1 窗口 2 日 ← 用户判断:B2是B1的第二天",
    "V7": "V1(反包) 但 B1 窗口 3 日",
}

_CONN = None
_RULES = None


def _init():
    global _CONN, _RULES
    _CONN = sqlite3.connect(DB_PATH, timeout=60)
    _RULES = make_exit_rules()


def b2_variant_sig(df, sig, variant):
    """按变体重算 B2 信号（其余战法不动）。"""
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

    win = {"V5": 5, "V6": 2, "V7": 3}.get(variant, 10)
    b1_prev = sig["B1"].rolling(win, min_periods=1).max().shift(1)\
        .fillna(False).astype(bool)
    mult = 2.0 if variant in ("V4",) else 1.5
    base = (b1_prev & (chg > 4) & (v > v.shift(1) * mult) & (j < 55)
            & (upper_shadow < entity) & (entity > 0) & bull)

    # 反包（灾后重建）：今日阳线，前日阴线，今日实体完全覆盖前日阴线实体
    engulf = ((c > o) & (c.shift(1) < o.shift(1))
              & (o <= c.shift(1)) & (c >= o.shift(1)))

    # 突破：收盘破前 20 日最高（防前视 shift(1)）或 上穿黄线
    prior_high = h.rolling(20, min_periods=1).max().shift(1)
    yx = yellow_line(c)
    breakout = (c > prior_high) | ((c > yx) & (c.shift(1) <= yx.shift(1)))

    if variant == "V0":
        s["B2"] = base
    elif variant in ("V1", "V6", "V7"):
        s["B2"] = base & engulf
    elif variant == "V2":
        s["B2"] = base & breakout
    elif variant == "V5":
        s["B2"] = base & engulf & breakout
    return s


def _work(code):
    try:
        df = load_df(_CONN, code)
        if df is None or len(df) < 120:
            return []
        sig0 = compute_signals(df)
        rows = []
        for var in VARIANTS:
            s = b2_variant_sig(df, sig0, var)
            try:
                res = backtest_stock(s, df, rules=_RULES)
            except Exception:
                continue
            for tr in res.get("B2", []):
                rows.append({"var": var,
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

    t0 = time.time()
    buckets = {v: [] for v in VARIANTS}
    done = 0
    with mp.Pool(processes=workers, initializer=_init) as pool:
        for rows in pool.imap_unordered(_work, codes, chunksize=20):
            done += 1
            for r in rows:
                buckets[r["var"]].append(r)
            if done % 1000 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(codes)}  {el/60:.1f}m  "
                      f"ETA {el/max(done,1)*(len(codes)-done)/60:.1f}m", flush=True)
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

    res = {v: seg(buckets[v]) for v in VARIANTS}
    payload = {"version": "b2_scan", "generated_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "split_date": split_date, "variants": V_DESC, "results": res}
    jp = os.path.join(OUT_DIR, f"b2_scan_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 88)
    print("B2 变体扫描（用户判断: B2 确定性最高, 胜率应为全场最高）")
    print("=" * 88)
    print(f"{'变体':<6}{'段':<5}{'交易数':>8}{'胜率':>8}{'盈亏比':>8}{'平均单笔%':>10}{'收益%':>10}")
    for var in VARIANTS:
        r = res[var]
        for seg_k in ("ALL", "IS", "OOS"):
            d = r.get(seg_k, {})
            flag = " ⚠样本不足" if d.get("insufficient") else ""
            print(f"{var:<6}{seg_k:<5}{d.get('n', 0):>8}"
                  f"{d.get('win_rate', 0):>7.1f}%{d.get('pl_ratio', 0) or 0:>8.2f}"
                  f"{d.get('avg_pnl', 0):>10.3f}{d.get('sum_pnl_pct', 0):>10.1f}{flag}")
        print("-" * 88)
    print("=" * 88)
    mp_ = os.path.join(OUT_DIR, f"b2_scan_{tag}.md")
    with open(mp_, "w", encoding="utf-8") as f:
        f.write(f"# B2 变体扫描（{datetime.now():%Y-%m-%d %H:%M}）\n\n"
                f"- 切点: {split_date} (IS≤{is_lo})\n"
                f"- 知识库口径: B2=B1之后确定性买点; 倍量柱+暴力K线+反包(灾后重建)+突破压力位\n\n"
                f"| 变体 | 定义 | 段 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | 收益% |\n"
                f"|---|---|---|---:|---:|---:|---:|---:|\n")
        for var in VARIANTS:
            r = res[var]
            for seg_k in ("ALL", "IS", "OOS"):
                d = r.get(seg_k, {})
                flag = " ⚠" if d.get("insufficient") else ""
                f.write(f"| {var} | {V_DESC[var]} | {seg_k} | {d.get('n',0)} | "
                        f"{d.get('win_rate',0):.1f}% | {d.get('pl_ratio',0) or 0:.2f} | "
                        f"{d.get('avg_pnl',0):.3f} | {d.get('sum_pnl_pct',0):.1f}{flag} |\n")
    print(f"报告: {mp_}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="B2 变体扫描")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    run(limit=a.limit, workers=a.workers, tag=a.tag)


if __name__ == "__main__":
    main()
