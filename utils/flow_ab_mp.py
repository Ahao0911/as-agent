# -*- coding: utf-8 -*-
"""
F3 · 流程版 A/B/C/D 环境过滤对比（多进程, 全市场）
==================================================
在 v4 流程（3 条离场规则 + 收盘确认 + 连续4红砖）基础上, 对比环境过滤:
  A = 仅个股 白线>黄线（amv off, env off）
  B = A + 活跃市值 regime 多头                              ⭐正式口径
  C = 白线>黄线 + DIF>0 + 活跃市值 regime（env on）
  D = E4 全流程（补票+增强离场）+ 活跃市值 regime（env off）

Usage:  python -u utils/flow_ab_mp.py [--limit N] [--workers 6] [--tag TAG]
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

from utils.backtest import make_exit_rules
from utils.backtest_data import DB_PATH, get_benchmark
from utils.flow_backtest import FlowConfig, _prepare_block, run_flow_backtest_ledger
from utils.flow_experiments import (universe_from_cache, load_df, load_amv_state,
                                    amv_regime_stats, stats_flow, IS_RATIO,
                                    IS_TAIL_TRIM, START, OOS_MIN_TRADES)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")

_t = lambda b1=0.6, b2=0.4, nd=0.0: {"B1": b1, "B2": b2, "needle": nd}
CFGS = {
    "A": FlowConfig(env_macd_filter=False, resonance_on=False, needle_patch=False),
    "B": FlowConfig(env_macd_filter=False, resonance_on=False, needle_patch=False,
                    amv_filter=True, amv_mode="regime"),
    "C": FlowConfig(env_macd_filter=True, resonance_on=False, needle_patch=False,
                    amv_filter=True, amv_mode="regime"),
    "D": FlowConfig(env_macd_filter=False, resonance_on=True, needle_patch=True,
                    needle_stop_pct=0.05, exit_enhanced=True, resonance_window=5,
                    tranche=_t(0.5, 0.3, 0.2), amv_filter=True, amv_mode="regime"),
}
DESC = {"A": "A 仅个股白线>黄线", "B": "B +活跃市值regime ⭐正式口径",
        "C": "C +DIF>0 +活跃市值", "D": "D E4全流程+活跃市值"}
_PREP = FlowConfig(env_macd_filter=True, resonance_on=True, resonance_window=5)

_AMV = None
_CONN = None
_RULES = None


def _init(amv):
    global _AMV, _CONN, _RULES
    _AMV = amv
    _CONN = sqlite3.connect(DB_PATH, timeout=60)
    _RULES = make_exit_rules()


def _work(code):
    try:
        df = load_df(_CONN, code)
        if df is None or len(df) < 120:
            return []
        blk = _prepare_block(df, _PREP, want_resonance=True, amv_state=_AMV)
        rows = []
        for name, cfg in CFGS.items():
            blk_k = dict(blk)
            if not cfg.env_macd_filter:
                blk_k["env_ok"] = blk["wx"] > blk["yx"]
            if blk.get("res_mats"):
                wh = int(getattr(cfg, "resonance_window", 5))
                blk_k["res_matrix"] = blk["res_mats"].get(wh, blk["res_mats"].get(5))
            trades, _ = run_flow_backtest_ledger(None, code, code, cfg, _RULES,
                                                 block=blk_k)
            for t in trades:
                hold = max((pd.Timestamp(t.exit_date) -
                            pd.Timestamp(t.b1_entry_date)).days, 1)
                rows.append({"cfg": name, "entry": t.b1_entry_date,
                             "exit": t.exit_date, "pnl": t.pnl_pct,
                             "bucket": t.resonance_bucket, "hold": hold})
        return rows
    except Exception:
        return []


def run(limit=0, workers=6, tag=""):
    os.makedirs(OUT_DIR, exist_ok=True)
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    amv = load_amv_state()
    bench = get_benchmark(start=START)
    if not len(bench):
        raise SystemExit("基准获取失败")
    cal = [str(d)[:10] for d in bench["date"]]
    sp_idx = int(len(cal) * IS_RATIO)
    split_date, is_lo = cal[sp_idx], cal[max(sp_idx - IS_TAIL_TRIM, 0)]
    ast = amv_regime_stats(amv, start=START)
    print(f"[split] {split_date} (IS≤{is_lo}) | amv 起点={ast.get('init_state')} "
          f"多头{ast.get('bull_share_pct')}% 切换{ast.get('switches')}次")

    t0 = time.time()
    buckets = {k: [] for k in CFGS}
    done = 0
    with mp.Pool(processes=workers, initializer=_init, initargs=(amv,)) as pool:
        for rows in pool.imap_unordered(_work, codes, chunksize=10):
            done += 1
            for r in rows:
                buckets[r["cfg"]].append(r)
            if done % 250 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(codes)}  已用 {el/60:.1f}m  "
                      f"ETA {el/done*(len(codes)-done)/60:.1f}m", flush=True)
    print(f"[run] 完成 {done} 只, 耗时 {(time.time()-t0)/60:.1f} 分钟")

    def seg(trs):
        is_t = [t for t in trs if t["entry"] < split_date and t["entry"] < is_lo]
        oos_t = [t for t in trs if t["entry"] >= split_date]
        o = {"ALL": stats_flow(trs), "IS": stats_flow(is_t), "OOS": stats_flow(oos_t)}
        for k in ("IS", "OOS"):
            if 0 < o[k]["n"] < OOS_MIN_TRADES:
                o[k]["insufficient"] = True
        return o

    res = {k: seg(buckets[k]) for k in CFGS}
    payload = {"version": "flow_ab5", "generated_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "split_date": split_date, "is_lo": is_lo, "amv_stats": ast,
               "results": res}
    jp = os.path.join(OUT_DIR, f"flow_ab5_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    def row(d):
        if d.get("n", 0) == 0:
            return "0 | — | — | — | —"
        ins = " ⚠️" if d.get("insufficient") else ""
        return (f"{d['n']} | {d['win_rate']}% | {d['pl_ratio']} | "
                f"{d['avg_pnl']}% | {d['total_ret_pct']}%{ins}")

    L = ["# 流程版 A/B/C/D 环境过滤对比（flow_ab5 · v4离场规则）", "",
         f"- 生成 {payload['generated_at']}  区间 {START}~今  IS/OOS=60:40 "
         f"（切点 {split_date}, IS≤{is_lo}）",
         f"- 活跃市值 regime: 起点({ast.get('init_date')})={ast.get('init_state')}; "
         f"多头 {ast.get('bull_share_pct')}%/空头 {ast.get('bear_share_pct')}%; "
         f"切换 {ast.get('switches')} 次",
         "", "| 组 | 段 | 轮数 | 胜率 | 盈亏比 | 平均单笔% | 收益%(1%仓) |",
         "|---|---|---:|---:|---:|---:|---:|"]
    for k in CFGS:
        for segk in ("ALL", "IS", "OOS"):
            L.append(f"| {DESC[k]} | {segk} | {row(res[k][segk])} |")
    mp_ = os.path.join(OUT_DIR, f"flow_ab5_{tag}.md")
    with open(mp_, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print("\n" + "=" * 92)
    print("流程版 A/B/C/D（v4 离场规则）胜率三元组")
    print("=" * 92)
    print(f"{'组':<26}{'段':>5}{'轮数':>9}{'胜率':>9}{'盈亏比':>9}{'平均单笔%':>11}{'收益%':>10}")
    for k in CFGS:
        for segk in ("ALL", "IS", "OOS"):
            d = res[k][segk]
            print(f"{DESC[k]:<26}{segk:>5}{d['n']:>9}{d['win_rate']:>8.1f}%"
                  f"{d['pl_ratio']:>9.2f}{d['avg_pnl']:>11.2f}{d['total_ret_pct']:>10.2f}")
        print("-" * 92)
    print(f"报告: {mp_}")
    return payload


def main():
    import argparse
    ap = argparse.ArgumentParser(description="流程版 A/B/C/D 环境过滤")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    ap.add_argument("--tag", default=datetime.now().strftime("%Y-%m-%d"))
    a = ap.parse_args()
    run(limit=a.limit, workers=a.workers, tag=a.tag)


if __name__ == "__main__":
    mp.freeze_support()
    main()
