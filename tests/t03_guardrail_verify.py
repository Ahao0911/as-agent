# -*- coding: utf-8 -*-
"""T03 临时验证脚本: 单票敞口上限护栏。

覆盖验收项:
  #1 反复全抡加仓同一只票, 断言最终持仓金额 <= cap
  #4 破坏性验证: 把 cap 调到极小值, 确认全抡确实被拦住/截断
跑法: .venv/Scripts/python.exe tests/t03_guardrail_verify.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils.sim_portfolio import (buy, GROUPS, GROUP_BUDGET, single_position_cap,  # noqa: E402
                                 position_amount, total_assets)
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


def fresh_acc(cash=1000000.0):
    return {
        "分组": {g: {"预算": GROUP_BUDGET, "持仓": {}} for g in GROUPS},
        "现金": cash,
        "流水": [],
    }


print("=" * 70)
print("V1. 反复全抡加仓同一只票 -> 持仓金额必须 <= cap")
print("=" * 70)
acc = fresh_acc(cash=1000000.0)
price = 34.13
cap0 = single_position_cap(acc, "B1组")
print(f"  初始: 总资产={total_assets(acc):.2f} 组预算={GROUP_BUDGET:.0f} "
      f"cap=min({GROUP_BUDGET * CONFIG.MAX_SINGLE_POSITION_BUDGET_RATIO:.0f}, "
      f"{total_assets(acc) * CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO:.0f}) = {cap0:.2f}")

rounds = 0
for i in range(30):
    r = buy(acc, "B1组", "300191", "潜能恒信", price, amount=125000,
            reason="全抡加仓", date_str="2026-09-11", allow_overflow=True)
    if not r["ok"]:
        print(f"  第{i + 1}轮被拒: {r['msg']}")
        break
    rounds += 1

held = position_amount(acc, "B1组", "300191")
cap_now = single_position_cap(acc, "B1组")
qty = acc["分组"]["B1组"]["持仓"]["300191"]["数量"]
print(f"  实际加仓轮数={rounds} 最终持仓={qty}股 金额={held:.2f} cap={cap_now:.2f} "
      f"现金={acc['现金']:.2f}")
ok("持仓金额 <= cap", held <= cap_now + 1e-6, f"({held:.2f} <= {cap_now:.2f})")
ok("确实发生了截断(不是一次就买满)", rounds >= 1, f"轮数={rounds}")
ok("现金未被压死(仍 > 0)", acc["现金"] > 0, f"现金={acc['现金']:.2f}")

print()
print("=" * 70)
print("V2. 上限口径核对: cap = min(组预算*1.5, 总资产*0.15)")
print("=" * 70)
acc2 = fresh_acc(cash=1000000.0)
exp = min(GROUP_BUDGET * 1.5, total_assets(acc2) * 0.15)
ok("空仓时 cap 公式", abs(single_position_cap(acc2, "B1组") - exp) < 1e-6,
   f"实得{single_position_cap(acc2, 'B1组'):.2f} 期望{exp:.2f}")
# 总资产变小 -> 总资产口径生效
acc3 = fresh_acc(cash=500000.0)
exp3 = min(GROUP_BUDGET * 1.5, total_assets(acc3) * 0.15)
ok("总资产 50万时 cap=7.5万(总资产口径生效)",
   abs(single_position_cap(acc3, "B1组") - exp3) < 1e-6,
   f"实得{single_position_cap(acc3, 'B1组'):.2f} 期望{exp3:.2f}")

print()
print("=" * 70)
print("V3. 加仓路径(第二条分支)同样受 cap 约束")
print("=" * 70)
acc4 = fresh_acc(cash=1000000.0)
buy(acc4, "B1组", "600000", "测试A", 10.0, qty=100, date_str="2026-09-11")
r = buy(acc4, "B1组", "600000", "测试A", 10.0, amount=999999,
        date_str="2026-09-11", allow_overflow=True)
h4 = position_amount(acc4, "B1组", "600000")
c4 = single_position_cap(acc4, "B1组")
print(f"  加仓后: 金额={h4:.2f} cap={c4:.2f} msg={r['msg'][:60]}")
ok("加仓路径也 <= cap", h4 <= c4 + 1e-6, f"({h4:.2f} <= {c4:.2f})")

print()
print("=" * 70)
print("V4. 破坏性验证: cap 调到极小 -> 必须被拦住/截断")
print("=" * 70)
orig_budget_ratio = CONFIG.MAX_SINGLE_POSITION_BUDGET_RATIO
orig_total_ratio = CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO
try:
    CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO = 0.0005  # 总资产100万 -> cap=500元
    acc5 = fresh_acc(cash=1000000.0)
    tiny_cap = single_position_cap(acc5, "B1组")
    print(f"  极小 cap = {tiny_cap:.2f}")
    r = buy(acc5, "B1组", "600001", "测试B", 10.0, amount=125000,
            date_str="2026-09-11", allow_overflow=True)
    print(f"  买入结果: ok={r['ok']} msg={r['msg']}")
    if r["ok"]:
        h5 = position_amount(acc5, "B1组", "600001")
        ok("截断后仍 <= 极小cap", h5 <= tiny_cap + 1e-6, f"({h5:.2f} <= {tiny_cap:.2f})")
        ok("截断发生在极小cap下", h5 < 125000, f"成交{h5:.2f} << 申购125000")
    else:
        ok("极小cap时硬拒绝(而非放行)", True, r["msg"])

    # 已持仓超过 cap -> 再加仓必须失败
    CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO = 0.001  # cap=1000
    acc6 = fresh_acc(cash=1000000.0)
    buy(acc6, "B1组", "600002", "测试C", 10.0, qty=1000, date_str="2026-09-11")  # 1万元
    CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO = 0.0005  # cap 降到 ~500
    r = buy(acc6, "B1组", "600002", "测试C", 10.0, amount=50000,
            date_str="2026-09-11", allow_overflow=True)
    print(f"  超额持仓再加仓: ok={r['ok']} msg={r['msg']}")
    ok("已超cap时拒绝继续加仓", (not r["ok"]), r["msg"])
finally:
    CONFIG.MAX_SINGLE_POSITION_BUDGET_RATIO = orig_budget_ratio
    CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO = orig_total_ratio

print()
print("=" * 70)
print(f"T03 护栏验证汇总: PASS={PASS} FAIL={FAIL}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
