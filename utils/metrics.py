# -*- coding: utf-8 -*-
"""组合层绩效指标计算(metrics)
================================
架构师要求独立成文件：`metrics.py` 同时被 `portfolio_backtest` 与 `backtest_grid` 依赖，
独立可测、零业务依赖（只用标准库 + numpy），避免与回测逻辑耦合。

【输入契约】
    equity_curve : list[(date_str, equity)] —— 组合每日净值曲线，**必须含空仓日**
                   （空仓日 equity 不变 → 日收益 0，这是 Sharpe 正确性的前提；
                    若只保留交易日会高估波动率、低估交易天数）。
    closed       : list[dict] —— 已平仓记录，需含 "pnl_amt"(金额) 与 "pnl_pct"(百分比)，
                   或退化字段 "盈亏"(金额)。胜率/盈亏比按**金额**口径统计。

【口径声明】
  - sharpe   : 日收益序列 ddof=1 标准差 → ×√252；rf 为**年化**无风险利率，先折算到日。
  - max_drawdown : 在净值曲线上取"峰值→谷值"最大回撤(正数百分比)。
  - calmar   : 年化收益率 / MaxDD(%)；**MaxDD=0 时返回 None**（除零无意义，不能返回 inf）。
  - 所有返回百分比的函数统一用 "百分比数值"（12.34 表示 12.34%），与 backtest.py 的 收益 字段一致。

用法:
    from utils.metrics import full_report
    rep = full_report(equity_curve, closed_trades, years=2.5)
"""
from __future__ import annotations

import math
from typing import Optional, Sequence, Union

import numpy as np

# 年化交易日数（A 股惯例；Sharpe 与年化收益共用同一口径，保证可比）
TRADING_DAYS_PER_YEAR: int = 252

# 曲线元素类型别名： (日期字符串, 净值)
CurvePoint = Sequence  # (str, float)
Curve = Sequence[CurvePoint]

_EPS = 1e-12  # 浮点比较容差


# ============================================================
# 曲线基础工具
# ============================================================
def _equities(curve: Curve) -> np.ndarray:
    """从净值曲线抽取净值数组（失败/空 → 空数组）。

    Args:
        curve: [(date, equity), ...]。

    Returns:
        np.ndarray: float 净值序列（保持原顺序）。
    """
    if not curve:
        return np.asarray([], dtype=float)
    vals = []
    for point in curve:
        try:
            vals.append(float(point[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return np.asarray(vals, dtype=float)


def daily_returns(curve: Curve) -> np.ndarray:
    """日收益序列（简单收益 pct_change），**含空仓的 0 收益日**。

    Args:
        curve: [(date, equity), ...]。

    Returns:
        np.ndarray: 长度 = len(curve) - 1 的日收益小数序列。
        净值<=0 的异常点跳过（记为 0），避免除零与 inf。
    """
    eq = _equities(curve)
    if len(eq) < 2:
        return np.asarray([], dtype=float)
    prev = eq[:-1]
    cur = eq[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.where(np.abs(prev) > _EPS, cur / prev - 1.0, 0.0)
    rets = np.where(np.isfinite(rets), rets, 0.0)
    return rets.astype(float)


def total_return(curve: Curve) -> float:
    """总收益率(%) = (末值 - 初值) / 初值 × 100。

    Args:
        curve: 净值曲线。

    Returns:
        float: 总收益率百分比；曲线为空或初值<=0 时返回 0.0。
    """
    eq = _equities(curve)
    if len(eq) < 2 or abs(eq[0]) <= _EPS:
        return 0.0
    return float((eq[-1] / eq[0] - 1.0) * 100.0)


def annualized(curve: Curve, years: Optional[float] = None) -> float:
    """年化收益率(%)。优先用几何年化，years 非法时退化为按交易日折算。

    Args:
        curve: 净值曲线。
        years: 回测年数(>0)。为 None 时用 len(curve)/252 估算。

    Returns:
        float: 年化收益率百分比；不足 2 点时返回 0.0。
        亏损超过 100%（末值<=0）时返回 -100.0（封底，避免复数幂）。
    """
    eq = _equities(curve)
    if len(eq) < 2 or abs(eq[0]) <= _EPS:
        return 0.0
    ratio = eq[-1] / eq[0]
    if ratio <= 0:
        return -100.0
    if years is None or years <= _EPS:
        years = max(len(eq) / float(TRADING_DAYS_PER_YEAR), 1.0 / TRADING_DAYS_PER_YEAR)
    return float((ratio ** (1.0 / years) - 1.0) * 100.0)


def max_drawdown(curve: Curve) -> float:
    """最大回撤(%)：净值曲线上"历史峰值 → 后续谷值"的最大跌幅(正数)。

    Args:
        curve: 净值曲线。

    Returns:
        float: 最大回撤百分比（0 表示无回撤）；曲线为空或全非正时返回 0.0。
    """
    eq = _equities(curve)
    if len(eq) < 2:
        return 0.0
    peak = -np.inf
    worst = 0.0
    for v in eq:
        if v > peak:
            peak = v
        if peak > _EPS:
            dd = (peak - v) / peak
            if dd > worst:
                worst = dd
    return float(worst * 100.0)


def sharpe(curve: Curve, rf: float = 0.0, periods: int = TRADING_DAYS_PER_YEAR) -> Optional[float]:
    """夏普比率 = 日超额收益均值 / 日收益标准差(ddof=1) × √periods。

    Args:
        curve: 净值曲线（**必须含空仓日**，空仓日日收益记为 0）。
        rf: **年化**无风险利率小数（如 0.02 = 2%），内部折算为日利率 rf/periods。
        periods: 年化周期数，A 股默认 252。

    Returns:
        Optional[float]: 夏普比率；当样本不足 2 日或日波动为 0（如全程空仓）时返回 None。
    """
    rets = daily_returns(curve)
    if rets.size < 2 or periods <= 0:
        return None
    rf_daily = float(rf) / float(periods)
    excess = rets - rf_daily
    std = float(np.std(excess, ddof=1))
    if std <= _EPS or not math.isfinite(std):
        return None
    return float(np.mean(excess) / std * math.sqrt(periods))


def calmar(curve: Curve, years: Optional[float] = None) -> Optional[float]:
    """Calmar 比率 = 年化收益率(%) / 最大回撤(%)。

    Args:
        curve: 净值曲线。
        years: 回测年数，透传给 annualized。

    Returns:
        Optional[float]: Calmar 比率。**MaxDD == 0 → 返回 None**（除零无定义，
        架构要求不得返回 inf，调用方需按"无回撤 → 不可比"处理并标注）。
    """
    mdd = max_drawdown(curve)
    if mdd <= _EPS:
        return None
    return float(annualized(curve, years) / mdd)


# ============================================================
# 交易维度指标（金额口径）
# ============================================================
def _pnl_amount(trade: Union[dict, object]) -> float:
    """取单笔已平仓交易的**金额盈亏**。

    兼容三种来源:
        - ClosedTrade dataclass  → .pnl_amt
        - dict("pnl_amt")        → 金额盈亏(优先)
        - dict("盈亏")           → sim_portfolio 风格金额盈亏
    ⚠️ 绝不使用 收益%/pnl_pct 当金额（那是百分比，口径错误）。
    """
    if isinstance(trade, dict):
        for key in ("pnl_amt", "盈亏", "pnl"):
            if key in trade and trade[key] is not None:
                try:
                    return float(trade[key])
                except (TypeError, ValueError):
                    continue
        return 0.0
    for attr in ("pnl_amt", "pnl"):
        if hasattr(trade, attr):
            try:
                return float(getattr(trade, attr))
            except (TypeError, ValueError):
                continue
    return 0.0


def win_rate(closed: Sequence) -> float:
    """胜率(%) = 盈利笔数 / 总笔数 × 100（按金额口径 > 0 记胜）。

    Args:
        closed: 已平仓交易序列（dict 或 ClosedTrade）。

    Returns:
        float: 胜率百分比；无交易时返回 0.0。
    """
    if not closed:
        return 0.0
    wins = sum(1 for t in closed if _pnl_amount(t) > _EPS)
    return float(wins / len(closed) * 100.0)


def profit_factor(closed: Sequence) -> Optional[float]:
    """盈亏比 = 平均盈利金额 / |平均亏损金额|（金额口径，非百分比）。

    Args:
        closed: 已平仓交易序列。

    Returns:
        Optional[float]: 盈亏比；无盈利或无亏损样本时返回 None（不可比，不得返回 inf）。
    """
    if not closed:
        return None
    pnls = [_pnl_amount(t) for t in closed]
    wins = [p for p in pnls if p > _EPS]
    losses = [p for p in pnls if p < -_EPS]
    if not wins or not losses:
        return None
    avg_win = float(np.mean(wins))
    avg_loss = abs(float(np.mean(losses)))
    if avg_loss <= _EPS:
        return None
    return float(avg_win / avg_loss)


def avg_win_loss_pct(closed: Sequence) -> tuple[float, float]:
    """平均盈利% / 平均亏损%（百分比口径，与 backtest.summarize 口径一致，便于对照）。

    Args:
        closed: 已平仓交易序列，需含 pnl_pct 字段。

    Returns:
        tuple[float, float]: (平均盈利%, 平均亏损%)；缺失时该侧为 0.0。
    """
    pcts = []
    for t in closed:
        if isinstance(t, dict):
            v = t.get("pnl_pct")
        else:
            v = getattr(t, "pnl_pct", None)
        if v is not None:
            try:
                pcts.append(float(v))
            except (TypeError, ValueError):
                continue
    if not pcts:
        return 0.0, 0.0
    wins = [p for p in pcts if p > _EPS]
    losses = [p for p in pcts if p < -_EPS]
    return (float(np.mean(wins)) if wins else 0.0,
            float(np.mean(losses)) if losses else 0.0)


def oos_is_ratio(oos_val: Optional[float], is_val: Optional[float]) -> Optional[float]:
    """样本外/样本内指标比（>0.5 视为通过，<0.5 视为过拟合征兆）。

    Args:
        oos_val: 样本外指标值。
        is_val: 样本内指标值。

    Returns:
        Optional[float]: 比值；任一为 None 或分母绝对值过小时返回 None。
    """
    if oos_val is None or is_val is None:
        return None
    denom = abs(float(is_val))
    if denom <= _EPS:
        return None
    return float(oos_val) / float(is_val)


# ============================================================
# 汇总报告
# ============================================================
def full_report(curve: Curve, closed: Sequence = (), years: Optional[float] = None,
                rf: float = 0.0, benchmark: Optional[float] = None) -> dict:
    """组合绩效总览（组合层回测的标准输出）。

    Args:
        curve: 每日净值曲线 [(date, equity), ...]（含空仓日）。
        closed: 已平仓交易序列。
        years: 回测年数；None 时按曲线长度/252 估算。
        rf: 年化无风险利率小数。
        benchmark: 基准（如沪深300）同期收益率%，可选。

    Returns:
        dict: 指标字典，键含:
            total_return(%) / annualized(%) / max_drawdown(%) / sharpe / calmar /
            win_rate(%) / profit_factor / avg_win_pct / avg_loss_pct /
            trades / final_equity / days / calmar_note / excess_return(%)
    """
    eq = _equities(curve)
    final_equity = float(eq[-1]) if eq.size else 0.0
    mdd = max_drawdown(curve)
    calmar_val = calmar(curve, years)
    total_ret = total_return(curve)
    report = {
        "total_return": round(total_ret, 4),
        "annualized": round(annualized(curve, years), 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": (round(sharpe(curve, rf=rf), 4) if sharpe(curve, rf=rf) is not None else None),
        "calmar": (round(calmar_val, 4) if calmar_val is not None else None),
        "win_rate": round(win_rate(closed), 4),
        "profit_factor": (round(profit_factor(closed), 4)
                          if profit_factor(closed) is not None else None),
        "avg_win_pct": round(avg_win_loss_pct(closed)[0], 4),
        "avg_loss_pct": round(avg_win_loss_pct(closed)[1], 4),
        "trades": len(closed),
        "final_equity": round(final_equity, 2),
        "days": int(eq.size),
        # 边界标注：MaxDD=0（全程无回撤）时 Calmar 无定义
        "calmar_note": ("MaxDD=0(阶段内无回撤), Calmar 无定义(None)"
                        if calmar_val is None else ""),
    }
    if benchmark is not None:
        try:
            report["excess_return"] = round(total_ret - float(benchmark), 4)
        except (TypeError, ValueError):
            report["excess_return"] = None
    return report


if __name__ == "__main__":  # pragma: no cover - 手工自检
    # 构造 5 日净值: 100 → 110 → 99 → 105 → 120
    demo = [("d1", 100.0), ("d2", 110.0), ("d3", 99.0), ("d4", 105.0), ("d5", 120.0)]
    closed_demo = [{"pnl_amt": 1000.0, "pnl_pct": 5.0},
                   {"pnl_amt": -400.0, "pnl_pct": -2.0},
                   {"pnl_amt": 600.0, "pnl_pct": 3.0}]
    print("总收益% :", total_return(demo))          # 20.0
    print("最大回撤%:", max_drawdown(demo))          # 10.0
    print("胜率%   :", win_rate(closed_demo))        # 66.67
    print("盈亏比  :", profit_factor(closed_demo))   # 800/400 = 2.0
    print("夏普    :", sharpe(demo))
    print("Calmar  :", calmar(demo, years=0.02))
    print("全量报告:", full_report(demo, closed_demo, years=0.02))
    # 边界: 单调上涨 → MaxDD=0 → Calmar 必须 None
    flat = [("d1", 100.0), ("d2", 101.0), ("d3", 102.0)]
    print("边界(无回撤) Calmar:", calmar(flat), "note:", full_report(flat)["calmar_note"])
