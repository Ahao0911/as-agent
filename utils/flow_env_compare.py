# -*- coding: utf-8 -*-
"""
F3 · 五战法 × 环境过滤(A/B/C) 胜率三元组扫描（多进程, 全市场）
==============================================================
回答用户核心问题: **加活跃市值过滤后, 每个战法胜率到多少、付出多少交易数**。

环境过滤口径（在冻结内核信号之上做消费端 gate, 不改内核）:
  A = 个股 白线>黄线（现有现状; B1/B2/B3/砖型内核已含 bull, 单针无）
  B = A 且 活跃市值 regime 多头（+4%转多/−2.3%转空, 状态机）
  C = B 且 DIF>0（叠加项目既有 env_macd_filter 层）
另报「单针+白」= 单针补「白线>黄线」(A/B/C 各一)。

防前视: 活跃市值状态 reindex+ffill 到各股票交易日, 取 signal 日 T 及以前最近状态。

Usage:
    python -u utils/flow_env_compare.py [--limit N] [--workers 8] [--tag TAG]
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

from utils.backtest import compute_signals, backtest_stock, make_exit_rules
from utils.backtest_data import DB_PATH, get_benchmark
from utils.indicators import white_line, yellow_line
from utils.resonance import macd_series
from utils.flow_experiments import (universe_from_cache, load_df, load_amv_state,
                                    amv_regime_stats, stats_v1,
                                    _exit_dates_from_hold, IS_RATIO, IS_TAIL_TRIM,
                                    START, OOS_MIN_TRADES)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")
CFG_DESC = {"A": "A 仅个股白线>黄线", "B": "B 白线>黄线 + 活跃市值regime多头",
            "C": "C B + DIF>0(env层)"}
STRATS = ["B1", "B2", "B3", "砖型", "单针", "单针+白"]

_AMV = None
_CONN = None
_RULES = None


def _init(amv):
    global _AMV, _CONN, _RULES
    _AMV = amv
    _CONN = sqlite3.connect(DB_PATH, timeout=60)
    _RULES = make_exit_rules()


def _align_amv(idx):
    """活跃市值 regime 布尔序列对齐到股票交易日（ffill）。"""
    if _AMV is None or len(_AMV) == 0:
        return None
    a = _AMV.reindex(idx.union(_AMV.index)).ffill().reindex(idx)
    return a["regime"].fillna(False).astype(bool)


def _work(code):
    """单只: 计算信号 → A/B/C 三闸 × (原信号 / 单针补白) → 返回交易明细行。"""
    try:
        df = load_df(_CONN, code)
        if df is None or len(df) < 120:
            return []
        sig = compute_signals(df)
        c = df["close"].astype(float)
        bull = (white_line(c) > yellow_line(c))
        dif = macd_series(df)["dif"]
        amv = _align_amv(df.index)

        gates = {"A": None}
        if amv is not None:
            gates["B"] = amv
            gates["C"] = amv & (dif > 0)

        # 单针补白线版信号（消费端覆盖; 其余战法不变）
        sig_nd = dict(sig)
        sig_nd["单针"] = sig["单针"].fillna(False).astype(bool) & bull

        rows = []
        for gname, gate in gates.items():
            for tag, sg in (("raw", sig), ("nd", sig_nd)):
                s = dict(sg)
                if gate is not None:
                    g = gate
                    for k in STRATS[:-1]:
                        if k in s:
                            s[k] = s[k].fillna(False).astype(bool) & g
                try:
                    res = backtest_stock(s, df, rules=_RULES)
                except Exception:
                    continue
                for strat in STRATS[:-1]:
                    for tr in res.get(strat, []):
                        if tag == "nd" and strat != "单针":
                            continue
                        rows.append({
                            "cfg": gname,
                            "strat": "单针+白" if (tag == "nd" and strat == "单针")
                            else strat,
                            "entry": str(pd.Timestamp(tr["日期"]) + pd.Timedelta(days=1))[:10],
                            "exit": None, "pnl": tr["收益"],
                            "hold": int(tr.get("持有", 10))})
        return rows
    except Exception:
        return []


def run(limit=0, workers=8, tag=""):
    os.makedirs(OUT_DIR, exist_ok=True)
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    amv = load_amv_state()

    # 切点（与 flow_experiments 同口径）
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
    buckets = {(g, s): [] for g in ("A", "B", "C") for s in STRATS}
    done = 0
    with mp.Pool(processes=workers, initializer=_init, initargs=(amv,)) as pool:
        for rows in pool.imap_unordered(_work, codes, chunksize=20):
            done += 1
            for r in rows:
                buckets[(r["cfg"], r["strat"])].append(r)
            if done % 300 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(codes)}  已用 {el/60:.1f}m  "
                      f"ETA {el/done*(len(codes)-done)/60:.1f}m", flush=True)
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

    res = {f"{g}|{s}": seg(buckets[(g, s)]) for g in ("A", "B", "C") for s in STRATS}
    payload = {"version": "flow_env5_five", "generated_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "split_date": split_date, "is_lo": is_lo, "start": START,
               "amv_stats": ast, "results": res}
    jp = os.path.join(OUT_DIR, f"flow_env5_five_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    L = ["# 五战法 × 环境过滤(A/B/C) 胜率三元组（flow_env5_five）", "",
         f"- 生成: {payload['generated_at']}  区间 {START}~今  IS/OOS=60:40 "
         f"（切点 {split_date}, IS≤{is_lo}）",
         f"- 股票池: 缓存 bars≥250 全市场",
         f"- 活跃市值 regime: 起点({ast.get('init_date')}) = **{ast.get('init_state')}**"
         f"（1993 起历史推演, 非默认多头）; 多头 {ast.get('bull_share_pct')}% / "
         f"空头 {ast.get('bear_share_pct')}%; 切换 **{ast.get('switches')}** 次",
         "",
         "口径: A=个股白线>黄线; B=A + 活跃市值 regime 多头; C=B + DIF>0。"
         "「单针+白」= 单针补白线>黄线。收益为固定 1% 单笔仓位累加曲线。"
         "交易数<30 的段标 ⚠️。", ""]

    def row(d):
        if d.get("n", 0) == 0:
            return "0 | — | — | — | —"
        ins = " ⚠️" if d.get("insufficient") else ""
        return (f"{d['n']} | {d['win_rate']}% | {d['pl_ratio']} | "
                f"{d['avg_pnl']}% | {d['total_ret_pct']}%{ins}")

    L += ["| 战法 | 组 | 段 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | 收益%(1%仓) |",
          "|---|---|---|---:|---:|---:|---:|---:|"]
    for s in STRATS:
        for g in ("A", "B", "C"):
            for segk in ("ALL", "IS", "OOS"):
                L.append(f"| {s} | {g} | {segk} | {row(res[f'{g}|{s}'][segk])} |")
    mp_ = os.path.join(OUT_DIR, f"flow_env5_five_{tag}.md")
    with open(mp_, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print("\n" + "=" * 100)
    print("五战法 × 环境过滤 胜率三元组（ALL）")
    print("=" * 100)
    print(f"{'战法':<10}{'组':>3}{'交易数':>9}{'胜率':>9}{'盈亏比':>9}{'平均单笔%':>11}{'收益%':>10}")
    for s in STRATS:
        for g in ("A", "B", "C"):
            d = res[f"{g}|{s}"]["ALL"]
            print(f"{s:<10}{g:>3}{d['n']:>9}{d['win_rate']:>8.1f}%"
                  f"{d['pl_ratio']:>9.2f}{d['avg_pnl']:>11.2f}{d['total_ret_pct']:>10.2f}")
        print("-" * 100)
    print(f"报告: {mp_}")
    return payload


def main():
    import argparse
    ap = argparse.ArgumentParser(description="五战法 × 环境过滤扫描")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    ap.add_argument("--tag", default=datetime.now().strftime("%Y-%m-%d"))
    a = ap.parse_args()
    run(limit=a.limit, workers=a.workers, tag=a.tag)


if __name__ == "__main__":
    mp.freeze_support()
    main()
