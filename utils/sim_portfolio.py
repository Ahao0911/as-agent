"""
Ahao Stock Agent · 模拟盘核心引擎(sim_portfolio)
100万模拟资金,按 图形买点体系战法分组,每日11:30自动选股买入,设止损,长期运行(2026-08-12 起无限期,2026-09-09 ahao 确认)
数据: data/sim_account.json

用法:
    python utils/sim_portfolio.py init                # 初始化100万账户
    python utils/sim_portfolio.py status              # 账户状态
    python utils/sim_portfolio.py buy --group B1组 --code 600519 --name 贵州茅台 --price 1350 --stop 1300
    python utils/sim_portfolio.py sell --group B1组 --code 600519 --price 1400 --reason 止盈
    python utils/sim_portfolio.py check-stop 600519 1280   # 止损检查(现价)
    python utils/sim_portfolio.py settle               # 结算(仅用户明确要求时手动执行)
"""
import os
import sys
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.strategy_config import CONFIG  # 图形买点体系唯一参数源(止损/仓位口径)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
ACCOUNT_FILE = os.path.join(DATA_DIR, "sim_account.json")

# 战法分组(每组预算25万,共100万)
GROUPS = ["B1组", "砖型组", "单针组", "深V组"]
GROUP_BUDGET = 250000
TOTAL_CAPITAL = 1000000
START_DATE = "2026-08-12"
END_DATE = None  # 无限期(2026-09-09 ahao 确认: 模拟盘长期运行、一直交易, 不再按一个月到期结算; 需结算时手动 settle)


def init_account():
    if os.path.exists(ACCOUNT_FILE):
        print(f"⚠ 账户已存在: {ACCOUNT_FILE} (如需重置请删除后重跑)")
        return load_account()
    account = {
        "起始日": START_DATE,
        "结束日": END_DATE or "长期运行",
        "总资金": TOTAL_CAPITAL,
        "现金": TOTAL_CAPITAL,
        "分组": {g: {"预算": GROUP_BUDGET, "持仓": {}} for g in GROUPS},
        "流水": [],
        "已平仓": [],
        "创建时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_account(account)
    print(f"✓ 模拟盘初始化: 总资金100万, 分组 {GROUPS}, 结束日 {END_DATE or '长期运行(无限期)'}")
    return account


def load_account():
    if not os.path.exists(ACCOUNT_FILE):
        return init_account()
    with open(ACCOUNT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_account(acc):
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(acc, f, ensure_ascii=False, indent=2)


def _add_flow(acc, entry):
    acc["流水"].append(entry)


def total_assets(acc):
    """账户总资产 = 现金 + Σ各组 Σ持仓(成本价 × 数量)。

    口径与 status() 保持一致: 持仓按成本价计市值(非现价)。

    Args:
        acc: 账户 dict。

    Returns:
        float: 总资产金额(元)。
    """
    mv = 0.0
    for g in acc.get("分组", {}).values():
        for p in g.get("持仓", {}).values():
            mv += float(p.get("成本价", 0)) * float(p.get("数量", 0))
    return float(acc.get("现金", 0.0)) + mv


def single_position_cap(acc, group=None):
    """单票敞口上限(元) = min(组预算 × 1.5, 总资产 × 15%)。

    双口径取 min: 既限制相对组预算的集中度, 也限制相对总资产的比例,
    防止「全抡让渡」把资金过度集中到单只股票(对治 300191 潜能恒信事故)。

    Args:
        acc: 账户 dict。
        group: 分组名, 当前组预算统一取 GROUP_BUDGET(保留参数以对齐接口语义)。

    Returns:
        float: 单票持仓金额上限(元)。
    """
    by_budget = GROUP_BUDGET * CONFIG.MAX_SINGLE_POSITION_BUDGET_RATIO
    by_total = total_assets(acc) * CONFIG.MAX_SINGLE_POSITION_TOTAL_RATIO
    return min(by_budget, by_total)


def position_amount(acc, group, code):
    """某分组内某只股票的现有持仓金额(成本口径), 未持仓返回 0.0。

    Args:
        acc: 账户 dict。
        group: 分组名。
        code: 股票代码。

    Returns:
        float: 持仓金额(元)。
    """
    p = acc.get("分组", {}).get(group, {}).get("持仓", {}).get(code)
    if not p:
        return 0.0
    return float(p.get("成本价", 0)) * float(p.get("数量", 0))


def buy(acc, group, code, name, price, qty=None, amount=None, stop=None, reason="", date_str=None,
        budget_cap=None, allow_overflow=False):
    """买入: qty(数量) 或 amount(金额) 二选一, 金额优先
    budget_cap: 组预算上限覆盖(满仓全抡时让渡额度可 > 组预算, 传 None 用组预算)
    allow_overflow: True 时突破组预算(让渡模式), 仅受现金约束

    无论 allow_overflow 真假, 一律受「单票敞口上限」约束(按 cap 截断而非硬拒绝):
    单票持仓金额 ≤ min(组预算 × 1.5, 总资产 × 15%)。
    """
    if group not in GROUPS:
        return {"ok": False, "msg": f"未知分组 {group}"}
    g = acc["分组"][group]
    price = float(price)
    if qty is None and amount is None:
        return {"ok": False, "msg": "需提供 qty 或 amount"}
    if qty is None:
        qty = int(amount / price / 100) * 100  # 按手取整
        if qty < 100:
            return {"ok": False, "msg": f"金额不足一手: {amount}元 @ {price}"}

    # 单票敞口上限护栏(allow_overflow 与否均生效, 对治 300191 全抡过度集中)
    cap = single_position_cap(acc, group)
    held = position_amount(acc, group, code)      # 该股现有持仓(新建仓为 0)
    headroom = cap - held                         # 剩余可买敞口
    if headroom <= 0:
        return {"ok": False, "msg": f"触及单票敞口上限: {code} 已持{held:.0f} ≥ 上限{cap:.0f}"}
    max_qty = int(headroom / price / 100) * 100   # 按手取整
    if max_qty < 100:
        return {"ok": False, "msg": f"单票敞口空间不足一手: {code} 剩余{headroom:.0f}元 @ {price}"}
    if qty > max_qty:
        qty = max_qty                             # 按 cap 截断, 不硬拒绝(全抡仍尽量打光现金)
    cost = qty * price
    # 分组预算检查 (allow_overflow=True 时跳过, 全抡让渡)
    # 注意: 变量名用 budget_limit 而非 cap, 避免遮蔽上面的「单票敞口上限 cap」
    if not allow_overflow:
        used = sum(p["成本价"] * p["数量"] for p in g["持仓"].values())
        budget_limit = budget_cap if budget_cap is not None else g["预算"]
        if used + cost > budget_limit:
            return {"ok": False, "msg": f"超出 {group} 预算: 已用{used:.0f} + {cost:.0f} > {budget_limit:.0f}"}
    if cost > acc["现金"]:
        return {"ok": False, "msg": f"现金不足: 需{cost:.0f}, 剩{acc['现金']:.0f}"}
    date = date_str or datetime.now().strftime("%Y-%m-%d")
    # 止损: 未指定时按买入价 -4% 自动(模拟盘允许,实盘人工) —— 口径统一自 图形买点体系参数源
    stop = stop if stop is not None else CONFIG.stop_price_for(price, mode="intraday")
    if code in g["持仓"]:
        # 加仓: 数量累加, 成本加权平均, 止损按新成本 -4% 重算(走同一口径)
        old = g["持仓"][code]
        old_qty = old["数量"]
        new_qty = old_qty + qty
        new_cost = round((old["成本价"] * old_qty + price * qty) / new_qty, 3)
        old["数量"] = new_qty
        old["成本价"] = new_cost
        old["止损价"] = CONFIG.stop_price_for(new_cost, mode="intraday")
        if price > old["最高价"]:
            old["最高价"] = price
        # 止盈/风控阶段字段保留
        acc["现金"] -= cost
        _add_flow(acc, {"日期": date, "分组": group, "代码": code, "名称": name,
                        "方向": "加仓", "数量": qty, "价格": price, "金额": cost,
                        "止损": old["止损价"], "理由": reason})
        return {"ok": True, "msg": f"✓ {group} 加仓 {name} {qty}股 @{price} 新成本{new_cost} 止损{old['止损价']}"}
    g["持仓"][code] = {
        "名称": name, "数量": qty, "成本价": price, "买入日期": date,
        "止损价": stop, "止盈价": None, "理由": reason,
        "最高价": price, "止盈阶段": 0, "阶梯止盈阶段": 0,  # 三层风控追踪字段
    }
    acc["现金"] -= cost
    _add_flow(acc, {"日期": date, "分组": group, "代码": code, "名称": name,
                    "方向": "买入", "数量": qty, "价格": price, "金额": cost,
                    "止损": stop, "理由": reason})
    return {"ok": True, "msg": f"✓ {group} 买入 {name} {qty}股 @{price} 止损{stop}"}


def sell(acc, group, code, price, reason="", date_str=None, ratio=None):
    """卖出: ratio=None(清仓) 或 减仓比例(0<ratio<1, 部分减仓)"""
    if group not in acc["分组"] or code not in acc["分组"][group]["持仓"]:
        return {"ok": False, "msg": f"{group} 无持仓 {code}"}
    p = acc["分组"][group]["持仓"][code]
    price = float(price)
    date = date_str or datetime.now().strftime("%Y-%m-%d")
    total_qty = p["数量"]

    # 计算卖出数量(部分减仓按手取整)
    if ratio is not None and 0 < ratio < 1:
        sell_qty = int(total_qty * ratio / 100) * 100
        if sell_qty < 100:
            sell_qty = min(100, total_qty)
        if sell_qty >= total_qty:
            sell_qty = total_qty
    else:
        sell_qty = total_qty

    proceeds = sell_qty * price
    pnl = proceeds - p["成本价"] * sell_qty
    acc["现金"] += proceeds

    closed = {**p, "分组": group, "卖出日期": date, "卖出价": price,
              "盈亏": round(pnl, 2), "卖出原因": reason, "数量": sell_qty}
    acc["已平仓"].append(closed)
    _add_flow(acc, {"日期": date, "分组": group, "代码": code, "名称": p["名称"],
                    "方向": "卖出" if sell_qty == total_qty else "减仓",
                    "数量": sell_qty, "价格": price, "金额": proceeds,
                    "盈亏": round(pnl, 2), "理由": reason})

    if sell_qty >= total_qty:
        del acc["分组"][group]["持仓"][code]  # 清仓
    else:
        p["数量"] = total_qty - sell_qty  # 部分减仓, 保留剩余
    action = "卖出" if sell_qty == total_qty else "减仓"
    return {"ok": True, "msg": f"✓ {group} {action} {p['名称']} {sell_qty}股 @{price} 盈亏{pnl:+.2f} ({reason})"}


def check_stop(acc, group, code, cur_price):
    """止损检查: 现价 <= 止损价 → 自动卖出"""
    p = acc["分组"].get(group, {}).get("持仓", {}).get(code)
    if not p:
        return None
    if cur_price <= p["止损价"]:
        r = sell(acc, group, code, cur_price, reason="止损")
        return r
    return None


def check_all_stops(acc, prices):
    """批量止损检查: prices = {code: 现价}(跨组找)"""
    sold = []
    for g_name, g in acc["分组"].items():
        for code, p in list(g["持仓"].items()):
            cur = prices.get(code)
            if cur and cur <= p["止损价"]:
                r = sell(acc, g_name, code, cur, reason="止损(自动)")
                sold.append(r)
    return sold


def run_risk_check(acc, prices, active_mv_pct=None, cfg=None):
    """每日三层风控: 遍历持仓(止损/移动止盈/阶梯止盈) + 系统兜底

    prices: {code: 现价}(跨组)
    active_mv_pct: 活跃市值涨跌幅(可选, 用于系统层大盘兜底)
    返回: 执行的动作列表
    """
    from utils.risk_engine import RiskConfig, check_position, check_system
    cfg = cfg or RiskConfig()
    actions = []

    # 1. 系统层 · 大盘兜底(优先, 影响所有持仓)
    sys_act = check_system(active_mv_pct, cfg)
    if sys_act:
        reason, ratio = sys_act
        for g_name, g in acc["分组"].items():
            for code in list(g["持仓"].keys()):
                cur = prices.get(code)
                if cur:
                    r = sell(acc, g_name, code, cur, reason=reason, ratio=ratio)
                    if r.get("ok"):
                        actions.append(r)
        return actions

    # 2. 个股层 · 止损/移动止盈/阶梯止盈
    for g_name, g in acc["分组"].items():
        for code, p in list(g["持仓"].items()):
            cur = prices.get(code)
            if not cur:
                continue
            act = check_position(p, cur, cfg)
            if act:
                reason, ratio = act
                r = sell(acc, g_name, code, cur, reason=reason, ratio=ratio)
                if r.get("ok"):
                    actions.append(r)
    return actions


def risk_status(acc, prices, active_mv_pct=None, cfg=None):
    """风控状态总览(只读, 不执行): 每只持仓的止损/止盈/回撤状态"""
    from utils.risk_engine import RiskConfig
    cfg = cfg or RiskConfig()
    rows = []
    for g_name, g in acc["分组"].items():
        for code, p in g["持仓"].items():
            cost = float(p.get("成本价", 0))
            cur = prices.get(code)
            if cur is None:
                continue
            high = float(p.get("最高价", cost))
            gain = (cur - cost) / cost * 100 if cost else 0
            drawdown = (high - cur) / high * 100 if high else 0
            rows.append({
                "分组": g_name, "代码": code, "名称": p.get("名称", ""),
                "成本": cost, "现价": round(cur, 2), "涨幅": round(gain, 2),
                "最高": round(high, 2), "回撤": round(drawdown, 2),
                "止损价": p.get("止损价"), "止盈阶段": p.get("止盈阶段", 0),
                "阶梯止盈阶段": p.get("阶梯止盈阶段", 0),
            })
    return {"status": "OK", "系统": {"活跃市值涨跌": active_mv_pct,
            "离场线": cfg.active_mv_exit, "降仓线": cfg.active_mv_warn},
            "持仓风控": rows}


def status(acc):
    """账户状态"""
    lines = []
    lines.append("═" * 64)
    lines.append(f"模拟盘状态: 起始{acc['起始日']} → 结束{acc['结束日']}")
    mv = 0.0
    for g_name, g in acc["分组"].items():
        g_mv = sum(p["数量"] * p["成本价"] for p in g["持仓"].values())
        mv += g_mv
        lines.append(f"  [{g_name}] 预算{g['预算']/10000:.0f}万 持仓{g_mv/10000:.2f}万 数量{len(g['持仓'])}只")
        for code, p in g["持仓"].items():
            lines.append(f"      {code} {p['名称']} {p['数量']}股 成本{p['成本价']} 止损{p['止损价']} 买于{p['买入日期']}")
    total = acc["现金"] + mv
    pnl = total - acc["总资金"]
    lines.append(f"  现金: {acc['现金']:.2f}  持仓市值: {mv:.2f}  总资产: {total:.2f}")
    lines.append(f"  累计盈亏: {pnl:+.2f} ({(pnl/acc['总资金']*100):+.2f}%)  已平仓: {len(acc['已平仓'])}笔")
    lines.append("═" * 64)
    for c in acc["已平仓"][-10:]:
        lines.append(f"  {c.get('卖出日期','?')} {c.get('分组','?')} {c.get('名称','?')} 卖@{c.get('卖出价','?')} 盈亏{c.get('盈亏',0):+.2f} ({c.get('卖出原因','')})")
    return "\n".join(lines)


def settle(acc):
    """到期结算(可重复调用, 输出完整战报)"""
    lines = [f"📊 模拟盘结算报告 (起始{acc['起始日']} → 结束{acc['结束日']})"]
    total_buy = 0.0
    for c in acc["已平仓"]:
        total_buy += c["成本价"] * c["数量"] if "盈亏" in c else 0
    wins = [c for c in acc["已平仓"] if c.get("盈亏", 0) > 0]
    lines.append(f"  总交易: {len(acc['已平仓'])}笔  盈利: {len(wins)}笔  胜率: {len(wins)/max(1,len(acc['已平仓']))*100:.0f}%")
    # 按分组统计
    lines.append("  分组战报:")
    for g in GROUPS:
        gc = [c for c in acc["已平仓"] if c["分组"] == g]
        gp = sum(c.get("盈亏", 0) for c in gc)
        lines.append(f"    {g}: {len(gc)}笔 盈亏{gp:+.2f}")
    return "\n".join(lines)


def _fetch_live_prices(codes):
    """拉腾讯实时价: {code: price}"""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data-sources-astock"))
        from _skill_defs import tencent_quote
        q = tencent_quote(list(codes))
        return {c: float(q[c]["price"]) for c in codes if c in q and q[c].get("price")}
    except Exception:
        return {}


def verify_price_multi(code):
    """多源交叉核对现价: 腾讯 vs mootdx, 差异>1% 报警"""
    prices = {}
    # 腾讯
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data-sources-astock"))
        from _skill_defs import tencent_quote
        q = tencent_quote([code])
        if code in q and q[code].get("price"):
            prices["腾讯"] = float(q[code]["price"])
    except Exception:
        pass
    # mootdx
    try:
        from utils.stock_data import get_stock_detail
        d = get_stock_detail(code)
        p = d.get("现价") or d.get("price")
        if p:
            prices["mootdx"] = float(p)
    except Exception:
        pass
    if len(prices) < 2:
        return {"ok": True, "价格": prices, "一致": True, "说明": "仅单源(降级)"}
    vals = list(prices.values())
    diff = abs(vals[0] - vals[1]) / max(vals) * 100
    return {"ok": diff <= 1.0, "价格": prices, "差异": round(diff, 2), "一致": diff <= 1.0,
            "说明": f"两源差异{diff:.2f}% {'✓一致' if diff <= 1.0 else '⚠️异常'}"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="模拟盘引擎")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("init", help="初始化账户")
    sub.add_parser("status", help="账户状态")
    sub.add_parser("settle", help="结算战报")
    p_buy = sub.add_parser("buy")
    p_buy.add_argument("--group", required=True, choices=GROUPS)
    p_buy.add_argument("--code", required=True)
    p_buy.add_argument("--name", required=True)
    p_buy.add_argument("--price", type=float, required=True)
    p_buy.add_argument("--qty", type=int, default=None)
    p_buy.add_argument("--amount", type=float, default=None)
    p_buy.add_argument("--stop", type=float, default=None)
    p_buy.add_argument("--reason", default="")
    p_sell = sub.add_parser("sell")
    p_sell.add_argument("--group", required=True, choices=GROUPS)
    p_sell.add_argument("--code", required=True)
    p_sell.add_argument("--price", type=float, required=True)
    p_sell.add_argument("--reason", default="")
    p_risk = sub.add_parser("risk-check", help="三层风控检查(拉实时价自动执行止损/止盈/系统兜底)")
    p_risk.add_argument("--mv", type=float, default=None, help="活跃市值涨跌幅%")
    p_rs = sub.add_parser("risk-status", help="风控状态总览(只读)")
    p_rs.add_argument("--mv", type=float, default=None, help="活跃市值涨跌幅%")
    args = parser.parse_args()

    if args.cmd == "init":
        init_account()
    elif args.cmd == "status":
        print(status(load_account()))
    elif args.cmd == "settle":
        print(settle(load_account()))
    elif args.cmd == "buy":
        acc = load_account()
        r = buy(acc, args.group, args.code, args.name, args.price,
                args.qty, args.amount, args.stop, args.reason)
        print(r["msg"])
        if r["ok"]:
            save_account(acc)
    elif args.cmd == "sell":
        acc = load_account()
        r = sell(acc, args.group, args.code, args.price, args.reason)
        print(r["msg"])
        if r["ok"]:
            save_account(acc)
    elif args.cmd == "risk-check":
        acc = load_account()
        codes = set()
        for g in acc["分组"].values():
            codes.update(g["持仓"].keys())
        if not codes:
            print("无持仓, 无需风控")
        else:
            prices = _fetch_live_prices(list(codes))
            if not prices:
                print("实时价拉取失败, 无法执行风控")
            else:
                actions = run_risk_check(acc, prices, args.mv)
                if actions:
                    for a in actions:
                        print(a["msg"])
                    save_account(acc)
                    print(f"✓ 共 {len(actions)} 笔风控动作已执行")
                else:
                    print("✓ 无风控触发(持仓均在止损/止盈安全区间)")
    elif args.cmd == "risk-status":
        acc = load_account()
        codes = set()
        for g in acc["分组"].values():
            codes.update(g["持仓"].keys())
        prices = _fetch_live_prices(list(codes)) if codes else {}
        rs = risk_status(acc, prices, args.mv)
        import json as _json
        print(_json.dumps(rs, ensure_ascii=False, indent=2, default=str))
    else:
        parser.print_help()
