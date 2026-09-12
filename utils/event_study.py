# -*- coding: utf-8 -*-
"""
事件研究法：战法信号质量科学评估（2026-09-12）
================================================
学术标准方法：不模拟资金（无"钱不够"问题），纯统计每个信号后
固定持有 N 日的前向收益分布（forward return）。

  买入基准: 信号日 T 收盘价（≈ 14:55 尾盘价，用户指定口径）
  前向收益: fwd(N) = close[T+N] / close[T] - 1   （持有 N 个交易日）
  输出: 每战法 × 每持有期(1/3/5/10/20日) 的 交易数/胜率/平均/中位/标准差
        按 IS(2010-2024)/OOS(2024-今) 双段报告

这是回答「B2 胜率到底应该是多少」的科学口径。
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

from utils.backtest import compute_signals
from utils.backtest_data import DB_PATH
from utils.flow_env_compare import load_df, universe_from_cache, load_amv_state
from utils.strategy_config import CONFIG

OUT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/data/backtest"
HOLD_DAYS = [1, 3, 5, 10, 20]
STRATS = ["B1", "B2", "B3", "砖型", "单针"]
IS_CUT = "2024-01-01"   # 2024前后双段（用户关注点）+ 60:40 线性切分
_AMV = None   # 活跃市值 regime 序列（worker 全局）


def _init(amv):
    global _AMV
    _AMV = amv


def _work(code):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=60)
        df = load_df(conn, code)
        conn.close()
        if df is None or len(df) < 250:
            return []
        sig = compute_signals(df)
        c = df["close"].astype(float)
        # 活跃市值多头过滤（2026-09-12 用户要求）：只在 regime=多头 时允许买入，
        # 用 T-1 状态（前一日活跃市值已知，防前视）
        if _AMV is not None and len(_AMV) > 0:
            st = _AMV.reindex(df.index.union(_AMV.index)).ffill().reindex(df.index)
            amv_bull = st["regime"].shift(1).fillna(False).astype(bool)
            amv_bull.index = df.index
        else:
            amv_bull = pd.Series(True, index=df.index)
        rows = []
        for strat in STRATS:
            ok = sig[strat].fillna(False).astype(bool) & amv_bull
            hits = df.index[ok]
            if len(hits) == 0:
                continue
            pos = df.index.get_indexer(hits)
            nbars = len(df)
            for N in HOLD_DAYS:
                fwd_all = (c.shift(-N) / c - 1).values * 100
                for p in pos:
                    f = fwd_all[p]
                    if np.isnan(f):
                        continue          # ❗修复: 先取值再判空, 避免 pos/fwd 错配
                    # 记录【真实交易日退出日】：非重叠判定必须用它，不能用 entry+日历天
                    ex = str(df.index[p + N])[:10] if p + N < nbars else ""
                    rows.append({"strat": strat, "N": N,
                                 "entry": str(df.index[p])[:10], "exit": ex,
                                 "fwd": round(f, 3)})
        return rows
    except Exception:
        return []


def stats(arr):
    if len(arr) == 0:
        return {"n": 0}
    a = np.array(arr)
    return {"n": len(a),
            "win": round((a > 0).mean() * 100, 1),
            "mean": round(a.mean(), 3),
            "median": round(float(np.median(a)), 3),
            "std": round(a.std(), 2)}


def run(workers=8, tag=""):
    codes = universe_from_cache()
    t0 = time.time()
    amv = load_amv_state()
    all_rows = []
    with mp.Pool(processes=workers, initializer=_init, initargs=(amv,)) as pool:
        for rows in pool.imap_unordered(_work, codes, chunksize=50):
            all_rows.extend(rows)
            if len(all_rows) % 200000 < 50:
                pass
    print(f"[run] 完成 {len(codes)} 只, 耗时 {(time.time()-t0)/60:.1f} 分钟, "
          f"信号样本 {len(all_rows):,} 条")

    df = pd.DataFrame(all_rows)
    df["seg"] = np.where(df["entry"] < IS_CUT, "IS(2010-23)", "OOS(2024-今)")

    print("\n" + "=" * 100)
    print("事件研究法：信号后固定持有 N 日的前向收益（买入基准=信号日收盘≈14:55尾盘）")
    print("  IS=2010~2023 ｜ OOS=2024~今 —— 两段对比即可回答「B2/各战法胜率应该是多少」")
    print("=" * 100)
    res = {}
    for strat in STRATS:
        print(f"----- {strat} -----")
        for N in HOLD_DAYS:
            sub = df[(df["strat"] == strat) & (df["N"] == N)]
            for seg in ["IS(2010-23)", "OOS(2024-今)"]:
                a = sub[sub["seg"] == seg]["fwd"].values
                st = stats(a)
                key = f"{strat}|N{N}|{seg}"
                res[key] = st
                print(f"  持有{N:>2}日 {seg:<12} n={st['n']:>7,}  "
                      f"胜率={st.get('win',0):>5.1f}%  均值={st.get('mean',0):>6.3f}%  "
                      f"中位={st.get('median',0):>6.3f}%  σ={st.get('std',0):>5.2f}")
        print()
    print("=" * 100)

    # ---- 复利计算: 固定持有 N 日·无重叠轮动（科学口径的"总体收益率"）----
    print("\n" + "=" * 100)
    print("总体收益率（复利）：战法内定持有期 · 无重叠轮动（一个账户、资金串行、退出日=真实交易日）")
    print("  ⚠ 持有期由 CONFIG.HOLD_DAYS_BY_STRAT 决定：None=规则驱动（不设固定日数，见规则驱动回测）")
    print("=" * 100)
    comp_res = {}
    SEGS = [("17年(2010-2026)", None, None),
            ("2024-2026两年", "2024-01-01", None)]
    for seg_name, s_lo, s_hi in SEGS:
        print(f"\n【{seg_name}】")
        for strat in STRATS:
            hold = CONFIG.hold_days_for(strat)
            if hold is None:
                print(f"  {strat:<4} 持有期 = 规则驱动（由战法自身离场规则决定，不人为规定日数）"
                      f" → 以规则驱动回测为准")
                continue
            for N in [hold]:
                # 同日多信号 → 等权组合（当日所有信号平均收益），保证确定性 + 符合"资金等分"逻辑
                sub = df[(df["strat"] == strat) & (df["N"] == N)]
                if s_lo:
                    sub = sub[sub["entry"] >= s_lo]
                tr = sub.groupby("entry", as_index=False).agg(
                    fwd=("fwd", "mean"), exit=("exit", "max"))\
                    .sort_values("entry", kind="mergesort")
                capital, bars, wins = 1.0, 0, 0
                last_exit = ""
                for _, t in tr.iterrows():
                    # ❗非重叠判定用【真实交易日退出日】。若用 entry+N 日历天，N=5 日历日≈3.6 交易日，
                    #   会把"上一笔还没走"的日子误判为空仓 → 交易重叠 → 笔数虚高、收益虚高。
                    if t["entry"] <= last_exit:
                        continue
                    capital *= (1 + t["fwd"] / 100)
                    bars += 1
                    wins += 1 if t["fwd"] > 0 else 0
                    last_exit = t["exit"]
                span = (pd.Timestamp(tr["entry"].max())
                        - pd.Timestamp(tr["entry"].min())).days / 365.25
                years = max(span, 0.5)
                annual = (capital ** (1 / years) - 1) * 100 if bars > 0 else 0
                wr = wins / bars * 100 if bars else 0
                comp_res[f"{seg_name}|{strat}|N{N}"] = {"bars": bars, "wr": round(wr, 1),
                              "total": round((capital - 1) * 100, 1),
                              "annual": round(annual, 1), "years": round(span, 2)}
                print(f"  {strat:<4} 持有{N:>2}日  无重叠 {bars:>5} 笔  胜率 {wr:>5.1f}%  "
                      f"总收益 {(capital-1)*100:>10.1f}%  年化 {annual:>7.1f}%")
    print("=" * 100)

    payload = {"version": "event_study", "generated_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "buy_ref": "signal-day close (≈14:55)", "hold_days": HOLD_DAYS,
               "results": res, "trades": all_rows, "compounding": comp_res}
    jp = os.path.join(OUT_DIR, f"event_study_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    mp_ = os.path.join(OUT_DIR, f"event_study_{tag}.md")
    with open(mp_, "w", encoding="utf-8") as f:
        f.write(f"# 事件研究法：信号质量科学评估（{datetime.now():%Y-%m-%d %H:%M}）\n\n"
                f"买入基准=信号日收盘(≈14:55尾盘)；前向收益=close[T+N]/close[T]-1\n\n")
        for strat in STRATS:
            f.write(f"## {strat}\n\n| 持有 | 段 | 信号数 | 胜率 | 平均% | 中位% | σ |\n"
                    f"|---|---|---:|---:|---:|---:|---:|\n")
            for N in HOLD_DAYS:
                for seg in ["IS(2010-23)", "OOS(2024-今)"]:
                    st = res.get(f"{strat}|N{N}|{seg}", {})
                    f.write(f"| {N}日 | {seg} | {st.get('n',0):,} | "
                            f"{st.get('win',0):.1f}% | {st.get('mean',0):.3f} | "
                            f"{st.get('median',0):.3f} | {st.get('std',0):.2f} |\n")
            f.write("\n")
    print(f"报告: {mp_}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="事件研究法信号质量评估")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    run(workers=a.workers, tag=a.tag)


if __name__ == "__main__":
    main()
