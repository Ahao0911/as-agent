"""
预测历史存储 — 盘前/盘后闭环的"记忆"层
forecast_history.jsonl 追加式记录每条推演（盘后明日推演 + 盘前情景），
供次日盘前读取、盘后验证，形成 预测→执行→验证→修正 闭环。

用法:
    python3 utils/forecast_store.py save   --date 2026-08-03 --type pre_market --json '{"三情景":[...]}'
    python3 utils/forecast_store.py latest                  # 最近一条（盘前读取昨日推演）
    python3 utils/forecast_store.py by_date --date 2026-08-03
    python3 utils/forecast_store.py verify  --date 2026-08-03 --json '{"命中情景":"基准情景","实际涨跌幅":0.5,"说明":"..."}'
    python3 utils/forecast_store.py stats                   # 校准统计：方向命中率
"""
import os
import sys
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FORECAST_FILE = os.path.join(DATA_DIR, "forecasts", "forecast_history.jsonl")

# 研判节点类型(闭环链条): 晚盘研判(明天) → 盘前印证+研判(今天) → 午间印证+研判(今天) → 晚盘印证(今天)
TYPE_POST_MARKET = "post_market"   # 晚盘: 明日研判
TYPE_PRE_MARKET = "pre_market"     # 盘前: 今日研判(印证昨晚)
TYPE_MIDDAY = "midday"             # 午间: 今日研判(印证盘前)


def _load_all():
    """读取全部预测记录（新→旧）"""
    if not os.path.exists(FORECAST_FILE):
        return []
    records = []
    with open(FORECAST_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    return list(reversed(records))


def save_forecast(date_str, ftype, forecast_data, source="", meta=None):
    """
    保存一条预测。
    date_str: 预测针对的交易日 (YYYY-MM-DD)
    ftype: pre_market / post_market
    forecast_data: 三情景等结构化内容 (dict)
    source: 来源说明（如 盘后复盘2026-08-02）
    """
    os.makedirs(os.path.dirname(FORECAST_FILE), exist_ok=True)
    rec = {
        "预测日期": date_str,
        "类型": ftype,
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "来源": source,
        "预测": forecast_data,
        "验证": None,
    }
    if meta:
        rec.update(meta)
    with open(FORECAST_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return rec


def load_latest_forecast(ftype=None):
    """最近一条预测（盘前读取昨日推演用）。ftype 过滤类型"""
    for r in _load_all():
        if ftype and r.get("类型") != ftype:
            continue
        return r
    return None


def load_forecast_by_date(date_str, ftype=None):
    """指定交易日的预测（盘后验证盘前情景用）"""
    for r in _load_all():
        if r.get("预测日期") == date_str:
            if ftype and r.get("类型") != ftype:
                continue
            return r
    return None


def verify_forecast(date_str, ftype=None, hit_scenario="", actual_chg=None, note="", generated_before=None):
    """
    验证某日某类型的研判：记录实际命中情景。
    date_str: 研判对应交易日
    ftype: 类型过滤(pre_market/midday/post_market),None=任一条
    hit_scenario: 命中情景(基准/强势/弱势) 或 自定义判定
    actual_chg: 实际涨跌幅(方向命中统计)
    note: 印证说明(如"集合竞价高开印证强势")
    generated_before: 只匹配生成时间早于该值(如"2026-08-13 00:00:00")的记录——
                     用于盘后验证"昨日推演"(预测日期=今天、生成于昨天的那条),避免挂错到今日自生成的记录
    """
    records = _load_all()
    target = None
    for r in records:
        if r.get("预测日期") == date_str and (not ftype or r.get("类型") == ftype):
            if generated_before and r.get("生成时间", "") >= generated_before:
                continue
            target = r
            break
    if target is None:
        return None, f"未找到 {date_str} 的研判记录(类型:{ftype or '任意'})"
    # 追加一次验证(支持多轮印证,保留历史)
    verifications = target.setdefault("验证列表", [])
    if target.get("验证") and not verifications:
        verifications.append(target["验证"])
    verifications.append({
        "命中情景": hit_scenario,
        "实际涨跌幅": actual_chg,
        "验证时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "说明": note,
    })
    target["验证"] = verifications[-1]
    # 重写文件
    os.makedirs(os.path.dirname(FORECAST_FILE), exist_ok=True)
    with open(FORECAST_FILE, "w", encoding="utf-8") as f:
        for r in reversed(records):
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return target, "已验证"


def calibration_stats():
    """
    预测校准统计：
    - 已验证条数 / 总条数
    - 情景命中分布
    - 方向命中率：预测的"大概率方向"与实际涨跌幅对比
      （基准+强势=偏多方向，弱势=偏空方向）
    """
    records = _load_all()
    verified = [r for r in records if r.get("验证")]
    total = len(records)
    hit_dist = {}
    for r in verified:
        s = r["验证"].get("命中情景", "未知")
        hit_dist[s] = hit_dist.get(s, 0) + 1

    # 方向命中：取概率最高的情景作为"预测方向"
    dir_hit = dir_total = 0
    for r in verified:
        fc = r.get("预测", {})
        scenarios = fc.get("三情景") or fc.get("情景") or []
        # 兼容 dict 格式: {"基准情景": {"概率": 40, ...}, ...}
        if isinstance(scenarios, dict):
            scenarios = [dict({"情景": k}, **v) for k, v in scenarios.items()]
        if not scenarios:
            continue
        top = max(scenarios, key=lambda s: s.get("概率", 0))
        act = r["验证"].get("实际涨跌幅")
        if act is None:
            continue
        dir_total += 1
        # 弱势情景概率最高 → 预测下跌；否则预测上涨
        pred_down = "弱势" in top.get("情景", "")
        if (pred_down and act < 0) or (not pred_down and act >= 0):
            dir_hit += 1

    return {
        "总预测数": total,
        "已验证数": len(verified),
        "待验证数": total - len(verified),
        "情景命中分布": hit_dist,
        "方向命中": f"{dir_hit}/{dir_total}" if dir_total else "暂无方向样本",
        "方向命中率": round(dir_hit / dir_total * 100, 1) if dir_total else None,
    }


def main():
    parser = argparse.ArgumentParser(description="预测历史存储")
    sub = parser.add_subparsers(dest="cmd")

    p_save = sub.add_parser("save", help="保存预测")
    p_save.add_argument("--date", required=True)
    p_save.add_argument("--type", choices=["pre_market", "post_market", "midday"], required=True)
    p_save.add_argument("--json", required=True, help="预测内容JSON")
    p_save.add_argument("--source", default="")

    sub.add_parser("latest", help="最近一条")
    p_date = sub.add_parser("by_date", help="按日期查")
    p_date.add_argument("--date", required=True)

    p_verify = sub.add_parser("verify", help="验证预测")
    p_verify.add_argument("--date", required=True)
    p_verify.add_argument("--json", required=True, help='{"命中情景":"...","实际涨跌幅":0.5}')

    sub.add_parser("stats", help="校准统计")

    args = parser.parse_args()
    if args.cmd == "save":
        rec = save_forecast(args.date, args.type, json.loads(args.json), args.source)
        print(f"✓ 预测已保存: {args.date} [{args.type}] 来源={args.source or '无'}")
    elif args.cmd == "latest":
        r = load_latest_forecast()
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str) if r else "无预测记录")
    elif args.cmd == "by_date":
        r = load_forecast_by_date(args.date)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str) if r else "无记录")
    elif args.cmd == "verify":
        data = json.loads(args.json)
        rec, msg = verify_forecast(args.date, data.get("命中情景", ""), data.get("实际涨跌幅"), data.get("说明", ""))
        print(f"{'✓' if rec else '✗'} {msg}")
    elif args.cmd == "stats":
        print(json.dumps(calibration_stats(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
