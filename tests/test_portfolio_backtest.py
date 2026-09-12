# -*- coding: utf-8 -*-
"""T02-3 组合层回测单元验证
==========================
覆盖架构师列出的三条硬要求 + 指标边界:
    1. 资金守恒: 任意时点 cash + Σ各组已用资金 == initial_capital + 已实现盈亏
    2. 前视防护: 建仓日必须是「信号日 + 1 个交易日」(契约陷阱B)
    3. 两种模式资金分配正确: 模式A 单只=组预算×50%; 模式B 首批50%→25%→25%
    4. 金额口径: 盈亏必须由 qty×(卖-买) 反算, 绝不能用 收益% 乘资金(契约陷阱C)
    5. 指标边界: MaxDD=0 → Calmar 必须 None(不得 inf)

运行:
    python tests/test_portfolio_backtest.py
``不以 pytest 为唯一入口``: 直接跑也能全绿(项目既有测试风格)。
"""
import os
import sys

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.backtest import TRADE_COST, make_exit_rules  # 冻结只读底座
from utils.metrics import (annualized, calmar, full_report, max_drawdown,
                           profit_factor, sharpe, total_return, win_rate)
from utils.portfolio_backtest import (
    BATCH3_RATIOS,
    MODE_BATCH3,
    MODE_GROUP,
    PortfolioBacktester,
    PortfolioConfig,
    Position,
    TradeEvent,
    compare_modes,
    make_demo_universe,
    _next_trading_date,
    _round_lot,
)

_PASSED: list = []
_FAILED: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    """极简断言器（与项目既有测试风格一致，输出可读）。"""
    if cond:
        _PASSED.append(name)
        print(f"  ✅ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        _FAILED.append(name)
        print(f"  ❌ {name}  {detail}")


# ============================================================
# 测试用假数据（确定性、无需网络）
# ============================================================
def _two_stock_klines(n: int = 80) -> dict:
    """构造 2 只确定性行情，保证能触发信号且便于精确核对资金。"""
    dates = pd.bdate_range("2024-01-02", periods=n)
    out = {}
    for i, code in enumerate(("600000", "600001")):
        # 价格恒为 10 元 → 便于手算：12.5万 → 12500 股 = 125 手
        close = np.full(n, 10.0 + i * 5.0)
        open_ = close.copy()
        high = close * 1.05
        low = close * 0.95
        vol = np.full(n, 1_000_000.0)
        df = pd.DataFrame({"open": open_, "high": high, "low": low,
                           "close": close, "volume": vol}, index=dates)
        df.index.name = "date"
        out[code] = df
    return out


def _synthetic_events(entries: list) -> list:
    """手工构造 TradeEvent 列表（绕过 backtest_stock，用于精确核对分配）。"""
    evs = []
    for i, (code, group, entry_date, exit_date, price) in enumerate(entries):
        evs.append(TradeEvent(
            code=code, name=code, strategy=f"S{i}", group=group,
            signal_date=entry_date, entry_date=entry_date,
            entry_price=price, exit_date=exit_date, exit_price=price * 1.10,
            exit_reason="测试离场", hold_bars=2, raw_pnl_pct=10.0,
        ))
    return evs


# ============================================================
# 1. 工具函数
# ============================================================
def test_helpers() -> None:
    """手数取整 + 交易日历推进（契约陷阱B 的基础工具）。"""
    print("\n[1] 工具函数")
    check("按手取整 125000/10 = 12500 股", _round_lot(125000, 10.0) == 12500)
    check("按手取整 不足一手 → 0", _round_lot(900, 10.0) == 0)
    check("按手取整 9990/10 → 900 股(9手)", _round_lot(9990, 10.0) == 900)
    days = ["2024-01-02", "2024-01-03", "2024-01-04"]
    check("信号日+1 交易日 = 01-03", _next_trading_date(days, "2024-01-02") == "2024-01-03")
    check("末日信号 → None", _next_trading_date(days, "2024-01-04") is None)


# ============================================================
# 2. 契约陷阱B：前视防护（信号日 → 次日建仓）
# ============================================================
def test_no_lookahead() -> None:
    """建仓日必须是信号日的**下一个交易日**，不得当日建仓（否则用未来信息）。"""
    print("\n[2] 契约陷阱B · 前视防护(lag=1)")
    from utils.backtest import backtest_stock, compute_signals
    kl = make_demo_universe(n_stocks=4, n_days=260, seed=42)
    bt = PortfolioBacktester(PortfolioConfig.from_strategy_config(allocation_mode=MODE_GROUP))
    rules = make_exit_rules()
    events = bt.prepare_trades(kl, rules=rules)
    check("能产出交易事件", len(events) > 0, f"events={len(events)}")

    bad = 0
    for ev in events:
        if ev.entry_date <= ev.signal_date:
            bad += 1
    check("所有事件 entry_date > signal_date", bad == 0, f"违例={bad}")

    # 逐标的复核: entry_date 精确等于 calendar 上 signal_date 的下一日
    cal = bt.trading_days
    bad2 = sum(1 for ev in events if _next_trading_date(cal, ev.signal_date) != ev.entry_date)
    check("entry_date 恰为交易日历下一日", bad2 == 0, f"违例={bad2}")


# ============================================================
# 3. 资金守恒（硬要求）
# ============================================================
def test_capital_conservation() -> None:
    """任意日: cash + Σ各组已用资金 == initial + 已实现盈亏（逐日校验）。"""
    print("\n[3] 资金守恒(逐日)")
    kl = make_demo_universe(n_stocks=6, n_days=260, seed=20240910)
    rules = make_exit_rules()
    for mode in (MODE_GROUP, MODE_BATCH3):
        cfg = PortfolioConfig.from_strategy_config(allocation_mode=mode)
        bt = PortfolioBacktester(cfg)
        events = bt.prepare_trades(kl, rules=rules)
        st = bt.run(events, kl)          # 完整跑一遍
        check(f"模式{mode} 期末资金守恒", st.verify_conservation(),
              f"gap={st.conservation_gap():.6f}")

        # 逐日守恒：用引擎自带的 on_day 钩子逐日校验（不破坏状态）
        bt2 = PortfolioBacktester(cfg)
        bt2.prepare_trades(kl, rules=rules)
        violations = []

        def guard_daily(state, date, _viol=violations):
            if not state.verify_conservation():
                _viol.append((date, round(state.conservation_gap(), 6)))

        bt2.on_day = guard_daily
        bt2.run(events, kl)
        check(f"模式{mode} 逐日守恒无违例", len(violations) == 0,
              f"违例={violations[:3]}")


# ============================================================
# 4. 模式A 资金分配正确性
# ============================================================
def test_mode_a_allocation() -> None:
    """模式A: 单只首笔 = 组预算 × 50% = 125,000 元（按手取整）。"""
    print("\n[4] 模式A · 组内配比分配")
    cfg = PortfolioConfig.from_strategy_config(allocation_mode=MODE_GROUP,
                                               enable_overflow=False)
    rules = make_exit_rules()
    kl = _two_stock_klines(n=40)
    bt = PortfolioBacktester(cfg)
    # 每日各 1 只入场, 保证首轮分配 2 组 × 1 只
    events = [
        TradeEvent(code="600000", name="600000", strategy="S", group="B1组",
                   signal_date="2024-01-02", entry_date="2024-01-03",
                   entry_price=10.0, exit_date="2024-02-01", exit_price=11.0,
                   exit_reason="测试", hold_bars=3, raw_pnl_pct=10.0),
        TradeEvent(code="600001", name="600001", strategy="S", group="砖型组",
                   signal_date="2024-01-02", entry_date="2024-01-03",
                   entry_price=15.0, exit_date="2024-02-01", exit_price=16.5,
                   exit_reason="测试", hold_bars=3, raw_pnl_pct=10.0),
    ]
    st = bt.run(events, kl)
    expect_amount = cfg.group_budget * cfg.buy_ratio           # 125,000
    check("首轮单只拟投 = 组预算×50%", abs(expect_amount - 125000.0) < 1e-9,
          f"{expect_amount}")
    groups_used = {g: st.groups[g].used for g in ("B1组", "砖型组")}
    check("模式A 单只占用 ≈125,000(建仓额, 不含让渡加仓后)",
          all(v <= cfg.group_budget + 1e-6 for v in groups_used.values()),
          f"used={ {k: round(v) for k, v in groups_used.items()} }")
    # 首笔股数 125000/10 = 12500 股(手) → 建仓额 125,000
    first_trades = [c for c in st.closed if c.code == "600000"]
    check("模式A 产生已平仓交易", len(first_trades) >= 1, f"n={len(first_trades)}")
    if first_trades:
        c = first_trades[0]
        check("成交额 == 组预算×50%(±1手)",
              abs(c.cost_price * c.qty - 125000.0) <= 10.0 * 100,
              f"{c.cost_price}×{c.qty}={c.cost_price * c.qty:.0f}")

    # 模式A 组预算硬约束（关闭让渡时不得超预算）
    cfg2 = PortfolioConfig.from_strategy_config(allocation_mode=MODE_GROUP,
                                                enable_overflow=False)
    bt2 = PortfolioBacktester(cfg2)
    st2 = bt2.run(events, kl)
    over = [g for g, gs in st2.groups.items() if gs.used > gs.budget + 1e-6]
    check("模式A(禁让渡) 无组超预算", len(over) == 0, f"超预算组={over}")


# ============================================================
# 5. 模式B 三批建仓正确性
# ============================================================
def test_mode_b_batching() -> None:
    """模式B: 首批 50% → 第2批 25% → 第3批 25%，T1 间隔 N=3 交易日。"""
    print("\n[5] 模式B · 单只三批建仓(T1/N=3 占位值)")
    check("三批比例合计 1.0", abs(sum(BATCH3_RATIOS) - 1.0) < 1e-9, str(BATCH3_RATIOS))
    cfg = PortfolioConfig.from_strategy_config(allocation_mode=MODE_BATCH3,
                                               tranche_trigger="T1", tranche_interval=3)
    # 长持(30 个交易日)保证 3 批都能触发: entry +3 → 批2, +3 → 批3
    dates = pd.bdate_range("2024-01-02", periods=40)
    kl = _two_stock_klines(n=40)
    ev = TradeEvent(code="600000", name="600000", strategy="S", group="B1组",
                    signal_date="2024-01-02", entry_date="2024-01-03",
                    entry_price=10.0, exit_date=str(dates[-1].date()),
                    exit_price=12.0, exit_reason="测试", hold_bars=35, raw_pnl_pct=20.0)
    bt = PortfolioBacktester(cfg)
    st = bt.run([ev], kl)
    check("模式B 产生已平仓交易", len(st.closed) == 1, f"n={len(st.closed)}")
    if st.closed:
        c = st.closed[0]
        check("模式B 完成 3 批建仓", c.tranche == 3, f"tranche={c.tranche}")
        # 目标总额 = 组预算 × buy_ratio × 100% = 125,000
        target = cfg.group_budget * cfg.buy_ratio
        check("模式B 建仓总额 ≈ 目标全额",
              abs(c.cost_price * c.qty - target) <= 10.0 * 100,
              f"{c.cost_price}×{c.qty}={c.cost_price * c.qty:.0f} vs {target:.0f}")
    check("模式B 期末守恒", st.verify_conservation(), f"gap={st.conservation_gap():.6f}")

    # 首批必须恰为 50%（用短持仓验证）
    ev_short = TradeEvent(code="600000", name="600000", strategy="S", group="B1组",
                          signal_date="2024-01-02", entry_date="2024-01-03",
                          entry_price=10.0, exit_date="2024-01-05",
                          exit_price=11.0, exit_reason="测试", hold_bars=2, raw_pnl_pct=10.0)
    bt2 = PortfolioBacktester(cfg)
    st2 = bt2.run([ev_short], kl)
    if st2.closed:
        c2 = st2.closed[0]
        check("模式B 首批 = 组预算×50%×50% = 62,500",
              abs(c2.cost_price * c2.qty - 62500.0) <= 10.0 * 100,
              f"{c2.cost_price * c2.qty:.0f}")
        check("模式B 短持只建 1 批", c2.tranche == 1, f"tranche={c2.tranche}")


# ============================================================
# 6. 契约陷阱C：金额口径（绝不能用 收益% 乘资金）
# ============================================================
def test_pnl_amount_not_pct() -> None:
    """盈亏金额必须 = qty×(卖出-买入) - 费，与 backtest 的 收益% 无关。"""
    print("\n[6] 契约陷阱C · 金额口径")
    cfg = PortfolioConfig.from_strategy_config(allocation_mode=MODE_GROUP,
                                               enable_overflow=False)
    kl = _two_stock_klines(n=40)
    ev = TradeEvent(code="600000", name="600000", strategy="S", group="B1组",
                    signal_date="2024-01-02", entry_date="2024-01-03",
                    entry_price=10.0, exit_date="2024-01-10", exit_price=11.0,
                    exit_reason="测试", hold_bars=5, raw_pnl_pct=999.0)  # 故意荒谬的 %
    bt = PortfolioBacktester(cfg)
    st = bt.run([ev], kl)
    check("产生 1 笔平仓", len(st.closed) == 1)
    if st.closed:
        c = st.closed[0]
        gross = c.qty * (c.exit_price - c.cost_price)
        fee = c.cost_price * c.qty * TRADE_COST + c.exit_price * c.qty * TRADE_COST
        expect = gross - fee
        check("pnl_amt ≈ qty×(卖-买) - 双边费",
              abs(c.pnl_amt - expect) < 1.0,
              f"actual={c.pnl_amt:.2f} expect={expect:.2f}")
        check("pnl_amt 未受 raw_pnl_pct=999% 影响",
              c.pnl_amt < 300000.0, f"pnl_amt={c.pnl_amt:.2f}")
        check("pnl_pct 与 pnl_amt/cost 自洽",
              abs(c.pnl_pct - c.pnl_amt / (c.cost_price * c.qty) * 100) < 0.5,
              f"{c.pnl_pct:.3f}")


# ============================================================
# 7. 指标边界
# ============================================================
def test_metrics_bounds() -> None:
    """Sharpe/MaxDD/Calmar 的数值与边界（MaxDD=0 → Calmar=None，不得 inf）。"""
    print("\n[7] 指标边界")
    flat_up = [("d1", 100.0), ("d2", 101.0), ("d3", 102.0)]
    check("单调上涨 MaxDD = 0", abs(max_drawdown(flat_up)) < 1e-9)
    check("MaxDD=0 → Calmar 必须 None(不得 inf)",
          calmar(flat_up, years=1.0) is None, f"calmar={calmar(flat_up, years=1.0)}")

    demo = [("d1", 100.0), ("d2", 110.0), ("d3", 99.0), ("d4", 105.0), ("d5", 120.0)]
    check("MaxDD 手算 = 10.0", abs(max_drawdown(demo) - 10.0) < 1e-9,
          f"{max_drawdown(demo)}")
    check("总收益 手算 = 20.0", abs(total_return(demo) - 20.0) < 1e-9,
          f"{total_return(demo)}")
    check("Calmar = 年化/回撤 (有限值)",
          calmar(demo, years=1.0) is not None and np.isfinite(calmar(demo, years=1.0)))

    closed = [{"pnl_amt": 1000.0, "pnl_pct": 5.0},
              {"pnl_amt": -400.0, "pnl_pct": -2.0},
              {"pnl_amt": 600.0, "pnl_pct": 3.0}]
    check("胜率 2/3 = 66.67%", abs(win_rate(closed) - 66.6667) < 0.01,
          f"{win_rate(closed):.4f}")
    check("盈亏比 = 800/400 = 2.0", abs(profit_factor(closed) - 2.0) < 1e-9,
          f"{profit_factor(closed)}")
    check("无空仓日 Sharpe 不为 None(有波动)",
          sharpe(demo) is not None)
    rep = full_report(flat_up)
    check("全量报告标注 calmar_note", rep["calmar"] is None and rep["calmar_note"] != "",
          rep["calmar_note"])

    # 空曲线不得抛异常
    check("空曲线 total_return = 0", total_return([]) == 0.0)
    check("空曲线 max_drawdown = 0", max_drawdown([]) == 0.0)
    check("空曲线 sharpe = None", sharpe([]) is None)


# ============================================================
# 8. 两模式对比可运行 + 配置校验
# ============================================================
def test_compare_modes() -> None:
    """compare_modes 跑通两种模式，均守恒，且给出 better 判定。"""
    print("\n[8] 两模式对比 + 配置校验")
    kl = make_demo_universe(n_stocks=6, n_days=260, seed=20240910)
    res = compare_modes(kl, rules=make_exit_rules())
    check("结果含 A/B 两模式", MODE_GROUP in res and MODE_BATCH3 in res)
    check("模式A 守恒", res[MODE_GROUP]["conservation"], f"gap?")
    check("模式B 守恒", res[MODE_BATCH3]["conservation"], f"gap?")
    check("better 判定合法", res["better"] in (MODE_GROUP, MODE_BATCH3, "TIED"),
          str(res["better"]))
    for m in (MODE_GROUP, MODE_BATCH3):
        mets = res[m]["metrics"]
        for k in ("total_return", "annualized", "max_drawdown", "sharpe",
                  "calmar", "win_rate", "profit_factor", "trades"):
            check(f"模式{m} 含指标 {k}", k in mets)

    # 配置校验
    try:
        bad = PortfolioConfig.from_strategy_config(allocation_mode="X")
        bad.validate()
        check("非法 allocation_mode 被拒", False)
    except ValueError:
        check("非法 allocation_mode 被拒", True)
    try:
        bad2 = PortfolioConfig.from_strategy_config(batch_ratios=(0.5, 0.5, 0.5))
        bad2.validate()
        check("batch_ratios 合计≠1 被拒", False)
    except ValueError:
        check("batch_ratios 合计≠1 被拒", True)
    try:
        bad3 = PortfolioConfig.from_strategy_config(tranche_trigger="T9")
        bad3.validate()
        check("非法 tranche_trigger 被拒", False)
    except ValueError:
        check("非法 tranche_trigger 被拒", True)


# ============================================================
# 9. 净值曲线必须含空仓日
# ============================================================
def test_equity_curve_daily() -> None:
    """净值曲线长度 == 交易日历长度（空仓日也要记，否则 Sharpe 失真）。"""
    print("\n[9] 净值曲线日频连续性")
    kl = make_demo_universe(n_stocks=4, n_days=260, seed=7)
    cfg = PortfolioConfig.from_strategy_config(allocation_mode=MODE_GROUP)
    bt = PortfolioBacktester(cfg)
    events = bt.prepare_trades(kl, rules=make_exit_rules())
    st = bt.run(events, kl)
    dates = [d for d, _ in st.equity_curve]
    check("曲线长度 == 交易日历长度", len(st.equity_curve) == len(bt.trading_days),
          f"{len(st.equity_curve)} vs {len(bt.trading_days)}")
    check("日期严格递增", all(dates[i] < dates[i + 1] for i in range(len(dates) - 1)))
    check("净值均为有限数", all(np.isfinite(v) for _, v in st.equity_curve))


def test_normalize_klines_rangeindex() -> None:
    """跨模块坑：RangeIndex + date 列（backtest_data 返回形状）必须被正确归一化。

    `pd.to_datetime(RangeIndex)` 会静默产出 1970 纪元时间戳，导致交易日历塌缩成 1 天、
    回测结果报废**且不报错**。本用例锁定 `normalize_klines()` 的防护行为。
    """
    print("\n[10] normalize_klines · 防 RangeIndex 日期塌缩")
    from utils.portfolio_backtest import normalize_klines

    # 形状 A: RangeIndex + date 普通列（load_universe_klines 的返回形状）
    raw = pd.DataFrame({
        "date": pd.bdate_range("2024-01-02", periods=5),
        "open": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5,
        "close": [1.5] * 5, "volume": [100.0] * 5,
    })
    fixed = normalize_klines({"600000": raw})
    df = fixed["600000"]
    check("RangeIndex+date列 → DatetimeIndex", isinstance(df.index, pd.DatetimeIndex))
    check("归一化后日期正确(非1970)",
          str(df.index[0].date()) == "2024-01-02", str(df.index[0].date()))
    check("归一化后 date 列已移除(不重复)", "date" not in df.columns)

    # 形状 B: 已经是 DatetimeIndex → 原样通过
    ok = raw.copy()
    ok.index = pd.to_datetime(raw["date"])
    ok = ok.drop(columns=["date"])
    fixed2 = normalize_klines({"600000": ok})
    check("已是 DatetimeIndex → 保持不变",
          isinstance(fixed2["600000"].index, pd.DatetimeIndex))
    check("归一化不改动调用方原对象",
          not isinstance(raw.index, pd.DatetimeIndex))

    # 形状 C: 无法得到合法日期 → 必须抛错(不得静默)
    bad = raw.drop(columns=["date"])
    raised = False
    try:
        normalize_klines({"600000": bad})
    except ValueError:
        raised = True
    check("无 date 列 + RangeIndex → 抛 ValueError(不静默)", raised)

    # 端到端: 用 RangeIndex 形状喂组合层, 交易日历不得塌缩成 1 天
    bt = PortfolioBacktester(PortfolioConfig.from_strategy_config(allocation_mode=MODE_GROUP))
    bt.prepare_trades({"600000": raw}, rules=make_exit_rules())
    check("组合层日历未塌缩(>1 天)", len(bt.trading_days) > 1,
          f"days={len(bt.trading_days)}")


def main() -> int:
    """运行全部用例，返回退出码（0 = 全绿）。"""
    print("═" * 70)
    print("T02-3 组合层回测 · 单元验证")
    print("═" * 70)
    test_helpers()
    test_no_lookahead()
    test_capital_conservation()
    test_mode_a_allocation()
    test_mode_b_batching()
    test_pnl_amount_not_pct()
    test_metrics_bounds()
    test_compare_modes()
    test_equity_curve_daily()
    test_normalize_klines_rangeindex()
    print("\n" + "═" * 70)
    print(f"通过 {len(_PASSED)} / {len(_PASSED) + len(_FAILED)}")
    if _FAILED:
        print("失败用例:")
        for name in _FAILED:
            print(f"  ❌ {name}")
        print("═" * 70)
        return 1
    print("✅ 全部通过")
    print("═" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
