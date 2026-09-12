# -*- coding: utf-8 -*-
"""参数网格搜索 + 样本外验证（T02-4）
==================================
回答用户最关心的问题：
    「砖型 2/3 还是 3/4 更好？」「B3 / 单针要不要砍？」「阶梯止损怎么设最好？」
靠的是 **参数网格 + 样本外（OOS）验证**，而不是拍脑袋。

【方法学（本模块的全部价值所在）】
1. **坐标下降两轮**（不做全笛卡尔展开）
   全展开 = 500+ 组 × 上千标的 × 8 年 → 跑不完。改为：
     Round 1: 固定其余参数为现值，逐个参数轴独立扫，各取最优；
     Round 2: 用 Round 1 的最优组合做起点，在每个轴 ±1 步邻域内再扫一遍。
   单战法 ≈ 40 组回测，可接受，且比全展开更不易过拟合（扫的面窄 → 更少捡到噪声）。

2. **IS:OOS = 60:40，全局统一切点**
   所有标的用**同一个日期切点**（全体交易日的 60% 分位），防止"每只股票 IS 期不同"
   导致的时间穿越。IS 尾部再剔除 30 根，防止 `_find_exit` 向后看把 OOS 价格泄露进 IS。

3. **数据不足硬失败**
   `--oos` 模式下若数据窗口不足，直接 `sys.exit(2)` + 明确指引，**绝不在 634 根数据上
   偷偷跑 OOS 出假结论**（这是本模块最重要的防诈设计）。

4. **稳定性检验 `stability_score`** = 最优参数 ±1 邻域内 calmar 的中位数 / best calmar
   < 0.5 → 打「针尖过拟合」标记。专门对治开源项目教训：过度优化把回撤从 17.4% 推到 60.2%。

5. **样本守卫**：OOS 交易数 < 30 → 标 `INSUFFICIENT_SAMPLE`，该结论不采信。

【约束（架构师定稿）】
    - `utils/backtest.py` **只读复用**，一行不改（QA 验 `git diff` 为空）。
      需要参数化信号 → 在**本文件做信号后置过滤**，不改 `compute_signals`。
    - 复用 `backtest.py: make_exit_rules / compute_signals / backtest_stock`。
    - 复用 `market_backtest.py: run_backtest_market / equity_curve / summarize`（不改那些函数）。
    - 组合参数从 `utils.strategy_config.CONFIG` 读，不硬编码。

用法:
    python utils/backtest_grid.py --help
    # 用户最关心的砖型问题（直接回答 2/3 还是 3/4）
    python utils/backtest_grid.py --strategy brick --param brick_ratio --values 0.6667,0.70,0.75,0.80 --max-stocks 50
    # 完整坐标下降两轮 + 样本外
    python utils/backtest_grid.py --strategy B1 --coordinate --oos --max-stocks 50
    # 全部战法
    python utils/backtest_grid.py --all --coordinate --max-stocks 50 --save data/backtest/grid_report.json
"""
import os
import sys
import json
import math
import argparse
import time
from dataclasses import dataclass, field
from typing import Callable, List, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

# ── 冻结底座（只读复用）────────────────────────────────────
from utils.backtest import (
    make_exit_rules,
    compute_signals,
    backtest_stock,
    TRADE_COST,
    DEFAULT_POOL,
)
from utils.indicators import kdj_j, white_line, yellow_line
from utils.strategy_config import CONFIG

# ── 团队并行产出（可能尚未就绪，做降级，不阻塞自己）────────
# T02-2 本文件同时交付 market_regime，直接可用
try:
    from utils.market_regime import (
        get_regime, regime_stats, split_by_regime,
        load_index_history, benchmark_return,
        BULL, SIDEWAYS, BEAR,
    )
    HAS_REGIME = True
except Exception as _e:   # pragma: no cover
    HAS_REGIME = False
    BULL, SIDEWAYS, BEAR = "BULL", "SIDEWAYS", "BEAR"
    get_regime = regime_stats = split_by_regime = None
    load_index_history = benchmark_return = None

# 指标口径模块（另一工程师产出，已就绪）。
# 已就绪时直接复用其 max_drawdown / total_return / annualized / oos_is_ratio，
# 保证与组合层口径**单一来源**；不可用时退化为下方内联实现（不阻塞）。
try:
    from utils.metrics import (
        max_drawdown as _ext_max_drawdown,
        total_return as _ext_total_return,
        annualized as _ext_annualized,
        oos_is_ratio as _ext_oos_is_ratio,
    )
    HAS_METRICS = True
except Exception:
    HAS_METRICS = False
    _ext_max_drawdown = _ext_total_return = _ext_annualized = _ext_oos_is_ratio = None

# T02-1 长历史数据层（另一位工程师并行开发）—— 有则用，无则退化为腾讯短历史
try:
    from utils.backtest_data import load_universe_klines as _load_long_klines
    HAS_BACKTEST_DATA = True
except Exception:
    HAS_BACKTEST_DATA = False
    _load_long_klines = None


# ============================================================
# 常量与配置
# ============================================================
DEFAULT_BACKTEST_START = "2018-01-01"
FALLBACK_BACKTEST_START = "2024-01-01"   # 退化口径（腾讯 ~700 根）
SPLIT_RATIO = 0.60                       # IS:OOS = 60:40（架构师定稿）
IS_TAIL_DROP = 30                        # IS 尾部剔除根数（防 _find_exit 向后看泄露）
MIN_OOS_TRADES = 30                      # OOS 交易数下限守卫
STABILITY_THRESHOLD = 0.5                # < 0.5 → 针尖过拟合嫌疑
OOS_IS_THRESHOLD = 0.6                   # 验收线（架构师定稿）
MIN_IS_BARS = 700                        # IS 至少需要的根数（低于此 --oos 硬失败）
GRID_TIEBREAK_TRADES = 30                # 选优时样本数下限：低于此的组降权（避免噪声夺冠）
CALMAR_CAP = 10.0                        # calmar 截断上限，防单组异常值主导选优


# ============================================================
# 指标口径（内联实现，待与 utils/metrics.py 合并）
# ============================================================
def equity_series_from_pnls(pnls_pct: List[float],
                            years: float = 1.0) -> Tuple[pd.Series, float]:
    """由单笔收益%序列构造**交易节点**净值曲线。

    口径说明：`backtest.py::backtest_stock` 的单笔独立满仓语义下，无法还原真实
    组合日净值（那属于 T02-3 组合层职责）。此处按"每笔独立满仓"口径把交易按
    信号日排序后复合，得到交易节点曲线，用于计算收益/回撤/calmar。

    Args:
        pnls_pct: 单笔收益（百分比数值，如 -0.04 = -0.04%，已扣 TRADE_COST）。
        years: 年数，用于年化。

    Returns:
        (equity: pd.Series 从 1.0 起, annual_pct: float 年化%)
    """
    if not pnls_pct:
        return pd.Series([1.0], dtype=float), 0.0
    vals = [1.0]
    cap = 1.0
    for p in pnls_pct:
        cap *= (1.0 + float(p) / 100.0)
        vals.append(cap)
    eq = pd.Series(vals, dtype=float)
    yrs = max(float(years), 1e-6)
    annual = ((cap ** (1.0 / yrs)) - 1.0) * 100.0 if cap > 0 else -100.0
    return eq, round(annual, 2)


def _eq_to_curve(eq: pd.Series) -> list:
    """把 pd.Series 净值曲线转成 utils.metrics 需要的 [(date, equity), ...] 形式。"""
    try:
        return [(str(i)[:10], float(v)) for i, v in eq.items()]
    except Exception:
        return [("t0", float(v)) for v in eq.tolist()]


def max_drawdown_pct(equity: pd.Series) -> float:
    """最大回撤%（基于净值曲线，含空仓平坦段）。

    优先复用 `utils.metrics.max_drawdown`（保证与组合层口径单一来源），
    不可用时走内联实现（口径一致：峰值→谷值最大跌幅，正数百分比）。

    Args:
        equity: 净值序列（从 1.0 起）。

    Returns:
        float 回撤百分比（正数，如 18.2）。
    """
    if equity is None or len(equity) == 0:
        return 0.0
    if HAS_METRICS and _ext_max_drawdown is not None:
        try:
            return round(float(_ext_max_drawdown(_eq_to_curve(equity))), 2)
        except Exception:
            pass
    arr = equity.astype(float).values
    peak = np.maximum.accumulate(arr)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - arr) / peak, 0.0)
    return round(float(np.max(dd)) * 100.0, 2)


def total_return_pct(equity: pd.Series) -> float:
    """区间总收益%。优先复用 `utils.metrics.total_return`，否则内联。"""
    if equity is None or len(equity) < 2 or float(equity.iloc[0]) <= 0:
        return 0.0
    if HAS_METRICS and _ext_total_return is not None:
        try:
            return round(float(_ext_total_return(_eq_to_curve(equity))), 2)
        except Exception:
            pass
    return round((float(equity.iloc[-1]) / float(equity.iloc[0]) - 1.0) * 100.0, 2)


def calmar_ratio(annual_pct: float, mdd_pct: float) -> float:
    """Calmar = 年化% / 最大回撤%。

    MaxDD == 0（无回撤）→ 返回哨兵 999.0（报告中须标注"不可比"）。
    MaxDD > 0 → 正常计算，并截断到 ±CALMAR_CAP 抑制异常值主导选优。
    """
    if mdd_pct is None or mdd_pct <= 1e-9:
        return 999.0
    raw = float(annual_pct) / float(mdd_pct)
    return round(max(-CALMAR_CAP, min(CALMAR_CAP, raw)), 3)


def win_rate_pct(pnls_pct: List[float]) -> float:
    """胜率% = 盈利笔数 / 总笔数（盈利判据 > 0，与 backtest.summarize 一致）。

    Args:
        pnls_pct: 单笔收益%列表。

    Returns:
        float 百分比，无样本返回 0.0。
    """
    if not pnls_pct:
        return 0.0
    wins = sum(1 for g in pnls_pct if g > 0)
    return round(wins / len(pnls_pct) * 100.0, 1)


def profit_factor(pnls_pct: List[float]) -> float:
    """盈亏比 = 盈利合计 / |亏损合计|（百分比口径，等价于金额口径的比值）。"""
    if not pnls_pct:
        return 0.0
    gain = sum(g for g in pnls_pct if g > 0)
    loss = -sum(g for g in pnls_pct if g <= 0)
    if loss <= 1e-9:
        return 999.0 if gain > 0 else 0.0
    return round(gain / loss, 2)


def sharpe_ratio(pnls_pct: List[float], periods_per_year: int = 252) -> float:
    """按「单笔收益」近似夏普（无风险利率取 0）。

    注意：本项目的单笔独立满仓语义下无法构造真实日收益序列（需组合层），
    故用单笔收益的 mean/std × sqrt(年均交易笔数) 做**相对可比**的近似。
    报告中须标注为近似值。样本 < 2 或 std=0 → 返回 0.0。
    """
    if not pnls_pct or len(pnls_pct) < 2:
        return 0.0
    arr = np.asarray(pnls_pct, dtype=float)
    sd = float(np.std(arr, ddof=1))
    if sd <= 1e-9:
        return 0.0
    mu = float(np.mean(arr))
    # 以"每年约 40 笔"作近似交易频次（单战法单标的一年信号量级），用于跨参数可比
    return round(mu / sd * math.sqrt(40.0), 3)


def _years_between(start, end) -> float:
    """两个日期之间的真实年数（与 market_backtest.equity_curve 的口径一致）。"""
    try:
        d0 = pd.Timestamp(start)
        d1 = pd.Timestamp(end)
        return max((d1 - d0).days / 365.25, 1e-6)
    except Exception:
        return 1.0


def evaluate_trades(pnls_pct: List[float], start, end) -> dict:
    """单组参数下的完整指标汇总（本模块统一出口）。

    Args:
        pnls_pct: 该参数下所有标的、所有交易的收益%列表（已扣成本）。
        start: 区间起始日期。
        end: 区间结束日期。

    Returns:
        dict: {trades, win_rate, profit_factor, total_return, annual, mdd,
               calmar, sharpe, avg_pnl, avg_win, avg_loss}
    """
    years = _years_between(start, end)
    eq, annual = equity_series_from_pnls(pnls_pct, years=years)
    mdd = max_drawdown_pct(eq)
    wins = [g for g in pnls_pct if g > 0]
    losses = [g for g in pnls_pct if g <= 0]
    return {
        "trades": len(pnls_pct),
        "win_rate": win_rate_pct(pnls_pct),
        "profit_factor": profit_factor(pnls_pct),
        "total_return": total_return_pct(eq),
        "annual": annual,
        "mdd": mdd,
        "calmar": calmar_ratio(annual, mdd),
        "sharpe": sharpe_ratio(pnls_pct),
        "avg_pnl": round(float(np.mean(pnls_pct)), 2) if pnls_pct else 0.0,
        "avg_win": round(float(np.mean(wins)), 2) if wins else 0.0,
        "avg_loss": round(float(np.mean(losses)), 2) if losses else 0.0,
        # ── 单笔口径（非复利）: 与"满仓复利"口径互为对照 ──────────
        # sum_pnl: 每笔各用一份资金、盈亏简单累加（等价 backtest.summarize 的"总收益累加%"）
        # 高交易数下"满仓复利"会把正/负边缘放大成指数级，故必须同时看单笔口径，
        # 否则 2700 笔 × 微负边缘 → 复利曲线归零，会让人误以为战法"必亏"。
        "sum_pnl": round(float(np.sum(pnls_pct)), 2) if pnls_pct else 0.0,
        "trades_per_year": round(len(pnls_pct) / years, 1) if years > 0 else 0.0,
    }


# ============================================================
# GridSpec：信号后置过滤（解决"不改 backtest.py 也能调参"）
# ============================================================
@dataclass
class GridSpec:
    """一个可扫的参数轴。

    Attributes:
        strategy: 战法名，如 "B1" / "砖型"。
        param: 参数名，如 "j_max" / "brick_ratio"。
        values: 候选值列表。
        kind: 参数类别 —— "signal"（信号后置过滤）/ "rule"（改 make_exit_rules）。
        apply: 信号过滤函数 `(df, base_signal, value) -> bool Series`（kind="signal"）。
        rule_key: 映射到规则 dict 的 key，如 "stop_pct" / "max_hold"（kind="rule"）。
        rule_transform: 把网格值转成规则值的函数（默认恒等）。
        label: 人类可读说明。
    """
    strategy: str
    param: str
    values: List[float]
    kind: str = "signal"
    apply: Optional[Callable] = None
    rule_key: Optional[str] = None
    rule_transform: Optional[Callable] = None
    label: str = ""

    def apply_to_signals(self, df: pd.DataFrame, base_signal: pd.Series, value) -> pd.Series:
        """把参数作用到信号序列上（kind="rule" 时原样返回）。"""
        if self.kind != "signal" or self.apply is None:
            return base_signal
        out = self.apply(df, base_signal, value)
        return out.fillna(False).astype(bool)

    def apply_to_rules(self, rules: dict, value) -> dict:
        """把参数写进规则 dict（只覆写本战法的 key，不动别的战法 —— 各战法止损不套用）。"""
        new_rules = {k: dict(v) for k, v in rules.items()}
        if self.kind == "rule" and self.rule_key:
            rule = new_rules.get(self.strategy)
            if rule is not None:
                v = self.rule_transform(value) if self.rule_transform else value
                rule[self.rule_key] = v
        return new_rules


# ── 各战法的信号过滤器（复用 indicators，逻辑与 backtest.compute_signals 同口径）──
def _filter_j_max(df: pd.DataFrame, base: pd.Series, v) -> pd.Series:
    """B1: J 值 ≤ v（backtest.py 写死 13，此处参数化）。"""
    k, d, j = kdj_j(df["high"].astype(float), df["low"].astype(float), df["close"].astype(float))
    return base & (j <= float(v))


def _filter_chg_min(df: pd.DataFrame, base: pd.Series, v) -> pd.Series:
    """B2: 当日涨幅 > v%（backtest.py 写死 >4）。"""
    chg = df["close"].astype(float).pct_change() * 100
    return base & (chg > float(v))


def _filter_vol_mult(df: pd.DataFrame, base: pd.Series, v) -> pd.Series:
    """B2: 放量倍数 ≥ v（backtest.py 写死 1.5）。"""
    v_ = df["volume"].astype(float)
    return base & (v_ > v_.shift(1) * float(v))


def _filter_j_min(df: pd.DataFrame, base: pd.Series, v) -> pd.Series:
    """B3: J 值 ≥ v（backtest.py 写死 ≥80）。"""
    k, d, j = kdj_j(df["high"].astype(float), df["low"].astype(float), df["close"].astype(float))
    return base & (j >= float(v))


def _filter_red_min(df: pd.DataFrame, base: pd.Series, v) -> pd.Series:
    """单针: 长期线(红线) ≥ v（backtest.py 写死 ≥60）。"""
    try:
        from utils.needle20 import needle20_lines
        lines = needle20_lines(df)
        return base & (lines["长期"] >= float(v))
    except Exception:
        return base & False


def _filter_white_max(df: pd.DataFrame, base: pd.Series, v) -> pd.Series:
    """单针: 短期线 ≤ v（backtest.py 写死 ≤20）。"""
    try:
        from utils.needle20 import needle20_lines
        lines = needle20_lines(df)
        return base & (lines["短期"] <= float(v))
    except Exception:
        return base & False


def _filter_brick_ratio(df: pd.DataFrame, base: pd.Series, v) -> pd.Series:
    """砖型: 红砖高度/前绿柱 ≥ v（直接回答用户"2/3 还是 3/4"）。

    口径（对齐 strategy_config.BRICK_RATIO 的原版定义）:
        红砖 = 连续阳线的累计涨幅; 绿柱 = 前一段连续阴线的累计跌幅。
        强红信号: 红砖累计涨幅 ≥ 前绿柱累计跌幅 × v  → 视为"绿翻强红"。
    注意: `brick.py` 自身的 翻红XG 用的是 3/4 硬编码（见 §8 R4 风险），
    本函数在信号层再做一次 ratio 门槛过滤，使 2/3 与 3/4 可在同一基准下对比。
    """
    try:
        from utils.brick import brick_chart
        b = brick_chart(df)
        br = b["砖型图"].astype(float)
    except Exception:
        return base & False

    close = df["close"].astype(float).values
    n = len(close)
    brick_vals = br.values if hasattr(br, "values") else np.asarray(br, dtype=float)

    # 逐日计算: 当前红砖累计涨幅 vs 上一段绿柱累计跌幅
    ok = np.zeros(n, dtype=bool)
    red_run = 0.0
    green_run = 0.0
    prev_by_brick = {}   # 砖型值 -> 该段累计涨跌幅
    for i in range(1, n):
        cur = brick_vals[i]
        prev = brick_vals[i - 1] if i > 0 else cur
        if cur > prev:                      # 红砖在增长
            red_run = close[i] / close[i - 1] - 1.0 if close[i - 1] > 0 else 0.0
            acc = 0.0
        elif cur < prev:                    # 绿柱在增长
            green_run = abs(close[i] / close[i - 1] - 1.0) if close[i - 1] > 0 else 0.0
            acc = 0.0
        else:
            acc = 0.0
        # 用"最近一段同色累计"近似：此处以单根幅度比做门槛（保守，避免状态机复杂度）
        base_red = abs(close[i] / close[i - 1] - 1.0) if close[i - 1] > 0 else 0.0
        prev_green = prev_by_brick.get("last_green", 0.0)
        if cur > prev and prev_green > 1e-9:
            ok[i] = (base_red / prev_green) >= float(v)
        if cur < prev:
            prev_by_brick["last_green"] = max(green_run, 1e-9)
    return base & pd.Series(ok, index=df.index)


# ============================================================
# 默认网格（架构师 §6.1 定稿范围）
# ============================================================
def default_grid(strategy: Optional[str] = None) -> List[GridSpec]:
    """内置网格定义。

    Args:
        strategy: 只取某战法的轴；None = 全部。

    Returns:
        list[GridSpec]，按战法分组（同一战法的轴在坐标下降里依次扫）。
    """
    specs: List[GridSpec] = [
        # ── B1 ── 对齐开源项目起点 J=5/SL=-5%
        GridSpec("B1", "j_max", [5, 8, 12, 13, 18], kind="signal", apply=_filter_j_max,
                 label="B1 J值上限(现写死13; 对标开源J=5)"),
        GridSpec("B1", "stop_pct", [-0.03, -0.04, -0.05, -0.06], kind="rule",
                 rule_key="stop_pct", label="B1 止损%(现-0.04; 对标SL-5%)"),
        GridSpec("B1", "max_hold", [10, 15, 20, 30], kind="rule",
                 rule_key="max_hold", rule_transform=lambda v: int(v),
                 label="B1 最长持有(现30)"),

        # ── B2 ──
        GridSpec("B2", "chg_min", [3.0, 4.0, 5.0, 6.0], kind="signal", apply=_filter_chg_min,
                 label="B2 当日涨幅下限%(现写死>4)"),
        GridSpec("B2", "vol_mult", [1.2, 1.5, 2.0], kind="signal", apply=_filter_vol_mult,
                 label="B2 放量倍数(现写死1.5)"),
        GridSpec("B2", "max_hold", [10, 15, 20], kind="rule",
                 rule_key="max_hold", rule_transform=lambda v: int(v),
                 label="B2 最长持有(现15)"),

        # ── B3 ──（用户问"B3 要不要砍"）
        GridSpec("B3", "j_min", [70, 80, 85, 90], kind="signal", apply=_filter_j_min,
                 label="B3 J值下限(现写死80)"),
        GridSpec("B3", "max_hold", [10, 15, 20], kind="rule",
                 rule_key="max_hold", rule_transform=lambda v: int(v),
                 label="B3 最长持有(现15)"),

        # ── 砖型 ── ★用户明确问的"2/3 还是 3/4"
        GridSpec("砖型", "brick_ratio", [2.0 / 3.0, 0.70, 3.0 / 4.0, 0.80],
                 kind="signal", apply=_filter_brick_ratio,
                 label="★砖型 红砖/前绿柱 比例(2/3 vs 3/4, 用户核心疑问)"),
        GridSpec("砖型", "max_hold", [15, 20, 25], kind="rule",
                 rule_key="max_hold", rule_transform=lambda v: int(v),
                 label="砖型 最长持有(现20)"),
        GridSpec("砖型", "stop_pct", [-0.04, -0.05, -0.06], kind="rule",
                 rule_key="stop_pct", label="砖型 兜底止损%(现-0.05)"),

        # ── 单针 ──（用户问"单针要不要砍"）
        GridSpec("单针", "red_min", [55, 60, 65], kind="signal", apply=_filter_red_min,
                 label="单针 长期线下限(现写死60)"),
        GridSpec("单针", "white_max", [20, 25, 30], kind="signal", apply=_filter_white_max,
                 label="单针 短期线上限(现写死20)"),
        GridSpec("单针", "max_hold", [10, 15, 20], kind="rule",
                 rule_key="max_hold", rule_transform=lambda v: int(v),
                 label="单针 最长持有(现15)"),
    ]
    if strategy:
        specs = [s for s in specs if s.strategy == strategy]
    return specs


# ============================================================
# 回测执行（复用冻结底座）
# ============================================================
def prepare_cache(klines: dict, strategies: List[str]) -> dict:
    """预计算各标的的基础信号（`compute_signals` 只跑一次 —— 性能关键）。

    Args:
        klines: {code: df}。
        strategies: 关心的战法列表。

    Returns:
        {code: {strategy: bool Series}}（只保留非空且长度匹配的）。
    """
    cache: Dict[str, dict] = {}
    for code, df in klines.items():
        try:
            if df is None or len(df) < 60:
                continue
            sig_all = compute_signals(df)
            cache[code] = {s: sig_all[s] for s in strategies if s in sig_all}
        except Exception:
            continue
    return cache


def _grid_key(specs: List[GridSpec], params: dict) -> str:
    """稳定排序的参数键，用于网格结果去重/查表。"""
    return "|".join(f"{s.param}={params.get(s.param)}" for s in specs)


# ============================================================
# 网格搜索器
# ============================================================
@dataclass
class GridSearcher:
    """参数网格搜索器（坐标下降两轮 + IS/OOS + 稳定性检验）。

    Attributes:
        specs: 网格轴列表（同一战法）。
        split_ratio: IS 占比，默认 0.60。
        is_tail_drop: IS 尾部剔除根数，默认 30。
        verbose: 是否打印过程。
        results: 全部参数组在 IS 上的评估明细（list[dict]）。
    """
    specs: List[GridSpec]
    split_ratio: float = SPLIT_RATIO
    is_tail_drop: int = IS_TAIL_DROP
    verbose: bool = True
    results: List[dict] = field(default_factory=list)

    # ---------- 搜索 ----------
    def search_coordinate(self, klines: dict, signals_cache: dict,
                          base_rules: dict, start, end) -> dict:
        """坐标下降两轮搜索，返回最优参数 dict。

        Round 1: 从当前规则默认值出发，逐个参数轴独立扫（其它轴固定），各取最优。
        Round 2: 用 Round 1 的组合做起点，在每个轴的 ±1 步邻域内再扫一遍。

        Args:
            klines: IS 段 {code: df}。
            signals_cache: `prepare_cache()` 输出。
            base_rules: `make_exit_rules()` 输出。
            start / end: IS 区间边界（年化用）。

        Returns:
            dict: {param: best_value}，并填充 `self.results`。
        """
        self.results = []
        best_params: Dict[str, float] = {}
        for s in self.specs:
            best_params[s.param] = self._default_value(s, base_rules)

        # ── Round 1 ──────────────────────────────────────────
        if self.verbose:
            print(f"    ── Round 1: 逐轴独立扫描 ──")
        for spec in self.specs:
            best_val, best_score = self._scan_axis(
                spec, klines, signals_cache, base_rules, best_params, start, end,
                values=spec.values, tag="R1")
            best_params[spec.param] = best_val
            if self.verbose:
                print(f"      {spec.param:<12} → {self._fmt(best_val):<10} "
                      f"calmar={best_score:.3f}  ({spec.label})")

        # ── Round 2: ±1 步邻域 ───────────────────────────────
        if self.verbose:
            print(f"    ── Round 2: 最优组合 ±1 步邻域 ──")
        for spec in self.specs:
            near = self._neighborhood(spec, best_params[spec.param])
            if len(near) <= 1:
                continue
            best_val, best_score = self._scan_axis(
                spec, klines, signals_cache, base_rules, best_params, start, end,
                values=near, tag="R2")
            best_params[spec.param] = best_val
            if self.verbose:
                print(f"      {spec.param:<12} → {self._fmt(best_val):<10} "
                      f"calmar={best_score:.3f}  (邻域 {len(near)} 点)")

        return best_params

    def _scan_axis(self, spec: GridSpec, klines: dict, signals_cache: dict,
                   base_rules: dict, current: dict, start, end,
                   values: List[float], tag: str) -> Tuple[float, float]:
        """扫单个参数轴，返回 (最优值, 最优 calmar)。"""
        best_val = current[spec.param]
        best_score = -1e9
        for v in values:
            params = dict(current)
            params[spec.param] = v
            metrics = self._evaluate_params(params, klines, signals_cache, base_rules, start, end)
            metrics.update({"tag": tag, "axis": spec.param, "value": v})
            self.results.append(metrics)
            score = self._rank_score(metrics)
            if score > best_score:
                best_score = score
                best_val = v
        return best_val, best_score

    def _evaluate_params(self, params: dict, klines: dict, signals_cache: dict,
                         base_rules: dict, start, end) -> dict:
        """评估一组参数：信号过滤 + 规则注入 + 回测 + 指标。

        ⚠️ 必须走 `_run_variant_with_filters`（在标的循环内用 df 做信号后置过滤），
        因为 `j_max`/`brick_ratio` 等 signal 类参数依赖 df（KDJ/砖型），
        不能在缓存层预先过滤。
        """
        strategies = sorted({s.strategy for s in self.specs})
        pnls: List[float] = []
        n_ok = 0
        for strategy in strategies:
            # ① 组装该战法的规则变体（只覆写本战法 key，不动别的战法）
            rules = base_rules
            for spec in self.specs:
                if spec.strategy == strategy and spec.kind == "rule":
                    rules = spec.apply_to_rules(rules, params.get(spec.param))
            # ② 带 signal 过滤回测（需要 df，故在循环内过滤）
            sub_pnls = _run_variant_with_filters(
                klines, strategy, signals_cache, self.specs, params, rules)
            pnls.extend(sub_pnls)
            if sub_pnls:
                n_ok += 1
        m = evaluate_trades(pnls, start, end)
        m["params"] = {k: self._fmt(v) for k, v in params.items()}
        m["n_stocks"] = n_ok
        return m

    @staticmethod
    def _rank_score(metrics: dict) -> float:
        """选优打分：以 calmar 为主，样本过少时大幅降权（防噪声夺冠）。

        Args:
            metrics: `evaluate_trades()` 输出 + trades 字段。

        Returns:
            float 打分（越大越好）。
        """
        cal = float(metrics.get("calmar", 0.0))
        if cal >= 999.0:      # 无回撤哨兵 → 不参与普通排序（避免误导）
            cal = CALMAR_CAP
        trades = int(metrics.get("trades", 0))
        if trades < GRID_TIEBREAK_TRADES:
            cal *= (trades / float(GRID_TIEBREAK_TRADES))
        return cal

    @staticmethod
    def _default_value(spec: GridSpec, base_rules: dict) -> float:
        """取该轴在现有规则里的默认值（作为 Round 1 的固定起点）。"""
        rule = base_rules.get(spec.strategy, {})
        if spec.kind == "rule" and spec.rule_key:
            return rule.get(spec.rule_key, spec.values[0])
        # 信号类参数的现值 = backtest.py 写死值（见 compute_signals）
        hardcoded = {
            ("B1", "j_max"): 13, ("B2", "chg_min"): 4.0, ("B2", "vol_mult"): 1.5,
            ("B3", "j_min"): 80, ("单针", "red_min"): 60, ("单针", "white_max"): 20,
            ("砖型", "brick_ratio"): float(CONFIG.BRICK_RATIO),
        }
        return hardcoded.get((spec.strategy, spec.param), spec.values[0])

    @staticmethod
    def _neighborhood(spec: GridSpec, value: float) -> List[float]:
        """在有序候选值上取 value 的 ±1 步邻域（含自身）。"""
        vals = list(spec.values)
        try:
            idx = min(range(len(vals)), key=lambda i: abs(float(vals[i]) - float(value)))
        except Exception:
            return [value]
        lo = max(0, idx - 1)
        hi = min(len(vals), idx + 2)
        return vals[lo:hi]

    @staticmethod
    def _fmt(v) -> str:
        """参数值的人类可读格式化（比例显示为 2/3、3/4）。"""
        try:
            f = float(v)
        except Exception:
            return str(v)
        known = {2.0 / 3.0: "2/3", 0.75: "3/4", 0.70: "0.70", 0.80: "0.80"}
        for k, name in known.items():
            if abs(f - k) < 1e-9:
                return name
        if abs(f - round(f)) < 1e-9:
            return str(int(round(f)))
        return f"{f:.4f}".rstrip("0")

    # ---------- 稳定性检验 ----------
    def stability_score(self, best_params: dict) -> float:
        """邻域平稳性 = 邻域内 calmar 中位数 / best calmar。

        ≥ 1.0 → 高原（参数稳健）；0.5~1.0 → 可接受；< 0.5 → 「针尖过拟合」嫌疑。

        实现: 在 `self.results` 中找与 best_params 仅差**一个轴一步**的组，
        取这些组 calmar 的中位数，除以 best calmar。

        Returns:
            float 稳定性得分；**best calmar ≤ 0 时返回 -1.0 哨兵**
            （此时比值无数学意义 —— 指标本身不赚钱，谈不上"稳不稳"，
             报告层须显示为"不可评估(策略无正期望)"而非误报"针尖"）。
            结果集为空或找不到 best 时返回 0.0。
        """
        if not self.results:
            return 0.0
        best = self._find_result(best_params)
        if best is None:
            return 0.0
        best_cal = float(best.get("calmar", 0.0))
        if best_cal >= 999.0:
            best_cal = CALMAR_CAP
        if best_cal <= 1e-9:
            return -1.0   # 哨兵: 策略无正期望，稳定性不可评估

        neigh = []
        for r in self.results:
            if self._is_neighbor(r, best_params):
                c = float(r.get("calmar", 0.0))
                neigh.append(CALMAR_CAP if c >= 999.0 else c)
        if not neigh:
            return 0.0
        import statistics
        med = statistics.median(neigh)
        return round(med / best_cal, 3)

    def _find_result(self, params: dict) -> Optional[dict]:
        """在结果集中定位与 params 完全一致的组。"""
        target = {k: self._fmt(v) for k, v in params.items()}
        for r in self.results:
            if r.get("params") == target:
                return r
        return None

    def _is_neighbor(self, r: dict, best_params: dict) -> bool:
        """判断结果 r 是否与 best_params 只差一个轴、且该轴值在 ±1 步邻域内。"""
        rp = r.get("params", {})
        target = {k: self._fmt(v) for k, v in best_params.items()}
        diff = 0
        for spec in self.specs:
            rv, bv = rp.get(spec.param), target.get(spec.param)
            if rv is None or bv is None:
                continue
            if rv != bv:
                diff += 1
                # 该轴的值必须仍在网格候选内（说明确实相邻而非跳变）
                allowed = {self._fmt(x) for x in self._neighborhood(spec, best_params[spec.param])}
                if rv not in allowed:
                    return False
        return diff == 1

    # ---------- 样本外 ----------
    def evaluate_oos(self, best_params: dict, klines_oos: dict,
                     signals_cache_oos: dict, base_rules: dict,
                     start, end) -> dict:
        """冻结最优参数，在 OOS 段一次性验证。

        Returns:
            dict: 指标 + {"insufficient": bool, "verdict": str, "oos_is_ratio": float}
        """
        m = self._evaluate_params(best_params, klines_oos, signals_cache_oos,
                                  base_rules, start, end)
        m["insufficient"] = m["trades"] < MIN_OOS_TRADES
        return m


def _run_variant_with_filters(klines_subset: dict, strategy: str,
                              signals_cache: dict, specs: List[GridSpec],
                              params: dict, rules: dict) -> List[float]:
    """带信号过滤器的回测执行（需要 df，故在标的循环内做过滤）。

    Args:
        klines_subset: {code: df}。
        strategy: 战法名。
        signals_cache: 基础信号缓存。
        specs: 全部网格轴。
        params: 当前参数组合。
        rules: 已注入规则参数的规则 dict。

    Returns:
        list[float] 单笔收益%。
    """
    axes = [s for s in specs if s.strategy == strategy and s.kind == "signal"]
    pnls: List[float] = []
    for code, df in klines_subset.items():
        sigmap = signals_cache.get(code)
        if not sigmap or strategy not in sigmap:
            continue
        sig = sigmap[strategy]
        for spec in axes:
            sig = spec.apply_to_signals(df, sig, params.get(spec.param))
        if sig.sum() == 0:
            continue
        try:
            res = backtest_stock({strategy: sig}, df, rules=rules)
        except Exception:
            continue
        pnls.extend(float(t["收益"]) for t in res.get(strategy, []))
    return pnls


# ============================================================
# IS/OOS 切分
# ============================================================
def global_split_date(klines: dict, split_ratio: float = SPLIT_RATIO,
                      drop_tail_bars: int = IS_TAIL_DROP) -> pd.Timestamp:
    """全局统一切点日期（全体交易日序列的 split_ratio 分位）。

    ★ 用全体标的的**并集日期**排序后取分位，而非各标的各自切 —— 防时间穿越。

    Args:
        klines: {code: df}。
        split_ratio: IS 占比。
        drop_tail_bars: IS 尾部剔除根数（切点往前推，保证 IS 不含尾部泄露区）。

    Returns:
        pd.Timestamp 切点日期（IS 的**右开**边界）。
    """
    dates = set()
    for df in klines.values():
        if df is not None and len(df):
            dates.update(df.index.tolist())
    if not dates:
        return pd.Timestamp("2100-01-01")
    all_dates = pd.DatetimeIndex(sorted(dates))
    pos = int(len(all_dates) * split_ratio)
    pos = max(0, min(pos, len(all_dates) - 1))
    # 尾部剔除：IS 末端再往前 30 根（防 _find_exit 向后看拿到 OOS 价格）
    cut_pos = max(0, pos - int(drop_tail_bars))
    return all_dates[cut_pos]


def slice_klines(klines: dict, start=None, end=None) -> dict:
    """按日期区间切片（左闭右开/闭由调用方决定），返回 {code: df}。"""
    out = {}
    for code, df in klines.items():
        if df is None or len(df) == 0:
            continue
        sub = df
        if start is not None:
            sub = sub[sub.index >= pd.Timestamp(start)]
        if end is not None:
            sub = sub[sub.index <= pd.Timestamp(end)]
        if len(sub) >= 60:
            out[code] = sub
    return out


def slice_is_oos(klines: dict, split_ratio: float = SPLIT_RATIO,
                 mode: str = "time",
                 index_df: pd.DataFrame = None) -> Tuple[dict, dict, dict]:
    """切分 IS/OOS。

    Args:
        klines: 全量 {code: df}。
        split_ratio: IS 占比（mode="time"）。
        mode: "time" 按时间轴全局统一切点；"regime" 按市场状态（BULL+SIDEWAYS→IS, BEAR→OOS）。
        index_df: mode="regime" 时必需。

    Returns:
        (klines_is, klines_oos, meta)
        meta: {"mode", "cut_date"|"is_days"/"oos_days", ...}，供报告落盘。
    """
    if mode == "regime":
        if not HAS_REGIME or index_df is None or len(index_df) == 0:
            raise RuntimeError("regime 切分需要 utils.market_regime 与指数数据")
        is_mask, oos_mask = split_by_regime(index_df=index_df)
        if is_mask is None:
            raise RuntimeError("regime 切分失败：指数标签为空")
        is_days = set(index_df.index[is_mask])
        oos_days = set(index_df.index[oos_mask])
        kis = {c: df[df.index.isin(is_days)] for c, df in klines.items()}
        kos = {c: df[df.index.isin(oos_days)] for c, df in klines.items()}
        kis = {c: df for c, df in kis.items() if len(df) >= 60}
        kos = {c: df for c, df in kos.items() if len(df) >= 60}
        meta = {"mode": "regime", "is_days": len(is_days), "oos_days": len(oos_days)}
        return kis, kos, meta

    cut = global_split_date(klines, split_ratio=split_ratio)
    kis = slice_klines(klines, end=cut - pd.Timedelta(days=1))
    kos = slice_klines(klines, start=cut)
    meta = {"mode": "time", "cut_date": str(cut)[:10],
            "is_stocks": len(kis), "oos_stocks": len(kos)}
    return kis, kos, meta


# ============================================================
# 数据加载（T02-1 就绪则用长历史，否则退化并告警）
# ============================================================
def normalize_klines(klines: dict) -> dict:
    """把各标的 df 规范成 `backtest.py` 需要的形态：DatetimeIndex 升序 + OHLCV。

    ⚠️ 集成适配（跨模块契约对齐）: `utils.backtest_data.load_universe_klines()`
    返回的是 `date` **列** + RangeIndex，而 `backtest.py::compute_signals/backtest_stock`
    以及本模块的 IS/OOS 日期切分都要求 **DatetimeIndex**。此处统一转换，
    双方文件都不改（各自保持自身职责）。

    Args:
        klines: {code: df}（df 可能带 date 列或已是 DatetimeIndex）。

    Returns:
        {code: df}，df 为 DatetimeIndex 升序、含 open/high/low/close/volume。
        无法规范化的标的被丢弃。
    """
    out = {}
    for code, df in (klines or {}).items():
        if df is None or len(df) == 0:
            continue
        d = df
        if not isinstance(d.index, pd.DatetimeIndex):
            date_col = None
            for cand in ("date", "日期", "trade_date", "Date"):
                if cand in d.columns:
                    date_col = cand
                    break
            if date_col is None:
                continue
            d = d.copy()
            d[date_col] = pd.to_datetime(d[date_col], errors="coerce")
            d = d.dropna(subset=[date_col]).set_index(date_col)
        else:
            d = d.copy()
        d = d.sort_index()
        # 数值列强制转换（防御脏数据）
        for col in ("open", "high", "low", "close", "volume"):
            if col not in d.columns:
                break
            d[col] = pd.to_numeric(d[col], errors="coerce")
        else:
            d = d.dropna(subset=["open", "high", "low", "close"])
            if len(d) > 0:
                out[code] = d
    return out


def load_klines(codes: List[str], start: str = DEFAULT_BACKTEST_START,
                max_stocks: int = 0) -> Tuple[dict, str]:
    """加载标的日线。

    优先 `utils.backtest_data.load_universe_klines`（长历史，2018 起）；
    不可用时退化到 `data_router.get_daily_bars`（腾讯 ~700 根，2024 起）。

    Args:
        codes: 股票代码列表。
        start: 期望起始日期。
        max_stocks: 限制数量（0=不限）。

    Returns:
        (klines, actual_start) —— klines 已规范化为 DatetimeIndex；
        actual_start 是**实际可用**的起始日期，调用方据此判断能否做 OOS。
    """
    if max_stocks and max_stocks > 0:
        codes = codes[:max_stocks]

    if HAS_BACKTEST_DATA:
        try:
            kl = _load_long_klines(codes, start=start)
            if kl:
                kl = normalize_klines(kl)
                if kl:
                    print(f"      数据源: backtest_data (长历史, 请求起 {start})")
                    return kl, start
        except Exception as e:
            print(f"      ⚠ backtest_data 加载失败({type(e).__name__}: {e}), 退化到短历史源")

    # ── 退化路径: data_router（腾讯 ~700 根）──────────────────
    from utils.data_router import get_daily_bars
    kl = {}
    for i, code in enumerate(codes):
        try:
            r = get_daily_bars(code, count=700, purpose="indicator", freshness="any")
            if r.get("status") != "OK" or not r.get("data"):
                continue
            df = pd.DataFrame(r["data"]).set_index("date")
            if len(df) >= 120:
                kl[code] = df
        except Exception:
            continue
        if (i + 1) % 50 == 0:
            print(f"      已加载 {i+1}/{len(codes)} (成功 {len(kl)})")
    kl = normalize_klines(kl)
    actual_start = FALLBACK_BACKTEST_START
    print(f"      数据源: data_router 退化路径 (短历史, 实际起 {actual_start})")
    return kl, actual_start


def resolve_codes(stocks_arg: str, limit: int, mode: str = "pool") -> List[str]:
    """解析股票池（--stocks 指定优先，否则用内置 DEFAULT_POOL）。

    Args:
        stocks_arg: 逗号分隔代码串。
        limit: 限制数量（0=不限）。
        mode: 保留参数（未来接 market_backtest.get_universe）。

    Returns:
        list[str] 代码（6 位）。
    """
    if stocks_arg:
        codes = [c.strip().zfill(6) for c in stocks_arg.split(",") if c.strip()]
    else:
        codes = [c for c, _ in DEFAULT_POOL]
    if limit and limit > 0:
        codes = codes[:limit]
    return codes


# ============================================================
# 单参数扫描（回答"砖型 2/3 还是 3/4"这类定点问题）
# ============================================================
def scan_single_param(strategy: str, param: str, values: List[float],
                      klines: dict, start, end,
                      use_oos: bool = False,
                      index_df: pd.DataFrame = None) -> List[dict]:
    """只扫一个参数轴（其它参数保持现值），输出逐值指标表。

    这是 `--param/--values` 模式的实现，用于定点回答用户疑问。

    Args:
        strategy: 战法名。
        param: 参数名（须在 `default_grid(strategy)` 中）。
        values: 候选值。
        klines: {code: df}（IS 或全量）。
        start / end: 区间边界。
        use_oos: True 时额外输出 OOS 段结果（需 klines 为全量 + index_df）。
        index_df: 指数数据（分市况统计用）。

    Returns:
        list[dict]，每项含 value + 全套指标 + 分市况统计。
    """
    specs_all = default_grid(strategy)
    target = next((s for s in specs_all if s.param == param), None)
    if target is None:
        raise ValueError(f"战法 {strategy} 无参数 {param}; 可选: {[s.param for s in specs_all]}")

    spec = GridSpec(target.strategy, target.param, values,
                    kind=target.kind, apply=target.apply,
                    rule_key=target.rule_key, rule_transform=target.rule_transform,
                    label=target.label)
    base_specs = [spec]
    base_rules = make_exit_rules()

    cache = prepare_cache(klines, [strategy])
    if not cache:
        print("  ⚠ 信号缓存为空（数据不足或计算失败）")
        return []

    default_val = GridSearcher._default_value(spec, base_rules)
    out = []
    regime_series = None
    if HAS_REGIME and index_df is not None and len(index_df) > 0:
        regime_series = get_regime(index_df)

    for v in values:
        params = {param: v}
        pnls = _run_variant_with_filters(
            klines, strategy, cache, base_specs, params,
            spec.apply_to_rules(base_rules, v))
        m = evaluate_trades(pnls, start, end)
        m["param"] = param
        m["value"] = v
        m["value_label"] = GridSearcher._fmt(v)
        m["is_default"] = (abs(float(v) - float(default_val)) < 1e-9)
        if regime_series is not None:
            m["by_regime"] = _regime_breakdown(klines, strategy, cache, base_specs,
                                               params, spec.apply_to_rules(base_rules, v),
                                               regime_series)
        out.append(m)

    return out


def _regime_breakdown(klines: dict, strategy: str, cache: dict,
                      specs: List[GridSpec], params: dict, rules: dict,
                      regime_series: pd.Series) -> dict:
    """按入场日市况（lag=1 前视防护）拆分交易统计。

    ⚠️ 用 `regime_series.shift(1)` 后的状态给**信号日**打标 —— 即用信号日**前一天**的
    大盘状态判断该笔是否属于该市况，绝不用当天状态（那是未来信息）。

    Returns:
        {BULL: {trades, win_rate, avg_pnl}, SIDEWAYS: {...}, BEAR: {...}}
    """
    lagged = regime_series.shift(1)
    buckets = {BULL: [], SIDEWAYS: [], BEAR: []}

    axes = [s for s in specs if s.strategy == strategy and s.kind == "signal"]
    for code, df in klines.items():
        sigmap = cache.get(code)
        if not sigmap or strategy not in sigmap:
            continue
        sig = sigmap[strategy]
        for s in axes:
            sig = s.apply_to_signals(df, sig, params.get(s.param))
        if sig.sum() == 0:
            continue
        try:
            res = backtest_stock({strategy: sig}, df, rules=rules)
        except Exception:
            continue
        for t in res.get(strategy, []):
            try:
                sig_date = pd.Timestamp(t["日期"])
                # lagged 的 index 是大盘交易日；对齐到最近的前一交易日
                if sig_date in lagged.index:
                    st = str(lagged.loc[sig_date])
                else:
                    prior = lagged.index[lagged.index <= sig_date]
                    st = str(lagged.loc[prior[-1]]) if len(prior) else SIDEWAYS
            except Exception:
                st = SIDEWAYS
            if st in buckets:
                buckets[st].append(float(t["收益"]))

    out = {}
    for r in (BULL, SIDEWAYS, BEAR):
        arr = buckets[r]
        out[r] = {
            "trades": len(arr),
            "win_rate": win_rate_pct(arr),
            "avg_pnl": round(float(np.mean(arr)), 2) if arr else 0.0,
            "total_pnl": round(float(np.sum(arr)), 2) if arr else 0.0,
        }
    return out


# ============================================================
# 坐标下降全流程（单战法）
# ============================================================
def run_coordinate_search(strategy: str, klines: dict, start, end,
                          do_oos: bool = False, split_ratio: float = SPLIT_RATIO,
                          index_df: pd.DataFrame = None, verbose: bool = True) -> dict:
    """单战法坐标下降两轮 + （可选）样本外验证。

    Args:
        strategy: 战法名。
        klines: 全量 {code: df}。
        start / end: 数据区间边界。
        do_oos: 是否做样本外验证（数据不足将抛 SystemExit）。
        split_ratio: IS 占比。
        index_df: 指数数据（分市况统计）。
        verbose: 打印过程。

    Returns:
        dict: {strategy, best_params, is_metrics, stability, stability_flag,
               oos, oos_is_ratio, verdict, by_regime}
    """
    specs = default_grid(strategy)
    if not specs:
        return {"strategy": strategy, "error": "无网格定义"}

    base_rules = make_exit_rules()

    if do_oos:
        kis, kos, meta = slice_is_oos(klines, split_ratio=split_ratio, mode="time")
        check_oos_feasible(kis, kos, meta, strategy, split_ratio)
        is_start, is_end = _bounds(kis)
        oos_start, oos_end = _bounds(kos)
        all_codes = sorted(set(kis) | set(kos))
        # 用同一批标的，保证 IS/OOS 可比
        oos_codes = set(kos)
        kis = {c: df for c, df in kis.items() if c in oos_codes} or kis
        cache_is = prepare_cache(kis, [strategy])
        cache_oos = prepare_cache(kos, [strategy])
    else:
        kis, kos, meta = klines, {}, {"mode": "none"}
        is_start, is_end = start, end
        oos_start = oos_end = None
        cache_is = prepare_cache(kis, [strategy])
        cache_oos = {}

    if verbose:
        print(f"    IS: {len(kis)} 只  {str(is_start)[:10]} ~ {str(is_end)[:10]}  "
              f"(有效信号缓存 {len(cache_is)} 只)")
        if do_oos:
            print(f"    OOS: {len(kos)} 只  {str(oos_start)[:10]} ~ {str(oos_end)[:10]}  "
                  f"(有效信号缓存 {len(cache_oos)} 只)")

    searcher = GridSearcher(specs, split_ratio=split_ratio, verbose=verbose)
    best_params = searcher.search_coordinate(kis, cache_is, base_rules, is_start, is_end)

    best_metrics = searcher._evaluate_params(best_params, kis, cache_is,
                                             base_rules, is_start, is_end)
    stability = searcher.stability_score(best_params)
    if stability < 0:
        stab_flag = "不可评估(best Calmar≤0, 策略无正期望)"
    elif stability >= STABILITY_THRESHOLD:
        stab_flag = "高原(稳健)"
    else:
        stab_flag = "针尖(过拟合嫌疑)"

    result = {
        "strategy": strategy,
        "best_params": {k: GridSearcher._fmt(v) for k, v in best_params.items()},
        "best_params_raw": {k: (float(v) if isinstance(v, (int, float)) else v)
                            for k, v in best_params.items()},
        "is_metrics": best_metrics,
        "stability": stability,
        "stability_flag": stab_flag,
        "n_variants": len(searcher.results),
        "grid_results": searcher.results,
    }

    if HAS_REGIME and index_df is not None and len(index_df) > 0:
        reg = get_regime(index_df)
        result["by_regime_is"] = _regime_breakdown(
            kis, strategy, cache_is, specs, best_params, base_rules, reg)

    if do_oos:
        oos_m = searcher.evaluate_oos(best_params, kos, cache_oos, base_rules,
                                      oos_start, oos_end)
        is_cal = float(best_metrics.get("calmar", 0.0))
        oos_cal = float(oos_m.get("calmar", 0.0))
        is_cal_raw, oos_cal_raw = is_cal, oos_cal
        if is_cal >= 999.0:
            is_cal = CALMAR_CAP
        if oos_cal >= 999.0:
            oos_cal = CALMAR_CAP

        # 比值仅在 **IS 为正** 时才有意义：IS 不赚钱/亏损时"比值"是数学噪声
        # （例: IS calmar=-0.03 → OOS/IS=6.6 完全无意义，必须显式标注不适用）。
        degenerate = is_cal <= 1e-9
        if degenerate:
            ratio = None
        else:
            ratio = round(oos_cal / is_cal, 3)
            if HAS_METRICS and _ext_oos_is_ratio is not None:
                try:
                    ext = _ext_oos_is_ratio(oos_cal, is_cal)
                    if ext is not None:
                        ratio = round(float(ext), 3)
                except Exception:
                    pass
        oos_m["oos_is_ratio"] = ratio
        oos_m["is_calmar_raw"] = round(is_cal_raw, 3)
        oos_m["oos_calmar_raw"] = round(oos_cal_raw, 3)
        oos_m["degenerate"] = degenerate

        # 判定链（顺序敏感）：样本不足 → 不采信；IS 不赚钱 → 不适用；再谈通过/未通过
        # ⚠️ None 安全：`ratio` 仅在 degenerate=True 时为 None，而 degenerate 分支
        #    总是先被下面的 elif 接住，故 `ratio >= OOS_IS_THRESHOLD` 处必为 float。
        #    （`utils.metrics.oos_is_ratio` 亦可能返回 None —— 上面已用自算值兜底，
        #     保证非 degenerate 时 ratio 恒为 float，与该口径对齐。）
        is_positive = float(best_metrics.get("sum_pnl", 0.0)) > 0
        if oos_m["insufficient"]:
            verdict = f"INSUFFICIENT_SAMPLE (OOS {oos_m['trades']} < {MIN_OOS_TRADES} 笔, 不采信)"
        elif degenerate and not is_positive:
            # IS 本身单笔累加为负 → 这套战法 IS 就不成立，无需谈 OOS 一致性
            verdict = (f"❌ 根基不成立 (IS 单笔累加 {best_metrics.get('sum_pnl', 0)}% ≤ 0, "
                       f"该战法在样本内无正期望, OOS/IS 比值不适用)")
        elif degenerate:
            # IS 单笔为正但复利曲线 calmar≈0（回撤吞掉收益）→ 比值不可用，改用单笔口径
            oos_sum = float(oos_m.get("sum_pnl", 0.0))
            ok = oos_sum > 0
            verdict = (f"{'✅' if ok else '❌'} IS Calmar≈0 比值不适用; 改判 OOS 单笔累加 "
                       f"{oos_sum}% ({'正期望保持' if ok else '正期望消失'})")
        elif ratio is None:
            # 兜底（理论上不可达）：防止未来改动破坏上述不变量 → 明确报"不适用"而非崩溃
            verdict = "⚠️ OOS/IS 比值不可计算（结果为 None），判定不适用"
        elif ratio >= OOS_IS_THRESHOLD:
            verdict = f"✅ 通过 (OOS/IS={ratio:.2f} ≥ {OOS_IS_THRESHOLD})"
        else:
            verdict = f"❌ 未通过 (OOS/IS={ratio:.2f} < {OOS_IS_THRESHOLD})"
        result.update({"oos_metrics": oos_m, "oos_is_ratio": ratio, "verdict": verdict,
                       "split_meta": meta})
        if HAS_REGIME and index_df is not None and len(index_df) > 0:
            result["by_regime_oos"] = _regime_breakdown(
                kos, strategy, cache_oos, specs, best_params, base_rules,
                get_regime(index_df))
    else:
        result.update({"oos_metrics": None, "oos_is_ratio": None, "verdict": "未做样本外"})

    return result


def check_oos_feasible(kis: dict, kos: dict, meta: dict, strategy: str,
                       split_ratio: float) -> None:
    """样本外可行性硬校验 —— 不足直接 SystemExit(2)，绝不出假结论。

    校验项：
        ① 切分后 IS / OOS 两侧都有标的；
        ② IS 段最长标的根数 ≥ MIN_IS_BARS；
        ③ OOS 段最长标的根数 ≥ 120（约半年）。

    Raises:
        SystemExit: 任一不满足（exit code 2）。
    """
    max_is = max((len(df) for df in kis.values()), default=0)
    max_oos = max((len(df) for df in kos.values()), default=0)

    problems = []
    if len(kis) == 0:
        problems.append("IS 段无有效标的")
    if len(kos) == 0:
        problems.append("OOS 段无有效标的")
    if max_is < MIN_IS_BARS:
        problems.append(f"IS 最长仅 {max_is} 根 < 下限 {MIN_IS_BARS} 根")
    if max_oos < 120:
        problems.append(f"OOS 最长仅 {max_oos} 根 < 下限 120 根")

    if problems:
        msg = [
            "",
            "=" * 78,
            "✗ 样本外验证不可行 —— 已硬失败退出（绝不在数据不足时输出假结论）",
            "=" * 78,
            f"  战法: {strategy} | 切分: {split_ratio:.0%} | 切点: {meta.get('cut_date', 'N/A')}",
            f"  IS  {len(kis)} 只 / 最长 {max_is} 根",
            f"  OOS {len(kos)} 只 / 最长 {max_oos} 根",
            "",
            "  原因:",
        ]
        msg += [f"    · {p}" for p in problems]
        msg += [
            "",
            "  处置（任选）:",
            "    1) 先扩数据窗口（推荐，T02-1 长历史数据层）:",
            "       python -c \"from utils.backtest_data import ensure_cached; "
            "print(ensure_cached(['600519'], start='2018-01-01'))\"",
            "    2) 去掉 --oos，只做 IS 网格搜索（此时结论不具样本外效力，报告中会标注）",
            "    3) 检查 --max-stocks 是否过小 / 股票池数据是否拉取失败",
            "=" * 78,
        ]
        print("\n".join(msg))
        sys.exit(2)


def _bounds(klines: dict) -> Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    """取 klines 全体日期的最小/最大边界。"""
    dates = [d for df in klines.values() if df is not None and len(df)
             for d in (df.index[0], df.index[-1])]
    if not dates:
        return None, None
    return min(dates), max(dates)


# ============================================================
# 报告输出
# ============================================================
def print_single_scan(strategy: str, rows: List[dict], target_param: str) -> None:
    """打印单参数扫描表（回答定点问题）。"""
    print()
    print("=" * 104)
    print(f"单参数扫描 · {strategy} · {target_param}   （其它参数保持现值，仅此轴变化）")
    print("=" * 104)
    print(f"{'参数值':<10}{'交易数':>7}{'胜率':>8}{'盈亏比':>8}{'单笔累加%':>10}"
          f"{'满仓复利%':>10}{'最大回撤%':>10}{'Calmar':>8}{'平均单笔%':>10}  备注")
    print("-" * 104)
    best = max(rows, key=lambda r: (r["calmar"] if r["calmar"] < 999 else CALMAR_CAP)) if rows else None
    for r in rows:
        mark = " ← 现值" if r.get("is_default") else ""
        star = " ★最优" if best is not None and r is best else ""
        cal = r["calmar"]
        cal_s = "无回撤" if cal >= 999 else f"{cal:.2f}"
        print(f"{r['value_label']:<10}{r['trades']:>7}{r['win_rate']:>7.1f}%"
              f"{r['profit_factor']:>8.2f}{r.get('sum_pnl', 0):>9.1f}%{r['total_return']:>9.1f}%"
              f"{r['mdd']:>9.1f}%{cal_s:>8}{r['avg_pnl']:>9.2f}%{mark}{star}")
    print("-" * 104)
    print("  口径: 「单笔累加%」= 每笔各占一份资金的盈亏简单相加(等价 backtest.summarize 总累加),")
    print("        「满仓复利%」= 按信号日顺序满仓轮动复利。★高交易数下复利会被边缘放大成指数级,")
    print("        判读战法优劣请以「单笔累加% / 平均单笔%/ 胜率 / 盈亏比」为主，复利仅作资金曲线参考。")

    # 分市况明细（若可用）
    if rows and any("by_regime" in r for r in rows):
        print("\n分市况明细（入场日按大盘 T-1 状态打标，前视安全）:")
        for r in rows:
            br = r.get("by_regime")
            if not br:
                continue
            parts = []
            for st in (BULL, SIDEWAYS, BEAR):
                d = br.get(st, {})
                parts.append(f"{st} n={d.get('trades', 0):<3} 胜率{d.get('win_rate', 0):>5.1f}% "
                             f"均{d.get('avg_pnl', 0):>+5.2f}%")
            print(f"  [{r['value_label']:<6}] " + " | ".join(parts))
    print("=" * 104)


def print_coordinate_report(result: dict) -> None:
    """打印坐标下降 + OOS 报告。"""
    st = result.get("strategy", "?")
    print()
    print("=" * 104)
    print(f"坐标下降两轮 + 样本外 · {st}")
    print("=" * 104)
    bp = result.get("best_params", {})
    print(f"最优参数: " + ", ".join(f"{k}={v}" for k, v in bp.items()))
    m = result.get("is_metrics", {})
    cal = m.get("calmar", 0)
    cal_s = "无回撤" if cal >= 999 else f"{cal:.2f}"
    print(f"IS 表现: 交易 {m.get('trades', 0)} 笔 | 胜率 {m.get('win_rate', 0)}% | "
          f"盈亏比 {m.get('profit_factor', 0)} | 单笔累加 {m.get('sum_pnl', 0)}% | "
          f"满仓复利 {m.get('total_return', 0)}% | MaxDD {m.get('mdd', 0)}% | Calmar {cal_s}")
    print(f"搜索组数: {result.get('n_variants', 0)} 组（坐标下降两轮，非全笛卡尔展开）")

    stab = result.get("stability", 0)
    flag = result.get("stability_flag", "")
    if stab < 0:
        line = f"稳定性 stability_score=N/A ⚠️ {flag}  (best Calmar≤0, 比值无数学意义)"
    else:
        emoji = "✅" if stab >= STABILITY_THRESHOLD else "⚠️"
        line = (f"稳定性 stability_score={stab:.3f} {emoji} {flag}"
                f"  (阈值 {STABILITY_THRESHOLD}: 邻域中位数/best)")
    print(line)

    oos = result.get("oos_metrics")
    if oos:
        ocal = oos.get("calmar", 0)
        ocal_s = "无回撤" if ocal >= 999 else f"{ocal:.2f}"
        print("-" * 104)
        split = result.get("split_meta", {})
        print(f"OOS 冻结验证 (切点 {split.get('cut_date', 'N/A')}, IS 尾部剔除了 "
              f"{IS_TAIL_DROP} 根防泄露):")
        print(f"  OOS 表现: 交易 {oos.get('trades', 0)} 笔 | 胜率 {oos.get('win_rate', 0)}% | "
              f"盈亏比 {oos.get('profit_factor', 0)} | 单笔累加 {oos.get('sum_pnl', 0)}% | "
              f"满仓复利 {oos.get('total_return', 0)}% | MaxDD {oos.get('mdd', 0)}% | "
              f"Calmar {ocal_s}")
        ratio_txt = "不适用(IS 无正期望)" if result.get("oos_is_ratio") is None \
            else str(result.get("oos_is_ratio"))
        print(f"  OOS/IS Calmar = {ratio_txt}   判定: {result.get('verdict')}")

    for seg in ("by_regime_is", "by_regime_oos"):
        br = result.get(seg)
        if not br:
            continue
        print(f"\n分市况（{seg.replace('by_regime_', '').upper()} 段）:")
        for stt in (BULL, SIDEWAYS, BEAR):
            d = br.get(stt, {})
            print(f"  {stt:<9} n={d.get('trades', 0):<4} 胜率 {d.get('win_rate', 0):>5.1f}%  "
                  f"平均单笔 {d.get('avg_pnl', 0):>+6.2f}%  累计 {d.get('total_pnl', 0):>+8.2f}%")
    print("=" * 104)


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="参数网格搜索 + 样本外验证（坐标下降两轮）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # ★ 回答用户"砖型 2/3 还是 3/4"
  python utils/backtest_grid.py --strategy brick --param brick_ratio \\
         --values 0.6667,0.70,0.75,0.80 --max-stocks 50

  # 单战法坐标下降两轮 + 样本外
  python utils/backtest_grid.py --strategy B1 --coordinate --oos --max-stocks 50

  # 全战法坐标下降（样本内）
  python utils/backtest_grid.py --all --coordinate --max-stocks 50

战法名: B1 / B2 / B3 / brick(砖型) / needle(单针)
""")
    parser.add_argument("--strategy", default="", help="战法名: B1/B2/B3/brick/needle")
    parser.add_argument("--param", default="", help="单参数扫描的参数名(配合 --values)")
    parser.add_argument("--values", default="", help="逗号分隔候选值")
    parser.add_argument("--coordinate", action="store_true", help="坐标下降两轮搜索")
    parser.add_argument("--all", action="store_true", help="对全部五套战法跑坐标下降")
    parser.add_argument("--oos", action="store_true",
                        help="样本外验证(60:40, 数据不足会硬失败退出)")
    parser.add_argument("--split", type=float, default=SPLIT_RATIO, help="IS 占比(默认0.6)")
    parser.add_argument("--regime", action="store_true",
                        help="分市况统计(BULL/SIDEWAYS/BEAR, T-1 前视防护)")
    parser.add_argument("--stocks", default="", help="股票代码逗号分隔(默认内置池20只)")
    parser.add_argument("--max-stocks", type=int, default=0, help="限制标的数(0=不限)")
    parser.add_argument("--start", default=DEFAULT_BACKTEST_START, help="起始日期")
    parser.add_argument("--save", default="", help="报告保存 JSON 路径")
    args = parser.parse_args()

    alias = {"brick": "砖型", "needle": "单针",
             "砖型": "砖型", "单针": "单针"}
    strategy = alias.get(args.strategy, args.strategy)

    t0 = time.time()
    print("=" * 78)
    print(f"参数网格搜索 + 样本外验证   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)

    # ── 1. 数据 ──
    codes = resolve_codes(args.stocks, args.max_stocks)
    print(f"[1/4] 加载数据: {len(codes)} 只标的 (请求起 {args.start})...")
    klines, actual_start = load_klines(codes, start=args.start, max_stocks=args.max_stocks)
    if not klines:
        print("✗ 无有效数据，退出")
        sys.exit(1)
    all_start, all_end = _bounds(klines)
    print(f"      实际: {len(klines)} 只, {str(all_start)[:10]} ~ {str(all_end)[:10]}")
    if HAS_BACKTEST_DATA:
        print("      数据层: backtest_data (长历史 ✓)")
    else:
        print("      ⚠ 数据层: 退化到短历史(2024起)。--oos 大概率会因数据不足硬失败，"
              "请等 T02-1 backtest_data.py 就绪")

    # ── 2. 市场状态 ──
    index_df = None
    if (args.regime or args.oos) and HAS_REGIME:
        print("[2/4] 市场状态分层 (沪深300)...")
        try:
            index_df = load_index_history("2018-01-01")
            if len(index_df) > 0:
                rs = regime_stats(index_df)
                print(f"      {rs['区间起']} ~ {rs['区间止']}: "
                      f"BULL {rs['BULL']}({rs['BULL%']}%) / "
                      f"SIDEWAYS {rs['SIDEWAYS']}({rs['SIDEWAYS%']}%) / "
                      f"BEAR {rs['BEAR']}({rs['BEAR%']}%)")
        except Exception as e:
            print(f"      ⚠ 市况分层失败: {e}")
    else:
        print("[2/4] 市场状态分层: 跳过（未加 --regime/--oos）")

    # ── 3+4. 搜索 ──
    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
              "start": args.start, "actual_start": actual_start,
              "end": str(all_end)[:10], "n_stocks": len(klines),
              "split_ratio": args.split,
              "config": {"GROUP_BUDGET": CONFIG.GROUP_BUDGET,
                         "BUY_RATIO": CONFIG.BUY_RATIO,
                         "RESERVE_CASH": CONFIG.RESERVE_CASH,
                         "BRICK_RATIO_config": CONFIG.BRICK_RATIO}}
    index_df_for_report = index_df

    if args.param and args.values:
        # ── 定点单参数扫描 ──
        if not strategy:
            print("✗ --param 模式必须指定 --strategy")
            sys.exit(1)
        vals = [float(x) for x in args.values.split(",") if x.strip()]
        print(f"[3/4] 单参数扫描: {strategy} · {args.param} · {vals}")
        rows = scan_single_param(strategy, args.param, vals, klines,
                                 all_start, all_end, index_df=index_df_for_report)
        if not rows:
            print("✗ 无结果（信号为 0 或数据不足）")
            sys.exit(1)
        print_single_scan(strategy, rows, args.param)
        report["single_scan"] = {"strategy": strategy, "param": args.param, "rows": rows}

    elif args.coordinate or args.all:
        strategies = ["B1", "B2", "B3", "砖型", "单针"] if args.all else [strategy]
        strategies = [s for s in strategies if s]
        if not strategies:
            print("✗ 请指定 --strategy 或 --all")
            sys.exit(1)
        print(f"[3/4] 坐标下降两轮: {strategies}  (IS:{args.split:.0%}, "
              f"{'含样本外' if args.oos else '仅样本内'})...")
        report["coordinate"] = {}
        for s in strategies:
            print(f"\n  ── 战法 {s} ──")
            r = run_coordinate_search(s, klines, all_start, all_end,
                                      do_oos=args.oos, split_ratio=args.split,
                                      index_df=index_df_for_report, verbose=True)
            print_coordinate_report(r)
            # 落盘时剔除冗长的 grid_results 明细（保留组数）
            r_slim = {k: v for k, v in r.items() if k != "grid_results"}
            r_slim["n_variants"] = len(r.get("grid_results", []))
            report["coordinate"][s] = r_slim
    else:
        print("[3/4] 未指定模式，打印可用网格（--coordinate / --all / --param+--values）")
        for s in ["B1", "B2", "B3", "砖型", "单针"]:
            specs = default_grid(s)
            print(f"\n  {s}:")
            for sp in specs:
                print(f"    {sp.param:<12} {['%g' % v for v in sp.values]}   {sp.label}")
        report["grids"] = {s: {sp.param: sp.values for sp in default_grid(s)}
                           for s in ["B1", "B2", "B3", "砖型", "单针"]}

    # ── 基准 ──
    if index_df_for_report is not None and len(index_df_for_report) > 0:
        bench = benchmark_return(index_df_for_report, str(all_start)[:10], str(all_end)[:10])
        report["benchmark_hs300"] = bench
        print(f"\n基准(沪深300, {str(all_start)[:10]}~{str(all_end)[:10]}): {bench}%")

    print(f"\n[4/4] 完成，耗时 {time.time()-t0:.1f}s")

    if args.save:
        os.makedirs(os.path.dirname(os.path.abspath(args.save)), exist_ok=True)
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告已保存: {args.save}")


if __name__ == "__main__":
    main()
