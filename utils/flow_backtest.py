# -*- coding: utf-8 -*-
"""
流程回测器（F2 · 状态机 EMPTY→HOLD_B1→HOLD_B2→HOLD_B3→补票→离场）
==================================================================
按 docs/flow_redesign_2026-09-11.md B3 / A1 实现。

信号源（全部只读复用冻结内核，**不修改任何函数**）:
    signals = backtest.compute_signals(df)   # B1 入场 / B2 加仓（B3、单针序列消费端弃用）
    lines   = needle20.needle20_lines(df)    # 补票条件 白线≤20 & 红线≥CONFIG.NEEDLE_LONG_MIN
    brick   = brick.brick_chart(df)          # 翻红/翻绿/红砖序列
    macd    = resonance.macd_series(df)      # DIF/DEA/柱（与 macd_analysis 同口径）

A0 映射落实（消费端弃用，非改内核）:
    - signals["B3"]  弃用为买点 → 改为状态转移条件（5日涨≥8% & J≥80）
    - signals["单针"] 弃用全市场扫 → 改为流程内补票动作（lines 现算）

离场层分级优先级（v4，当日多触发按最高优先级计因）:
    1. 硬止损（v4: **收盘确认** close≤stop, 替代盘中 lo≤stop）→ 清仓
    2. 补票止损（漏下去 3%/5%）→ 仅平补票段
    3. ★v4 新增 3 条知识库原有离场规则（最高优先级, 在硬止损之后/减仓之前）:
       3a. 前高止盈（h≥前高; 放量突破前高→放飞抬高止盈线, 否则止盈离场）
       3b. 连续 2 日收盘跌破白线 → 离场
       3c. 3 个交易日内不恢复上涨（收盘≤入场价）→ 离场
    4. 红翻绿 → 减仓 50%（v4 移除「红砖缩短」减仓, 见下）
    5. 四分之三阴量线（预警→减仓 40%；次日放量阴线确认→清仓）   [exit_enhanced]
    6. 顶部大风车（确认→清仓；孤立预警→减仓 40%）               [exit_enhanced]
    7. 连续 4 红砖（减仓 30%，非清仓，brick_rules.md:35-38）
    8. MACD 顶背离 / 死叉预警（减仓 —— 解释见下）
    9. 白线有效跌破黄线（2 日确认）→ 清仓
   10. 跌破前低 / 到期（B1 30日 / B2 15日）→ 清仓

    ★ v4 说明: 原「红砖缩短」减仓(砖型图逐日回落即减)触发过频(占全部减仓腿绝大多数),
      据知识库「连续4根红砖后才减仓」改为**仅保留第7级「连续4红砖减仓」**,
      红砖缩短不再单独触发（见报告「本次修正说明」）。

    ★ 与设计文档的偏差声明: 设计对 4/5 明确"减仓预警"、对 7 仅写"预警"。
      本实现把 7 也按**减仓**处理（非清仓）——理由: 白线破黄线(8)已是硬离场，
      MACD 死叉若做清仓会使流程高频换手失真。该解释在报告口径节标注。

前视防护: 当日判定（共振分/环境层/补票/离场）只用 T 及以前数据；
入场执行价 = 信号日次日开盘（T+1 open）；共振分层键取信号日（= 入场日 T-1）。

组合层关系: 输出事件级 FlowTrade；资金分配由 v1 portfolio_backtest 调度，
tranche 权重只决定单只内部三段比例。两层解耦。

资金台账（比例记账）: 初始现金 1.0。开段扣 w*(1+fee)，平段回笼 w*e*(1-fee)
（e = exit/entry）。**守恒不变量: cash == 1.0 + realized（精确成立，±1e-9）**，
且任一时刻 cash ≥ -1e-9。单测据此断言。
"""
import os
import sys
from dataclasses import dataclass, field
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from utils.backtest import compute_signals, TRADE_COST   # 冻结内核只读
from utils.brick import brick_chart                       # 冻结内核只读
from utils.indicators import white_line, yellow_line, kdj_j  # backtest.py 同源
from utils.needle20 import needle20_lines
from utils.strategy_config import CONFIG
from utils.resonance import (resonance_matrix, presence_matrix, macd_series,
                             score_at, bucket)
from utils.vol_price import three_quarter_yin_volume, top_windmill, key_k

SIDEWAYS = "SIDEWAYS"


class FlowState(Enum):
    """流程状态机状态。"""
    EMPTY = "EMPTY"
    HOLD_B1 = "HOLD_B1"
    HOLD_B2 = "HOLD_B2"
    HOLD_B3 = "HOLD_B3"


@dataclass
class FlowConfig:
    """流程回测参数（只落在本 dataclass，**绝不回写 strategy_config**）。"""
    env_macd_filter: bool = True      # 环境层: DIF>0 + 白线>黄线（设计 D8）
    resonance_on: bool = False        # E2=False / E3,E4=True
    needle_patch: bool = False        # 补票开关（E4=True）
    needle_stop_pct: float = 0.05     # 补票止损 3%/5% 两档
    needle_max_count: int = 1         # 每轮补票上限（知识库未明说 → 默认1, 待用户确认）
    tranche: dict = field(default_factory=lambda: {"B1": 0.6, "B2": 0.4, "needle": 0.0})
    b2_vol_mult: float = 1.8          # B2 倍量确认（信号后置过滤, 不改冻结内核）
    max_hold: dict = field(default_factory=lambda: {"B1": 30, "B2": 15})
    exit_enhanced: bool = False       # 离场增强: 四分之三阴量线/顶部大风车（默认关, 保基线可比）
    resonance_window: int = 5         # v3 共振时间窗（交易日根数）: 3/5; 1=同日口径
    warn_reduce_frac: float = 0.4     # MACD 预警级减仓比例（设计 30~50%，取 0.4）
    b3_gain_min: float = 0.08         # B3 转移: 5日涨≥8%
    b3_j_min: float = 80.0            # B3 转移: J≥80
    fee_half: float = 0.0             # 单边费率; ≤0 时按 backtest.TRADE_COST/2
    # ---- v4: 3 条知识库原有离场规则（最高优先级, 硬止损之后 / 减仓之前）----
    exit_prevhigh: bool = True        # 前高止盈（放量突破→放飞抬高止盈线）
    exit_breakwl: bool = True         # 连续2日收盘跌破白线 → 离场
    exit_nogain3: bool = True         # 3个交易日内不恢复上涨就走
    stop_close_confirm: bool = True   # 止损改收盘确认: close≤stop（替代盘中 lo≤stop）
    # ---- v5: 环境过滤 ----
    amv_filter: bool = False          # 活跃市值「非空头区间」才允许买入
    amv_mode: str = "regime"          # "regime"(+4%转多/-2.3%转空) | "daily"(当日>-2.3%)
    needle_bull: bool = False         # 单针补票补「白线>黄线」(与其它战法对齐)

    def __post_init__(self):
        if self.fee_half <= 0:
            self.fee_half = TRADE_COST / 2.0   # 往返成本对半拆到单边


@dataclass
class FlowTrade:
    """单标的单轮流程交易（事件级）。tranches 三段独立记账。"""
    code: str
    name: str
    b1_signal_date: str
    b1_entry_date: str
    b1_entry_price: float
    b2_entry_date: object = None
    b2_entry_price: float = None
    needle_entry_date: object = None
    needle_entry_price: float = None
    exit_date: str = ""
    exit_reason: str = ""
    tranches: list = field(default_factory=list)
    resonance_bucket: str = "0"       # "0"/"1"/"2"/"3+" ← E3 分层键
    regime_entry: str = SIDEWAYS      # 大盘状态（lag=1，沿用 v1 market_regime）
    b3_date: object = None            # 状态升级日（不新增资金）

    @property
    def pnl_pct(self):
        """流程轮合计收益率%（按 tranche 权重加权，已扣费）。"""
        tot = 0.0
        wsum = 0.0
        for t in self.tranches:
            tot += t["qty_w"] * t["pnl_pct"]
            wsum += t["qty_w"]
        return round(tot / wsum, 4) if wsum else 0.0

    @property
    def invested_w(self):
        """本轮投入权重合计。"""
        return round(sum(t["qty_w"] for t in self.tranches), 6)


# ============================================================
# 内部工具
# ============================================================

def _as_indexed(df):
    """防御式归一: 保证 DatetimeIndex 升序（防 1970 塌缩事故复发）。"""
    if df is None or len(df) == 0:
        raise ValueError("flow_backtest: 空 DataFrame")
    if not isinstance(df.index, pd.DatetimeIndex):
        d = df.copy()
        if "date" in d.columns:
            d.index = pd.to_datetime(d["date"])
            d = d.drop(columns=["date"])
        else:
            d.index = pd.to_datetime(d.index)
        if d.index.min().year < 1990:
            raise ValueError(f"flow_backtest: 日期索引异常(疑似1970塌缩) {d.index.min()}")
        df = d
    return df.sort_index()


def _s(x):
    """Timestamp/str → 'YYYY-MM-DD'。"""
    return str(pd.Timestamp(x))[:10]


def _prev_high(h, entry_idx, entry_price):
    """前高止盈基准（与冻结内核 backtest._find_exit 同口径）。

    前高 = 信号日前 60 日内最高价（不含信号日）, 且至少高于入场价 5%
    （避免止盈价过低）。信号日索引 = entry_idx - 1（入场在 T+1 开盘）。
    """
    if entry_idx > 1:
        seg = h.iloc[max(0, entry_idx - 60):entry_idx - 1]
        ph = float(seg.max()) if len(seg) else entry_price * 1.2
    else:
        seg = h.iloc[:entry_idx]
        ph = float(seg.max()) if len(seg) else entry_price * 1.2
    return max(ph, entry_price * 1.05)


# ============================================================
# 状态机
# ============================================================

def run_flow_backtest(df, code, name, cfg, rules, regime_fn=None):
    """单标的流程状态机（设计 B3 签名）。

    Args:
        df: 单只日K（DatetimeIndex 或含 date 列；open/high/low/close/volume）。
        code/name: 标的代码与名称。
        cfg: FlowConfig。
        rules: make_exit_rules() 输出（max_hold 兜底来源）。
        regime_fn: 可选 callable(entry_date_str) -> str，打 regime_entry 标。

    Returns:
        list[FlowTrade]（按入场时间升序）。
    """
    trades, _ = run_flow_backtest_ledger(df, code, name, cfg, rules, regime_fn=regime_fn)
    return trades


def _prepare_block(df, cfg, want_resonance=True, amv_state=None):
    """预计算单标的全部信号/指标序列（供多配置复用，避免重复计算）。

    F3 实验矩阵要对同一只股票跑多个配置（E2/E3/E4 × env 开关 × AMV 过滤），
    指标计算（compute_signals/brick/needle20/macd/共振矩阵）是主要开销，
    与配置无关 → 只算一次，状态机循环按配置各自跑。

    Args:
        amv_state: 可选，活跃市值状态表（index=日期, 列 ["regime","daily"] 布尔;
                   v5 用）。按各股票交易日 reindex+ffill 对齐（防前视: 取
                   signal 日 T 及以前已知的最近状态）。None → 不做 AMV 过滤。

    Returns:
        dict: 全部逐日 Series 与价格序列。
    """
    df = _as_indexed(df)
    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    idx = df.index

    sig = compute_signals(df)
    b1_sig = sig["B1"].fillna(False).astype(bool)
    b2_sig = sig["B2"].fillna(False).astype(bool)
    # A0 映射: sig["B3"] / sig["单针"] 消费端弃用（不作买点），不取用
    wx = white_line(c)               # 白线（知行趋势线, 价格级）
    yx = yellow_line(c)              # 黄线（知行多空线, 价格级）
    _, _, jv = kdj_j(h, l, c)        # KDJ J
    lines = needle20_lines(df)
    wl = lines["短期"]               # 白线（单针四线, 0-100 百分位）
    rl = lines["长期"]               # 红线
    brick = brick_chart(df)
    b_xd = brick["翻绿XD"].fillna(False).astype(bool)
    b_up = brick["红升"].fillna(False).astype(bool)
    b_val = brick["砖型图"].astype(float)
    macd = macd_series(df)
    dif, dea = macd["dif"], macd["dea"]

    v20 = v.rolling(20, min_periods=20).mean()      # B2 倍量确认基准
    v5 = v.rolling(5, min_periods=5).mean()         # 确认级放量基准
    kk_low = key_k(df)["关键K低点"]                  # B2 tranche 止损锚

    # 离场增强序列（四分之三阴量线/顶部大风车）
    # ★ 预编译真实序列（不按 cfg 门控）——多配置共用同一 block 时,
    #   由 run_flow_backtest_ledger 按各 cfg.exit_enhanced 在运行期门控。
    yin34 = three_quarter_yin_volume(df)
    tw = top_windmill(df)
    wm_warn, wm_conf = tw["大风车"], tw["大风车确认"]
    heavy_yin = ((c < o) & (v > v5 * 1.5)).fillna(False).astype(bool)

    res_base = resonance_matrix(df) if want_resonance else None
    # v3 时间窗口径: 预编译 1/3/5 日三种「曾出现」矩阵（供多配置复用）
    res_mats = {}
    if res_base is not None:
        for wn in (1, 3, 5):
            res_mats[wn] = presence_matrix(res_base, wn)
    win = int(getattr(cfg, "resonance_window", 5))
    res_matrix = res_mats.get(win, res_mats.get(5)) if res_mats else None
    bull_env = (wx > yx)
    env_ok = (bull_env & (dif > 0)) if cfg.env_macd_filter else bull_env

    # v5 活跃市值状态对齐（防前视: reindex 到本股票交易日, ffill 取 T 及以前的最近值）
    amv_regime = amv_daily = None
    if amv_state is not None and len(amv_state):
        try:
            aligned = amv_state.reindex(idx.union(amv_state.index)).ffill().reindex(idx)
            amv_regime = aligned["regime"].fillna(False).astype(bool)
            amv_daily = aligned["daily"].fillna(False).astype(bool)
        except Exception:
            amv_regime = amv_daily = None

    return {"o": o, "h": h, "l": l, "c": c, "v": v, "idx": idx,
            "signals": sig,   # 完整五路信号 dict（E1 基线策略 IS/OOS 分段复用）
            "b1_sig": b1_sig, "b2_sig": b2_sig,
            "wx": wx, "yx": yx, "jv": jv, "wl": wl, "rl": rl,
            "b_xd": b_xd, "b_up": b_up, "b_val": b_val,
            "dif": dif, "dea": dea, "v20": v20, "v5": v5,
            "kk_low": kk_low, "yin34": yin34,
            "wm_warn": wm_warn, "wm_conf": wm_conf,
            "heavy_yin": heavy_yin, "res_matrix": res_matrix,
            "res_mats": res_mats,   # {1,3,5} 窗口矩阵，供多配置按 cfg.resonance_window 取用
            "bull_ws": bull_env,    # 白线>黄线（裸口径, needle_bull 用）
            "amv_regime": amv_regime, "amv_daily": amv_daily,
            "env_ok": env_ok}


def run_flow_backtest_ledger(df, code, name, cfg, rules, regime_fn=None,
                             block=None):
    """同 run_flow_backtest，额外返回资金台账（供守恒断言/单测）。

    Args:
        block: 可选，``_prepare_block()`` 的输出（多配置复用时传入避免重算）。

    Returns:
        (list[FlowTrade], dict)  dict = {"cash", "realized", "n_rounds",
                                         "n_tranches", "violations"}
    """
    if block is None:
        block = _prepare_block(df, cfg, want_resonance=cfg.resonance_on)
    n = len(block["idx"])
    empty_ledger = {"cash": 1.0, "realized": 0.0, "n_rounds": 0,
                    "n_tranches": 0, "violations": []}
    if n < 35:
        return [], empty_ledger

    o, h, l, c, v = block["o"], block["h"], block["l"], block["c"], block["v"]
    idx = block["idx"]
    b1_sig, b2_sig = block["b1_sig"], block["b2_sig"]
    wx, yx, jv = block["wx"], block["yx"], block["jv"]
    wl, rl = block["wl"], block["rl"]
    b_xd, b_up, b_val = block["b_xd"], block["b_up"], block["b_val"]
    dif, dea = block["dif"], block["dea"]
    v20, kk_low = block["v20"], block["kk_low"]
    res_matrix = block["res_matrix"]
    env_ok = block["env_ok"]

    fee = float(cfg.fee_half)
    warn_frac = float(np.clip(cfg.warn_reduce_frac, 0.0, 1.0))

    # v5 活跃市值门控（运行期, 按各 cfg.amv_filter/amv_mode 生效; 防前视: signal 日状态）
    if cfg.amv_filter:
        amv = block.get("amv_regime" if cfg.amv_mode == "regime" else "amv_daily")
        if amv is not None:
            env_ok = env_ok & amv.astype(bool)

    X = {"c": c, "h": h, "l": l, "v": v, "v20": block["v20"],
         "b_xd": b_xd, "b_up": b_up, "b_val": b_val,
         "dif": dif, "dea": dea, "wx": wx, "yx": yx,
         "bull_ws": block.get("bull_ws", (wx > yx)),
         "yin34": block["yin34"], "heavy_yin": block["heavy_yin"],
         "wm_warn": block["wm_warn"], "wm_conf": block["wm_conf"]}
    # 离场增强（四分之三阴量线/顶部大风车）按配置开关在运行期门控:
    # block 预编译了真实序列, 但仅当 cfg.exit_enhanced 才启用（否则置全 False,
    # 保证默认行为与基线一致, 且支持同一 block 上多配置按需启用）
    if not cfg.exit_enhanced:
        _false = pd.Series(False, index=idx)
        X["yin34"] = _false
        X["heavy_yin"] = _false
        X["wm_warn"] = _false
        X["wm_conf"] = _false

    trades = []
    ledger = {"cash": 1.0, "realized": 0.0, "n_rounds": 0, "n_tranches": 0,
              "violations": []}
    cash, realized = 1.0, 0.0
    pos = None

    def _entry(kind, w, price, dt, i, stop):
        """开一段仓（扣现金），返回 open tranche。"""
        nonlocal cash
        w = float(w)
        if w <= 0 or price <= 0:
            return None
        cash -= w * (1 + fee)
        if cash < -1e-9:
            ledger["violations"].append(f"{_s(dt)} {kind} 现金透支 cash={cash:.6f}")
        return {"kind": kind, "qty_w": w, "entry": float(price), "entry_date": _s(dt),
                "entry_idx": int(i), "stop": float(stop) if stop else None}

    def _close(tr, exit_price, dt, reason):
        """平一段仓（或部分平），回笼现金，落已实现盈亏。"""
        nonlocal cash, realized
        w = float(tr["qty_w"])
        e = float(exit_price) / tr["entry"]
        returned = w * e * (1 - fee)
        invested = w * (1 + fee)
        cash += returned
        pnl_pct = (returned / invested - 1) * 100
        realized += returned - invested
        return {"kind": tr["kind"], "qty_w": round(w, 6),
                "entry": round(tr["entry"], 4), "exit": round(float(exit_price), 4),
                "pnl_pct": round(pnl_pct, 4), "exit_date": _s(dt), "reason": reason}

    def _reduce(tr, frac, price, dt, reason):
        """部分平仓：按 frac 缩减 qty_w，剩余留仓。"""
        frac = float(np.clip(frac, 0.0, 1.0))
        if frac <= 0 or tr["qty_w"] <= 1e-9:
            return None
        part = dict(tr)
        part["qty_w"] = tr["qty_w"] * frac
        closed = _close(part, price, dt, reason)
        tr["qty_w"] -= part["qty_w"]
        return closed

    for i in range(n - 1):
        today = idx[i]
        nx_open = float(o.iloc[i + 1])

        # ---------- ① 离场评估（先于建仓）----------
        if pos is not None and i >= pos["active_from"]:
            action, reason, price, frac = _eval_exit(i, pos, pos["opens"], X, cfg)
            if action == "exit_full":
                for tr in list(pos["opens"]):
                    pos["closed"].append(_close(tr, price, today, reason))
                trades.append(_make_trade(pos, code, name, today, reason, regime_fn))
                pos = None
            elif action == "close_needle":
                for tr in list(pos["opens"]):
                    if tr["kind"] == "needle":
                        pos["closed"].append(_close(tr, price, today, reason))
                        pos["opens"].remove(tr)
                if not pos["opens"]:
                    trades.append(_make_trade(pos, code, name, today, reason, regime_fn))
                    pos = None
            elif action == "reduce":
                # 同一原因每轮只减一次（防同一持续形态逐日把仓位磨光 → 持有段过短）
                if reason in pos["reduced"]:
                    pass
                else:
                    tgt = _pick_reduce_target(pos["opens"])
                    if tgt is not None:
                        closed = _reduce(tgt, frac if frac else warn_frac,
                                         price, today, reason)
                        if closed:
                            pos["closed"].append(closed)
                        pos["reduced"].add(reason)
                        if all(t["qty_w"] <= 1e-9 for t in pos["opens"]):
                            trades.append(_make_trade(pos, code, name, today, reason,
                                                      regime_fn))
                            pos = None

        # ---------- ② 建仓 / 状态转移 ----------
        if pos is None:
            if bool(b1_sig.iloc[i]) and bool(env_ok.iloc[i]):
                w = float(cfg.tranche.get("B1", 0.6))
                res = score_at(res_matrix, idx[i + 1], lag=1) \
                    if res_matrix is not None else (0, [])
                pos = {
                    "state": FlowState.HOLD_B1,
                    "b1_signal_date": _s(today),
                    "b1_entry_date": _s(idx[i + 1]),
                    "b1_entry_price": nx_open,
                    "b1_entry_idx": i + 1,
                    "b1_stop": float(l.iloc[i]),          # B1 信号日低点
                    "b2_entry_date": None, "b2_entry_price": None,
                    "b2_stop": None, "b2_entry_idx": None,
                    "b3_date": None,
                    "needle_entry_date": None, "needle_entry_price": None,
                    "needle_count": 0, "warn_prev": False, "reduced": set(),
                    "resonance_bucket": bucket(res[0]),
                    "prev_high": _prev_high(h, i + 1, nx_open),  # v4 前高止盈基准
                    "opens": [], "closed": [],
                    "active_from": i + 1,
                }
                tr = _entry("B1", w, nx_open, idx[i + 1], i + 1, pos["b1_stop"])
                if tr:
                    pos["opens"].append(tr)
        else:
            # B2 加仓（仅 HOLD_B1；信号自带 b1_prev 前提 + 倍量后置过滤）
            if pos["state"] == FlowState.HOLD_B1 and pos["b2_entry_date"] is None:
                v20_i = v20.iloc[i]
                vol_ok = bool(v.iloc[i] > v20_i * cfg.b2_vol_mult) \
                    if not np.isnan(v20_i) else False
                if bool(b2_sig.iloc[i]) and vol_ok:
                    stop = float(kk_low.iloc[i]) if not np.isnan(kk_low.iloc[i]) \
                        else float(l.iloc[i])             # 关键K低点优先
                    pos["b2_entry_date"] = _s(idx[i + 1])
                    pos["b2_entry_price"] = nx_open
                    pos["b2_stop"] = stop
                    pos["b2_entry_idx"] = i + 1
                    pos["state"] = FlowState.HOLD_B2
                    tr = _entry("B2", float(cfg.tranche.get("B2", 0.4)),
                                nx_open, idx[i + 1], i + 1, stop)
                    if tr:
                        pos["opens"].append(tr)

            # B3 状态升级（不新增资金；5日涨≥8% & J≥80）
            if pos["state"] == FlowState.HOLD_B2:
                c5 = (c.iloc[i] > c.iloc[i - 5] * (1 + cfg.b3_gain_min)) if i >= 5 else False
                if c5 and bool(jv.iloc[i] >= cfg.b3_j_min):
                    pos["state"] = FlowState.HOLD_B3
                    pos["b3_date"] = _s(today)

            # 单针补票（白线≤20 & 红线≥CONFIG.NEEDLE_LONG_MIN；每轮限 needle_max_count）
            # v5b: cfg.needle_bull=True 时补「白线>黄线」硬前提（与其它战法对齐）
            needle_bull_ok = (not cfg.needle_bull) or bool(X["bull_ws"].iloc[i])
            if (cfg.needle_patch and pos["needle_entry_date"] is None
                    and pos["needle_count"] < cfg.needle_max_count
                    and float(cfg.tranche.get("needle", 0.0)) > 0
                    and needle_bull_ok
                    and bool(wl.iloc[i] <= 20) and bool(rl.iloc[i] >= CONFIG.NEEDLE_LONG_MIN)):
                stop = nx_open * (1 - cfg.needle_stop_pct)
                pos["needle_entry_date"] = _s(idx[i + 1])
                pos["needle_entry_price"] = nx_open
                pos["needle_count"] += 1
                tr = _entry("needle", float(cfg.tranche.get("needle", 0.0)),
                            nx_open, idx[i + 1], i + 1, stop)
                if tr:
                    pos["opens"].append(tr)

        # ---------- ③ 预警状态推进（阴量线: 预警次日放量阴线 → 确认清仓）----------
        if pos is not None:
            pos["warn_prev"] = bool(X["yin34"].iloc[i])

        # ---------- ④ 数据耗尽兜底平仓 ----------
        if pos is not None and i == n - 2:
            px = float(c.iloc[i])
            for tr in list(pos["opens"]):
                pos["closed"].append(_close(tr, px, today, "数据结束平仓"))
            trades.append(_make_trade(pos, code, name, today, "数据结束平仓", regime_fn))
            pos = None

    ledger["cash"] = round(cash, 10)
    ledger["realized"] = round(realized, 10)
    ledger["n_rounds"] = len(trades)
    ledger["n_tranches"] = sum(len(t.tranches) for t in trades)
    return trades, ledger


def _eval_exit(i, pos, opens, X, cfg):
    """离场层分级评估（v4：补 3 条知识库离场规则 + 收盘确认止损 + 连续4红砖）。

    优先级（当日多触发按最高优先级计因）:
      清仓: 硬止损(收盘确认) / 补票止损 /
            ★前高止盈 / ★连续2日收盘跌破白线 / ★3日不涨 /
            3-4阴量线·大风车【确认】 / 白线破黄线 / 跌破前低 / 到期
      减仓: 红翻绿 50% / 连续4红砖 30% /
            3-4阴量线·大风车【预警】40% / MACD顶背离·死叉 cfg.warn_reduce_frac

    ★ v4 变更:
      (1) 第 1 级硬止损由盘中 lo≤stop 改为**收盘确认** close≤stop（防插针假破位）。
      (2) 新增 3a/3b/3c 三条知识库原规则（在硬止损之后、减仓之前）:
          - 前高止盈: h≥前高; 放量突破前高→放飞抬高止盈线; 否则止盈离场
          - 连续2日收盘跌破白线: c[i]<白线[i] 且 c[i-1]<白线[i-1] → 离场
          - 3日不涨: 入场后第 3 个交易日收盘仍≤入场价 → 离场
      (3) 移除「红砖缩短」减仓（触发过频），仅保留「连续4红砖」减仓（第7级）。

    Returns:
        (action, reason, price, frac)
          action ∈ {"exit_full", "close_needle", "reduce", "none"}
          frac: 减仓比例（仅 action=="reduce" 有效，否则 None）
    """
    lo = float(X["l"].iloc[i])
    px = float(X["c"].iloc[i])
    entry_idx = pos.get("b1_entry_idx")

    # 1. 硬止损（B1=信号日低点；B2/B3/补票=关键K低点）
    #    v4: 收盘确认 close≤stop（原盘中 lo≤stop）。命中即按收盘价成交（如实、偏保守）。
    stops = [(t["stop"], t["kind"]) for t in opens if t.get("stop")]
    if stops:
        hit = min(s for s, _ in stops)
        hit_now = (px <= hit) if cfg.stop_close_confirm else (lo <= hit)
        if hit_now:
            reason = "收盘止损" if cfg.stop_close_confirm else "盘中止损"
            if any(k != "B1" for s, k in stops if s == hit):
                reason = "收盘跌破关键K" if cfg.stop_close_confirm else "跌破关键K止损"
            fill = px if cfg.stop_close_confirm else hit
            return "exit_full", reason, fill, None

    # 2. 补票止损（needle 段独立止损，漏下去 3%/5%）→ 仅平补票段
    #    v4: 同样收盘确认（与第 1 级口径一致）
    for t in opens:
        if t["kind"] == "needle" and t.get("stop"):
            trig = (px <= t["stop"]) if cfg.stop_close_confirm else (lo <= t["stop"])
            if trig:
                return "close_needle", "补票止损", (px if cfg.stop_close_confirm
                                                else t["stop"]), None

    # 3. ★v4 新增: 3 条知识库原有离场规则（清仓）——
    #    与冻结内核 backtest._find_exit 的 B1 规则同口径
    # 3a. 前高止盈（放量突破前高 → 放飞抬高止盈线; 否则止盈离场）
    ph = pos.get("prev_high")
    if cfg.exit_prevhigh and ph:
        if float(X["h"].iloc[i]) >= ph:
            vol_break = (i > (entry_idx or 0) and px > ph
                         and float(X["v"].iloc[i]) > float(X["v20"].iloc[i]))
            if vol_break:
                pos["prev_high"] = max(ph, px * 1.02)   # 放飞, 继续持有
            else:
                return "exit_full", "前高止盈", ph, None
    # 3b. 连续 2 日收盘跌破白线 → 离场
    if cfg.exit_breakwl and entry_idx is not None and i > entry_idx + 1:
        if (float(X["c"].iloc[i]) < float(X["wx"].iloc[i])
                and float(X["c"].iloc[i - 1]) < float(X["wx"].iloc[i - 1])):
            return "exit_full", "破白线离场", px, None
    # 3c. 3 个交易日内不恢复上涨（入场后第 3 个交易日收盘仍≤入场价）→ 离场
    if cfg.exit_nogain3 and entry_idx is not None and i == entry_idx + 3:
        if px <= pos.get("b1_entry_price", 0.0):
            return "exit_full", "3日不涨离场", px, None

    # 4. 红翻绿 → 减仓 50%（v4 移除「红砖缩短」减仓, 见模块 docstring）
    if bool(X["b_xd"].iloc[i]):
        return "reduce", "红翻绿减仓", px, 0.50

    # 5. 四分之三阴量线（S1: 预警→减仓 40%；次日放量阴线确认→清仓）
    if X["heavy_yin"].iloc[i] and pos.get("warn_prev"):
        return "exit_full", "四分之三阴量线确认", px, None
    if bool(X["yin34"].iloc[i]):
        return "reduce", "四分之三阴量线预警", px, 0.40

    # 6. 顶部大风车（确认→清仓；孤立预警→减仓 40%）
    if bool(X["wm_conf"].iloc[i]):
        return "exit_full", "顶部大风车S1确认", px, None
    if bool(X["wm_warn"].iloc[i]):
        return "reduce", "顶部大风车预警", px, 0.40

    # 7. 连续 4 红砖 → 减仓 30%（brick_rules.md:35-38, 非清仓）
    if i >= 3 and all(bool(X["b_up"].iloc[i - k]) for k in range(4)):
        return "reduce", "连续4红砖减仓", px, 0.30

    # 8. MACD 顶背离 / 死叉预警 → 减仓（解释见模块 docstring）
    if i >= 10 and px > float(X["c"].iloc[i - 10]) \
            and float(X["dif"].iloc[i]) < float(X["dif"].iloc[i - 5]):
        return "reduce", "MACD顶背离预警", px, float(cfg.warn_reduce_frac)
    if i >= 1 and float(X["dif"].iloc[i]) < float(X["dea"].iloc[i]) \
            and float(X["dif"].iloc[i - 1]) >= float(X["dea"].iloc[i - 1]):
        return "reduce", "MACD死叉预警", px, float(cfg.warn_reduce_frac)

    # 9. 白线有效跌破黄线（2 日确认）→ 清仓（终极）
    if i >= 1 and bool(X["wx"].iloc[i] < X["yx"].iloc[i]) \
            and bool(X["wx"].iloc[i - 1] < X["yx"].iloc[i - 1]):
        return "exit_full", "白线破黄线止损", px, None

    # 10. 跌破前低 / 到期 → 清仓
    if i >= 21 and px < float(X["l"].iloc[i - 20:i].min()):
        return "exit_full", "跌破前低", px, None
    if entry_idx is not None \
            and i - entry_idx >= int(cfg.max_hold.get("B1", 30)):
        return "exit_full", "到期离场", px, None
    if pos.get("b2_entry_idx") is not None \
            and i - pos["b2_entry_idx"] >= int(cfg.max_hold.get("B2", 15)):
        return "exit_full", "到期离场", px, None

    return "none", "", px, None


def _pick_reduce_target(opens):
    """减仓目标段：预警类优先减试错仓 B1；无 B1 则减最大权重段。"""
    for t in opens:
        if t["kind"] == "B1" and t["qty_w"] > 1e-9:
            return t
    cand = [t for t in opens if t["qty_w"] > 1e-9]
    return max(cand, key=lambda t: t["qty_w"]) if cand else None


def _make_trade(pos, code, name, exit_dt, reason, regime_fn):
    """由持仓状态构造 FlowTrade。"""
    regime = SIDEWAYS
    if regime_fn is not None:
        try:
            regime = str(regime_fn(pos["b1_entry_date"]))
        except Exception:
            regime = SIDEWAYS
    return FlowTrade(
        code=code, name=name,
        b1_signal_date=pos["b1_signal_date"],
        b1_entry_date=pos["b1_entry_date"],
        b1_entry_price=round(pos["b1_entry_price"], 4),
        b2_entry_date=pos["b2_entry_date"],
        b2_entry_price=round(pos["b2_entry_price"], 4) if pos["b2_entry_price"] else None,
        needle_entry_date=pos["needle_entry_date"],
        needle_entry_price=round(pos["needle_entry_price"], 4) if pos["needle_entry_price"] else None,
        exit_date=_s(exit_dt), exit_reason=reason,
        tranches=pos["closed"],
        resonance_bucket=pos["resonance_bucket"],
        regime_entry=regime,
        b3_date=pos.get("b3_date"),
    )


def _is_yin(df):
    """阴线判定（内部小工具）。"""
    return df["close"].astype(float) < df["open"].astype(float)
