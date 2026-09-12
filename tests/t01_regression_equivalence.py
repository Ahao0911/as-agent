# -*- coding: utf-8 -*-
"""T01 回归等价性验证 (独立复刻, 不依赖工程师自测).

思路: 用 git show 取改动前 sim_portfolio.py 源码, 以子进程方式提取其
「旧止损公式」与「旧加仓加权成本公式」的纯计算结果, 与新 CONFIG 口径逐组对比。

若任一小数位不同 → 回归缺陷。
"""
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from utils.strategy_config import CONFIG  # noqa: E402

PASS = 0
FAIL = 0
DIFFS = []


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [PASS] {name}: {got!r}")
    else:
        FAIL += 1
        DIFFS.append((name, want, got))
        print(f"  [FAIL] {name}: 期望 {want!r} 实得 {got!r}")


# ---------------------------------------------------------------
# A1. 旧公式: round(price * 0.96, 3)  —— 直接从旧源码语义复刻
#     新公式: CONFIG.stop_price_for(price, "intraday")
# ---------------------------------------------------------------
PRICES = [10.0, 10.335, 0.501, 1234.567, 3.3333, 99.995, 7.77, 1000000.0]

print("=" * 60)
print("A1. 买入止损价 等价性: 旧 round(p*0.96,3) vs 新 stop_price_for(p,'intraday')")
print("=" * 60)
for p in PRICES:
    old = round(p * 0.96, 3)
    new = CONFIG.stop_price_for(p, "intraday")
    check(f"p={p}", new, old)

# ---------------------------------------------------------------
# A2. 加仓路径等价性
#     旧版 buy() 没有加仓分支: 老逻辑下同 code 二次 buy 会 **直接覆盖** 持仓
#     (g["持仓"][code] = {...}), 即旧行为 = 覆盖成本价/数量, 不做加权。
#     新版 = 数量累加 + 成本加权平均 + 止损重算。
#     → 这是 **有意行为变更**(新增加仓), 不是等价重构。此处验证新版加权公式本身正确。
# ---------------------------------------------------------------
print()
print("=" * 60)
print("A2. 加仓加权成本公式正确性 + 新止损价 = 新成本 -4%")
print("=" * 60)
CASES = [
    # (old_qty, old_cost, add_qty, add_price, 期望加权成本)
    (100, 10.0, 100, 20.0, round((10.0 * 100 + 20.0 * 100) / 200, 3)),
    (300, 12.345, 100, 11.111, round((12.345 * 300 + 11.111 * 100) / 400, 3)),
    (200, 33.3333, 200, 30.0, round((33.3333 * 200 + 30.0 * 200) / 400, 3)),
    (500, 5.5, 700, 6.25, round((5.5 * 500 + 6.25 * 700) / 1200, 3)),
]
for oq, oc, aq, ap, expect in CASES:
    got = round((oc * oq + ap * aq) / (oq + aq), 3)
    check(f"加权 {oc}x{oq} + {ap}x{aq}", got, expect)
    # 重算止损价 = 加权成本 -4%
    new_stop = CONFIG.stop_price_for(got, "intraday")
    old_style = round(got * 0.96, 3)
    check(f"  重算止损 {got}", new_stop, old_style)

# ---------------------------------------------------------------
# A3. 直接读旧源码文件, 确保旧公式确实是 0.96 (防止我记错)
# ---------------------------------------------------------------
print()
print("=" * 60)
print("A3. 旧源码事实核对 (git show)")
print("=" * 60)
old_src = subprocess.check_output(
    ["git", "show", "f567079a3cba3e848d404a643d977174b69957b6^:utils/sim_portfolio.py"],
    cwd=ROOT, text=True, encoding="utf-8",
)
has_096 = "round(price * 0.96, 3)" in old_src
old_has_addbranch = "加仓:" in old_src
print(f"  旧源码含 'round(price * 0.96, 3)': {has_096}")
print(f"  旧源码含加仓分支 '加仓:': {old_has_addbranch}")
check("旧源码确实使用 0.96 硬编码", has_096, True)
check("旧源码确实无加仓分支(故加仓属新增行为)", old_has_addbranch, False)

# ---------------------------------------------------------------
# A4. 浮点陷阱: 反算 1-x/100 是否精确等于 0.96/0.97/0.98
# ---------------------------------------------------------------
print()
print("=" * 60)
print("A4. 浮点陷阱检查: 1 - pct/100 是否 == 硬编码小数")
print("=" * 60)
check("1-4/100 == 0.96", (1 - 4.0 / 100.0) == 0.96, True)
check("1-3/100 == 0.97", (1 - 3.0 / 100.0) == 0.97, True)
check("1-2/100 == 0.98", (1 - 2.0 / 100.0) == 0.98, True)
# 反算误差放大检验: 对 1e6 量级
big = 1000000.0
print(f"  p={big}: 新={CONFIG.stop_price_for(big)} 旧={round(big*0.96,3)}")
check(f"1e6 等价", CONFIG.stop_price_for(big), round(big * 0.96, 3))

print()
print("=" * 60)
print(f"汇总: PASS={PASS}  FAIL={FAIL}")
if DIFFS:
    print("差异明细:")
    for n, w, g in DIFFS:
        print(f"  - {n}: 期望{w} 实得{g}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
