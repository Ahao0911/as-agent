# -*- coding: utf-8 -*-
"""
战法优化实验 v6（2026-09-12）
==============================
基于回测结论的三项优化，对比验证（不动冻结内核 backtest.py，信号后处理方式）：
  O1: 砍 B3 独立信号        —— B3 双低最差(胜率41.2%/收益-43.5%)，且为错版
                                (用户体系里 B3=主升持有节点，非独立买点)
  O2: 砖型门槛提高          —— 翻红XG(任何绿翻红,21.9万轮垃圾) → 绿翻强红
                                (红砖高度 ≥ 绿柱高度 × CONFIG.BRICK_RATIO，知识库口径)
  O3: B2 量比 1.5 → 2.0     —— 知识库「倍量柱」口径(显著放大)，当前 1.5 偏宽松

对比配置: base / O1 / O1+O2 / O1+O2+O3
输出: 各战法「胜率/交易数/盈亏比/平均单笔/收益」ALL+IS+OOS
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
from utils.flow_env_compare import (load_df, universe_from_cache, load_amv_state,
                                    amv_regime_stats, stats_v1, _exit_dates_from_hold,
                                    IS_RATIO, IS_TAIL_TRIM, START, OOS_MIN_TRADES)
from utils.brick import brick_chart
from utils.indicators import kdj_j, white_line, yellow_line
from utils.strategy_config import CONFIG

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")
STRATS = ["B1", "B2", "B3", "砖型", "单针"]          # O1 后 B3 退出独立信号
OPTS = ["base", "X2", "X4", "X24"]
OPT_DESC = {
    "base": "原信号(对照, 含恢复的B3加仓)",
    "X1": "X1 B1/砖型 加「5日窗≥1项量价共振」硬过滤",
    "X2": "X2 B3 改回调加仓(B2后回调-5~-1%时加, 替代追加速)",
    "X3": "X3 砖型×超跌共振(翻红时近5日曾现短期≤20)",
    "X2": "B3回调加仓(对照)", "X4": "B3中继K线(用户定义)", "X24": "B3中继K线且回调加仓并存",
}

_CONN = None
_RULES = None


def _init():
    global _CONN, _RULES
    _CONN = sqlite3.connect(DB_PATH, timeout=60)
    _RULES = make_exit_rules()


def apply_opts(sig, df, x1=False, x2=False, x3=False, x4=False):
    """信号后处理 v6.2（X 系列实验）。返回新 sig dict。

    X1: B1/砖型 加「最近5交易日出现过≥1项量价共振」硬过滤
        (E3 已证: 命中1项依据时胜率最高, +2.6pp)
        共振池(vol_price 现成): 倍量柱/缩量到地量/反包/放量缩量回调/缩量阴线价升
    X2: B3 改「回调加仓」—— B2 后回调 -5%~-1% 时加仓(低吸不追高)
    X3: 砖型×超跌共振 —— 翻红 且 最近5日曾出现单针超跌(短期线≤20)
    """
    s = dict(sig)
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)
    bull = white_line(c) > yellow_line(c)

    if x1:
        from utils.vol_price import (heavy_volume_bar, shrink_to_floor,
                                     reversal_engulf, volume_shrink_pullback,
                                     shrink_yin_up)
        pool = []
        for fn in (heavy_volume_bar, shrink_to_floor, reversal_engulf,
                   volume_shrink_pullback, shrink_yin_up):
            try:
                pool.append(fn(df).fillna(False).astype(bool))
            except Exception:
                continue
        if pool:
            any_hit = pool[0]
            for pp in pool[1:]:
                any_hit = any_hit | pp
            presence = any_hit.rolling(5, min_periods=1).max().fillna(0).astype(bool)
            s["B1"] = s["B1"].fillna(False).astype(bool) & presence
            s["砖型"] = s["砖型"].fillna(False).astype(bool) & presence

    if x2:
        chg = c.pct_change() * 100
        b2_prev = s["B2"].rolling(10, min_periods=1).max().shift(1)\
            .fillna(False).astype(bool)
        pullback = (chg <= -1) & (chg >= -5)
        s["B3"] = b2_prev & pullback & bull

    if x4:
        # X4: B3 中继K线（用户口述定义 2026-09-12）:
        #   「B3 = B2 后的中继K线(确认延续信号):
        #    出现在B2确认阳线之后; 缩量到半量以内(相比B2那天的量);
        #    小阳线最好, 缩半量的小阴线也可以(分歧转一致);
        #    主力整理蓄力, 不破B2最低点就继续持有」
        low = df["low"].astype(float)
        b2_sig = s["B2"].fillna(False).astype(bool)
        b2_vol = v.where(b2_sig).ffill(limit=5)      # 最近一次 B2 的量（5日内有效）
        b2_low = low.where(b2_sig).ffill(limit=5)    # 最近一次 B2 的最低点
        after_b2 = b2_sig.shift(1).fillna(False).astype(bool)\
            .rolling(3, min_periods=1).max().astype(bool)   # B2 之后 3 日内
        shrink = v <= b2_vol * 0.5                       # 缩量至 B2 量的半量以内
        small = ((c - o).abs() / c.shift(1).replace(0, np.nan)) <= 0.02   # 小K线(实体≤2%)
        not_break = c >= b2_low                          # 不破 B2 最低点
        s["B3"] = after_b2 & shrink & small & not_break

    if x3:
        try:
            from utils.needle20 import needle20_lines
            lines = needle20_lines(df)
            oversold_5d = (lines["短期"] <= 20).rolling(5, min_periods=1)\
                .max().fillna(0).astype(bool)
            s["砖型"] = s["砖型"].fillna(False).astype(bool) & oversold_5d
        except Exception:
            pass
    return s




def _work(code):
    """单只: base + 三档优化 × 五战法 → 交易明细行。"""
    try:
        df = load_df(_CONN, code)
        if df is None or len(df) < 120:
            return []
        sig0 = compute_signals(df)
        rows = []
        for opt in OPTS:
            if opt == "base":
                s = dict(sig0)
            else:
                x1 = "X1" in opt
                x2 = "X2" in opt
                x3 = "X3" in opt
                x4 = "X4" in opt
                s = apply_opts(sig0, df, x1=x1, x2=x2, x3=x3, x4=x4)
            try:
                res = backtest_stock(s, df, rules=_RULES, entry_mode="same_close")
            except Exception:
                continue
            for strat in STRATS:
                for tr in res.get(strat, []):
                    rows.append({"opt": opt, "strat": strat,
                                 "entry": str(pd.Timestamp(tr["日期"]) + pd.Timedelta(days=1))[:10],
                                 "pnl": tr["收益"],
                                 "hold": int(tr.get("持有", 10))})
        return rows
    except Exception:
        return []


def run(limit=0, workers=8, tag=""):
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
    buckets = {(o, s): [] for o in OPTS for s in STRATS}
    done = 0
    with mp.Pool(processes=workers, initializer=_init) as pool:
        for rows in pool.imap_unordered(_work, codes, chunksize=20):
            done += 1
            for r in rows:
                buckets[(r["opt"], r["strat"])].append(r)
            if done % 500 == 0:
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

    res = {f"{o}|{s}": seg(buckets[(o, s)]) for o in OPTS for s in STRATS}
    # 逐笔全量落盘（牛熊分段分析需要）
    all_trades = [{"opt": o, "strat": s, **r}
                  for (o, s), lst in buckets.items() for r in lst]
    payload = {"version": "flow_optimize_v6", "generated_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "split_date": split_date, "is_lo": is_lo, "start": START,
               "opts": OPT_DESC, "results": res,
               "trades": all_trades}
    jp = os.path.join(OUT_DIR, f"flow_optimize_v6_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # 控制台报告
    print("\n" + "=" * 92)
    print("战法优化对比 v6（三项优化: O1砍B3 / O2砖型强红≥2/3 / O3 B2倍量柱）")
    print("=" * 92)
    hdr = f"{'战法':<6}{'配置':<10}{'段':<5}{'交易数':>8}{'胜率':>8}{'盈亏比':>8}{'平均单笔%':>10}{'收益%':>10}"
    for strat in STRATS:
        print("-" * 92)
        for opt in OPTS:
            r = res[f"{opt}|{strat}"]
            for seg_k in ("ALL", "IS", "OOS"):
                d = r.get(seg_k, {})
                insuf = d.get("insufficient")
                flag = " ⚠样本不足" if insuf else ""
                print(f"{strat:<6}{opt:<10}{seg_k:<5}{d.get('n', 0):>8}"
                      f"{d.get('win_rate', 0):>7.1f}%{d.get('pl_ratio', 0) or 0:>8.2f}"
                      f"{d.get('avg_pnl', 0):>10.3f}{d.get('sum_pnl_pct', 0):>10.1f}{flag}")
    print("=" * 92)
    mp_ = os.path.join(OUT_DIR, f"flow_optimize_v6_{tag}.md")
    with open(mp_, "w", encoding="utf-8") as f:
        f.write(f"# 战法优化对比 v6（{datetime.now():%Y-%m-%d %H:%M}）\n\n")
        f.write(f"- 切点: {split_date} (IS≤{is_lo})\n")
        f.write(f"- 优化项: O1 砍B3独立信号 / O2 砖型绿翻强红(≥2/3) / O3 B2量比2.0(倍量柱)\n\n")
        for strat in STRATS:
            f.write(f"## {strat}\n\n| 配置 | 段 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | 收益% |\n"
                    f"|---|---|---:|---:|---:|---:|---:|\n")
            for opt in OPTS:
                r = res[f"{opt}|{strat}"]
                for seg_k in ("ALL", "IS", "OOS"):
                    d = r.get(seg_k, {})
                    insuf = " ⚠" if d.get("insufficient") else ""
                    f.write(f"| {OPT_DESC[opt]} | {seg_k} | {d.get('n',0)} | "
                            f"{d.get('win_rate_pct',0):.1f}% | {d.get('profit_factor',0) or 0:.2f} | "
                            f"{d.get('avg_pnl_pct',0):.3f} | {d.get('sum_pnl_pct',0):.1f}{insuf} |\n")
            f.write("\n")
    print(f"报告: {mp_}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="战法优化对比 v6")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    run(limit=a.limit, workers=a.workers, tag=a.tag)


if __name__ == "__main__":
    main()
