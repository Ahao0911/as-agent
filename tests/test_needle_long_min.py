# -*- coding: utf-8 -*-
"""
单针「白线下20买」长期线门槛固化回归测试
========================================================
背景（2026-09-11 用户拍板）:
    将 CONFIG.NEEDLE_LONG_MIN 由知识库原始 60 提升到 85
    （全市场 4935 只入场门槛扫描择优：长期≥85 → ALL 52.9% / IS 53.5% / OOS 50.7%）。
本测试锁定该生产门槛，覆盖:
    C  配置常量值（85）+ 知识库对照常量（60）
    B  边界: 短期=20 与 长期=84/85 的通过性（84 不过 / 85 过 / 84.99 不过 / 85.01 过）
    R  回测路径 compute_signals 的单针信号同样受 CONFIG 门槛约束
运行: python tests/test_needle_long_min.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import utils.needle20 as needle_mod
import utils.backtest as bt
from utils.needle20 import needle20_signal
from utils.strategy_config import CONFIG

RESULTS = []


def _record(name, passed, detail=""):
    RESULTS.append((name, passed, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}  {detail}")


def _mk_lines_for(df, s, l):
    """构造 needle20_lines 的返回（短期=s, 长期=l, 其余中性），索引对齐 df。"""
    n = len(df)
    return pd.DataFrame({
        "短期": np.full(n, float(s)),
        "中期": np.full(n, 50.0),
        "中长期": np.full(n, 50.0),
        "长期": np.full(n, float(l)),
    }, index=df.index)


def _flat_df(n=30):
    return pd.DataFrame({
        "open": np.full(n, 10.0), "high": np.full(n, 10.2),
        "low": np.full(n, 9.8), "close": np.full(n, 10.0),
        "volume": np.full(n, 1e6),
    })


# ---------------------------------------------------------------------------
# C. 配置常量
# ---------------------------------------------------------------------------
def test_config_values():
    print("\n===== C. 配置常量 =====")
    _record("C1 NEEDLE_LONG_MIN == 85",
            CONFIG.NEEDLE_LONG_MIN == 85, f"实际={CONFIG.NEEDLE_LONG_MIN}")
    _record("C2 NEEDLE_LONG_MIN_KB == 60 (知识库对照常量)",
            CONFIG.NEEDLE_LONG_MIN_KB == 60, f"实际={CONFIG.NEEDLE_LONG_MIN_KB}")


# ---------------------------------------------------------------------------
# B. needle20_signal 边界（核心）
# ---------------------------------------------------------------------------
def test_boundary_needle20_signal():
    print("\n===== B. needle20_signal 边界 =====")
    orig = needle_mod.needle20_lines
    df = _flat_df()

    def _run(s, l):
        needle_mod.needle20_lines = (lambda ss, ll: (lambda d: _mk_lines_for(d, ss, ll)))(s, l)
        return needle20_signal(df)

    try:
        r84 = _run(10.0, 84.0)      # 长期刚好低于门槛
        r85 = _run(10.0, 85.0)      # 长期刚好等于门槛
        r8499 = _run(10.0, 84.99)
        r8501 = _run(10.0, 85.01)
        r70 = _run(10.0, 70.0)      # ≥60 但 <85 → 证明已不再用 60
        r_s21 = _run(20.01, 90.0)   # 短期 >20 → 不通过
        r_s20 = _run(20.0, 90.0)    # 短期 =20 边界 → 通过
    finally:
        needle_mod.needle20_lines = orig

    _record("B1 短期=10 & 长期=84 → 白线下20=False", r84["白线下20"] is False, f"{r84['白线下20']}")
    _record("B2 短期=10 & 长期=85 → 白线下20=True", r85["白线下20"] is True, f"{r85['白线下20']}")
    _record("B3 短期=10 & 长期=84.99 → 白线下20=False", r8499["白线下20"] is False, f"{r8499['白线下20']}")
    _record("B4 短期=10 & 长期=85.01 → 白线下20=True", r8501["白线下20"] is True, f"{r8501['白线下20']}")
    _record("B5 短期=10 & 长期=70(≥60<85) → 白线下20=False (旧60口径已弃用)",
            r70["白线下20"] is False, f"{r70['白线下20']}")
    _record("B6 短期=20.01(>20) & 长期=90 → 白线下20=False", r_s21["白线下20"] is False, f"{r_s21['白线下20']}")
    _record("B7 短期=20(=20) & 长期=90 → 白线下20=True", r_s20["白线下20"] is True, f"{r_s20['白线下20']}")


# ---------------------------------------------------------------------------
# R. 回测路径 compute_signals 受 CONFIG 约束
# ---------------------------------------------------------------------------
def test_backtest_compute_signals():
    print("\n===== R. 回测路径 compute_signals =====")
    df = _flat_df()
    orig = bt.needle20_lines
    try:
        bt.needle20_lines = (lambda ll: (lambda d: _mk_lines_for(d, 10.0, ll)))(84.0)
        s84 = bt.compute_signals(df)["单针"]
        bt.needle20_lines = (lambda ll: (lambda d: _mk_lines_for(d, 10.0, ll)))(85.0)
        s85 = bt.compute_signals(df)["单针"]
    finally:
        bt.needle20_lines = orig

    _record("R1 compute_signals 长期=84 → 单针 全False",
            bool(~s84.any()), f"any={bool(s84.any())}")
    _record("R2 compute_signals 长期=85 → 单针 全True",
            bool(s85.all()), f"all={bool(s85.all())}")


def main():
    print("=" * 64)
    print("单针长期线门槛(85) 固化回归测试")
    print("=" * 64)
    test_config_values()
    test_boundary_needle20_signal()
    test_backtest_compute_signals()

    total = len(RESULTS)
    passed = sum(1 for _, p, _ in RESULTS if p)
    print("-" * 64)
    print(f"汇总: {passed}/{total} 通过")
    fails = [(n, d) for n, p, d in RESULTS if not p]
    if fails:
        print("失败项:")
        for n, d in fails:
            print(f"  - {n}: {d}")
    else:
        print("全部通过 ✅")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
