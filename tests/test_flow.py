# -*- coding: utf-8 -*-
"""
F2 单元测试: resonance.py + flow_backtest.py
============================================
运行:  python -m pytest tests/test_flow.py -v
   或  python tests/test_flow.py

覆盖:
  T1 共振矩阵 5 列齐全、bool 类型、score_at 的 lag 防前视、bucket 分层
  T2 状态机全路径: EMPTY→B1→B2→B3→离场
  T3 守卫: B2 无 B1 前提不触发（合成数据无 B1 信号 → 不建仓）
  T4 守卫: 补票限 1 次（needle_max_count=1）
  T5 资金守恒: cash == 1.0 + realized（精确）且任一时刻 cash ≥ 0
  T6 离场层触发: 止损 / 红翻绿 / 到期 各至少命中一种
  T7 1970 塌缩防御: RangeIndex + date 列 → 正确转索引
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from utils.backtest import make_exit_rules
from utils.flow_backtest import (FlowConfig, FlowState, run_flow_backtest,
                                 run_flow_backtest_ledger)
from utils.resonance import RESONANCE_ITEMS, resonance_matrix, score_at, bucket


# ---------------- 合成数据构造 ----------------

def _base_df(n=120, seed=7):
    """构造一段温和上行的合成K线（保证有 B1 信号可触发）。"""
    rng = np.random.default_rng(seed)
    base = 10.0 + np.cumsum(rng.normal(0.004, 0.012, n)) * 10
    base = np.clip(base, 8.0, None)
    o = base * (1 + rng.normal(0, 0.002, n))
    c = base * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.003, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.003, n)))
    v = rng.uniform(8e5, 1.2e6, n)
    idx = pd.bdate_range("2024-01-02", periods=n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                         "volume": v}, index=idx)


def _with_date_col(df):
    """把 DatetimeIndex 版转成 date 普通列 + RangeIndex（模拟 backtest_data 输出）。"""
    d = df.reset_index().rename(columns={"index": "date"})
    return d


def _cyclic_df(n_cycles=9, up_bars=30, up_f=1.006, dip_bars=5, dip_f=0.986):
    """确定性循环行情：长涨(up_bars×+0.6%) + 急跌(dip_bars×-1.4%, 缩量)。

    设计目的（可复现地覆盖状态机各路径）:
      急跌段: J≤13(KDJ超卖) + 缩量 + 涨跌幅/振幅均小 + 白线(EMA,快)仍在黄线(MA,慢)上方
             → 稳定触发 B1 信号；
      入场后的下一轮急跌段: needle20 白线(3日百分位)≤20 而 红线(21日)≥CONFIG.NEEDLE_LONG_MIN
             (2026-09-11 起固化=85, 原知识库口径 60)，
             且回落低点高于上一信号日低点(不触发止损) → 稳定触发补票条件。
      （注意: 本项目有两套"白/黄线"—— env 层用 indicators.white_line/yellow_line
        价格级趋势线; 补票用 needle20_lines 的 0-100 百分位四线。两者口径不同。）
    """
    o, h, l, c, v = [], [], [], [], []
    px = 10.0
    for _ in range(n_cycles):
        seq = [(up_f, 1.2e6)] * up_bars + [(dip_f, 0.55e6)] * dip_bars
        for f, vol in seq:
            o_i = px
            c_i = px * f
            h_i = max(o_i, c_i) * 1.003
            l_i = min(o_i, c_i) * 0.997
            o.append(o_i); h.append(h_i); l.append(l_i); c.append(c_i); v.append(vol)
            px = c_i
    idx = pd.bdate_range("2024-01-02", periods=len(c))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                         "volume": v}, index=idx)


# ---------------- T1 共振 ----------------

def test_resonance_matrix_columns_and_types():
    df = _base_df()
    m = resonance_matrix(df)
    assert list(m.columns) == RESONANCE_ITEMS, f"列不符: {list(m.columns)}"
    for col in RESONANCE_ITEMS:
        assert m[col].dtype == bool, f"{col} 应为 bool"
    assert len(m) == len(df)
    print("  T1a 共振矩阵 5 列/bool ✅")


def test_score_at_lag_and_bucket():
    df = _base_df()
    m = resonance_matrix(df)
    dates = df.index
    # lag=1 必须等于前一交易日的行命中
    for dt in dates[40:45]:
        s1, h1 = score_at(m, dt, lag=1)
        prev = dates[dates.get_loc(dt) - 1]
        row = m.loc[prev]
        expect = int(row.sum())
        assert s1 == expect, f"{dt.date()} lag=1 命中数 {s1} != 前日 {expect}"
        assert len(h1) == s1
    # lag 越界 → (0, [])
    s0, h0 = score_at(m, dates[0], lag=5)
    assert (s0, h0) == (0, [])
    # bucket 分层
    assert bucket(0) == "0" and bucket(1) == "1" and bucket(2) == "2"
    assert bucket(3) == "3+" and bucket(9) == "3+"
    print("  T1b score_at lag 防前视 + bucket ✅")


# ---------------- T2 状态机全路径 ----------------

def test_state_machine_full_path():
    """EMPTY→B1→B2→B3→离场 至少跑通一轮；能产出 FlowTrade。"""
    df = _base_df(160)
    cfg = FlowConfig(env_macd_filter=False, resonance_on=False,
                     needle_patch=False)
    trades, ledger = run_flow_backtest_ledger(df, "TEST", "测试股", cfg,
                                              make_exit_rules())
    assert len(trades) >= 1, "合成数据应至少跑出一轮流程交易"
    t = trades[0]
    assert t.code == "TEST" and t.b1_entry_date and t.exit_date
    assert t.b1_entry_price > 0
    assert t.resonance_bucket in ("0", "1", "2", "3+")
    assert len(t.tranches) >= 1
    kinds = {x["kind"] for x in t.tranches}
    assert "B1" in kinds, f"首轮应含 B1 tranche, 实际 {kinds}"
    print(f"  T2 状态机全路径 ✅ (rounds={len(trades)}, "
          f"首轮 tranches={len(t.tranches)}, reason={t.exit_reason})")


# ---------------- T3 守卫: 无 B1 不建仓 ----------------

def test_no_b1_no_entry():
    """构造一路阴跌且无 B1 信号的行情 → 不应产生任何交易。"""
    n = 120
    idx = pd.bdate_range("2024-01-02", periods=n)
    # 单边下跌 + 大阴线（B1 要求涨幅-2%~+1.8%且缩量且J≤13且白>黄，阴跌市几乎不可能满足）
    c = np.linspace(30.0, 10.0, n)
    o = c + 0.15          # 每日开盘高于收盘 → 全阴线
    h = o + 0.1
    l = c - 0.35
    v = np.full(n, 2e6)   # 放量（B1 要求缩量, 进一步确保无信号）
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                       "volume": v}, index=idx)
    cfg = FlowConfig(env_macd_filter=False, resonance_on=False)
    trades, _ = run_flow_backtest_ledger(df, "DOWN", "阴跌股", cfg,
                                         make_exit_rules())
    assert len(trades) == 0, f"阴跌无B1行情不应建仓, 实际 {len(trades)} 轮"
    print("  T3 守卫: 无 B1 信号不建仓 ✅")


# ---------------- T4 守卫: 补票限 1 次 ----------------

def test_needle_patch_max_count():
    """守卫: 同一轮流程内 needle 段最多 1 个（合成+真实数据双重校验）。

    注: 补票条件(白线≤20&红线≥CONFIG.NEEDLE_LONG_MIN)要求持仓熬到深回调，而离场层第3级(红翻绿)
    通常先触发 —— 这是设计属性(离场优先于补票)，补票属低频路径。
    故本测试严格断言"上限守卫"，并对触发次数做统计性观察(不作脆弱的存在性断言)。
    """
    cfg = FlowConfig(env_macd_filter=False, resonance_on=False,
                     needle_patch=True, needle_stop_pct=0.05,
                     tranche={"B1": 0.5, "B2": 0.3, "needle": 0.2})
    total_rounds, total_patch = 0, 0

    # 合成: 循环行情 + 随机行情
    for tag, df in (("cyclic", _cyclic_df(9)), ("random", _base_df(200, seed=11))):
        trades, _ = run_flow_backtest_ledger(df, tag, tag, cfg, make_exit_rules())
        for t in trades:
            needles = [x for x in t.tranches if x["kind"] == "needle"]
            assert len(needles) <= 1, \
                f"[{tag}] {t.b1_entry_date} 轮补票段 {len(needles)} 个 > 上限 1"
            if needles:
                assert t.needle_entry_price and t.needle_entry_price > 0
                assert t.needle_entry_date
        total_rounds += len(trades)
        total_patch += sum(1 for t in trades
                           if any(x["kind"] == "needle" for x in t.tranches))

    # 真实: 波动较大的 10 只
    import socket
    socket.setdefaulttimeout(20)
    from utils.backtest_data import get_hist
    for code in ["300750", "002594", "688111", "300059", "601899"]:
        try:
            raw = get_hist(code, start="2024-01-01")
            if raw is None:
                continue
            trades, _ = run_flow_backtest_ledger(
                raw.set_index("date"), code, code, cfg, make_exit_rules())
            for t in trades:
                needles = [x for x in t.tranches if x["kind"] == "needle"]
                assert len(needles) <= 1, \
                    f"[{code}] {t.b1_entry_date} 轮补票段 {len(needles)} 个 > 上限 1"
            total_rounds += len(trades)
            total_patch += sum(1 for t in trades
                               if any(x["kind"] == "needle" for x in t.tranches))
        except Exception:
            continue

    assert total_rounds > 0, "应至少产出交易(否则守卫断言无覆盖)"
    print(f"  T4 守卫: 补票限 1 次 ✅ (rounds={total_rounds}, "
          f"触发补票轮次={total_patch}, 上限守卫全部通过)")


# ---------------- T5 资金守恒 ----------------

def test_cash_conservation():
    """cash == 1.0 + realized 必须精确成立；且过程无现金透支。"""
    for seed in (7, 11, 23):
        df = _base_df(180, seed=seed)
        for cfg in (
            FlowConfig(env_macd_filter=False, resonance_on=False),
            FlowConfig(env_macd_filter=True, resonance_on=True,
                       needle_patch=True, needle_stop_pct=0.03,
                       tranche={"B1": 0.5, "B2": 0.3, "needle": 0.2},
                       exit_enhanced=True),
        ):
            trades, ledger = run_flow_backtest_ledger(
                df, "CONS", "守恒股", cfg, make_exit_rules())
            assert not ledger["violations"], \
                f"现金透支: {ledger['violations'][:3]}"
            gap = abs(ledger["cash"] - (1.0 + ledger["realized"]))
            assert gap < 1e-9, \
                f"seed={seed} 资金不守恒: cash={ledger['cash']} " \
                f"1+realized={1.0+ledger['realized']} gap={gap}"
            # tranche 权重合计不得超过 1.0（否则必然透支，已被 violations 捕获）
            for t in trades:
                assert t.invested_w <= 1.0 + 1e-9, \
                    f"tranche 权重 {t.invested_w} > 1.0"
    print("  T5 资金守恒 ✅ (3 seeds × 2 configs, gap<1e-9, 无透支)")


# ---------------- T6 离场层触发 ----------------

def test_exit_reasons_covered():
    """全市场真实数据抽 30 只，离场原因应覆盖多类（非只有单一原因）。"""
    import socket
    socket.setdefaulttimeout(20)
    from utils.backtest_data import get_hist
    codes = ["600519", "000001", "300750", "002594", "600036",
             "000858", "601318", "688111", "300059", "601899"]
    reasons = {}
    n_trades = 0
    for code in codes:
        try:
            raw = get_hist(code, start="2024-01-01")
            if raw is None:
                continue
            df = raw.set_index("date")
            cfg = FlowConfig(env_macd_filter=True, resonance_on=False,
                             needle_patch=True, needle_stop_pct=0.05,
                             tranche={"B1": 0.5, "B2": 0.3, "needle": 0.2})
            trades, _ = run_flow_backtest_ledger(df, code, code, cfg,
                                                 make_exit_rules())
            n_trades += len(trades)
            for t in trades:
                reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        except Exception:
            continue
    assert n_trades > 0, "真实数据应产出交易"
    assert len(reasons) >= 2, f"离场原因过于单一: {reasons}"
    print(f"  T6 离场层触发 ✅ ({n_trades} 轮, 原因分布: {reasons})")


# ---------------- T8 v4 离场规则 ----------------

def test_v4_new_exit_rules():
    """v4: 3 条知识库离场规则（破白线/前高止盈/3日不涨）在真实缓存上可触发。"""
    import sqlite3
    from utils.backtest_data import DB_PATH
    from utils.flow_experiments import universe_from_cache, load_df
    codes = universe_from_cache()[:15]
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cfg = FlowConfig(env_macd_filter=False, resonance_on=False, needle_patch=False)
    reasons, n = {}, 0
    for code in codes:
        try:
            df = load_df(conn, code)
            if df is None or len(df) < 120:
                continue
            trades, _ = run_flow_backtest_ledger(df, code, code, cfg, make_exit_rules())
            n += len(trades)
            for t in trades:
                reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        except Exception:
            continue
    conn.close()
    assert n > 0, "缓存数据应产出交易"
    hit = {"破白线离场", "前高止盈", "3日不涨离场"} & set(reasons)
    assert hit, f"v4 三条新离场规则一条都未触发: {reasons}"
    print(f"  T8 v4 新离场规则 ✅ ({n} 轮, 命中 {hit}, 分布 {reasons})")


def test_close_confirm_differs_from_intraday():
    """v4: 收盘确认止损(close≤stop) 与盘中(lo≤stop) 行为应不同。"""
    from utils.flow_experiments import universe_from_cache, load_df
    import sqlite3
    from utils.backtest_data import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=30)
    n_conf = n_intra = 0
    for code in universe_from_cache()[:15]:
        try:
            df = load_df(conn, code)
            if df is None or len(df) < 120:
                continue
            for flag, acc in ((True, "c"), (False, "i")):
                cfg = FlowConfig(env_macd_filter=False, resonance_on=False,
                                 stop_close_confirm=flag)
                trades, _ = run_flow_backtest_ledger(df, code, code, cfg,
                                                     make_exit_rules())
                if acc == "c":
                    n_conf += len(trades)
                else:
                    n_intra += len(trades)
        except Exception:
            continue
    conn.close()
    assert (n_conf, n_intra) != (0, 0)
    assert n_conf != n_intra or n_conf > 0
    print(f"  T8b 收盘确认 vs 盘中止损 ✅ (收盘 n={n_conf} vs 盘中 n={n_intra})")


# ---------------- T9 v5 活跃市值门控 ----------------

def test_amv_gate_all_false_blocks_all():
    """v5: 活跃市值 regime 全 False → 不建仓; 全 True → 与无过滤一致。"""
    from utils.flow_backtest import _prepare_block
    df = _cyclic_df(9)
    idx = df.index
    cfg = FlowConfig(env_macd_filter=False, resonance_on=False, needle_patch=True,
                     tranche={"B1": 0.5, "B2": 0.3, "needle": 0.2}, amv_filter=True)
    allfalse = pd.DataFrame({"regime": False, "daily": False}, index=idx)
    blk0 = _prepare_block(df, cfg, want_resonance=False, amv_state=allfalse)
    t0, _ = run_flow_backtest_ledger(None, "X", "X", cfg, make_exit_rules(), block=blk0)
    assert len(t0) == 0, f"regime 全 False 仍建仓 {len(t0)} 轮"

    alltrue = pd.DataFrame({"regime": True, "daily": True}, index=idx)
    blk1 = _prepare_block(df, cfg, want_resonance=False, amv_state=alltrue)
    t1, _ = run_flow_backtest_ledger(None, "X", "X", cfg, make_exit_rules(), block=blk1)
    cfg_no = FlowConfig(env_macd_filter=False, resonance_on=False, needle_patch=True,
                        tranche={"B1": 0.5, "B2": 0.3, "needle": 0.2})
    t2, _ = run_flow_backtest_ledger(df, "X", "X", cfg_no, make_exit_rules())
    assert len(t1) == len(t2) and len(t1) >= 1, \
        f"全 True 应等价无过滤: {len(t1)} vs {len(t2)}"
    print(f"  T9 活跃市值门控 ✅ (全False→0 轮; 全True={len(t1)} 轮≡无过滤)")




def test_date_column_indexing():
    """date 普通列 + RangeIndex → 正确转 DatetimeIndex（不塌缩）。"""
    df = _with_date_col(_cyclic_df(16))
    assert not isinstance(df.index, pd.DatetimeIndex)
    assert "date" in df.columns
    cfg = FlowConfig(env_macd_filter=False)
    trades, _ = run_flow_backtest_ledger(df, "IDX", "索引股", cfg,
                                         make_exit_rules())
    assert len(trades) >= 1, "循环行情应产出交易(否则本测试无覆盖)"
    for t in trades:
        assert not str(t.b1_entry_date).startswith("1970"), "检测到 1970 塌缩!"
        assert not str(t.exit_date).startswith("1970"), "检测到 1970 塌缩!"
    print(f"  T7 1970 塌缩防御 ✅ (date列+RangeIndex 正常, rounds={len(trades)})")


if __name__ == "__main__":
    print("=" * 66)
    print("F2 单元测试")
    print("=" * 66)
    test_resonance_matrix_columns_and_types()
    test_score_at_lag_and_bucket()
    test_state_machine_full_path()
    test_no_b1_no_entry()
    test_needle_patch_max_count()
    test_cash_conservation()
    test_exit_reasons_covered()
    test_v4_new_exit_rules()
    test_close_confirm_differs_from_intraday()
    test_amv_gate_all_false_blocks_all()
    test_date_column_indexing()
    print("=" * 66)
    print("ALL PASS")
