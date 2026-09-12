# -*- coding: utf-8 -*-
"""
F3 · 入场门槛扫描（B1 / 单针）—— 胜率冲刺
==========================================
大盘过滤(活跃市值)被证明 IS 有效/OOS 失效(过拟合)。真正的杠杆是**入场质量**。
本脚本对 B1/单针 的入场条件逐项收紧, 给每个门槛组合的「交易数/胜率/盈亏比」(IS+OOS)。

口径: 在冻结内核信号之上做**消费端门槛收紧**(不改内核), 离场规则用 make_exit_rules 原规则,
     非重叠推进(i = exit_idx+1)与 backtest_stock 一致(直接复用 _find_exit)。
      指标(白黄线/KDJ/振幅/量比/单针四线/砖型)每只只算一次, 各门槛组合复用。

B1 收紧维度: J ≤ {13/10/7/5} × 振幅 < {7/5/3.5} × 量比 v/v20 < {1.0/0.85/0.7}
单针收紧维度: 短期线 ≤ {20/15/10} × 长期线 ≥ {60/70/80}

Usage:  python -u utils/flow_entry_scan.py [--limit N] [--workers 6] [--tag TAG]
"""
import os
import sys
import json
import time
import socket
import sqlite3
import multiprocessing as mp
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
socket.setdefaulttimeout(20)

import numpy as np
import pandas as pd

from utils.backtest import compute_signals, make_exit_rules, _find_exit, TRADE_COST
from utils.backtest_data import DB_PATH, get_benchmark
from utils.indicators import white_line, yellow_line, kdj_j
from utils.needle20 import needle20_lines
from utils.brick import brick_chart
from utils.flow_experiments import (universe_from_cache, load_df, IS_RATIO,
                                    IS_TAIL_TRIM, START, OOS_MIN_TRADES)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")

# (label, j_thr, amp_thr, vr_thr)  放大为百分数一致比较
B1_COMBOS = [
    ("B1 base(J≤13,振幅<7,量比<1.0)", 13, 7.0, 1.00),
    ("B1 J≤10", 10, 7.0, 1.00),
    ("B1 J≤7", 7, 7.0, 1.00),
    ("B1 J≤5", 5, 7.0, 1.00),
    ("B1 振幅<5", 13, 5.0, 1.00),
    ("B1 振幅<3.5", 13, 3.5, 1.00),
    ("B1 量比<0.85", 13, 7.0, 0.85),
    ("B1 量比<0.70", 13, 7.0, 0.70),
    ("B1 J≤10&振幅<5", 10, 5.0, 1.00),
    ("B1 J≤7&振幅<5", 7, 5.0, 1.00),
    ("B1 J≤7&量比<0.70", 7, 7.0, 0.70),
    ("B1 J≤10&振幅<5&量比<0.85", 10, 5.0, 0.85),
    ("B1 J≤7&振幅<5&量比<0.70", 7, 5.0, 0.70),
]
ND_COMBOS = [
    ("单针 base(短期≤20,长期≥60)", 20, 60),
    ("单针 短期≤15", 15, 60),
    ("单针 短期≤10", 10, 60),
    ("单针 长期≥70", 20, 70),
    ("单针 长期≥80", 20, 80),
    ("单针 长期≥85", 20, 85),
    ("单针 长期≥90", 20, 90),
    ("单针 短期≤15&长期≥70", 15, 70),
    ("单针 短期≤15&长期≥80", 15, 80),
    ("单针 短期≤10&长期≥80", 10, 80),
]

_SPLIT = None
_IS_LO = None
_CONN = None
_RULES = None


def _init(split_date, is_lo):
    global _SPLIT, _IS_LO, _CONN, _RULES
    _SPLIT, _IS_LO = split_date, is_lo
    _CONN = sqlite3.connect(DB_PATH, timeout=60)
    _RULES = make_exit_rules()


def _run_mask(mask, o, c, n, df, rule, extra):
    """按布尔 mask 入场, 非重叠推进, 复用冻结内核 _find_exit。返回 pnl 列表(%)."""
    out = []
    i = 0
    while i < n - 1:
        if mask[i]:
            buy_idx = i + 1
            buy_price = float(o.iloc[buy_idx])
            if buy_price <= 0:
                i += 1
                continue
            try:
                eidx, eprice, _ = _find_exit(buy_idx, df, rule, extra)
            except Exception:
                i += 1
                continue
            pnl = (eprice - buy_price) / buy_price - TRADE_COST
            out.append((str(df.index[i])[:10], pnl * 100))
            i = eidx + 1
        else:
            i += 1
    return out


def _accum(trades):
    """trades: [(entry_str, pnl%)] → 分段聚合 (n, n_win, sum_win, sum_loss, sum_pnl)。"""
    def _acc(ts):
        n = len(ts)
        if n == 0:
            return [0, 0, 0.0, 0.0, 0.0]
        wins = [p for _, p in ts if p > 0]
        losses = [p for _, p in ts if p <= 0]
        return [n, len(wins), float(np.sum(wins)), float(np.sum(losses)),
                float(np.sum([p for _, p in ts]))]
    is_t = [t for t in trades if t[0] < _SPLIT and t[0] < _IS_LO]
    oos_t = [t for t in trades if t[0] >= _SPLIT]
    return {"ALL": _acc(trades), "IS": _acc(is_t), "OOS": _acc(oos_t)}


def _work(code):
    try:
        df = load_df(_CONN, code)
        if df is None or len(df) < 120:
            return {}
        o = df["open"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        v = df["volume"].astype(float)
        n = len(df)
        sig = compute_signals(df)
        b1 = sig["B1"].fillna(False).astype(bool).values
        _, _, jv = kdj_j(h, l, c)
        j = jv.values
        amp = ((h - l) / c.shift(1) * 100).values
        v20 = v.rolling(20, min_periods=20).mean().values
        vr = np.divide(v.values, v20, out=np.full(n, np.nan), where=v20 > 0)
        try:
            ndf = needle20_lines(df)
            short = ndf["短期"].values
            long = ndf["长期"].values
            nd_cols = {col: ndf[col].values for col in ndf.columns}
        except Exception:
            short = long = np.full(n, np.nan)
            nd_cols = None
        wl = white_line(c).values
        yl = yellow_line(c).values
        try:
            bkf = brick_chart(df)
            bk = {col: bkf[col].values for col in bkf.columns}
        except Exception:
            bk = None
        extra = {"open": o.values, "high": h.values, "low": l.values,
                 "close": c.values, "vol": v.values, "vol20": v20,
                 "white": wl, "yellow": yl, "needle": nd_cols, "brick": bk}

        rule_b1 = _RULES["B1"]
        rule_nd = _RULES["单针"]
        res = {}
        for label, jt, at, vt in B1_COMBOS:
            mask = (b1 & (j <= jt) & (amp < at) & (vr < vt)).astype(bool)
            res[("B1", label)] = _accum(_run_mask(mask, o, c, n, df, rule_b1, extra))
        for label, st, lt in ND_COMBOS:
            mask = ((short <= st) & (long >= lt)).astype(bool)
            res[("单针", label)] = _accum(_run_mask(mask, o, c, n, df, rule_nd, extra))
        return res
    except Exception:
        return {}


def _merge(dst, src):
    for k, segs in src.items():
        if k not in dst:
            dst[k] = {s: [0, 0, 0.0, 0.0, 0.0] for s in ("ALL", "IS", "OOS")}
        for s in ("ALL", "IS", "OOS"):
            a = dst[k][s]
            b = segs[s]
            a[0] += b[0]; a[1] += b[1]; a[2] += b[2]; a[3] += b[3]; a[4] += b[4]


def _summary(acc):
    n, w, sw, sl, sp = acc
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "pl_ratio": 0.0, "avg_pnl": 0.0,
                "total_ret_pct": 0.0}
    wr = w / n * 100
    aw = sw / w if w else 0.0
    nl = n - w
    al = sl / nl if nl else 0.0
    plr = round(aw / abs(al), 2) if al else 0.0
    return {"n": n, "win_rate": round(wr, 1), "pl_ratio": plr,
            "avg_pnl": round(sp / n, 3), "total_ret_pct": round(sp * 0.01, 2)}


def run(limit=0, workers=6, tag=""):
    os.makedirs(OUT_DIR, exist_ok=True)
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    bench = get_benchmark(start=START)
    if not len(bench):
        raise SystemExit("基准获取失败")
    cal = [str(d)[:10] for d in bench["date"]]
    sp_idx = int(len(cal) * IS_RATIO)
    split_date, is_lo = cal[sp_idx], cal[max(sp_idx - IS_TAIL_TRIM, 0)]
    print(f"[split] {split_date} (IS≤{is_lo})")

    t0 = time.time()
    agg = {}
    done = 0
    with mp.Pool(processes=workers, initializer=_init,
                 initargs=(split_date, is_lo)) as pool:
        for res in pool.imap_unordered(_work, codes, chunksize=10):
            done += 1
            _merge(agg, res)
            if done % 500 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(codes)}  已用 {el/60:.1f}m", flush=True)
    print(f"[run] 完成 {done} 只, 耗时 {(time.time()-t0)/60:.1f} 分钟")

    out = {}
    for (strat, label), segs in agg.items():
        out[f"{strat}|{label}"] = {s: _summary(segs[s]) for s in
                                   ("ALL", "IS", "OOS")}
        for s in ("IS", "OOS"):
            d = out[f"{strat}|{label}"][s]
            if 0 < d["n"] < OOS_MIN_TRADES:
                d["insufficient"] = True

    payload = {"version": "flow_entry_scan", "generated_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "split_date": split_date, "is_lo": is_lo, "results": out}
    jp = os.path.join(OUT_DIR, f"flow_entry_scan_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    L = ["# 入场门槛扫描（B1 / 单针）胜率三元组（flow_entry_scan）", "",
         f"- 生成 {payload['generated_at']}  切点 {split_date} (IS≤{is_lo})", "",
         "| 门槛 | 段 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | 收益%(1%仓) |",
         "|---|---|---:|---:|---:|---:|---:|"]
    for key in out:
        strat, label = key.split("|", 1)
        for s in ("ALL", "IS", "OOS"):
            d = out[key][s]
            ins = " ⚠️" if d.get("insufficient") else ""
            if d["n"] == 0:
                L.append(f"| {strat} {label} | {s} | 0 | — | — | — | — |")
            else:
                L.append(f"| {strat} {label} | {s} | {d['n']} | {d['win_rate']}% | "
                         f"{d['pl_ratio']} | {d['avg_pnl']}% | {d['total_ret_pct']}%{ins} |")
    mp_ = os.path.join(OUT_DIR, f"flow_entry_scan_{tag}.md")
    with open(mp_, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print("\n" + "=" * 96)
    print("入场门槛扫描（胜率冲刺）")
    print("=" * 96)
    print(f"{'门槛':<32}{'段':>5}{'交易数':>9}{'胜率':>9}{'盈亏比':>9}{'收益%':>10}")
    for key in out:
        strat, label = key.split("|", 1)
        for s in ("ALL", "IS", "OOS"):
            d = out[key][s]
            print(f"{strat+' '+label:<32}{s:>5}{d['n']:>9}{d['win_rate']:>8.1f}%"
                  f"{d['pl_ratio']:>9.2f}{d['total_ret_pct']:>10.2f}")
        print("-" * 96)
    print(f"报告: {mp_}")
    return payload


def main():
    import argparse
    ap = argparse.ArgumentParser(description="入场门槛扫描")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    ap.add_argument("--tag", default=datetime.now().strftime("%Y-%m-%d"))
    a = ap.parse_args()
    run(limit=a.limit, workers=a.workers, tag=a.tag)


if __name__ == "__main__":
    mp.freeze_support()
    main()
