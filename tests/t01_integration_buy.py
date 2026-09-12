# -*- coding: utf-8 -*-
"""T01 集成验证: sim_portfolio.buy() 端到端走 CONFIG 口径, 含加仓路径.

不 mock: 直接构造内存 account dict 调 buy(), 断言产出止损价 == CONFIG 口径。
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from utils.sim_portfolio import buy, GROUPS  # noqa: E402
from utils.strategy_config import CONFIG  # noqa: E402

PASS = 0
FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def fresh_acc():
    return {
        "分组": {g: {"预算": CONFIG.GROUP_BUDGET, "持仓": {}} for g in GROUPS},
        "现金": 1000000.0,
        "流水": [],
    }


print("=" * 64)
print("I1. 首次买入自动止损 = CONFIG 口径")
print("=" * 64)
for price in (10.0, 10.335, 0.501, 33.3333, 1234.567):
    acc = fresh_acc()
    r = buy(acc, "B1组", "600000", "测试", price, qty=100, date_str="2026-09-10")
    assert r["ok"], r
    pos = acc["分组"]["B1组"]["持仓"]["600000"]
    expect = CONFIG.stop_price_for(price, "intraday")
    ok(f"buy@{price} 止损", pos["止损价"] == expect,
       f"实得{pos['止损价']} 期望{expect}")

print()
print("=" * 64)
print("I2. 加仓路径: 数量累加 + 成本加权 + 止损重算")
print("=" * 64)
acc = fresh_acc()
buy(acc, "B1组", "600000", "测试", 10.0, qty=100, date_str="2026-09-10")
buy(acc, "B1组", "600000", "测试", 20.0, qty=100, date_str="2026-09-10")
pos = acc["分组"]["B1组"]["持仓"]["600000"]
ok("加仓后数量=200", pos["数量"] == 200, f"实得{pos['数量']}")
ok("加仓后成本=15.0", pos["成本价"] == 15.0, f"实得{pos['成本价']}")
ok("加仓后止损=14.4", pos["止损价"] == CONFIG.stop_price_for(15.0), f"实得{pos['止损价']}")
# 流水方向
dirs = [f["方向"] for f in acc["流水"]]
ok("流水含加仓标记", "加仓" in dirs, f"实得{dirs}")

print()
print("=" * 64)
print("I3. 加仓加权成本 vs 旧行为(旧版为覆盖) —— 行为变更确认")
print("=" * 64)
# 若按旧版覆盖语义, 成本会是 20.0(第二次买入价); 新版为加权 15.0
print(f"  新版加权成本=15.0, 旧版覆盖语义=20.0 → 行为确已变更(新增功能, 非等价重构)")
ok("加仓行为为加权而非覆盖", pos["成本价"] == 15.0)

print()
print("=" * 64)
print("I4. 指定 stop 参数优先于 CONFIG")
print("=" * 64)
acc = fresh_acc()
buy(acc, "B1组", "600000", "测试", 10.0, qty=100, stop=9.0, date_str="2026-09-10")
pos = acc["分组"]["B1组"]["持仓"]["600000"]
ok("显式 stop=9.0 生效", pos["止损价"] == 9.0, f"实得{pos['止损价']}")

print()
print("=" * 64)
print("I5. 加仓时显式 stop 被忽略(加仓路径总是重算) —— 潜在语义陷阱记录")
print("=" * 64)
acc = fresh_acc()
buy(acc, "B1组", "600000", "测试", 10.0, qty=100, date_str="2026-09-10")
buy(acc, "B1组", "600000", "测试", 10.0, qty=100, stop=5.0, date_str="2026-09-10")
pos = acc["分组"]["B1组"]["持仓"]["600000"]
print(f"  加仓传 stop=5.0, 实际止损={pos['止损价']} (加仓路径忽略显式 stop, 强制重算 -4%)")
ok("加仓忽略显式 stop(记录行为)", pos["止损价"] == CONFIG.stop_price_for(10.0))

print()
print("=" * 64)
print(f"集成汇总: PASS={PASS} FAIL={FAIL}")
print("=" * 64)
sys.exit(1 if FAIL else 0)
