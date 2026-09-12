# -*- coding: utf-8 -*-
"""T01 边界与异常验证 (B 方向) —— 独立证伪, 重点找静默 fallback / 荒谬值."""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from utils.strategy_config import CONFIG, StrategyConfig  # noqa: E402

PASS = 0
FAIL = 0
NOTES = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("=" * 64)
print("B1. stop_price_for 非法 buy_price")
print("=" * 64)
for bad in (0, -1, -0.001):
    try:
        r = CONFIG.stop_price_for(bad)
        print(f"  [FAIL] p={bad} 未抛异常, 静默返回 {r}  <-- 缺陷")
        FAIL += 1
    except ValueError as e:
        ok(f"p={bad} 抛 ValueError", True, f"-> {e}")
    except Exception as e:
        print(f"  [WARN] p={bad} 抛出非 ValueError: {type(e).__name__}: {e}")

# 1e9 大数
try:
    r = CONFIG.stop_price_for(1e9)
    ok("p=1e9 正常返回", abs(r - 9.6e8) < 1, f"-> {r}")
except Exception as e:
    ok("p=1e9 正常返回", False, f"异常 {e}")

print()
print("=" * 64)
print("B2. stop_price_for 非法 mode")
print("=" * 64)
for bad_mode in ("foo", "", None, "INTRADAY", "Intraday", "close ", 0, 1):
    try:
        r = CONFIG.stop_price_for(10.0, bad_mode)
        print(f"  [FAIL] mode={bad_mode!r} 未抛异常, 静默返回 {r}  <-- 静默 fallback 缺陷")
        FAIL += 1
    except ValueError:
        ok(f"mode={bad_mode!r} 抛 ValueError", True)
    except Exception as e:
        print(f"  [WARN] mode={bad_mode!r} 抛出非 ValueError: {type(e).__name__}: {e}")

print()
print("=" * 64)
print("B3. stop_price_for round 位数验证 (3 位是否真成立)")
print("=" * 64)
cases = [33.3333, 0.501, 10.335, 99.9999]
for p in cases:
    raw = p * 0.96
    r = CONFIG.stop_price_for(p)
    decimals = len(str(r).split(".")[1]) if "." in str(r) else 0
    print(f"  p={p}: raw={raw!r} -> round3={r!r} (小数位={decimals})")
ok("33.3333 结果", CONFIG.stop_price_for(33.3333) == round(33.3333 * 0.96, 3),
   f"-> {CONFIG.stop_price_for(33.3333)}")

print()
print("=" * 64)
print("B4. scale_in_amount 边界")
print("=" * 64)
print(f"  scale_in_amount() 默认 = {CONFIG.scale_in_amount()}")
ok("默认 = 250000*0.5", CONFIG.scale_in_amount() == 125000.0)
print(f"  scale_in_amount(0) = {CONFIG.scale_in_amount(0)}")
print(f"  scale_in_amount(-100) = {CONFIG.scale_in_amount(-100)}")
try:
    r = CONFIG.scale_in_amount(-100)
    if r < 0:
        print(f"  [WARN] 负数预算返回负金额 {r} (无校验, 但调用方通常会拦截)")
        NOTES.append("scale_in_amount 接受负数预算并返回负金额, 无输入校验")
except Exception as e:
    print(f"  scale_in_amount(-100) 抛 {type(e).__name__}: {e}")

print()
print("=" * 64)
print("B5. mode 大小写/空白容错 (是否应容错? 现为严格)")
print("=" * 64)
# 记录行为即可
for m in (" intraday", "intraday "):
    try:
        CONFIG.stop_price_for(10.0, m)
        print(f"  mode={m!r} 通过(未 strip)")
    except ValueError:
        print(f"  mode={m!r} 抛异常(不复空格容错)")

print()
print("=" * 64)
print("B6. 浮点反算误差累积: 连续多次 stop_price_for")
print("=" * 64)
p = 10.0
for i in range(5):
    p = CONFIG.stop_price_for(p)
print(f"  连续 5 次 -4% 后: {p}")
ok("无 NaN/Inf", p == p and abs(p) != float("inf"), f"-> {p}")

print()
print("=" * 64)
print(f"B 方向汇总: PASS={PASS} FAIL={FAIL}")
if NOTES:
    print("Notes:")
    for n in NOTES:
        print("  -", n)
print("=" * 64)
sys.exit(1 if FAIL else 0)
