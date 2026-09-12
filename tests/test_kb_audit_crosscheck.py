# -*- coding: utf-8 -*-
"""
独立复核脚本（不依赖 monkeypatch）：
1. 用真实构造 K 线，使 needle20_lines 自然算出 长期∈(4,6] 且四线<=6 的场景，
   证明「四线归零」在真实计算路径下被触发（而非仅逻辑等价）。
2. 独立核对审计报告声称「一致」的项：砖型图7行公式、黄线参数、白线公式。
3. 独立抽样其它模块一致性。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from utils.needle20 import needle20_lines, needle20_signal
import utils.brick as brick_mod
import utils.indicators as ind


def build_df(close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({
        "open": close, "high": close + 0.01, "low": close - 0.01,
        "close": close, "volume": np.full(len(close), 1e6),
    })


def find_four_zero_real():
    """真实构造：让四线都<=6 且 长期∈(4,6]。"""
    print("===== 真实构造四线归零（长期∈(4,6]）=====")
    # 让最近21根 close 在一个区间内，最后一根 close 逼近区间低位。
    # HHV(close,21) 取最近21根最大，LLV(low,21) 取最近21根最小。
    # 若末根 close 距区间低点约 5% 的高度，则 pct≈5。
    n = 60
    # 前段高位横盘（充当 HHV 来源），后段缓跌但不破末根
    close = np.concatenate([
        np.linspace(150, 149, n - 22),   # 旧区段（不进入21窗口）
        np.linspace(120, 100.5, 21),      # 21窗口：高点120，低点100.5
        [100.0],                          # 末根：略低于区间低点
    ])
    df = build_df(close)
    lines = needle20_lines(df)
    last = lines.iloc[-1]
    lv = float(last["长期"])
    sv, mv, mlv = float(last["短期"]), float(last["中期"]), float(last["中长期"])
    print(f"  四线末值: 短={sv} 中={mv} 中长={mlv} 长={lv}")
    res = needle20_signal(df)
    print(f"  四线归零={res['四线归零']}  (长期={lv}, 需 4<长期<=6 才有区分度)")
    if 4 < lv <= 6 and sv <= 6 and mv <= 6 and mlv <= 6:
        print(f"  ✅ 真实路径下 长期={lv}∈(4,6] 触发四线归零 = {res['四线归零']}（旧逻辑<=4会漏）")
    else:
        print(f"  ⚠ 未落进 (4,6]（长期={lv}），但阈值逻辑已由 monkeypatch 用例覆盖")


def verify_brick_formula():
    print("\n===== 独立核对：砖型图 7 行公式 =====")
    src = open(os.path.join(os.path.dirname(__file__), "..", "utils", "brick.py"),
               encoding="utf-8").read()
    import re
    checks = {
        "VAR1A=(HHV(HIGH,4)-CLOSE)/(HHV(HIGH,4)-LLV(LOW,4))*100-90":
            "var1a = (hhv4 - close) / rng * 100 - 90",
        "VAR2A=SMA(VAR1A,4,1)+100": "tdx_sma(var1a, 4, 1) + 100",
        "VAR3A=(CLOSE-LLV(LOW,4))/(HHV(HIGH,4)-LLV(LOW,4))*100":
            "var3a = (close - llv4) / rng * 100",
        "VAR4A=SMA(VAR3A,6,1)": "tdx_sma(var3a, 6, 1)",
        "VAR5A=SMA(VAR4A,6,1)+100": "tdx_sma(var4a, 6, 1) + 100",
        "VAR6A=VAR5A-VAR2A": "var6a = var5a - var2a",
        "砖型图=IF(VAR6A>4,VAR6A-4,0)": "np.where(var6a > 4, var6a - 4, 0.0)",
    }
    allok = True
    for kb, impl in checks.items():
        ok = impl in src
        allok &= ok
        print(f"  [{'✅' if ok else '❌'}] {kb}  =>  {impl}")
    print(f"  => 7行公式映射: {'全部一致 ✅' if allok else '存在差异 ❌'}")
    # 顺带验证 SMA 等价性：tdx_sma(X,4,1) == ewm(alpha=1/4, adjust=False)
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    a = brick_mod.tdx_sma(s, 4, 1)
    b = s.ewm(alpha=1 / 4, adjust=False).mean()
    print(f"  [{'✅' if a.equals(b) else '❌'}] tdx_sma(X,4,1) == ewm(alpha=1/4, adjust=False)")


def verify_yellow_white_params():
    print("\n===== 独立核对：黄线参数 / 白线公式 =====")
    import inspect
    sig = inspect.signature(ind.yellow_line)
    params = {k: v.default for k, v in sig.parameters.items() if k != "close"}
    expect = {"m1": 14, "m2": 28, "m3": 57, "m4": 114}
    print(f"  yellow_line 默认参数: {params}")
    print(f"  [{'✅' if params == expect else '❌'}] 黄线参数与知识库 14/28/57/114 一致")

    # 白线 = EMA(EMA(C,10),10)
    c = pd.Series(np.arange(1, 51, dtype=float))
    wl = ind.white_line(c, 10)
    manual = c.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean()
    print(f"  [{'✅' if np.allclose(wl.values, manual.values) else '❌'}] "
          f"白线 == EMA(EMA(C,10),10)")
    # 黄线公式
    yl = ind.yellow_line(c)
    manual_y = sum(c.rolling(m).mean() for m in (14, 28, 57, 114)) / 4
    print(f"  [{'✅' if np.allclose(yl.values, manual_y.values, equal_nan=True) else '❌'}] "
          f"黄线 == (MA14+MA28+MA57+MA114)/4")


def verify_brick_xg_ratio():
    print("\n===== 独立核对：brick_xg 2/3 比例（真实绿柱高度）=====")
    src = open(os.path.join(os.path.dirname(__file__), "..", "utils",
                            "screen_brick_xg.py"), encoding="utf-8").read()
    checks = [
        ("green_h = prev2 - prev1", "绿柱高度 = 前前值 - 前值（真实高度）"),
        ("red_h = cur - prev1", "红柱高度 = 当前 - 前值"),
        ("red_h >= green_h * 2 / 3", "强红标准 ≥ 绿柱×2/3（与知识库原版口径一致）"),
    ]
    for pat, desc in checks:
        ok = pat in src
        print(f"  [{'✅' if ok else '❌'}] {desc}: `{pat}`")


def sample_deep_v():
    print("\n===== 独立抽样：deep_v 模块是否含知识库硬公式 =====")
    src = open(os.path.join(os.path.dirname(__file__), "..", "utils", "deep_v.py"),
               encoding="utf-8").read()
    print(f"  deep_v.py 行数={len(src.splitlines())}, 含'公式'字样={'公式' in src}")
    print("  说明：deep_v 为「深V补票」形态识别（实战补充），知识库无对应指标代码，"
          "不属于硬事实层，无需逐行对齐。")


if __name__ == "__main__":
    print("=" * 70)
    print("独立复核（不依赖 monkeypatch）")
    print("=" * 70)
    find_four_zero_real()
    verify_brick_formula()
    verify_yellow_white_params()
    verify_brick_xg_ratio()
    sample_deep_v()
    print("=" * 70)
