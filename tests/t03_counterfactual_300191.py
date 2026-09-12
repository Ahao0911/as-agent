# -*- coding: utf-8 -*-
"""T03 反事实推演: 若 2026-09-02 已启用单票敞口上限, 300191 潜能恒信的损失会是多少。

只读推演, 不写账户。
跑法: .venv/Scripts/python.exe tests/t03_counterfactual_300191.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils.sim_portfolio import (buy, GROUPS, GROUP_BUDGET, single_position_cap,  # noqa: E402
                                 position_amount)
from utils.strategy_config import CONFIG  # noqa: E402

# 事故实际数据(来自 data/sim_account.json 已平仓流水 + 主理人简报)
CODE, NAME, GROUP = "300191", "潜能恒信", "单针组"
EXIT_PRICE = 34.13          # 2026-09-11 止损价
ACTUAL_LOSS = -35179.20     # 实际单笔亏损
ACTUAL_QTY = 16800          # 事故持仓股数
IMPLIED_COST = EXIT_PRICE - ACTUAL_LOSS / ACTUAL_QTY   # 反推成本价 ≈ 36.224
TOTAL_ASSETS_THEN = 1000000.0                          # 当时账户总资金


def build(with_guardrail: bool):
    """模拟全抡让渡反复加仓同一只票, 返回 (持仓股数, 持仓金额, 轮数)。"""
    acc = {"分组": {g: {"预算": GROUP_BUDGET, "持仓": {}} for g in GROUPS},
           "现金": TOTAL_ASSETS_THEN, "流水": []}
    if not with_guardrail:
        # 复刻旧行为: 直接绕过 cap(临时把上限调到不可能触发)
        old_b, old_t = (CONFIG.MAX_SINGLE_POSITION_BUDGET_RATIO,
                        CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO)
        CONFIG.MAX_SINGLE_POSITION_BUDGET_RATIO = 1e9
        CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO = 1e9
    try:
        rounds = 0
        for _ in range(60):
            r = buy(acc, GROUP, CODE, NAME, IMPLIED_COST, amount=125000,
                    reason="全抡加仓", date_str="2026-09-02", allow_overflow=True)
            if not r["ok"]:
                break
            rounds += 1
            if position_amount(acc, GROUP, CODE) >= 609000:   # 达到事故规模即停
                break
        qty = acc["分组"][GROUP]["持仓"][CODE]["数量"]
        return qty, position_amount(acc, GROUP, CODE), rounds
    finally:
        if not with_guardrail:
            CONFIG.MAX_SINGLE_POSITION_BUDGET_RATIO = old_b
            CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO = old_t


print("=" * 72)
print("反事实推演: 300191 潜能恒信 (2026-09-02 建仓 → 2026-09-11 @34.13 止损)")
print("=" * 72)
print(f"  反推成本价 = {IMPLIED_COST:.4f}   (由 亏损{ACTUAL_LOSS} / {ACTUAL_QTY}股 反推)")

qty_old, amt_old, r_old = build(with_guardrail=False)
loss_old = (EXIT_PRICE - IMPLIED_COST) * qty_old
print(f"\n  [无护栏·事故实况] 加仓{r_old}轮 → {qty_old}股 / {amt_old:,.2f}元"
      f" (组预算{GROUP_BUDGET:,.0f}的 {amt_old / GROUP_BUDGET:.2f}倍)")
print(f"                     止损亏损 = {loss_old:,.2f} 元")

cap = min(GROUP_BUDGET * CONFIG.MAX_SINGLE_POSITION_BUDGET_RATIO,
          TOTAL_ASSETS_THEN * CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO)
qty_new, amt_new, r_new = build(with_guardrail=True)
loss_new = (EXIT_PRICE - IMPLIED_COST) * qty_new
print(f"\n  [有护栏·本次新增] 加仓{r_new}轮 → {qty_new}股 / {amt_new:,.2f}元"
      f" (组预算的 {amt_new / GROUP_BUDGET:.2f}倍)")
print(f"                     cap = min({GROUP_BUDGET * 1.5:,.0f}, "
      f"{TOTAL_ASSETS_THEN * 0.15:,.0f}) = {cap:,.2f}")
print(f"                     止损亏损 = {loss_new:,.2f} 元")

print()
print("-" * 72)
print(f"  超额占用资金减少: {amt_old - amt_new:,.2f} 元")
print(f"  单笔亏损减少:     {abs(loss_old) - abs(loss_new):,.2f} 元 "
      f"({(1 - abs(loss_new) / abs(loss_old)) * 100:.1f}%)")
print(f"  护栏是否生效:     {'是' if amt_new <= cap + 1e-6 else '否'}")
print("-" * 72)
