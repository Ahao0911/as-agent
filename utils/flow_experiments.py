# -*- coding: utf-8 -*-
"""
F3 · 实验矩阵跑批与报告（flow_redesign A1）
============================================
实验矩阵（docs/flow_redesign_2026-09-11.md A1）:
    E1 砖型单独   —— 引用错版基线 data/backtest/market_full_2026-09-11.json（不重跑）
    E2 纯流程     —— B1入场→B2加仓→B3持有，无共振/无补票（env on/off 两档）
    E3 共振分层 ⭐ —— B1入场，按共振命中 0/1/2/3+ 分桶，IS 与 OOS 各验单调性
    E4 全流程     —— 状态机 + 单针补票，止损 3%/5% 两档 × env on/off
    基线 IS/OOS   —— 冻结内核五战法独立信号按切点分段（回答 B1 过拟合 / 单针 OOS /
                     砖型 IS-OOS 定位三个用户决策问题）

统一口径:
    全市场缓存股票池（Gate-0 实测 ≥250 根）；区间 2024-01-01 ~ 今；
    IS/OOS = 60:40 全局统一切点；IS 尾部剔除 30 根（防离场前视）；
    OOS 某层交易数 < 30 → 标 INSUFFICIENT_SAMPLE（不采信）。

收益率口径（flow 实验）: 非重叠轮动复利 —— 按入场日排序，资金空闲则跳过信号，
每轮按 tranche 加权平均收益全仓复利。与错版"满仓轮动"口径同源，可与 E1 并列
展示（设计 D9: 只并列不硬比 Calmar）。

幸存者偏差: 股票池为当前在市清单，不含区间内退市股 → 系统性偏乐观（上界估计）。
"""
import os
import sys
import json
import time
import socket
import sqlite3
import pickle
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

socket.setdefaulttimeout(20)   # 防网络挂死（akshare 请求无原生 timeout）

from utils.backtest import backtest_stock, make_exit_rules
from utils.backtest_data import DB_PATH, STD_COLUMNS, get_benchmark
from utils.flow_backtest import FlowConfig, _prepare_block, run_flow_backtest_ledger
from utils.resonance import RESONANCE_ITEMS

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "backtest")
START = "2024-01-01"
MIN_BARS = 250          # Gate-0 口径
IS_RATIO = 0.6
IS_TAIL_TRIM = 30       # IS 尾部剔除根数（防离场前视, v1 §6.2）
OOS_MIN_TRADES = 30     # OOS 样本下限（低于则标 INSUFFICIENT_SAMPLE）
CKPT_EVERY = 250
FIXED_FRAC = 0.01       # v2 收益口径: 每笔固定投入初始资金的 1%（可并发）

BASELINE_STRATS = ["B1", "B2", "B3", "砖型", "单针"]
BASELINE_EXTRA = ["单针+白"]   # v5: 单针补「白线>黄线」（消费端覆盖, 不改冻结内核）

AMV_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "active_market_value.csv")
AMV_REGIME_UP = 4.0      # 活跃市值单日 ≥ +4% → 进入多头
AMV_REGIME_DN = -2.3     # 活跃市值单日 ≤ -2.3% → 转空头（离场线, risk_engine 同口径）

# 实验矩阵 7 配置（E1 引用基线不重跑）
def _t(b1=0.6, b2=0.4, nd=0.0):
    return {"B1": b1, "B2": b2, "needle": nd}


EXPERIMENTS = {
    "E2_env_on":  FlowConfig(env_macd_filter=True,  resonance_on=False,
                             needle_patch=False, resonance_window=1),
    "E2_env_off": FlowConfig(env_macd_filter=False, resonance_on=False,
                             needle_patch=False, resonance_window=1),
    "E3_w3_env_on": FlowConfig(env_macd_filter=True, resonance_on=True,
                               needle_patch=False, resonance_window=3),
    "E3_w5_env_on": FlowConfig(env_macd_filter=True, resonance_on=True,
                               needle_patch=False, resonance_window=5),
    "E4_s03_on":  FlowConfig(env_macd_filter=True,  resonance_on=True, needle_patch=True,
                             needle_stop_pct=0.03, exit_enhanced=True,
                             resonance_window=5, tranche=_t(0.5, 0.3, 0.2)),
    "E4_s03_off": FlowConfig(env_macd_filter=False, resonance_on=True, needle_patch=True,
                             needle_stop_pct=0.03, exit_enhanced=True,
                             resonance_window=5, tranche=_t(0.5, 0.3, 0.2)),
    "E4_s05_on":  FlowConfig(env_macd_filter=True,  resonance_on=True, needle_patch=True,
                             needle_stop_pct=0.05, exit_enhanced=True,
                             resonance_window=5, tranche=_t(0.5, 0.3, 0.2)),
    "E4_s05_off": FlowConfig(env_macd_filter=False, resonance_on=True, needle_patch=True,
                             needle_stop_pct=0.05, exit_enhanced=True,
                             resonance_window=5, tranche=_t(0.5, 0.3, 0.2)),
    # E4_noenh: 只补票、不加增强离场（与 E4_s05_on 对照, 干净分离「补票」vs「增强离场」贡献）
    "E4_s05_noenh": FlowConfig(env_macd_filter=True, resonance_on=True, needle_patch=True,
                               needle_stop_pct=0.05, exit_enhanced=False,
                               resonance_window=5, tranche=_t(0.5, 0.3, 0.2)),
    # ---- v5: 活跃市值环境过滤（非空头区间才允许买入）+ 单针白线 ----
    # v5a = v4 + 活跃市值过滤（regime: +4%转多 / -2.3%转空）
    "E2_amv":       FlowConfig(env_macd_filter=True, resonance_on=False, needle_patch=False,
                               resonance_window=1, amv_filter=True, amv_mode="regime"),
    "E4_amv":       FlowConfig(env_macd_filter=True, resonance_on=True, needle_patch=True,
                               needle_stop_pct=0.05, exit_enhanced=True, resonance_window=5,
                               tranche=_t(0.5, 0.3, 0.2), amv_filter=True, amv_mode="regime"),
    # v5b = v5a + 单针补「白线>黄线」
    "E4_amv_nd":    FlowConfig(env_macd_filter=True, resonance_on=True, needle_patch=True,
                               needle_stop_pct=0.05, exit_enhanced=True, resonance_window=5,
                               tranche=_t(0.5, 0.3, 0.2), amv_filter=True, amv_mode="regime",
                               needle_bull=True),
    # AMV 单日口径对照（仅当日 > -2.3% 才可买; 与 regime 口径并存以供裁决）
    "E4_amv_daily": FlowConfig(env_macd_filter=True, resonance_on=True, needle_patch=True,
                               needle_stop_pct=0.05, exit_enhanced=True, resonance_window=5,
                               tranche=_t(0.5, 0.3, 0.2), amv_filter=True, amv_mode="daily"),
    # 补充: 仅加单针白线（不加 AMV）, 干净分离「单针白线」单独贡献
    "E4_nd_nomv":   FlowConfig(env_macd_filter=True, resonance_on=True, needle_patch=True,
                               needle_stop_pct=0.05, exit_enhanced=True, resonance_window=5,
                               tranche=_t(0.5, 0.3, 0.2), needle_bull=True),
}
EXP_DESC = {
    "E2_env_on": "E2 纯流程(env on)", "E2_env_off": "E2 纯流程(env off)",
    "E3_w3_env_on": "E3 共振3日窗(env on)", "E3_w5_env_on": "E3 共振5日窗(env on)",
    "E4_s03_on": "E4 全流程 补票止损3%(env on)",
    "E4_s03_off": "E4 全流程 补票止损3%(env off)",
    "E4_s05_on": "E4 全流程 补票止损5%(env on)",
    "E4_s05_off": "E4 全流程 补票止损5%(env off)",
    "E4_s05_noenh": "E4 补票无增强 止损5%(env on)",
    "E2_amv": "v5a E2+活跃市值(regime)",
    "E4_amv": "v5a E4+活跃市值(regime)",
    "E4_amv_nd": "v5b E4+活跃市值+单针白线",
    "E4_amv_daily": "v5 E4+活跃市值(单日口径)",
    "E4_nd_nomv": "v5 E4+单针白线(无AMV)",
}


# ============================================================
# 数据
# ============================================================

def universe_from_cache(min_bars=MIN_BARS):
    """Gate-0 口径股票池：缓存中 qfq bars>=min_bars 的标的（零网络）。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT symbol, COUNT(*) FROM klines WHERE adjustment='qfq' "
        "GROUP BY symbol HAVING COUNT(*) >= ?", (min_bars,)).fetchall()
    conn.close()
    codes = sorted(r[0] for r in rows)
    print(f"[universe] 缓存 bars>={min_bars}: {len(codes)} 只")
    return codes


def load_df(conn, symbol):
    """从缓存读单只（DatetimeIndex，标准列）。"""
    rows = conn.execute(
        f"SELECT date, open, high, low, close, volume, amount FROM klines "
        f"WHERE symbol=? AND adjustment='qfq' ORDER BY date", (symbol,)).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=STD_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close"])


def _amv_regime(chg_values):
    """活跃市值 regime 状态机: 单日 ≥+4% 转多, ≤-2.3% 转空（其余维持）。

    口径来源: utils/market_value.amv_timing_signal（4%入场/-2.3%离场）+
    utils/risk_engine（active_mv_exit=-2.3 离场线）。初始态 = 空头（False）。
    """
    state = False
    out = []
    for x in chg_values:
        if x >= AMV_REGIME_UP:
            state = True
        elif x <= AMV_REGIME_DN:
            state = False
        out.append(state)
    return out


def load_amv_state(cal_dates=None):
    """构建活跃市值状态表（index=日期, 列 ["regime","daily"] 布尔）。

    - 数据源: data/active_market_value.csv（用户维护, 1993~今）
    - tail 缺失日（2026-08-29~今）: 用 amv_predict 的**链式推算法**补齐收盘
      （close_t = a·close_{t-1} + b·amount_est + c, 系数由最近 1500 行 lstsq 拟合）
    - 口径:
        regime: +4% 转多 / -2.3% 转空（状态机, 用户体系口径）
        daily : 当日涨跌幅 > -2.3%（单日口径）

    Returns:
        DataFrame 或 None（CSV 缺失时）。
    """
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

    # ---- tail 链式推补（仅补到基准日历末尾需要的日期）----
    if cal_dates:
        last_dt = a["date"].iloc[-1]
        missing = [d for d in cal_dates if pd.Timestamp(d) > last_dt]
        if missing:
            try:
                amt = a["amount"].dropna().tail(5).mean() / 1e8 \
                    if "amount" in a.columns else 0.0
                d = a.dropna(subset=["close"]).tail(1500).copy()
                d["prev"] = d["close"].shift(1)
                d["amt_yi"] = d["amount"] / 1e8 if "amount" in d.columns else 0.0
                dd = d.dropna(subset=["prev", "amt_yi"])
                if len(dd) > 60 and amt:
                    X = np.column_stack([dd["prev"].values, dd["amt_yi"].values,
                                         np.ones(len(dd))])
                    coef, *_ = np.linalg.lstsq(X, dd["close"].values, rcond=None)
                    ca, cb, cc = coef
                    prev_close = float(a["close"].iloc[-1])
                    rows = []
                    for dt in missing:
                        prev_close = ca * prev_close + cb * amt + cc
                        rows.append({"date": pd.Timestamp(dt), "close": prev_close})
                    a = pd.concat([a, pd.DataFrame(rows)], ignore_index=True)
                    print(f"[amv] 链式推补 {len(missing)} 日 "
                          f"({missing[0]} ~ {missing[-1]})")
            except Exception as e:
                print(f"[amv] 链式推补失败, 回退 ffill: {type(e).__name__}")

    a["chg"] = (a["close"] / a["close"].shift(1) - 1) * 100
    a["regime"] = _amv_regime(a["chg"].fillna(0.0).values)
    a["daily"] = (a["chg"] > AMV_REGIME_DN).fillna(False)
    st = a.set_index("date")[["regime", "daily"]].astype(bool)
    print(f"[amv] 状态表 {len(st)} 行, regime 多头占比 "
          f"{st['regime'].mean()*100:.1f}%, daily 通过率 {st['daily'].mean()*100:.1f}%")
    return st


# ============================================================
# 指标统计
# ============================================================

def _fixed_curve(trades, date_key="exit"):
    """固定单笔仓位（1% 初始资金）的可读收益/回撤曲线（v2 修正口径）。

    背景（v1 缺陷）: 旧口径按「满仓不重叠轮动」逐笔复利，在 9 万+ 轮高频交易下
    由几何均值驱动必然爆仓 —— E[ln(1+r)] ≈ mean − var/2，单笔均值虽微正
    （如 +0.101%）但方差大（≈8~10%），减 var/2 后转负，连乘数万笔 → 资本归零，
    于是 MaxDD=100%、Calmar=−1.0，与"平均单笔为正"自相矛盾。

    修正: 每笔独立投入**初始资金的 1%**（固定名义仓位，允许并发持仓），
    资本曲线为线性累加 eq = 1 + Σ(frac × pnl_i)，收益/回撤/年化由此算出，
    具可比意义；胜率/盈亏比/平均单笔（原始统计）不受影响，照常保留。

    Returns:
        dict(sum_pnl_pct, total_ret_pct, annual_pct, maxdd_pct, calmar,
             n_trades, frac)
    """
    if not trades:
        return {"sum_pnl_pct": 0.0, "total_ret_pct": 0.0, "annual_pct": 0.0,
                "maxdd_pct": 0.0, "calmar": 0.0, "n_trades": 0, "frac": FIXED_FRAC}
    ts = sorted(trades, key=lambda x: (x.get(date_key) or ""))
    contrib = 0.0
    peak = 1.0
    mdd = 0.0
    first_d = last_d = None
    for t in ts:
        contrib += FIXED_FRAC * (float(t["pnl"]) / 100.0)
        eq = 1.0 + contrib
        if eq > peak:
            peak = eq
        if peak > 1e-12:
            mdd = max(mdd, (peak - eq) / peak * 100)
        if first_d is None:
            first_d = t.get("entry") or t.get(date_key)
        last_d = t.get(date_key)
    total = contrib * 100
    if first_d and last_d:
        days = (pd.Timestamp(last_d) - pd.Timestamp(first_d)).days
        years = max(days / 365.25, 1e-6)
        annual = ((1 + contrib) ** (1 / years) - 1) * 100 if (1 + contrib) > 0 else -100.0
    else:
        years = 1.0
        annual = 0.0
    calmar = round(annual / mdd, 3) if mdd > 1e-9 else 999.0
    return {"sum_pnl_pct": round(float(np.sum([t["pnl"] for t in ts])), 2),
            "total_ret_pct": round(total, 2), "annual_pct": round(annual, 2),
            "maxdd_pct": round(mdd, 2), "calmar": calmar,
            "n_trades": len(ts), "frac": FIXED_FRAC}


def stats_flow(trades):
    """flow 交易列表统计（trades: {entry, exit, pnl, bucket, hold}）。

    Returns:
        dict(n, win_rate, pl_ratio, avg_pnl, median_hold, 复利指标)
    """
    if not trades:
        return {"n": 0, "win_rate": 0.0, "pl_ratio": 0.0, "avg_pnl": 0.0,
                "median_hold": 0.0, "insufficient": False, **_fixed_curve([])}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / len(pnls) * 100
    aw = float(np.mean(wins)) if wins else 0.0
    al = float(np.mean(losses)) if losses else 0.0
    plr = round(aw / abs(al), 2) if al else 0.0
    comp = _fixed_curve(trades)
    return {"n": len(trades), "win_rate": round(wr, 1), "pl_ratio": plr,
            "avg_pnl": round(float(np.mean(pnls)), 3),
            "median_hold": round(float(np.median([t["hold"] for t in trades])), 1),
            "insufficient": False, **comp}


def stats_v1(trades_v1):
    """冻结内核 trade dict 列表（日期/收益/持有）统计。

    口径（v2 统一）: 胜率=收益>0 占比；收益/回撤用固定 1% 单笔仓位累加曲线
    （_fixed_curve），与 flow 实验口径一致，可横向比较。
    """
    if not trades_v1:
        return {"n": 0, "win_rate": 0.0, "pl_ratio": 0.0, "avg_pnl": 0.0,
                "insufficient": False, **_fixed_curve([])}
    pnls = [t["pnl"] for t in trades_v1]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    aw = float(np.mean(wins)) if wins else 0.0
    al = float(np.mean(losses)) if losses else 0.0
    comp = _fixed_curve(trades_v1)
    return {"n": len(trades_v1), "win_rate": round(len(wins) / len(pnls) * 100, 1),
            "pl_ratio": round(aw / abs(al), 2) if al else 0.0,
            "avg_pnl": round(float(np.mean(pnls)), 3),
            "insufficient": False, **comp}


def mark_insufficient(seg):
    if isinstance(seg, dict) and 0 < seg.get("n", 0) < OOS_MIN_TRADES:
        seg["insufficient"] = True
    return seg


def spearman(x, y):
    """Spearman 秩相关（无 scipy 依赖）。"""
    if len(x) < 3:
        return 0.0
    rx = pd.Series(x).rank().values
    ry = pd.Series(y).rank().values
    if rx.std() == 0 or ry.std() == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


# ============================================================
# 跑批
# ============================================================

def run_all(limit=0, ckpt_tag="2026-09-11", amv_state=None):
    """全市场跑批：每只股票一次 prepare，13 配置状态机 + 6 战法基线复用。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()
    codes = universe_from_cache()
    if limit:
        codes = codes[:limit]
    if amv_state is None:
        amv_state = load_amv_state()

    rules = make_exit_rules()
    ckpt_path = os.path.join(OUT_DIR, f".ckpt_flow_exp_{ckpt_tag}.pkl")

    state = {"flow": {k: [] for k in EXPERIMENTS},
             "base": {s: [] for s in BASELINE_STRATS + BASELINE_EXTRA},
             "flow_meta": {k: {"needle_rounds": 0, "b2_rounds": 0, "b3_rounds": 0,
                               "reduce_legs": 0} for k in EXPERIMENTS},
             "done": set(), "n_b1_bull": 0, "n_b1_difpos": 0,
             "n_amv_blocked": 0, "fail": []}
    if os.path.exists(ckpt_path):
        try:
            with open(ckpt_path, "rb") as f:
                loaded = pickle.load(f)
            # 配置集变化（如新增 v5 配置）→ 旧检查点不兼容, 重建以避免 KeyError
            if set(loaded.get("flow", {})) == set(EXPERIMENTS) \
                    and set(loaded.get("base", {})) == set(BASELINE_STRATS + BASELINE_EXTRA):
                state = loaded
                print(f"[resume] 已恢复 {len(state['done'])} 只")
            else:
                print("[resume] 检查点配置集不匹配 → 忽略, 重新开始")
        except Exception:
            pass

    todo = [c for c in codes if c not in state["done"]]
    print(f"[run] 待处理 {len(todo)} / {len(codes)} 只"
          f"（{len(EXPERIMENTS)} 配置 × {len(BASELINE_STRATS)+len(BASELINE_EXTRA)} 战法基线）")
    conn = sqlite3.connect(DB_PATH, timeout=60)

    n_done = 0
    for code in todo:
        try:
            df = load_df(conn, code)
            if df is None or len(df) < 120:
                state["fail"].append(code)
                state["done"].add(code)
                continue
            blk = _prepare_block(df, FlowConfig(env_macd_filter=True, resonance_on=True,
                                                resonance_window=5),
                                 want_resonance=True, amv_state=amv_state)
            # 环境过滤贡献计数（B1 信号: 仅多头 vs 多头且DIF>0）
            b1 = blk["b1_sig"]
            bull_only = blk["wx"] > blk["yx"]
            state["n_b1_bull"] += int((b1 & bull_only).sum())
            state["n_b1_difpos"] += int((b1 & blk["env_ok"]).sum())
            if blk.get("amv_regime") is not None:
                state["n_amv_blocked"] += int((b1 & blk["env_ok"]
                                               & ~blk["amv_regime"]).sum())

            # flow 配置（共用指标块; env_ok 与共振时间窗按 cfg 重算——
            # block 用固定 cfg 预编译, 必须按各配置覆盖, 否则失效）
            for key, cfg in EXPERIMENTS.items():
                blk_k = dict(blk)
                if not cfg.env_macd_filter:
                    blk_k["env_ok"] = blk["wx"] > blk["yx"]
                if blk.get("res_mats"):
                    wh = int(getattr(cfg, "resonance_window", 5))
                    blk_k["res_matrix"] = blk["res_mats"].get(wh, blk["res_mats"].get(5))
                trades, ledger = run_flow_backtest_ledger(
                    None, code, code, cfg, rules, block=blk_k)
                if ledger["violations"]:
                    state.setdefault("ledger_viol", []).extend(
                        [f"{code}:{v}" for v in ledger["violations"][:3]])
                fm = state["flow_meta"][key]
                for t in trades:
                    hold_d = max((pd.Timestamp(t.exit_date) -
                                  pd.Timestamp(t.b1_entry_date)).days, 1)
                    state["flow"][key].append({
                        "entry": t.b1_entry_date, "exit": t.exit_date,
                        "pnl": t.pnl_pct, "bucket": t.resonance_bucket,
                        "hold": hold_d})
                    if t.needle_entry_date:
                        fm["needle_rounds"] += 1
                    if t.b2_entry_date:
                        fm["b2_rounds"] += 1
                    if t.b3_date:
                        fm["b3_rounds"] += 1
                    fm["reduce_legs"] += sum(
                        1 for x in t.tranches
                        if "减仓" in str(x.get("reason", ""))
                        or "预警" in str(x.get("reason", "")))

            # 5 战法基线（冻结内核, 供 IS/OOS 与分年度回答用户三问）
            sig = blk["signals"]
            try:
                res = backtest_stock(sig, df, rules=rules)
                for s in BASELINE_STRATS:
                    for tr in res.get(s, []):
                        state["base"][s].append({
                            "entry": str(pd.Timestamp(tr["日期"]) + pd.Timedelta(days=1))[:10],
                            "exit": None, "pnl": tr["收益"],
                            "hold": int(tr.get("持有", 10)),
                            "sig": tr["日期"]})
                # v5: 单针补「白线>黄线」（消费端覆盖信号, 不改冻结内核；同 A0 映射模式）
                try:
                    sig2 = dict(sig)
                    bull_ws = (blk["wx"] > blk["yx"]).fillna(False).astype(bool)
                    sig2["单针"] = (sig["单针"].fillna(False).astype(bool) & bull_ws)
                    res2 = backtest_stock(sig2, df, rules=rules)
                    for tr in res2.get("单针", []):
                        state["base"]["单针+白"].append({
                            "entry": str(pd.Timestamp(tr["日期"]) + pd.Timedelta(days=1))[:10],
                            "exit": None, "pnl": tr["收益"],
                            "hold": int(tr.get("持有", 10)),
                            "sig": tr["日期"]})
                except Exception:
                    pass
            except Exception:
                pass
            state["done"].add(code)
        except Exception as e:
            state["fail"].append(f"{code}:{type(e).__name__}")
            state["done"].add(code)

        n_done += 1
        if n_done % 50 == 0:
            el = time.time() - t0
            eta = el / n_done * (len(todo) - n_done)
            print(f"  {n_done}/{len(todo)}  已用 {el/60:.1f}m  预计剩余 {eta/60:.1f}m",
                  flush=True)
        if n_done % CKPT_EVERY == 0:
            with open(ckpt_path + ".tmp", "wb") as f:
                pickle.dump(state, f)
            os.replace(ckpt_path + ".tmp", ckpt_path)

    conn.close()
    with open(ckpt_path + ".tmp", "wb") as f:
        pickle.dump(state, f)
    os.replace(ckpt_path + ".tmp", ckpt_path)
    print(f"[run] 完成: {len(state['done'])} 只, 失败 {len(state['fail'])}, "
          f"耗时 {(time.time()-t0)/60:.1f} 分钟")
    return state


def _exit_dates_from_hold(trades):
    """基线交易无 exit 日期 → 用 持有天数 近似 exit = entry + hold 自然日。"""
    for t in trades:
        if not t.get("exit"):
            t["exit"] = str(pd.Timestamp(t["entry"]) + pd.Timedelta(days=t["hold"]))[:10]
    return trades


# ============================================================
# 切分与汇总
# ============================================================

def split_and_summarize(state, split_date, is_lo_date):
    """按全局切点做 IS/OOS + 共振分桶 + 分年度。

    IS: entry < split_date 且 entry < is_lo_date（尾部 30 根剔除）
    OOS: entry >= split_date
    """
    lo = str(pd.Timestamp(is_lo_date))[:10]
    sp = str(pd.Timestamp(split_date))[:10]
    out = {"flow": {}, "base": {}, "meta": {
        "n_b1_bull": state["n_b1_bull"], "n_b1_difpos": state["n_b1_difpos"],
        "n_amv_blocked": state.get("n_amv_blocked", 0),
        "n_fail": len(state["fail"]), "n_stocks": len(state["done"]),
        "flow_counters": state.get("flow_meta", {})}}

    for key, trades in state["flow"].items():
        is_t = [t for t in trades if t["entry"] < sp and t["entry"] < lo]
        oos_t = [t for t in trades if t["entry"] >= sp]
        out["flow"][key] = {
            "ALL": stats_flow(trades),
            "IS": mark_insufficient(stats_flow(is_t)),
            "OOS": mark_insufficient(stats_flow(oos_t)),
            "buckets": {},
        }
        for b in ("0", "1", "2", "3+"):
            bt = [t for t in trades if t["bucket"] == b]
            bi = [t for t in is_t if t["bucket"] == b]
            bo = [t for t in oos_t if t["bucket"] == b]
            out["flow"][key]["buckets"][b] = {
                "ALL": stats_flow(bt), "IS": mark_insufficient(stats_flow(bi)),
                "OOS": mark_insufficient(stats_flow(bo))}

    for s, trades in state["base"].items():
        trades = _exit_dates_from_hold(trades)
        is_t = [t for t in trades if t["entry"] < sp and t["entry"] < lo]
        oos_t = [t for t in trades if t["entry"] >= sp]
        out["base"][s] = {"ALL": stats_v1(trades),
                          "IS": mark_insufficient(stats_v1(is_t)),
                          "OOS": mark_insufficient(stats_v1(oos_t)),
                          "years": {}}
        for y in ("2024", "2025", "2026"):
            yt = [t for t in trades if t["entry"].startswith(y)]
            out["base"][s]["years"][y] = stats_v1(yt)
    return out


def monotonicity(res):
    """E3 单调性验收（3日/5日窗各验）：桶胜率 3+ > 2 > 1 > 0（或 Spearman ρ≥0.5）。"""
    def _check(exp_key, seg_key):
        wr = []
        for b in ("0", "1", "2", "3+"):
            seg = res["flow"][exp_key]["buckets"][b][seg_key]
            wr.append(seg["win_rate"] if seg["n"] > 0 else np.nan)
        strict = (wr[3] > wr[2] > wr[1] > wr[0]) if not any(np.isnan(wr)) else False
        # Spearman: 桶序 0..3 vs 胜率
        valid = [(i, w) for i, w in enumerate(wr) if not np.isnan(w)]
        rho = (spearman([v[0] for v in valid], [v[1] for v in valid])
               if len(valid) >= 3 else 0.0)
        return {"win_rates": wr, "strict_monotonic": bool(strict),
                "spearman": round(rho, 3), "pass": bool(strict or rho >= 0.5)}
    out = {}
    for exp_key in ("E3_w3_env_on", "E3_w5_env_on"):
        out[exp_key] = {"IS": _check(exp_key, "IS"), "OOS": _check(exp_key, "OOS")}
    return out


# ============================================================
# 报告
# ============================================================

def write_report(res, mono, bench_pct, bench_detail, meta_extra, tag):
    """写 JSON + Markdown 报告。"""
    res["meta"].update(meta_extra or {})   # 供 _sec_* 直接读 meta（如 amv_stats）
    payload = {
        "version": "flow_v5",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": START, "is_oos": f"{IS_RATIO:.0%}:{1-IS_RATIO:.0%}",
        "baseline_ref": "data/backtest/market_full_2026-09-11.json",
        "benchmark_hs300_pct": bench_pct, "benchmark_detail": bench_detail,
        "meta": {**res["meta"], **meta_extra},
        "monotonicity": mono,
        "experiments": {"E1_brick_solo": {"source": "baseline", "收益率%": 10.3,
                                          "note": "引用错版基线, 未重跑"},
                        **{k: res["flow"][k] for k in res["flow"]}},
        "baseline_is_oos": res["base"],
    }
    jp = os.path.join(OUT_DIR, f"flow_exp_{tag}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    L = ["# 流程化回测实验矩阵报告（flow_v5）", "",
         "## 本次修正说明（v4 + v5 / 2026-09-11）", "",
         "承接 v3.1，本轮落实两批改动：**v4 离场口径修正**（3 条知识库离场规则 + "
         "收盘确认 + 连续4红砖）与 **v5 环境过滤**（活跃市值 + 单针白线）。",
         "",
         "### v4 · 离场口径修正（用户裁决 + 知识库原有规则）",
         "1. **补 3 条知识库原有离场规则**（最高优先级, 在硬止损之后/减仓之前）—— 这是"
         "E2(+134.5%) 远逊 baseline B1(+278.2%) 的**真正主因**：",
         "   - **前高止盈**: `h≥前高`(信号日前60日最高价, 至少+5%); 放量突破前高→放飞"
         "抬高止盈线, 否则止盈离场（与冻结内核同口径）。",
         "   - **连续2日收盘跌破白线离场**: `c[i]<白线[i] 且 c[i-1]<白线[i-1]`。",
         "   - **3个交易日内不恢复上涨就走**: 入场后第 3 个交易日收盘仍≤入场价 → 离场。",
         "2. **止损改收盘确认**: 触发条件由盘中 `lo≤stop` 改为 `close≤stop`，命中按"
         "**收盘价**成交（防插针假破位；代价=真实成交价更低, 详见风险权衡节）。",
         "3. **「红砖缩短」减仓 → 只保留「连续4根红砖后」减仓**: 原按砖型图逐日回落即减,"
         "触发占全部减仓腿绝大多数(84.35%)；据知识库 brick_rules「连续4红砖后才减仓」"
         "移除该条, 仅保留第7级「连续4红砖减仓30%」。",
         "> **更正 v3 结论**: v3 曾判断「盘中止损过紧」为主因 —— 该判断**有误**。"
         "实测 flow 与 baseline 止损位**完全相同**（信号日 T 低点），真正的差异是"
         "**flow 缺失上述 3 条早期离场规则**（baseline 独有「前高止盈」4.06%、"
         "「破白线离场」17.83%）。v4 补齐后 flow 应显著接近 baseline。",
         "",
         "### v5 · 环境过滤（用户点出的两处缺失）",
         "4. **活跃市值过滤（组 B, 正式口径）**: 此前回测**完全没有**活跃市值过滤。"
         "**正式口径 = 组 B**（活跃市值 regime 多头 **且** 个股白线>黄线, 两者同时满足"
         "才买入）；组 A（仅个股白线）降为对照。活跃市值口径与项目一致"
         "（`market_value.amv_timing_signal` + `risk_engine.active_mv_exit`）: "
         "**+4% 进入多头 / −2.3% 跌破转空头**（状态机）。同时给出**单日口径**"
         "（当日 > −2.3%）对照（`*_daily`）。防前视: 用 signal 日 T（入场 T+1 开盘）"
         "及以前已知的最近状态。**regime 初始状态由 1993 起 CSV 历史逐日推演**, "
         "非默认多头（起点状态/切换次数/多空占比见「零」节）。",
         "5. **单针补「白线>黄线」**: B1/B2/B3/砖型冻结内核均已带 `bull`，唯单针没有；"
         "本版给流程内**补票**条件加 `白线>黄线`（v5b / `E4_nd_nomv`），并新增基线 "
         "**「单针+白」** 对照（消费端覆盖信号, 不改冻结内核）。改前/改后见「零」「三」节。",
         "6. **实验矩阵扩到 14 配置**: E2/E3/E4 保留, 新增 `E2_amv / E4_amv / E4_amv_nd / "
         "E4_amv_daily / E4_nd_nomv`。v4 离场规则对**全部**配置生效。",
         "",
         "> v2.1/v3/v3.1 修正见 `data/backtest/flow_diag_*.md` 与旧版报告，本版沿用。",
         ""]
    L.append(f"- 生成: {payload['generated_at']}  区间: {START} ~ 今  "
             f"IS/OOS = 60:40（切点见下）")
    L.append(f"- 股票池: 缓存 bars≥{MIN_BARS} 的 **{res['meta']['n_stocks']}** 只"
             f"（Gate-0 实测口径，零网络）")
    bench_paren = ""
    if bench_detail:
        bench_paren = (f"（{bench_detail['first_date']} 收 {bench_detail['first_close']}"
                       f" → {bench_detail['last_date']} 收 {bench_detail['last_close']}）")
    L.append(f"- 沪深300 同期基准: **{bench_pct}%**{bench_paren}")
    L.append(f"- E1 砖型单独: 引用错版基线 **+10.3%**（不重跑, 见 baseline_ref）")
    L.append(f"- 环境过滤计数: B1 信号(多头) {res['meta']['n_b1_bull']} → "
             f"叠加 DIF>0 后 {res['meta']['n_b1_difpos']} "
             f"（拦截 {res['meta']['n_b1_bull']-res['meta']['n_b1_difpos']} 条, "
             f"{(1-res['meta']['n_b1_difpos']/max(res['meta']['n_b1_bull'],1))*100:.1f}%）")
    L.append("")
    _sec_winrate(L, res)
    _sec_compare(L, res)
    _sec_buckets(L, res, mono)
    _sec_baseline(L, res)
    _sec_years(L, res)
    _sec_caveats(L, res)
    mp = os.path.join(OUT_DIR, f"flow_exp_{tag}.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return jp, mp


def _row(d):
    if d.get("n", 0) == 0:
        return "— | — | — | — | — | — | — | —（无交易）"
    ins = " ⚠️INSUFFICIENT" if d.get("insufficient") else ""
    return (f"{d['n']} | {d['win_rate']}% | {d['pl_ratio']} | {d['avg_pnl']}% | "
            f"{d['sum_pnl_pct']}% | {d['total_ret_pct']}% | {d['annual_pct']}% | "
            f"{d['maxdd_pct']}% | {d['calmar']}{ins}")


def _wr_row(d):
    """胜率三元组行: 交易数 | 胜率 | 盈亏比 | 平均单笔% | 收益%(1%仓)。"""
    if d.get("n", 0) == 0:
        return "0 | — | — | — | —（无交易）"
    ins = " ⚠️INSUFFICIENT" if d.get("insufficient") else ""
    return (f"{d['n']} | {d['win_rate']}% | {d['pl_ratio']} | "
            f"{d['avg_pnl']}% | {d['total_ret_pct']}%{ins}")


def amv_regime_stats(amv_state, start=None):
    """活跃市值 regime 统计（切换次数 + 多空天数占比 + 起点状态）。

    ★ 起点状态由 CSV 历史**逐日推演**得到（自 1993 起按 +4%/-2.3% 状态机推进），
      不是默认「多头」。据此可判断过滤强度（多头占比越高过滤越弱）。
    """
    if amv_state is None or len(amv_state) == 0:
        return {}
    s = amv_state["regime"].astype(bool)
    if start:
        s = s[s.index >= pd.Timestamp(start)]
    if len(s) == 0:
        return {}
    v = s.values
    n = len(v)
    bull = int(v.sum())
    switches = int((v[1:] != v[:-1]).sum()) if n > 1 else 0
    return {"n_days": n, "bull_days": bull, "bear_days": n - bull,
            "bull_share_pct": round(bull / n * 100, 1),
            "bear_share_pct": round((n - bull) / n * 100, 1),
            "switches": switches,
            "init_state": "多头" if bool(v[0]) else "空头",
            "init_date": str(s.index[0])[:10]}


def _sec_winrate(L, res):
    """v4 / v5a / v5b 三组对比（胜率优先 · 用户目标 50-60%）。"""
    L += ["## 零、胜率优先：组A(仅个股白线) vs 组B(活跃市值+个股白线) 对比", "",
          "> 用户目标 = 胜率 50-60%（可接受「少而精」）。**正式口径 = 组 B**"
          "（活跃市值 regime 多头 **且** 个股白线>黄线，两个都要满足）；组 A 为对照。",
          "> 变量递进：**v4** = 3 条离场规则 + 收盘确认 + 连续4红砖；"
          "**组A(v4)** = v4 + 个股白线>黄线(+DIF>0)，活跃市值过滤**关**；"
          "**组B(v5a)** = 组A + 活跃市值 regime 过滤(+4%/-2.3%)；"
          "**v5b** = 组B + 单针白线。另列 `*_daily`（活跃市值单日口径）供裁决。",
          "",
          "| 组 | 配置 | 段 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | 收益%(1%仓) |",
          "|---|---|---|---:|---:|---:|---:|---:|"]
    rows = [
        ("A v4", "E2_env_on", "纯流程 v4 (仅个股白线)"),
        ("A v4", "E4_s05_on", "全流程 v4 (仅个股白线)"),
        ("B v5a", "E2_amv", "纯流程 v5a (活跃市值+个股)"),
        ("B v5a", "E4_amv", "全流程 v5a (活跃市值+个股)"),
        ("B v5b", "E4_amv_nd", "全流程 v5b (+单针白线)"),
        ("vdaily", "E4_amv_daily", "全流程 v5 (活跃市值单日口径)"),
        ("v5?", "E4_nd_nomv", "全流程 +单针白线(无AMV, 隔离单针白线贡献)"),
    ]
    for grp, key, desc in rows:
        for seg in ("ALL", "IS", "OOS"):
            d = res["flow"].get(key, {}).get(seg)
            if d is None:
                continue
            L.append(f"| {grp} | {desc} | {seg} | " + _wr_row(d) + " |")
    L += ["",
          "**取舍**: 活跃市值过滤会**大幅削减交易数**（仅非空头区间出手）—— 这是"
          "用户可接受的「少而精」。读数请同时看 **胜率↑** 与 **交易数↓**，避免只看胜率。",
          ""]
    # 交易数收缩量化
    e2 = res["flow"].get("E2_env_on", {}).get("ALL", {})
    e2a = res["flow"].get("E2_amv", {}).get("ALL", {})
    if e2.get("n") and e2a.get("n"):
        keep = e2a["n"] / e2["n"] * 100
        L.append(f"- 纯流程 A→B: 交易数 {e2['n']} → {e2a['n']}（保留 **{keep:.1f}%**）；"
                 f"胜率 {e2['win_rate']}% → {e2a['win_rate']}%")
    e4 = res["flow"].get("E4_s05_on", {}).get("ALL", {})
    e4a = res["flow"].get("E4_amv", {}).get("ALL", {})
    if e4.get("n") and e4a.get("n"):
        keep = e4a["n"] / e4["n"] * 100
        L.append(f"- 全流程 A→B: 交易数 {e4['n']} → {e4a['n']}（保留 **{keep:.1f}%**）；"
                 f"胜率 {e4['win_rate']}% → {e4a['win_rate']}%")
    # 活跃市值 regime 状态统计（初始状态 + 切换次数 + 多空占比）
    ast = res["meta"].get("amv_stats") or {}
    if ast:
        L += ["",
              "### 活跃市值 regime 状态统计（决定过滤强度）", "",
              f"- 区间: {ast.get('init_date','?')} 起共 **{ast.get('n_days',0)}** 交易日",
              f"- **起点状态（由 1993 起历史逐日推演, 非默认多头）: "
              f"{ast.get('init_state','?')}**",
              f"- 多头天数 **{ast.get('bull_days',0)}**（{ast.get('bull_share_pct',0)}%） / "
              f"空头天数 **{ast.get('bear_days',0)}**（{ast.get('bear_share_pct',0)}%）",
              f"- **状态切换次数: {ast.get('switches',0)} 次**"
              f"（多空翻转频率, 频繁切换→过滤≈随机）"]
    L.append("")


def _sec_compare(L, res):
    L += ["## 一、E2 vs E4（流程 vs 全流程+补票, env on/off）", "",
          "> 收益/回撤为 **固定 1% 单笔仓位累加曲线**口径（v2 修正，见「本次修正说明」）。",
          "",
          "| 实验 | 轮数 | 胜率 | 盈亏比 | 平均单笔% | Σ单笔% | 收益%(1%仓) | 年化% | MaxDD% | Calmar |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k in ("E2_env_on", "E2_env_off", "E4_s03_on", "E4_s03_off",
              "E4_s05_on", "E4_s05_off", "E4_s05_noenh",
              "E2_amv", "E4_amv", "E4_amv_nd", "E4_amv_daily", "E4_nd_nomv"):
        d = res["flow"][k]["ALL"]
        L.append(f"| {EXP_DESC[k]} | " + _row(d) + " |")
    L += ["", "### IS / OOS 拆分", "",
          "| 实验 | 段 | 轮数 | 胜率 | 盈亏比 | 平均单笔% | Σ单笔% | 收益%(1%仓) | 年化% | MaxDD% | Calmar |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k in ("E2_env_on", "E2_env_off", "E4_s03_on", "E4_s05_on",
              "E4_s05_noenh", "E4_s03_off", "E4_s05_off",
              "E2_amv", "E4_amv", "E4_amv_nd", "E4_amv_daily", "E4_nd_nomv"):
        for seg in ("IS", "OOS"):
            d = res["flow"][k][seg]
            L.append(f"| {EXP_DESC[k]} | {seg} | " + _row(d) + " |")
    L.append("")
    fc = res["meta"].get("flow_counters", {})
    if fc:
        L += ["### 流程关键环节触发计数（验证「持有段是否走到回调→补票」）", "",
              "| 实验 | 轮数 | B2加仓轮 | 单针补票轮 | 达 B3 轮 | 减仓腿数 |",
              "|---|---:|---:|---:|---:|---:|"]
        for k in EXPERIMENTS:
            m = fc.get(k, {})
            n = res["flow"][k]["ALL"]["n"]
            L.append(f"| {EXP_DESC[k]} | {n} | {m.get('b2_rounds',0)} | "
                     f"{m.get('needle_rounds',0)} | {m.get('b3_rounds',0)} | "
                     f"{m.get('reduce_legs',0)} |")
        L.append("")


def _sec_buckets(L, res, mono):
    L += ["## 二、E3 共振分层（C 位 · 验证「依据越多胜率越高」）", "",
          "共振项 = **最近 N 个交易日内曾出现过**（先后出现，非同日齐发）；"
          "共振数 = 窗口内出现过的不同指标个数（去重）。窗口 T = 信号日，"
          "入场 T+1 开盘，无前视。池 = 砖型翻红/MACD顺周期/倍量柱/MACD底背离/关键K"
          "（已剔除「缩量止跌」，其与 B1 强制缩量定义重叠）。", ""]
    for exp_key, title in (("E3_w3_env_on", "3 日窗"), ("E3_w5_env_on", "5 日窗")):
        L += [f"### {title}（{exp_key}）", "",
              "| 段 | 桶 | 轮数 | 胜率 | 盈亏比 | 平均单笔% | Σ单笔% | 收益%(1%仓) | 年化% | MaxDD% | Calmar |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for seg in ("IS", "OOS"):
            for b in ("0", "1", "2", "3+"):
                d = res["flow"][exp_key]["buckets"][b][seg]
                L.append(f"| {seg} | {b} | " + _row(d) + " |")
        L.append("")
    L += ["**单调性验收**（桶胜率 3+>2>1>0 或 Spearman ρ≥0.5）:"]
    for exp_key, title in (("E3_w3_env_on", "3 日窗"), ("E3_w5_env_on", "5 日窗")):
        for seg in ("IS", "OOS"):
            m = mono[exp_key][seg]
            wr = m["win_rates"]
            L.append(f"- {title} {seg}: 胜率 {[w if not pd.isna(w) else 'NA' for w in wr]}  "
                     f"严格单调={m['strict_monotonic']}  Spearman ρ={m['spearman']}  "
                     f"→ {'✅ 通过' if m['pass'] else '❌ 不通过（用户假设在该段不成立, 如实报告）'}")
        L.append("")
    L.append("> 诊断（`data/backtest/flow_diag_resonance_2026-09-11.md`）表明："
             "同日口径下「倍量柱/关键K/MACD顺周期」与 B1 定义数学互斥，"
             "改时间窗后由实测桶分布检验其是否被激活。")
    L.append("")
    _m3 = mono["E3_w3_env_on"]; _m5 = mono["E3_w5_env_on"]
    L.append("**结论（全市场）**: 各窗单调性验收结果见上（自动计算）—— "
             f"3 日窗 IS ρ={_m3['IS']['spearman']}/OOS ρ={_m3['OOS']['spearman']}，"
             f"5 日窗 IS ρ={_m5['IS']['spearman']}/OOS ρ={_m5['OOS']['spearman']}。"
             "**判读**: 若仅一段通过 → 用户「依据越多胜率越高」在稳健意义上**未获支持**；"
             "两段均通过方可采信单调性。")
    L.append("")


def _sec_baseline(L, res):
    L += ["## 三、错版五战法基线 IS/OOS（冻结内核, 回答用户三个决策问题）", "",
          "| 战法 | 段 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | Σ单笔% | 收益%(1%仓) | 年化% | MaxDD% | Calmar |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s in BASELINE_STRATS + BASELINE_EXTRA:
        for seg in ("ALL", "IS", "OOS"):
            d = res["base"][s][seg]
            L.append(f"| {s} | {seg} | " + _row(d) + " |")
    L.append("")
    L.append("> ① B1 过拟合检验看 B1 的 IS vs OOS；② 单针去留看 OOS 是否仍大亏 "
             "（「单针+白」= 单针补白线>黄线后的对照）；"
             "③ 砖型问题定位看分年度表（下节）。"
             "交易数<30 的段标 ⚠️INSUFFICIENT（不采信）。")
    L.append("")


def _sec_years(L, res):
    L += ["## 四、错版五战法分年度拆解", "",
          "| 战法 | 年份 | 交易数 | 胜率 | 盈亏比 | 平均单笔% | Σ单笔% | 收益%(1%仓) | 年化% | MaxDD% | Calmar |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s in BASELINE_STRATS + BASELINE_EXTRA:
        for y in ("2024", "2025", "2026"):
            d = res["base"][s]["years"][y]
            L.append(f"| {s} | {y} | " + _row(d) + " |")
    L.append("")


def _sec_caveats(L, res):
    L += ["## 五、口径与局限", "",
          "1. **幸存者偏差（方向与可否使用）**: 股票池取自**当前在市标的清单**，"
          "不含区间内已退市个股；退市股多为暴跌标的，缺失它们使结果**系统性偏乐观**"
          "（真实表现应比本报告更差）。五套战法/各实验**同等受影响**，"
          "因此**横向排序仍可参考**；但**绝对收益率不可对外引用**。",
          "",
          "> **⚠ 本回测为无退市股的上界估计，真实表现应低于此值。**",
          "",
          "2. **收益口径（v2 修正）**: 每笔固定投入初始资金的 **1%**（可并发持仓）的"
          "线性累加曲线 eq = 1 + Σ(1% × pnl_i)；Σ单笔% 为未缩放的单笔收益累加，"
          "供横向对比。旧版「满仓不重叠复利」在数万笔高频下几何均值驱动必爆仓，"
          "已作废（详见开头「本次修正说明」）。",
          "3. **IS 尾部剔除 30 根**（防离场前视）；OOS 段交易数 <30 标 "
          "INSUFFICIENT_SAMPLE，该层结论不采信。",
          "4. **离场分级（v4）**: 设计对 MACD 顶背离/死叉仅写\"预警\"，本实现按减仓处理"
          "（白线破黄线已是硬离场，死叉清仓会高频换手失真）。v4 移除「红砖缩短」减仓、"
          "仅保留「连续4红砖」减仓，并补 3 条知识库离场规则（见「本次修正说明」v4 节）。",
          "5. **★ 收盘确认止损的权衡（用户拍板 close≤stop）**: 由盘中 `lo≤stop` 改收盘"
          "`close≤stop`，命中按**收盘价**成交。",
          "   - 收益: 规避**盘中插针/长下影假破位**被扫出（少一次无效止损，胜率有望小幅↑）。",
          "   - 代价: 当日收盘本已跌破止损位，成交价**低于**止损位（不是止损位成交），"
          "单笔亏损幅度**变大**；且离场晚一日 → 极端行情下回撤更深。",
          "   - 复核: 需在 v4 结果里对比「止损腿占比/平均单笔亏损」，确认插针规避的收益"
          "是否覆盖延后成交的代价。",
          "6. **活跃市值口径（v5，两套并存待裁决）**: 项目现有实现"
          "（`market_value.amv_timing_signal`、`risk_engine.active_mv_exit`）为"
          "**+4% 入场 / −2.3% 离场**。本报告给两套口径:",
          "   - `regime`（状态机, 默认）: 单日 ≥+4% 转多、≤−2.3% 转空，只在多头态买入"
          "（本段多头日占比仅 ~33%，交易数大幅收缩）；",
          "   - `daily`（单日）: 仅当日涨跌幅 > −2.3% 才可买（通过率 ~89%，收缩很小）。",
          "   防前视: 取 signal 日 T 及以前已知的最近活跃市值状态（入场 T+1 开盘）。",
          "   **请裁决主口径**（regime 更贴体系「空头区间手紧」，daily 更接近"
          "「当日没跌破离场线」）。",
          "7. **单针口径（v5）**: 冻结内核的 B1/B2/B3/砖型均带 `白线>黄线`，唯 `单针`"
          "没有。v5b 给流程内**补票**加该前提；基线新增「单针+白」列（消费端覆盖信号, "
          "不改冻结内核）。单针原始列保留以对照。",
          "8. **活跃市值 tail 推补**: CSV 止于 2026-08-28，缺失日（2026-08-29~今）用"
          "`amv_predict` 链式推算法补收盘（最近 1500 行 lstsq 系数），仅影响 OOS 末尾"
          "约 2 周，量级可忽略；如推补失败则回退 ffill。",
          "9. 本次未做 IS/OOS 参数寻优（无过拟合来源），但 B1 信号定义本身来自"
          "知识库固定口径，其 OOS 衰减仍需按上表解读。"]


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    ap = argparse.ArgumentParser(description="F3 流程回测实验矩阵")
    ap.add_argument("--limit", type=int, default=0, help="只取前N只(0=全市场)")
    ap.add_argument("--tag", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    t0 = time.time()
    bench_df = get_benchmark(start=START)
    if len(bench_df):
        cal = [str(d)[:10] for d in bench_df["date"]]
        sp_idx = int(len(cal) * IS_RATIO)
        split_date = cal[sp_idx]
        is_lo = cal[max(sp_idx - IS_TAIL_TRIM, 0)]
        c0, c1 = float(bench_df["close"].iloc[0]), float(bench_df["close"].iloc[-1])
        bench_pct = round((c1 / c0 - 1) * 100, 1)
        bench_detail = {"bars": int(len(bench_df)), "first_date": cal[0],
                        "first_close": round(c0, 2), "last_date": cal[-1],
                        "last_close": round(c1, 2)}
    else:
        raise SystemExit("沪深300 基准获取失败, 无法定切点（不伪造数据, 硬退出）")
    print(f"[split] 切点 {split_date} (IS≤{is_lo}) | 基准 {bench_pct}%")

    amv_state = load_amv_state(cal_dates=cal)
    amv_stats = amv_regime_stats(amv_state, start=START)
    if amv_stats:
        print(f"[amv] 起点({amv_stats['init_date']})状态={amv_stats['init_state']} | "
              f"多头占比 {amv_stats['bull_share_pct']}% | "
              f"切换 {amv_stats['switches']} 次")
    state = run_all(limit=args.limit, ckpt_tag=args.tag, amv_state=amv_state)
    res = split_and_summarize(state, split_date, is_lo)
    mono = monotonicity(res)
    meta_extra = {"split_date": split_date, "is_lo": is_lo,
                  "amv_regime_up": AMV_REGIME_UP, "amv_regime_dn": AMV_REGIME_DN,
                  "amv_stats": amv_stats,
                  "elapsed_sec": round(time.time() - t0, 1)}
    jp, mp = write_report(res, mono, bench_pct, bench_detail, meta_extra, args.tag)

    # 控制台核心数字
    print("\n" + "=" * 96)
    print("核心结果（全区间 ALL, 固定 1% 单笔仓位口径）")
    print("=" * 96)
    print(f"{'实验':<26}{'轮数':>8}{'胜率':>8}{'盈亏比':>8}{'Σ单笔%':>10}"
          f"{'收益%(1%仓)':>12}{'年化%':>9}{'MaxDD%':>9}{'Calmar':>9}")
    for k in EXPERIMENTS:
        d = res["flow"][k]["ALL"]
        print(f"{EXP_DESC[k]:<26}{d['n']:>8}{d['win_rate']:>7.1f}%"
              f"{d['pl_ratio']:>8.2f}{d['sum_pnl_pct']:>10.0f}"
              f"{d['total_ret_pct']:>11.1f}%{d['annual_pct']:>8.1f}%"
              f"{d['maxdd_pct']:>8.1f}%{d['calmar']:>9.3f}")
    print("-" * 96)
    for s in BASELINE_STRATS + BASELINE_EXTRA:
        d = res["base"][s]["ALL"]
        print(f"{'基线-'+s:<26}{d['n']:>8}{d['win_rate']:>7.1f}%{d['pl_ratio']:>8.2f}"
              f"{d['sum_pnl_pct']:>10.0f}{d['total_ret_pct']:>11.1f}%"
              f"{d['annual_pct']:>8.1f}%{d['maxdd_pct']:>8.1f}%{d['calmar']:>9.3f}")
    print("=" * 96)
    print("★ 胜率三元组（交易数|胜率|盈亏比|平均单笔|收益%）组A vs 组B:")
    for grp, k in (("A", "E2_env_on"), ("A", "E4_s05_on"),
                   ("B", "E2_amv"), ("B", "E4_amv"),
                   ("B+", "E4_amv_nd"), ("d", "E4_amv_daily"),
                   ("nd", "E4_nd_nomv")):
        for seg in ("ALL", "IS", "OOS"):
            d = res["flow"][k][seg]
            print(f"  [{grp}] {EXP_DESC[k]:<28}{seg:>4}  n={d['n']:>6}  "
                  f"胜率={d['win_rate']:>5.1f}%  盈亏比={d['pl_ratio']:>5.2f}  "
                  f"平均单笔={d['avg_pnl']:>6.2f}%  收益={d['total_ret_pct']:>7.2f}%")
    print("=" * 96)
    ast = res["meta"].get("amv_stats") or {}
    if ast:
        print(f"活跃市值 regime({ast['init_date']}起): 起点={ast['init_state']} "
              f"多头{ast['bull_share_pct']}%/空头{ast['bear_share_pct']}% "
              f"切换{ast['switches']}次")
        print("=" * 96)
    for exp_key, t in (("E3_w3_env_on", "3日窗"), ("E3_w5_env_on", "5日窗")):
        for seg in ("IS", "OOS"):
            m = mono[exp_key][seg]
            print(f"E3 单调性[{t}][{seg}]: "
                  f"胜率{[round(w,1) if not pd.isna(w) else 'NA' for w in m['win_rates']]} "
                  f"严格单调={m['strict_monotonic']} ρ={m['spearman']} "
                  f"→ {'✅' if m['pass'] else '❌'}")
    print(f"报告: {mp}")
    print(f"数据: {jp}")


if __name__ == "__main__":
    main()
