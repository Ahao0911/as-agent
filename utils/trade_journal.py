"""
交易记录模块 — 操作日志 + 行为观察（分层沉淀）
两层结构（GPT建议）:
1. trade_journal.json      — 操作流水（结构化，每笔交易一条）
2. trade_observations.jsonl — 每日行为观察（追加式，单条记录）
3. trader_profile.md       — 稳定画像（只有满足条件才更新：同一问题重复3次/连续多日/用户确认）

用法:
    python3 utils/trade_journal.py add --code 600519 --name 贵州茅台 --side buy --shares 100 --price 1350 --reason "B1超跌低吸" --stop 1300
    python3 utils/trade_journal.py today        # 查看今日记录
    python3 utils/trade_journal.py list         # 全部记录
    python3 utils/trade_journal.py observe "追高买入"  # 追加行为观察
    python3 utils/trade_journal.py stats        # 错误模式统计(最近20笔)
"""
import os
import sys
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
JOURNAL_FILE = os.path.join(DATA_DIR, "trade_journal.json")
OBSERVATION_FILE = os.path.join(DATA_DIR, "trade_observations.jsonl")
PROFILE_FILE = os.path.join(DATA_DIR, "trader_profile.md")


# ===================== 交易记录 =====================

def _load_journal():
    if os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_journal(records):
    with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def add_trade(code, name="", side="buy", shares=0, price=0.0, reason="",
              stop=None, target=None, time_str="", extra=None, unit="手"):
    """
    添加一笔交易记录
    unit: 手（股票，1手=100股）/ 份（ETF，1份=1份）——ETF必须用"份"避免金额差100倍
    """
    records = _load_journal()
    is_etf = str(code).startswith(("5", "15", "56", "58"))
    if unit == "份":
        shares_unit = shares          # 份数即股数
        lot_label = "份"
    else:
        shares_unit = shares * 100    # 手 → 股
        lot_label = "手"
    trade = {
        "日期": time_str or datetime.now().strftime("%Y-%m-%d"),
        "时间": datetime.now().strftime("%H:%M:%S"),
        "代码": str(code).zfill(6),
        "名称": name,
        "方向": side,          # buy/sell
        "手数": shares,        # 用户输入的数值（手或份）
        "单位": lot_label,     # 手/份
        "股数": shares_unit,   # 实际股数/份数
        "成交均价": price,
        "操作理由": reason,
        "计划止损": stop,
        "计划止盈": target,
        "创建时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        trade.update(extra)
    records.append(trade)
    _save_journal(records)
    return trade


def get_today_trades():
    """今日交易记录"""
    today = datetime.now().strftime("%Y-%m-%d")
    return [t for t in _load_journal() if t.get("日期") == today]


def get_all_trades():
    return _load_journal()


def has_today_trades():
    return len(get_today_trades()) > 0


# ===================== 持仓计算 =====================

def get_positions():
    """
    计算当前持仓 = 累计买入 - 累计卖出(按代码聚合,FIFO 匹配)
    修复:之前用 (已卖数+持仓数) 做分母算加权均价,会把"已清仓后重建仓"的历史买价摊到新仓,
    导致 589180 这种"清仓后再买"的标的成本错(1.811 历史价摊入 1.84 新建仓 → 算出 1.829)。
    现在改用 FIFO:卖出按时间顺序从最早的 buy 扣减,剩余 buy 即当前持仓的成本基础。
    返回: {代码: {名称, 持仓数, 单位, 成本均价, 成本总额, 已卖数, 已实现盈亏}}
    """
    positions = {}
    for t in _load_journal():
        code = t.get("代码")
        if not code:
            continue
        p = positions.setdefault(code, {
            "名称": t.get("名称", ""), "持仓数": 0, "单位": t.get("单位", "手"),
            "成本总额": 0.0, "买入总额": 0.0, "已卖数": 0, "卖出总额": 0.0,
            "buy_queue": [],   # FIFO: [[price, shares_remaining], ...]
            "已实现盈亏": 0.0,
        })
        shares = t.get("股数", 0)
        price = t.get("成交均价", 0)
        if t.get("方向") == "buy":
            p["持仓数"] += shares
            p["买入总额"] += price * shares
            p["buy_queue"].append([price, shares])
        else:
            # sell: FIFO 从最早的 buy 扣减,实现盈亏 = 卖价 - 被卖出那部分买入价
            remaining = shares
            cost_of_sold = 0.0
            while remaining > 0 and p["buy_queue"]:
                q_price, q_shares = p["buy_queue"][0]
                if q_shares > remaining:
                    cost_of_sold += q_price * remaining
                    p["buy_queue"][0][1] -= remaining
                    remaining = 0
                else:
                    cost_of_sold += q_price * q_shares
                    p["buy_queue"].pop(0)
                    remaining -= q_shares
            p["持仓数"] -= shares
            p["已卖数"] += shares
            p["卖出总额"] += price * shares
            p["已实现盈亏"] += price * shares - cost_of_sold
    # 计算均价:用 FIFO 剩余 buy 队列加权(已清仓后重建仓会自然得到新成本)
    result = {}
    for code, p in positions.items():
        if p["持仓数"] > 0 and p["buy_queue"]:
            p["成本总额"] = sum(q_price * q_shares for q_price, q_shares in p["buy_queue"])
            p["成本均价"] = p["成本总额"] / p["持仓数"]
        else:
            p["成本均价"] = 0.0
        result[code] = p
    return result


def get_positions_list():
    """仅返回持仓>0的标的(未清仓)"""
    return {c: p for c, p in get_positions().items() if p["持仓数"] > 0}


# ===================== 行为观察 =====================

def add_observation(behavior, detail="", trade_code=None):
    """追加一条行为观察（不直接改画像）"""
    obs = {
        "日期": datetime.now().strftime("%Y-%m-%d"),
        "行为": behavior,
        "详情": detail,
        "关联股票": trade_code,
        "时间戳": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(OBSERVATION_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(obs, ensure_ascii=False) + "\n")
    return obs


def get_observations(days=30):
    """近N天行为观察"""
    if not os.path.exists(OBSERVATION_FILE):
        return []
    result = []
    with open(OBSERVATION_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                result.append(json.loads(line))
    return result


def error_pattern_stats(n=20):
    """
    错误模式统计（最近N笔/近30天观察）
    返回: dict {计划外交易次数, 提前入场, 黄白线不满足开仓, 止损延迟, ...}
    """
    obs = get_observations(30)
    patterns = ["计划外交易", "提前入场", "追高", "仓位过大", "止损迟疑",
                "过早卖出", "报复性交易", "频繁操作", "冲动加仓", "恐慌卖出"]
    stats = {"总观察数": len(obs)}
    for p in patterns:
        stats[p] = sum(1 for o in obs if p in o.get("行为", ""))
    return stats


# ===================== 画像更新（分层，谨慎） =====================

def maybe_update_profile(force=False):
    """
    检查是否满足画像更新条件：
    - 同一问题近7天重复≥3次
    - 或用户明确确认
    满足才把行为观察沉淀进 trader_profile.md
    """
    obs = get_observations(7)
    from collections import Counter
    behavior_count = Counter(o.get("行为", "") for o in obs)
    frequent = {k: v for k, v in behavior_count.items() if v >= 3}

    if not frequent and not force:
        return None, "条件未满足（需同一行为7天内≥3次）"

    # 追加到画像
    lines = []
    for behavior, count in frequent.items():
        lines.append(f"- ⚠️ {behavior}（近7天{count}次，已确认高频）")
    if force:
        lines.append("- （用户手动确认更新）")

    with open(PROFILE_FILE, "a", encoding="utf-8") as f:
        f.write("\n## 画像更新\n")
        f.write(f"- 更新日期: {datetime.now().strftime('%Y-%m-%d')}\n")
        for l in lines:
            f.write(l + "\n")
    return lines, "已更新画像"


# ===================== CLI =====================

def main():
    parser = argparse.ArgumentParser(description="交易记录模块")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="添加交易")
    p_add.add_argument("--code", required=True)
    p_add.add_argument("--name", default="")
    p_add.add_argument("--side", choices=["buy", "sell"], default="buy")
    p_add.add_argument("--shares", type=int, default=0, help="数量（手或份，配合--unit）")
    p_add.add_argument("--unit", choices=["手", "份"], default="手", help="ETF请用 份")
    p_add.add_argument("--price", type=float, default=0.0, help="成交均价")
    p_add.add_argument("--reason", default="")
    p_add.add_argument("--stop", type=float, default=None)
    p_add.add_argument("--target", type=float, default=None)

    sub.add_parser("today", help="今日记录")
    sub.add_parser("list", help="全部记录")
    sub.add_parser("stats", help="错误模式统计")
    sub.add_parser("positions", help="当前持仓(买入-卖出)")

    p_obs = sub.add_parser("observe", help="添加行为观察")
    p_obs.add_argument("behavior", help="行为描述")

    args = parser.parse_args()

    if args.cmd == "add":
        t = add_trade(args.code, args.name, args.side, args.shares,
                      args.price, args.reason, args.stop, args.target, unit=args.unit)
        print(f"✓ 已记录: {t['名称'] or t['代码']} {t['方向']} {t['手数']}{t['单位']} @{t['成交均价']}（股数{t['股数']}）")
    elif args.cmd == "positions":
        poss = get_positions_list()
        if not poss:
            print("当前无持仓")
        print(f"当前持仓 {len(poss)} 只:")
        for code, p in sorted(poss.items()):
            label = "份" if p["单位"] == "份" else "股"
            print(f"  {code} {p['名称']} {p['持仓数']}{label} 成本{p['成本均价']:.3f} 已实现盈亏{p['已实现盈亏']:+.2f}")
    elif args.cmd == "today":
        trades = get_today_trades()
        if not trades:
            print("今日无交易记录")
        for t in trades:
            print(f"  {t['代码']} {t['名称']} {t['方向']} {t['手数']}手 @{t['成交均价']} 理由: {t['操作理由']}")
    elif args.cmd == "list":
        for t in get_all_trades():
            print(f"  [{t['日期']}] {t['代码']} {t['名称']} {t['方向']} {t['手数']}手 @{t['成交均价']}")
    elif args.cmd == "stats":
        print(json.dumps(error_pattern_stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "observe":
        o = add_observation(args.behavior)
        print(f"✓ 观察已记录: {o['行为']}（单次观察，不直接改画像）")
        lines, msg = maybe_update_profile()
        if lines:
            print(f"⚠ 画像更新: {msg}")
            for l in lines:
                print(f"  {l}")
        else:
            print(f"ℹ {msg}")


if __name__ == "__main__":
    main()
