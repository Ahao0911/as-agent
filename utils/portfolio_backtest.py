# -*- coding: utf-8 -*-
"""组合层回测(portfolio_backtest)
==================================
解决现有回测的根本问题：`utils/backtest.py` 是**单标的、每笔交易独立按满仓算收益**，
没有"共享预算 + 现金约束 + 持仓上限"的组合语义，所以它算出的"收益率"不反映真实账户表现。

本模块在**不改一行 backtest.py**（冻结只读）的前提下，把单标的交易事件汇总成
时间轴事件流，用**事件驱动状态机**逐日推进组合资金，产出真实净值曲线。

【两种资金分配模式】（用户 M4 选项 C：两者都要，跑出对比）
  模式 A "GROUP"（现有实盘规则，2026-08-20 用户确认）
      三轮买入（来源: utils/midday_sim.py 三轮循环 + sim_portfolio.buy）:
        第一轮: 每组信号最优 1 只, 每只 = group_budget × BUY_RATIO (25万 × 50% = 12.5万)
        第二轮: 组内补第 2 只, 每只 = group_budget × BUY_RATIO (仍受组预算 25万 约束)
        第三轮: 现金富余时让渡给**已买入股票**加仓(allow_overflow=True, 可突破组预算),
                保留 reserve_cash 现金
  模式 B "BATCH3"（用户新提：单只票分三批建仓）
      同一只票分三批买入: 首批 50% → 第二批 25% → 第三批 25%
      ⚠️ 触发条件**用户未明确**，实现为可配置参数 `tranche_trigger`:
          "T1" 固定时间间隔(买入后 N 个交易日补第 2 批, 再 N 日补第 3 批) ← **默认, N=3**
          "T2" 价格回调触发(跌破买入价 X% 补)
          "T3" 价格上涨触发(突破买入价 X% 补) —— 属追高, 与用户"不追高"原则可能冲突
      ⚠️ **T1/N=3 是待用户确认的占位值**, 非用户确认口径。

【三个已实测的契约陷阱（不处理会算错钱）】
  陷阱A  `backtest_stock()` 返回的 trade **无 code 字段** → 组合层必须逐标的调用并自己打标。
  陷阱B  trade["日期"] 是**信号日**，不是买入日（backtest.py:273-274 buy_idx=i+1 但写入 df.index[i]）
        → 现金流日历必须按「信号日 + 1 个交易日」建。
  陷阱C  trade["收益"] 是**已扣费的百分比数值**(-0.04 = -0.04%)，不是小数、不是金额
        → 算金额盈亏**必须** qty × (卖出 - 买入) 反算，**绝不能**用 收益% × 资金。

【资金守恒硬约束】
    任意时点: cash + Σ 各组已用资金(建仓成本口径) == initial_capital
    （第三轮让渡加仓可突破组预算，但**总资金恒定**；手续费在离场时从现金中扣除，
      故用"已用资金"作守恒量、用"净值"作真实盈亏量，二者口径分离且都能自校验。）

用法:
    python utils/portfolio_backtest.py                       # 假数据自检 + 两模式对比
    python utils/portfolio_backtest.py --stocks 600519,000001 --mode A
"""
from __future__ import annotations

import os
import sys
import argparse
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 冻结底座(只读复用, 禁止修改 backtest.py) ──────────────────────────────
from utils.backtest import (
    TRADE_COST,
    backtest_stock,
    compute_signals,
    make_exit_rules,
)
from utils.strategy_config import CONFIG
from utils.metrics import full_report

# ── 组合层默认口径 ────────────────────────────────────────────────────────
STRATEGY_GROUPS: Dict[str, str] = {
    "B1": "B1组",
    "B2": "B1组",     # B1/B2/B3 同一波段(见 midday_sim: "B系战法组")
    "B3": "B1组",
    "砖型": "砖型组",
    "单针": "单针组",
}
DEFAULT_GROUPS: Tuple[str, ...] = ("B1组", "砖型组", "单针组", "深V组")

# 模式 B 分批比例（用户新提：首批 50% → 第二批 25% → 第三批 25%）
BATCH3_RATIOS: Tuple[float, float, float] = (0.50, 0.25, 0.25)
# 触发类型常量
TRIGGER_TIME, TRIGGER_PULLBACK, TRIGGER_BREAKOUT = "T1", "T2", "T3"

MODE_GROUP, MODE_BATCH3 = "A", "B"


def _round_lot(amount: float, price: float) -> int:
    """按手取整股数（1 手 = 100 股），与 sim_portfolio.buy 口径一致。

    Args:
        amount: 拟投金额(元)。
        price: 买入价(元/股)。

    Returns:
        int: 可取整的股数；不足 1 手返回 0。
    """
    if price is None or price <= 0 or amount is None or amount <= 0:
        return 0
    return int(amount / price / 100) * 100


def _next_trading_date(trading_days: Sequence[str], sig_date: str) -> Optional[str]:
    """求信号日的**下一个交易日**（陷阱 B 的核心工具）。

    Args:
        trading_days: 升序交易日字符串列表(YYYY-MM-DD)。
        sig_date: 信号日。

    Returns:
        Optional[str]: 下一交易日；信号日为最后一个交易日时返回 None。
    """
    if not trading_days:
        return None
    import bisect
    idx = bisect.bisect_right(list(trading_days), sig_date)
    if idx >= len(trading_days):
        return None
    return trading_days[idx]


def _date_at_or_after(trading_days: Sequence[str], date: str) -> Optional[str]:
    """求 >= date 的第一个交易日（离场日兜底用）。"""
    import bisect
    if not trading_days:
        return None
    idx = bisect.bisect_left(list(trading_days), date)
    if idx >= len(trading_days):
        return trading_days[-1]
    return trading_days[idx]


# ============================================================
# 数据结构（严格对齐架构师 4.3 节接口签名）
# ============================================================
@dataclass
class PortfolioConfig:
    """组合层配置。默认值来源 `utils.strategy_config.CONFIG`（唯一参数源）。

    ⚠️ 回测口径与实盘隔离：回测可覆写本对象参数，但**不回写 CONFIG**。
    """

    total_capital: float = 1_000_000.0
    group_budget: float = 250_000.0
    buy_ratio: float = 0.50
    max_positions_per_group: int = 2
    reserve_cash: float = 20_000.0
    max_groups_per_stock: int = 1
    intrabar_stop: bool = True
    cost_rate: float = TRADE_COST

    # ── 资金分配模式 ──
    allocation_mode: str = MODE_GROUP          # "A"(组内配比) / "B"(单只三批)
    # 模式 A: 第一/二轮为建仓、第三轮为让渡加仓（对齐 midday_sim 三轮）
    enable_round2: bool = True                 # 组内补第 2 只
    enable_overflow: bool = True               # 第三轮全抡让渡加仓(可突破组预算)
    overflow_min_cash: float = 100_000.0       # 触发全抡的现金门槛(对齐 midday_sim)
    overflow_max_ratio: float = 1.0            # 让渡加仓上限 = 组预算 × 该比例(防单只超配)
    # 模式 B: 分批参数（⚠️ 触发条件为占位值，待用户确认）
    batch_ratios: Tuple[float, float, float] = BATCH3_RATIOS
    tranche_trigger: str = TRIGGER_TIME        # T1/T2/T3
    tranche_interval: int = 3                  # T1: 间隔交易日数（**占位值, 待用户确认**）
    tranche_pullback_pct: float = 3.0          # T2: 回调触发幅度%（**占位值, 待用户确认**）
    tranche_breakout_pct: float = 3.0          # T3: 突破触发幅度%（**占位值, 待用户确认**）
    max_positions_total: int = 8               # 全组合上限(4组 × 2只)

    @classmethod
    def from_strategy_config(cls, **overrides) -> "PortfolioConfig":
        """从 `CONFIG` 读 GROUP_BUDGET / BUY_RATIO / RESERVE_CASH（不硬编码）。

        Args:
            **overrides: 需覆写的字段(如 allocation_mode="B")。

        Returns:
            PortfolioConfig: 实例。
        """
        cfg = cls(
            group_budget=float(CONFIG.GROUP_BUDGET),
            buy_ratio=float(CONFIG.BUY_RATIO),
            reserve_cash=float(CONFIG.RESERVE_CASH),
            cost_rate=float(TRADE_COST),
        )
        for key, val in overrides.items():
            if not hasattr(cfg, key):
                raise ValueError(f"未知配置字段: {key!r}")
            setattr(cfg, key, val)
        return cfg

    def validate(self) -> None:
        """参数自检，非法即抛 ValueError（回测前必须通过）。"""
        if self.allocation_mode not in (MODE_GROUP, MODE_BATCH3):
            raise ValueError(f"allocation_mode 非法: {self.allocation_mode!r}")
        if abs(sum(self.batch_ratios) - 1.0) > 1e-9:
            raise ValueError(f"batch_ratios 必须合计 1.0, 当前={self.batch_ratios}")
        if self.tranche_trigger not in (TRIGGER_TIME, TRIGGER_PULLBACK, TRIGGER_BREAKOUT):
            raise ValueError(f"tranche_trigger 非法: {self.tranche_trigger!r}")
        if self.tranche_interval < 1:
            raise ValueError("tranche_interval 必须 >= 1")
        if not (0 < self.buy_ratio <= 1):
            raise ValueError("buy_ratio 必须在 (0, 1]")
        if self.reserve_cash < 0:
            raise ValueError("reserve_cash 不能为负")


@dataclass
class TradeEvent:
    """由 `backtest_stock()` 输出规范化而来（解决契约陷阱 A/B/C）。

    金额盈亏一律由 `qty × (exit_price - entry_price) - fee` 反算，
    `raw_pnl_pct` 仅作对照参考，**不参与任何金额计算**。
    """

    code: str
    name: str
    strategy: str
    group: str
    signal_date: str                  # = trade["日期"]（**信号日**）
    entry_date: str                   # = 信号日的下一交易日 ★陷阱B
    entry_price: float                # = trade["买入"]（次日开盘价）
    exit_date: str
    exit_price: float                 # = trade["卖出"]
    exit_reason: str
    hold_bars: int
    raw_pnl_pct: float                # = trade["收益"]（**百分比数值, 仅参考**）★陷阱C


@dataclass
class Position:
    """组合内一个持仓（支持加仓/分批）。"""

    code: str
    name: str
    group: str
    qty: int = 0
    cost_price: float = 0.0           # 加权平均成本
    cost_amount: float = 0.0          # 累计投入成本(不含费)
    entry_date: str = ""
    signal_date: str = ""
    strategy: str = ""
    target_ratio: float = 0.0         # 目标资金占组预算比例
    tranche: int = 1                  # 已建批次数(模式 B)
    highest_price: float = 0.0
    fee_accum: float = 0.0            # 该仓位累计已确认手续费
    last_add_date: str = ""           # 最近一次建仓日(分批计时基准)
    exit_date: str = ""               # 计划离场日(来自所属 TradeEvent)
    exit_reason: str = ""             # 计划离场原因
    hold_bars: int = 0                # 计划持有根数

    def market_value(self, price: float) -> float:
        """按现价计算的市值。"""
        return float(self.qty * price) if price and price > 0 else 0.0

    def unrealized_pnl(self, price: float) -> float:
        """浮动盈亏金额（含已确认手续费）。"""
        if price is None or price <= 0:
            return 0.0
        return float(self.qty * price - self.cost_amount - self.fee_accum)

    def as_dict(self) -> dict:
        """序列化（用于报告/落盘）。"""
        return asdict(self)


@dataclass
class ClosedTrade:
    """已平仓交易（金额口径 + 百分比口径并存，便于交叉校验）。"""

    code: str
    name: str
    group: str
    strategy: str
    qty: int
    cost_price: float
    exit_price: float
    entry_date: str
    exit_date: str
    pnl_amt: float                    # 金额盈亏(已扣费)
    pnl_pct: float                    # 百分比盈亏(已扣费)
    exit_reason: str
    hold_bars: int
    tranche: int = 1

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class GroupState:
    """一个战法分组的预算状态。"""

    name: str
    budget: float = 0.0
    used: float = 0.0                 # 已用资金(建仓成本口径, 不含费)
    positions: List[str] = field(default_factory=list)
    created: List[str] = field(default_factory=list)  # 本组当日新建仓的 code(第三轮让渡用)
    overflow: float = 0.0             # 让渡突破组预算的金额

    def available(self) -> float:
        """组内剩余预算（可为 0，不返负）。"""
        return max(0.0, self.budget - self.used)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["available"] = round(self.available(), 2)
        return d


@dataclass
class PortfolioState:
    """组合全局状态 + 每日净值曲线。"""

    cash: float = 0.0
    initial_capital: float = 0.0
    groups: Dict[str, GroupState] = field(default_factory=dict)
    positions: Dict[str, Position] = field(default_factory=dict)   # key = "组|code"
    closed: List[ClosedTrade] = field(default_factory=list)
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    fees_paid: float = 0.0           # 已平仓交易的**双边**费用累计(含买入侧)
    realized_pnl_gross: float = 0.0  # 已实现**毛**盈亏(卖出成交额−建仓成交额, 不含费)
    realized_pnl: float = 0.0        # 已实现**净**盈亏(已扣双边费, 用于绩效口径)
    skipped: List[dict] = field(default_factory=list)
    as_of: str = ""

    # ---- 组合级查询 ----
    def group_used(self, group: str) -> float:
        """该组已用资金(建仓成本口径)。"""
        g = self.groups.get(group)
        return float(g.used) if g else 0.0

    def total_used(self) -> float:
        """全组合已用资金(建仓成本口径)。"""
        return float(sum(g.used for g in self.groups.values()))

    def open_fees(self) -> float:
        """当前**未平仓**持仓已发生的买入侧手续费合计（尚未计入 fees_paid）。"""
        return float(sum(p.fee_accum for p in self.positions.values()))

    def allocated_capital(self) -> float:
        """守恒量 A(不含费): cash + Σ各组已用资金(建仓成交额口径)。

        恒等式（**不含费**）: allocated_capital + total_fees == initial_capital + realized_pnl_gross
        """
        return float(self.cash + self.total_used())

    def total_fees(self) -> float:
        """累计已发生手续费 = 已平仓实现部分 + 未平仓持仓买入费。"""
        return float(self.fees_paid + self.open_fees())

    def total_equity(self, prices: Dict[str, float]) -> float:
        """总净值 = 现金 + 持仓市值。

        ⚠️ 不再减 fees_paid: 所有手续费在建仓/离场时已直接从 `cash` 扣除，
        此处再减会**双重扣费**（老实现的 bug）。买入侧未实现费已体现在现金里。
        """
        mv = 0.0
        for pos in self.positions.values():
            mv += pos.market_value(prices.get(pos.code))
        return float(self.cash + mv)

    def snapshot_equity(self, date: str, prices: Dict[str, float]) -> None:
        """记录当日净值到曲线（**每日都要记，含空仓日**）。"""
        self.equity_curve.append((date, round(self.total_equity(prices), 2)))
        self.as_of = date

    def verify_conservation(self, tol: float = 1e-6) -> bool:
        """资金守恒校验（严格恒等式，逐日可验）。

        `cash + Σ已用资金 + 累计已发生手续费 == initial_capital + 已实现毛盈亏`

        其中:
            - cash      : 账户现金
            - Σ已用资金 : 各持仓的建仓成交额(不含费)
            - 累计已发生手续费 : 已平仓部分(fees_paid) + 未平仓持仓买入费(open_fees)
            - 已实现毛盈亏(gross) : 已平仓的 (卖出成交额 − 建仓成交额)，**不含费**

        即: 现金 + 占用资金 + 已付/应付费用 == 本金 + 毛盈亏。
        （每笔买卖只做 cash↔used 搬运，费用单独累计，故该式**任意时点**恒成立。）
        """
        expected = self.initial_capital + self.realized_pnl_gross
        actual = self.allocated_capital() + self.total_fees()
        delta = abs(actual - expected)
        return delta <= max(tol, 1e-6 * abs(self.initial_capital))

    def conservation_gap(self) -> float:
        """守恒偏差额(元)，正常应 ≈ 0；非 0 即 accounting bug。"""
        return float(self.allocated_capital() + self.total_fees()
                     - (self.initial_capital + self.realized_pnl_gross))


def normalize_klines(klines: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """归一化行情入参：保证每只 DataFrame 都是 **DatetimeIndex + 升序 + 去重**。

    为什么要这一步（跨模块实测坑）:
        `utils.backtest_data.load_universe_klines()` 返回的是 **`date` 普通列 +
        RangeIndex(0,1,2…)**，不是 DatetimeIndex。若直接 `pd.to_datetime(df.index)`，
        RangeIndex 会被静默解释为纳秒时间戳 → **所有日期塌缩成 1970-01-01**，
        交易日历只剩 1 天，回测结果**静默报废且不报错**（比崩溃更危险）。

    本函数在**不修改调用方对象**的前提下（按需 copy）做三件事:
        1. 若 index 不是 DatetimeIndex：优先取 `date`/`日期`/`trade_date` 列转索引，
           否则尝试把 index 强转 datetime（仅在能成功产出合法日期时采纳）。
        2. 索引按时间升序排序。
        3. 索引去重（保留后出现者，避免重复日期破坏"严格递增"）。
        ⚠️ 校验：若转换后出现 1970 纪元时间戳（RangeIndex 误转的典型特征），
           视为失败并抛 ValueError，**绝不静默吞掉**。

    Args:
        klines: {code: DataFrame}。

    Returns:
        dict: {code: DataFrame}，均带 DatetimeIndex（升序、唯一）。

    Raises:
        ValueError: 当某只 DataFrame 无法得到合法日期索引时。
    """
    out: Dict[str, pd.DataFrame] = {}
    for code, df in (klines or {}).items():
        if df is None or df.empty:
            continue
        if isinstance(df.index, pd.DatetimeIndex):
            fixed = df.sort_index()
        else:
            # 先找日期列
            date_col = None
            for cand in ("date", "日期", "trade_date", "datetime"):
                if cand in df.columns:
                    date_col = cand
                    break
            if date_col is not None:
                fixed = df.copy()
                fixed.index = pd.to_datetime(fixed[date_col])
                fixed = fixed.drop(columns=[date_col])
            else:
                # 尝试直接把 index 转日期（纯字符串日期索引场景）
                converted = pd.to_datetime(df.index)
                if _is_epoch_collapse(converted):
                    raise ValueError(
                        f"[{code}] 无法得到合法日期索引：index 非 DatetimeIndex 且无 date 列，"
                        f"强转得到 1970 纪元时间戳（疑似 RangeIndex 误转）。"
                        f"请改用 DatetimeIndex 或提供 date 列。")
                fixed = df.copy()
                fixed.index = converted
            fixed = fixed.sort_index()
        if _is_epoch_collapse(pd.DatetimeIndex(fixed.index)):
            raise ValueError(f"[{code}] 日期索引疑似塌缩为 1970 纪元，拒绝执行（防静默报废）")
        fixed = fixed[~fixed.index.duplicated(keep="last")]
        out[code] = fixed
    return out


def _is_epoch_collapse(idx: pd.DatetimeIndex) -> bool:
    """检测 1970 纪元塌缩（RangeIndex 被误转成 datetime 的典型特征）。

    Args:
        idx: 待检测索引。

    Returns:
        bool: 若所有值都落在 1970-01-01 ~ 1970-01-02（纳秒纪元区）则视为塌缩。
    """
    if len(idx) == 0:
        return False
    try:
        vals = pd.DatetimeIndex(idx)
    except Exception:
        return False
    if vals.tz is not None:
        vals = vals.tz_localize(None)
    in_epoch = (vals >= pd.Timestamp("1970-01-01")) & (vals < pd.Timestamp("1970-01-03"))
    return bool(in_epoch.all())


# ============================================================
# 组合回测引擎
# ============================================================
class PortfolioBacktester:
    """组合层回测引擎（事件驱动状态机）。

    职责边界：本类只做"资金分配 + 建仓/离场调度 + 净值记账"。
    单标的的买卖点与离场规则**全部复用冻结的 backtest.py**，不重写战法逻辑。
    """

    def __init__(self, config: Optional[PortfolioConfig] = None) -> None:
        """初始化。

        Args:
            config: 组合配置；None 时从 CONFIG 构造默认值。
        """
        self.config: PortfolioConfig = config or PortfolioConfig.from_strategy_config()
        self.config.validate()
        self.rules: Optional[dict] = None
        self.klines: Dict[str, pd.DataFrame] = {}
        self.trading_days: List[str] = []
        self.day_index: Dict[str, int] = {}
        self._price_map: Dict[str, Dict[str, float]] = {}   # {code: {date: close}}
        self._open_map: Dict[str, Dict[str, float]] = {}    # {code: {date: open}}
        # 可选: 每日守恒校验回调, 签名 on_day(state, date) -> None（供 QA/自检）
        self.on_day: Optional[Callable[[PortfolioState, str], None]] = None

    # ---------------- 预处理 ----------------
    def _build_calendar(self, klines: Dict[str, pd.DataFrame]) -> List[str]:
        """用全体标的交易日并集构造全局交易日历（升序）。

        用并集而非交集：某标的停牌不该让全组合"冻结一天"，停牌日按无价处理。
        """
        days = set()
        for df in klines.values():
            if df is None or df.empty:
                continue
            days.update(pd.to_datetime(df.index).strftime("%Y-%m-%d").tolist())
        ordered = sorted(days)
        self.trading_days = ordered
        self.day_index = {d: i for i, d in enumerate(ordered)}
        return ordered

    def _build_price_maps(self, klines: Dict[str, pd.DataFrame]) -> None:
        """预建 open/close 查表，避免逐日反复索引 DataFrame。"""
        self._price_map = {}
        self._open_map = {}
        for code, df in klines.items():
            if df is None or df.empty:
                continue
            idx = pd.to_datetime(df.index).strftime("%Y-%m-%d")
            closes = df["close"].astype(float).values
            opens = df["open"].astype(float).values
            self._price_map[code] = dict(zip(idx, closes))
            self._open_map[code] = dict(zip(idx, opens))

    def _price(self, code: str, date: str) -> Optional[float]:
        """取该日收盘价；无价(停牌/未上市)返回 None。"""
        return self._price_map.get(code, {}).get(date)

    def _open_price(self, code: str, date: str) -> Optional[float]:
        """取该日开盘价；无价返回 None。"""
        return self._open_map.get(code, {}).get(date)

    def _snapshot_prices(self, date: str) -> Dict[str, float]:
        """当日全组合持仓的收盘价快照（缺失用最近成本价兜底, 保证净值不跳空）。"""
        prices: Dict[str, float] = {}
        for pos in self._state().positions.values():
            p = self._price(pos.code, date)
            prices[pos.code] = p if p and p > 0 else pos.cost_price
        return prices

    # ---------------- 状态访问 ----------------
    def _state(self) -> PortfolioState:
        """当前运行状态（run() 期间由 self._st 指向）。"""
        if getattr(self, "_st", None) is None:
            raise RuntimeError("状态未初始化：请通过 run() 执行")
        return self._st

    # ---------------- 交易事件归一化 ----------------
    def prepare_trades(self, klines: Dict[str, pd.DataFrame], rules: Optional[dict] = None,
                       strategies: Optional[Sequence[str]] = None) -> List[TradeEvent]:
        """逐标的调 `backtest_stock()`，归一化为 `TradeEvent` 列表。

        解决三陷阱:
            A) 逐标的调用并注入 code/name(group 由 strategy→group 映射得出)
            B) signal_date → entry_date 用交易日历 +1 天
            C) 保留 raw_pnl_pct 仅作参考，金额由 qty×(卖-买) 反算

        Args:
            klines: {code: DataFrame}（MultiIndex 无关，需 DatetimeIndex + OHLCV）。
            rules: 离场规则；None 用 `make_exit_rules()`。
            strategies: 只保留这些战法；None 表示全战法。

        Returns:
            List[TradeEvent]: 按 entry_date 升序（同日按 code 字典序稳定排序）。
        """
        self.rules = rules or make_exit_rules()
        self.klines = normalize_klines(klines)   # 防 RangeIndex 日期塌缩（跨模块实测坑）
        self._build_calendar(self.klines)
        self._build_price_maps(self.klines)

        events: List[TradeEvent] = []
        for code in sorted(klines.keys()):
            df = klines.get(code)
            if df is None or df.empty or len(df) < 2:
                continue
            signals = compute_signals(df)
            per_stock = backtest_stock(signals, df, rules=self.rules)  # ★陷阱A: 逐标的调用
            for strat, trades in per_stock.items():
                if strategies is not None and strat not in strategies:
                    continue
                group = STRATEGY_GROUPS.get(strat, f"{strat}组")
                for t in trades:
                    sig_date = str(t.get("日期", ""))[:10]
                    entry_date = _next_trading_date(self.trading_days, sig_date)  # ★陷阱B
                    if entry_date is None:
                        continue
                    # 离场日 = 出场交易日; 缺失时按 entry + hold_bars-1 兜底
                    exit_date = self._infer_exit_date(code, sig_date, t, entry_date)
                    events.append(TradeEvent(
                        code=code,
                        name=code,
                        strategy=strat,
                        group=group,
                        signal_date=sig_date,
                        entry_date=entry_date,
                        entry_price=float(t.get("买入", 0.0) or 0.0),   # ★陷阱C: 金额只认价格
                        exit_date=exit_date,
                        exit_price=float(t.get("卖出", 0.0) or 0.0),
                        exit_reason=str(t.get("原因", "")),
                        hold_bars=int(t.get("持有", 1) or 1),
                        raw_pnl_pct=float(t.get("收益", 0.0) or 0.0),    # 仅参考
                    ))
        events.sort(key=lambda e: (e.entry_date, e.code, e.strategy))
        return events

    def _infer_exit_date(self, code: str, sig_date: str, trade: dict, entry_date: str) -> str:
        """推断离场交易日。

        `backtest_stock` 只给出信号日与持有根数。离场日 = entry_date 在全局
        交易日历上后移 (hold_bars - 1) 个交易日（买当日算 1 根）。
        """
        hold = int(trade.get("持有", 1) or 1)
        i = self.day_index.get(entry_date)
        if i is None:
            return entry_date
        j = min(i + max(hold - 1, 0), len(self.trading_days) - 1)
        return self.trading_days[j]

    # ---------------- 资金分配（两种模式的核心差异） ----------------
    def _open_positions_of_group(self, st: PortfolioState, group: str) -> int:
        """该组当前持仓只数。"""
        g = st.groups.get(group)
        return len(g.positions) if g else 0

    def _groups_of_stock(self, st: PortfolioState, code: str) -> List[str]:
        """该标的当前已在哪些组持仓（用于"同标的不跨组重复持有"约束）。"""
        return [g for g in st.groups if any(k.endswith(f"|{code}") for k in
                                            st.groups[g].positions)]

    def _open_position(self, st: PortfolioState, ev: TradeEvent, position_key: str,
                       amount: float, group_state: GroupState, price: float,
                       ratio: float, allow_overflow: bool) -> bool:
        """执行一次建仓/加仓（现金与预算约束由调用方已完成校验）。

        记账口径（与 sim_portfolio.buy 对齐）:
            - 现金流出 = 成交额 + 买入费(cost_rate)
            - 持仓成本 cost_amount 只记**成交额**；买入费单独累计到 pos.fee_accum
              → 这样"已用资金"= 建仓成交额，守恒式干净；费用不污染预算占用。

        Args:
            st: 组合状态。
            ev: 交易事件（提供 code/strategy/信号日）。
            position_key: "组|code"。
            amount: 拟投金额(元)。
            group_state: 所属组状态。
            price: 成交价(次日开盘)。
            ratio: 该批次资金占**组预算**的比例。
            allow_overflow: 是否允许突破组预算。

        Returns:
            bool: 是否成交（股数不足 1 手 → False）。
        """
        cfg = self.config
        qty = _round_lot(amount, price)
        if qty <= 0:
            return False
        cost = qty * price
        fee = cost * cfg.cost_rate                 # 买入侧费用(单边)
        if cost + fee > st.cash + 1e-6:
            # 按可用现金回缩到整手(预留手续费)
            qty = _round_lot(max(st.cash / (1 + cfg.cost_rate), 0.0), price)
            if qty <= 0:
                return False
            cost = qty * price
            fee = cost * cfg.cost_rate
        if cost + fee > st.cash + 1e-6:
            return False

        st.cash -= (cost + fee)
        over_budget = max(0.0, group_state.used + cost - group_state.budget)
        group_state.used += cost
        if allow_overflow and over_budget > 0:
            group_state.overflow += over_budget

        pos = st.positions.get(position_key)
        if pos is None:
            pos = Position(code=ev.code, name=ev.name, group=group_state.name,
                           entry_date=ev.entry_date, signal_date=ev.signal_date,
                           strategy=ev.strategy, target_ratio=ratio,
                           tranche=0, last_add_date=ev.entry_date,
                           exit_date=ev.exit_date, exit_reason=ev.exit_reason,
                           hold_bars=ev.hold_bars)
            st.positions[position_key] = pos
            group_state.positions.append(position_key)
            group_state.created.append(ev.code)
        else:
            # 已在持仓: 保留最早的离场计划（首仓的离场日决定清仓时点）
            if not pos.exit_date or ev.exit_date < pos.exit_date:
                pos.exit_date = ev.exit_date
                pos.exit_reason = ev.exit_reason
                pos.hold_bars = ev.hold_bars
        # 加权平均成本（只按成交额加权，费不入成本）
        pos.cost_amount += cost
        pos.qty += qty
        pos.cost_price = round(pos.cost_amount / pos.qty, 4) if pos.qty else 0.0
        pos.highest_price = max(pos.highest_price, price)
        pos.last_add_date = ev.entry_date
        pos.fee_accum += fee
        pos.tranche += 1
        return True

    def _allocate(self, st: PortfolioState, ev: TradeEvent, date: str) -> bool:
        """资金分配决策（两种模式的分流入口）。

        约束链（任一不过 → 拒单并记 skip_reason）:
            ① 现金 >= 拟投金额 + reserve_cash
            ② 组已用 + 拟投 <= group_budget（让渡加仓除外）
            ③ 组内持仓数 <= max_positions_per_group
            ④ 同标的不跨组重复持有

        Args:
            st: 组合状态。
            ev: 待入场交易事件（entry_date == date）。
            date: 当前交易日。

        Returns:
            bool: 是否新开仓成功（模式 B 的补批不走这里）。
        """
        cfg = self.config
        price = self._open_price(ev.code, date) or ev.entry_price
        if price is None or price <= 0:
            st.skipped.append({"date": date, "code": ev.code, "reason": "无有效开盘价"})
            return False
        g = st.groups.get(ev.group)
        if g is None:
            st.skipped.append({"date": date, "code": ev.code, "reason": f"未知分组{ev.group}"})
            return False
        key = f"{ev.group}|{ev.code}"

        # ① 同标的不跨组重复持有（模式 A/B 共同约束）
        holders = self._groups_of_stock(st, ev.code)
        if holders and ev.group not in holders:
            if len(holders) >= cfg.max_groups_per_stock:
                st.skipped.append({"date": date, "code": ev.code,
                                   "reason": f"同标的已在他组持仓{holders}"})
                return False
        # ② 组内持仓上限
        if key not in st.positions and self._open_positions_of_group(st, ev.group) >= cfg.max_positions_per_group:
            st.skipped.append({"date": date, "code": ev.code,
                               "reason": f"组{ev.group}持仓已达上限{cfg.max_positions_per_group}"})
            return False
        # ③ 全组合持仓上限
        if key not in st.positions and len(st.positions) >= cfg.max_positions_total:
            st.skipped.append({"date": date, "code": ev.code,
                               "reason": f"全组合持仓达上限{cfg.max_positions_total}"})
            return False

        if cfg.allocation_mode == MODE_GROUP:
            return self._allocate_group_mode(st, ev, g, key, price)
        return self._allocate_batch3_mode(st, ev, g, key, price)

    def _allocate_group_mode(self, st: PortfolioState, ev: TradeEvent, g: GroupState,
                             key: str, price: float) -> bool:
        """模式 A：组内配比（第一轮 1 只 × 50% / 第二轮补第 2 只 × 50%）。

        对应 midday_sim.py 第 310-351 行的一二轮循环。
        第三轮让渡加仓由 `_overflow_add()` 在入场处理**之后**统一执行。
        """
        cfg = self.config
        # 第二轮约束
        if key not in st.positions and self._open_positions_of_group(st, ev.group) >= 1 and not cfg.enable_round2:
            st.skipped.append({"date": date_str(ev.entry_date), "code": ev.code, "reason": "第二轮补买已禁用"})
            return False
        amount = min(cfg.group_budget * cfg.buy_ratio, g.available())
        if amount <= 0:
            st.skipped.append({"date": ev.entry_date, "code": ev.code, "reason": "组预算已用满"})
            return False
        if st.cash - amount < cfg.reserve_cash:
            st.skipped.append({"date": ev.entry_date, "code": ev.code,
                               "reason": f"现金不足(需{amount:.0f}+保留{cfg.reserve_cash:.0f}, 余{st.cash:.0f})"})
            return False
        return self._open_position(st, ev, key, amount, g, price, cfg.buy_ratio, False)

    def _allocate_batch3_mode(self, st: PortfolioState, ev: TradeEvent, g: GroupState,
                              key: str, price: float) -> bool:
        """模式 B：单只票分三批建仓，首批 = 组预算 × batch_ratios[0] (50%)。

        后续批次由 `_process_tranches()` 按触发条件(默认 T1/N=3)补足。
        """
        cfg = self.config
        full_amount = cfg.group_budget * cfg.buy_ratio          # 单只目标总额
        first_amount = full_amount * cfg.batch_ratios[0]        # 首批 50%
        amount = min(first_amount, g.available())
        if amount <= 0:
            st.skipped.append({"date": ev.entry_date, "code": ev.code, "reason": "组预算已用满"})
            return False
        if st.cash - amount < cfg.reserve_cash:
            st.skipped.append({"date": ev.entry_date, "code": ev.code,
                               "reason": f"现金不足(需{amount:.0f}+保留{cfg.reserve_cash:.0f}, 余{st.cash:.0f})"})
            return False
        return self._open_position(st, ev, key, amount, g, price,
                                   cfg.buy_ratio * cfg.batch_ratios[0], False)

    def _tranche_triggered(self, pos: Position, date: str, price: float) -> bool:
        """模式 B：判断是否满足补批触发条件。

        Args:
            pos: 已持仓。
            date: 当前交易日。
            price: 当前价(用收盘价判断, 成交仍按开盘)。

        Returns:
            bool: 是否触发下一批。
        """
        cfg = self.config
        trig = cfg.tranche_trigger
        if trig == TRIGGER_TIME:
            i_now = self.day_index.get(date)
            i_last = self.day_index.get(pos.last_add_date, i_now)
            if i_now is None or i_last is None:
                return False
            return (i_now - i_last) >= cfg.tranche_interval
        if trig == TRIGGER_PULLBACK:
            return price <= pos.cost_price * (1 - cfg.tranche_pullback_pct / 100.0)
        # TRIGGER_BREAKOUT
        return price >= pos.cost_price * (1 + cfg.tranche_breakout_pct / 100.0)

    def _add_to_position(self, st: PortfolioState, pos: Position, date: str,
                         open_px: float, amount: float, allow_overflow: bool) -> bool:
        """对已有持仓加仓（模式 B 补批 / 模式 A 让渡 共用）。

        统一记账口径（与 `_open_position` 一致）:
            cash -= (成交额 + 买入费);  cost_amount += 成交额;  fee_accum += 买入费。

        Args:
            st: 组合状态。
            pos: 目标持仓。
            date: 成交日。
            open_px: 成交价(开盘)。
            amount: 拟投金额(元)。
            allow_overflow: 是否允许突破组预算。

        Returns:
            bool: 是否成交。
        """
        cfg = self.config
        g = st.groups.get(pos.group)
        if g is None or open_px is None or open_px <= 0:
            return False
        # 非让渡模式受组预算剩余约束；让渡模式仅受现金约束(可突破组预算)
        if not allow_overflow:
            amount = min(amount, g.available())
        if amount <= 0:
            return False
        qty = _round_lot(amount, open_px)
        if qty <= 0:
            return False
        cost = qty * open_px
        fee = cost * cfg.cost_rate
        if cost + fee > st.cash + 1e-6:
            qty = _round_lot(max(st.cash / (1 + cfg.cost_rate), 0.0), open_px)
            if qty <= 0:
                return False
            cost = qty * open_px
            fee = cost * cfg.cost_rate
            if cost + fee > st.cash + 1e-6:
                return False
        st.cash -= (cost + fee)
        over = max(0.0, g.used + cost - g.budget)
        g.used += cost
        if allow_overflow and over > 0:
            g.overflow += over
        pos.cost_amount += cost
        pos.qty += qty
        pos.cost_price = round(pos.cost_amount / pos.qty, 4) if pos.qty else 0.0
        pos.highest_price = max(pos.highest_price, open_px)
        pos.last_add_date = date
        pos.fee_accum += fee
        pos.tranche += 1
        return True

    def _process_tranches(self, st: PortfolioState, date: str) -> None:
        """模式 B：对每个未建满 N 批的持仓尝试补批（第 2/3 批）。

        触发条件由 `_tranche_triggered` 决定（默认 T1/N=3，**占位值待用户确认**）。
        """
        cfg = self.config
        for key, pos in list(st.positions.items()):
            if pos.tranche >= len(cfg.batch_ratios):
                continue
            if date <= pos.entry_date:      # 建仓当日不补
                continue
            close = self._price(pos.code, date) or pos.cost_price
            if not self._tranche_triggered(pos, date, close):
                continue
            open_px = self._open_price(pos.code, date) or close
            full_amount = cfg.group_budget * cfg.buy_ratio
            ratio = cfg.batch_ratios[pos.tranche]           # 第2批 25% / 第3批 25%
            amount = full_amount * ratio
            g = st.groups.get(pos.group)
            if g is not None and amount > g.available():
                st.skipped.append({"date": date, "code": pos.code,
                                   "reason": "组预算已用满(补批跳过)"})
                continue
            if st.cash - amount < cfg.reserve_cash:
                st.skipped.append({"date": date, "code": pos.code,
                                   "reason": f"现金不足(补批需{amount:.0f}, 余{st.cash:.0f})"})
                continue
            self._add_to_position(st, pos, date, open_px, amount, allow_overflow=False)

    def _overflow_add(self, st: PortfolioState, date: str) -> None:
        """模式 A 第三轮：现金富余时让渡给**当日新建仓**的股票加仓。

        对齐 midday_sim.py 第 353-384 行语义:
            - 触发门槛: 现金 > overflow_min_cash
            - 参与对象: **仅当日新建仓**的标的（`group_state.created`，每日清空）
            - 每只加仓额 = (现金 − reserve_cash) / 参与只数，按百取整
            - `allow_overflow=True` → 可突破组预算，但总资金恒定

        额外安全护栏（避免单只被"打爆"到远超组预算）:
            - 单只累计投入 ≤ group_budget × overflow_max_ratio（默认 1.0 倍组预算 = 25 万）
            - 每只当日只加一次
        """
        cfg = self.config
        if cfg.allocation_mode != MODE_GROUP or not cfg.enable_overflow:
            return
        if st.cash <= cfg.overflow_min_cash:
            return
        targets: List[Position] = []
        seen = set()
        for g in st.groups.values():
            for code in g.created:
                key = f"{g.name}|{code}"
                pos = st.positions.get(key)
                if pos is not None and key not in seen:
                    seen.add(key)
                    targets.append(pos)
        # 过滤掉已达单只上限的持仓
        cap_per_stock = cfg.group_budget * cfg.overflow_max_ratio
        targets = [p for p in targets if p.cost_amount < cap_per_stock]
        if not targets:
            return
        usable = st.cash - cfg.reserve_cash
        if usable <= 0:
            return
        per_stock = int(usable / len(targets) / 100) * 100
        if per_stock <= 0:
            return
        for pos in targets:
            # 单只不超过 cap（含加仓后）且不超过可用现金
            room = min(cap_per_stock - pos.cost_amount, st.cash - cfg.reserve_cash)
            amount = min(per_stock, room)
            if amount <= 0:
                continue
            open_px = self._open_price(pos.code, date) or self._price(pos.code, date)
            self._add_to_position(st, pos, date, open_px, amount, allow_overflow=True)

    # ---------------- 离场 ----------------
    def _process_exits(self, st: PortfolioState, date: str) -> None:
        """按交易日执行到期的离场（在入场之前执行, 保证现金回笼优先）。"""
        for key, pos in list(st.positions.items()):
            if pos.exit_date and date >= pos.exit_date:
                self._close_position(st, key, pos, date)

    def _close_position(self, st: PortfolioState, key: str, pos: Position, date: str) -> None:
        """平仓并记账。

        金额盈亏（★陷阱C 的正确算法）:
            pnl_amt = qty × exit_price − qty × entry_cost − buy_fee − sell_fee
                    = 卖出成交额 − 建仓成交额 − 双边费用
        **绝不**使用 backtest_stock 的 收益% 乘资金（那是百分比, 不是金额）。
        """
        cfg = self.config
        exit_px = self._price(pos.code, date) or pos.cost_price
        proceeds = pos.qty * exit_px
        exit_fee = proceeds * cfg.cost_rate
        gross = proceeds - pos.cost_amount                   # 毛盈亏(不含费)
        pnl_amt = gross - pos.fee_accum - exit_fee           # 净盈亏(扣双边费)
        pnl_pct = (pnl_amt / pos.cost_amount * 100.0) if pos.cost_amount > 0 else 0.0

        st.cash += (proceeds - exit_fee)
        st.fees_paid += exit_fee + pos.fee_accum             # 平仓时把买入侧费一并归集
        st.realized_pnl_gross += gross
        st.realized_pnl += pnl_amt

        g = st.groups.get(pos.group)
        if g is not None:
            g.used = max(0.0, g.used - pos.cost_amount)
            if key in g.positions:
                g.positions.remove(key)
        event = next((e for e in self._events_by_key.get(key, [])), None)
        st.closed.append(ClosedTrade(
            code=pos.code, name=pos.name, group=pos.group, strategy=pos.strategy,
            qty=pos.qty, cost_price=pos.cost_price, exit_price=round(exit_px, 4),
            entry_date=pos.entry_date, exit_date=date,
            pnl_amt=round(pnl_amt, 2), pnl_pct=round(pnl_pct, 4),
            exit_reason=(pos.exit_reason or (event.exit_reason if event else "离场")),
            hold_bars=(pos.hold_bars or (event.hold_bars if event else 0)),
            tranche=pos.tranche,
        ))
        del st.positions[key]

    # ---------------- 主流程 ----------------
    def run(self, trade_events: List[TradeEvent],
            klines: Optional[Dict[str, pd.DataFrame]] = None) -> PortfolioState:
        """★核心：按全局交易日历逐日推进的状态机。

        每日顺序（强序贯, 决定资金路径）:
            1) 先执行到期离场（现金回笼）   ← 离场优先于建仓
            2) 再处理新入场（受预算/现金/持仓数约束）
            3) 模式 B：补批；模式 A：第三轮让渡加仓
            4) mark-to-market 记录当日净值（**空仓日也记 0 收益**）

        Args:
            trade_events: `prepare_trades()` 的输出。
            klines: 标的行情；None 时用 `prepare_trades()` 缓存的 klines。

        Returns:
            PortfolioState: 含 cash/groups/positions/closed/equity_curve。
        """
        cfg = self.config
        if klines is not None:
            self.klines = normalize_klines(klines)   # 同样防 RangeIndex 日期塌缩
        if not self.trading_days:
            self._build_calendar(self.klines)
        if not self._price_map:
            self._build_price_maps(self.klines)

        st = PortfolioState(cash=float(cfg.total_capital), initial_capital=float(cfg.total_capital))
        st.groups = {g: GroupState(name=g, budget=float(cfg.group_budget)) for g in DEFAULT_GROUPS}
        self._st = st
        self._events_by_key = {}
        for ev in trade_events:
            self._events_by_key.setdefault(f"{ev.group}|{ev.code}", []).append(ev)

        # 事件按 entry_date 分桶
        by_entry: Dict[str, List[TradeEvent]] = {}
        for ev in trade_events:
            by_entry.setdefault(ev.entry_date, []).append(ev)
        # 待入场队列（未成功建仓的降级队列, 供后续日重试）
        pending: List[TradeEvent] = []

        for date in self.trading_days:
            # 1) 离场优先
            self._process_exits(st, date)
            # 2) 入场（当日事件 + 顺延的 pending）
            today_events = by_entry.get(date, []) + [e for e in pending
                                                     if e.entry_date < date]
            pending = [e for e in pending if e.entry_date >= date]
            for g_state in st.groups.values():
                g_state.created.clear()          # 第三轮让渡只认当日新建仓
            for ev in sorted(today_events, key=lambda e: (e.code, e.strategy)):
                self._allocate(st, ev, date)
            # 3) 模式 B 补批 / 模式 A 第三轮让渡
            if cfg.allocation_mode == MODE_BATCH3:
                self._process_tranches(st, date)
            else:
                self._overflow_add(st, date)
            # 4) 记账（含空仓日）
            st.snapshot_equity(date, self._snapshot_prices(date))
            # 5) 可选钩子: 每日守恒/健康校验（异常由调用方捕获, 不影响主流程）
            if self.on_day is not None:
                self.on_day(st, date)

        # 收尾：最后一根K线后仍未离场的持仓按最后价强平, 保证净值口径一致。
        # ⚠️ 只**覆盖**最后一天的净值快照, 不追加新点 —— 否则曲线长度会多 1、
        #    且出现重复日期破坏"严格递增"（Sharpe/年化依赖日频且唯一）。
        if st.positions and self.trading_days:
            last_date = self.trading_days[-1]
            for key, pos in list(st.positions.items()):
                self._close_position(st, key, pos, last_date)
            if st.equity_curve and st.equity_curve[-1][0] == last_date:
                st.equity_curve[-1] = (last_date, round(st.total_equity({}), 2))
            else:
                st.snapshot_equity(last_date, {})
        return st

    # ---------------- 一键入口 ----------------
    def run_all(self, klines: Dict[str, pd.DataFrame],
                rules: Optional[dict] = None) -> Tuple[PortfolioState, List[TradeEvent]]:
        """prepare_trades + run 的便捷组合。

        Args:
            klines: {code: DataFrame}。
            rules: 离场规则；None → make_exit_rules()。

        Returns:
            tuple: (PortfolioState, List[TradeEvent])。
        """
        events = self.prepare_trades(klines, rules=rules)
        state = self.run(events, klines)
        return state, events


def date_str(value: str) -> str:
    """把任意日期值规范为 YYYY-MM-DD 字符串（小工具，保持可读性）。"""
    return str(value)[:10]


def run_portfolio_backtest(klines: Dict[str, pd.DataFrame], rules: Optional[dict] = None,
                           config: Optional[PortfolioConfig] = None,
                           mode: str = MODE_GROUP) -> PortfolioState:
    """便捷入口（架构 4.3 节签名）。

    Args:
        klines: {code: DataFrame}。
        rules: 离场规则；None → make_exit_rules()。
        config: 组合配置；None → 从 CONFIG 构造（并用 mode 覆写分配模式）。
        mode: "A"(组内配比) / "B"(单只三批建仓)。

    Returns:
        PortfolioState: 组合最终状态（含 equity_curve / closed）。
    """
    cfg = config or PortfolioConfig.from_strategy_config(allocation_mode=mode)
    cfg.allocation_mode = mode
    cfg.validate()
    bt = PortfolioBacktester(cfg)
    state, _ = bt.run_all(klines, rules=rules)
    return state


def compare_modes(klines: Dict[str, pd.DataFrame], rules: Optional[dict] = None,
                  years: Optional[float] = None, **config_overrides) -> dict:
    """跑两种资金分配模式并输出对比（用户 M4 选项 C 的直接交付）。

    Args:
        klines: {code: DataFrame}。
        rules: 离场规则；None → make_exit_rules()。
        years: 回测年数；None → 按净值曲线长度估算。
        **config_overrides: 覆写配置（如 tranche_trigger="T2"）。

    Returns:
        dict: {
            "A": {"config":…, "metrics":…, "cash":…, "used":…, "conservation":…},
            "B": {...},
            "curve_A": [(date, equity), ...],
            "curve_B": [(date, equity), ...],
            "better": "A"|"B"|"TIED",  # 按 Calmar(缺失时退回总收益) 比较
        }
    """
    rules = rules or make_exit_rules()
    result: dict = {}
    for mode in (MODE_GROUP, MODE_BATCH3):
        cfg = PortfolioConfig.from_strategy_config(allocation_mode=mode, **config_overrides)
        cfg.validate()
        bt = PortfolioBacktester(cfg)
        state, _ = bt.run_all(klines, rules=rules)
        metrics = full_report(state.equity_curve, state.closed, years=years)
        result[mode] = {
            "config": {
                "allocation_mode": cfg.allocation_mode,
                "group_budget": cfg.group_budget,
                "buy_ratio": cfg.buy_ratio,
                "reserve_cash": cfg.reserve_cash,
                "batch_ratios": list(cfg.batch_ratios),
                "tranche_trigger": cfg.tranche_trigger,
                "tranche_interval": cfg.tranche_interval,
            },
            "metrics": metrics,
            "cash": round(state.cash, 2),
            "used": round(state.total_used(), 2),
            "fees_paid": round(state.fees_paid, 2),
            "skipped": len(state.skipped),
            "conservation": state.verify_conservation(),
        }
        result[f"curve_{mode}"] = state.equity_curve

    def score(mode_key: str) -> float:
        m = result[mode_key]["metrics"]
        val = m.get("calmar")
        return float(val) if val is not None else float(m.get("total_return", 0.0) or 0.0)

    sa, sb = score(MODE_GROUP), score(MODE_BATCH3)
    if abs(sa - sb) < 1e-9:
        result["better"] = "TIED"
    else:
        result["better"] = MODE_GROUP if sa > sb else MODE_BATCH3
    return result


# ============================================================
# 假数据构造（供自检与单测；**不依赖网络**）
# ============================================================
def make_demo_klines(trend: float = 0.004, n_days: int = 60,
                     code: str = "600000", seed: int = 7) -> Dict[str, pd.DataFrame]:
    """构造一段确定性行情（带趋势 + 周期波动），用于离线验证组合层。

    Args:
        trend: 每日漂移率。
        n_days: K 线根数。
        code: 标的代码。
        seed: 随机种子（保证可复现）。

    Returns:
        dict: {code: DataFrame(index=DatetimeIndex, open/high/low/close/volume)}。
    """
    return _synth_klines(code, n_days, trend, seed, style="mixed")


def _synth_klines(code: str, n_days: int, trend: float, seed: int, style: str) -> pd.DataFrame:
    """内部：生成单只确定性行情。

    style:
        "mixed"  : 趋势 + 周期波动(易触发单针/砖型)
        "b1wave" : 深回调 + 反弹(易触发 B1/B2/B3)
        "quiet"  : 低波动阴跌(用于验证"无信号不硬买")
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    base = pd.bdate_range("2024-01-02", periods=n_days)
    t = np.arange(n_days, dtype=float)
    if style == "b1wave":
        # 大幅回撤后反弹: 制造 B1(超跌缩量) → B2(放量突破) → B3(加速) 链条
        wave = -0.012 * np.sin(t / 9.0) + 0.006 * np.sin(t / 3.0)
        drift = trend + wave
        noise = rng.normal(0, 0.010, n_days)
    elif style == "quiet":
        drift = np.full(n_days, -0.0015)
        noise = rng.normal(0, 0.004, n_days)
    else:  # mixed
        drift = trend + np.sin(t / 6.0) * 0.008
        noise = rng.normal(0, 0.013, n_days)
    rets = np.clip(noise + drift, -0.095, 0.095)
    close = 10.0 * np.cumprod(1.0 + rets)
    open_ = close * (1.0 + rng.normal(0, 0.003, n_days))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.005, n_days)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.005, n_days)))
    # 成交量: 周期放大(利于放量条件) + 回调期缩量(B1 缩量条件)
    vol = 1_200_000 * (1.0 + 0.9 * np.abs(np.sin(t / 5.0)))
    if style == "b1wave":
        vol = vol * np.where(np.sin(t / 9.0) > 0, 0.6, 1.6)  # 下跌缩量/反弹放量
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close, "volume": vol}, index=base)
    df.index.name = "date"
    return df


def make_demo_universe(n_stocks: int = 6, n_days: int = 260,
                       seed: int = 20240910) -> Dict[str, pd.DataFrame]:
    """构造**信号丰富**的多标的假行情（离线自检/单测用，不依赖网络）。

    混合三种风格，保证两种资金分配模式都能被真实触发:
        前 1/3 标的走 "b1wave"(易出 B1/B2/B3 链条)
        中间走 "mixed"(易出单针/砖型)
        其余走 "quiet"(验证"没信号不硬买"及空仓日净值连续性)

    Args:
        n_stocks: 标的数量(>=1)。
        n_days: 每只 K 线根数(建议 >= 200，否则指标预热不足)。
        seed: 随机种子。

    Returns:
        dict: {code: DataFrame}。
    """
    styles = []
    for i in range(n_stocks):
        if i < max(1, n_stocks // 3):
            styles.append("b1wave")
        elif i < max(2, (n_stocks * 2) // 3):
            styles.append("mixed")
        else:
            styles.append("quiet")
    out: Dict[str, pd.DataFrame] = {}
    for i, style in enumerate(styles):
        code = f"60000{i}"
        out[code] = _synth_klines(code, n_days, trend=0.0018, seed=seed + i * 13, style=style)
    return out


def _print_compare(result: dict) -> None:
    """打印两模式对比表（报工用）。"""
    print("═" * 78)
    print("组合层回测 · 资金分配模式对比 (模式A 组内配比  vs  模式B 单只三批建仓)")
    print("═" * 78)
    fields = [("total_return", "总收益%", 8), ("annualized", "年化%", 8),
              ("max_drawdown", "最大回撤%", 10), ("sharpe", "夏普", 7),
              ("calmar", "Calmar", 8), ("win_rate", "胜率%", 7),
              ("profit_factor", "盈亏比", 7), ("trades", "笔数", 6)]
    header = f"{'指标':<12}" + "".join(f"{name:>{w}}" for _, name, w in fields)
    print(header)
    print("─" * 78)
    for mode, label in ((MODE_GROUP, "模式A 组内配比"), (MODE_BATCH3, "模式B 三批建仓")):
        m = result[mode]["metrics"]
        row = f"{label:<12}"
        for key, _name, w in fields:
            val = m.get(key)
            row += f"{('—' if val is None else val):>{w}}"
        print(row)
    print("─" * 78)
    for mode, label in ((MODE_GROUP, "模式A"), (MODE_BATCH3, "模式B")):
        d = result[mode]
        print(f"  [{label}] 期末现金 {d['cash']:,.0f}  持仓占用 {d['used']:,.0f}  "
              f"累计费用 {d['fees_paid']:,.0f}  资金守恒={'✅' if d['conservation'] else '❌'}  "
              f"拒单 {d['skipped']}")
        print(f"      配置: {d['config']}")
    print(f"  更优模式(按Calmar, 缺失退回总收益): {result['better']}")
    print("═" * 78)


def print_mode_report(result: dict) -> str:
    """把对比结果渲染成 Markdown 表格（供报工粘贴）。"""
    lines = ["| 指标 | 模式A 组内配比 | 模式B 单只三批建仓 |",
             "|------|---------------|------------------|"]
    rows = [("总收益率%", "total_return"), ("年化%", "annualized"),
            ("最大回撤%", "max_drawdown"), ("Sharpe", "sharpe"),
            ("Calmar", "calmar"), ("胜率%", "win_rate"),
            ("盈亏比", "profit_factor"), ("交易笔数", "trades")]
    for label, key in rows:
        va = result[MODE_GROUP]["metrics"].get(key)
        vb = result[MODE_BATCH3]["metrics"].get(key)
        fa = "—" if va is None else va
        fb = "—" if vb is None else vb
        lines.append(f"| {label} | {fa} | {fb} |")
    lines.append(f"| 资金守恒 | {'✅' if result[MODE_GROUP]['conservation'] else '❌'} "
                 f"| {'✅' if result[MODE_BATCH3]['conservation'] else '❌'} |")
    lines.append(f"| 拒单数 | {result[MODE_GROUP]['skipped']} | {result[MODE_BATCH3]['skipped']} |")
    lines.append(f"| 累计费用 | {result[MODE_GROUP]['fees_paid']:,.0f} "
                 f"| {result[MODE_BATCH3]['fees_paid']:,.0f} |")
    lines.append(f"\n**更优模式（按 Calmar，缺失退回总收益）: {result['better']}**")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - 手工自检
    parser = argparse.ArgumentParser(description="组合层回测(两种资金分配模式对比)")
    parser.add_argument("--stocks", default="", help="股票代码逗号分隔(默认用假数据自检)")
    parser.add_argument("--mode", default="", choices=["", "A", "B"], help="只跑单一模式")
    parser.add_argument("--markdown", action="store_true", help="额外输出 Markdown 对比表")
    args = parser.parse_args()

    if args.stocks:
        from utils.backtest import DEFAULT_POOL, START
        from utils.data_router import get_daily_bars
        codes = [c.strip().zfill(6) for c in args.stocks.split(",") if c.strip()]
        name_map = {c: n for c, n in DEFAULT_POOL}
        klines = {}
        for c in codes:
            r = get_daily_bars(c, count=700, purpose="indicator", freshness="any")
            if r.get("status") != "OK" or not r.get("data"):
                print(f"⚠ {c} 数据失败, 跳过")
                continue
            df = pd.DataFrame(r["data"]).set_index("date")
            df.index = pd.to_datetime(df.index)
            df = df[df.index >= START]
            if len(df) < 120:
                print(f"⚠ {c} 数据不足, 跳过")
                continue
            klines[c] = df
        if not klines:
            print("无可用数据")
            sys.exit(1)
    else:
        print("※ 无 --stocks, 使用内置信号丰富假数据(离线可复现)")
        klines = make_demo_universe()

    rules = make_exit_rules()
    if args.mode:
        cfg = PortfolioConfig.from_strategy_config(allocation_mode=args.mode)
        bt = PortfolioBacktester(cfg)
        st, evs = bt.run_all(klines, rules=rules)
        print(f"模式{args.mode} · 事件数 {len(evs)} · 成交 {len(st.closed)} 笔")
        print(full_report(st.equity_curve, st.closed))
        print(f"资金守恒: {st.verify_conservation()}  gap={st.conservation_gap():.6f}  "
              f"cash={st.cash:,.2f}  used={st.total_used():,.2f}")
    else:
        result = compare_modes(klines, rules=rules)
        _print_compare(result)
        if args.markdown:
            print(print_mode_report(result))
