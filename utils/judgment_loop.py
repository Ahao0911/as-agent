"""
Ahao Stock Agent · 研判闭环调度(judgment_loop)
晚盘复盘→生成明日研判 → 次日盘前(集合竞价)印证+研判 → 午间模拟印证+研判 → 晚盘印证
循环联动: 每次印证结果累积 → 更新 agent 认知(agent_insight.md)

用法:
    python utils/judgment_loop.py record --date 2026-08-13 --stage evening --json '{"方向":"偏多","情景":"..."}'
    python utils/judgment_loop.py verify --date 2026-08-12 --stage pre_market --hit 强势 --chg 1.2 --note "竞价高开"
    python utils/judgment_loop.py insight          # 更新 agent 认知
    python utils/judgment_loop.py chain --date 2026-08-12   # 查看某日研判链条
    python utils/judgment_loop.py clean            # 清理日志/报告
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import forecast_store as fs

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
INSIGHT_FILE = os.path.join(DATA_DIR, "agent_insight.md")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
MX_OUTPUT = os.path.join(DATA_DIR, "mx_output")

# 阶段映射: 记录/验证用的 stage → forecast_store 类型
STAGE_TYPE = {
    "evening": fs.TYPE_POST_MARKET,   # 晚盘研判(针对次日)
    "pre_market": fs.TYPE_PRE_MARKET, # 盘前研判(针对当日, 印证昨晚)
    "midday": fs.TYPE_MIDDAY,         # 午间研判(针对当日, 印证盘前)
}

# 印证的目标类型(某环节印证"上一环节生成的研判"):
#   盘前印证 = 昨晚晚盘研判(post_market, 预测日期=今天)
#   午间印证 = 今早盘前研判(pre_market, 预测日期=今天)
#   晚间印证 = 今日午间研判(midday, 预测日期=今天)
VERIFY_TARGET = {
    "pre_market": fs.TYPE_POST_MARKET,
    "midday": fs.TYPE_PRE_MARKET,
    "evening": fs.TYPE_MIDDAY,
}


def record_judgment(date_str, stage, content, source=""):
    """记录一段研判(晚盘/盘前/午间)"""
    ftype = STAGE_TYPE.get(stage)
    if not ftype:
        return {"ok": False, "msg": f"未知阶段 {stage}"}
    rec = fs.save_forecast(date_str, ftype, content, source=source or f"{stage}研判")
    return {"ok": True, "msg": f"✓ 已记录 {stage} 研判({date_str})", "rec": rec}


def verify_judgment(date_str, stage, hit, chg=None, note=""):
    """印证一段研判: stage=验证环节(盘前/午间/晚间), 验证的是"上一环节生成、预测日期=今天"的研判"""
    ftype = VERIFY_TARGET.get(stage)
    if not ftype:
        return {"ok": False, "msg": f"未知验证环节 {stage}"}
    rec, msg = fs.verify_forecast(date_str, ftype, hit, chg, note)
    if rec is None:
        return {"ok": False, "msg": msg}
    return {"ok": True, "msg": f"✓ {stage} 印证完成: {msg}"}


def update_insight():
    """从验证历史提取认知要点, 写入 agent_insight.md(覆盖式,小文件)"""
    stats = fs.calibration_stats()
    records = fs._load_all()
    lines = [
        "# 🤖 Agent 研判认知(自动更新,覆盖式)",
        f"> 更新于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 预测{stats.get('总预测数',0)}条, 已验证{stats.get('已验证数',0)}条",
        "",
        f"**方向命中率**: {stats.get('方向命中', '暂无')} ({stats.get('方向命中率', '-')}%)",
        f"**情景命中分布**: {json.dumps(stats.get('情景命中分布', {}), ensure_ascii=False)}",
        "",
        "## 研判-印证链条记录(最近10条)",
    ]
    seen = set()
    count = 0
    for r in records:
        key = (r.get("预测日期"), r.get("类型"))
        if key in seen:
            continue
        seen.add(key)
        verifications = r.get("验证列表") or ([r["验证"]] if r.get("验证") else [])
        if not verifications:
            continue
        for v in verifications:
            lines.append(f"- {r.get('预测日期')} [{r.get('类型')}] 命中:{v.get('命中情景','?')} 涨跌幅:{v.get('实际涨跌幅','?')} 注:{v.get('说明','')[:40]}")
            count += 1
            if count >= 10:
                break
        if count >= 10:
            break
    with open(INSIGHT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return INSIGHT_FILE


def chain_view(date_str):
    """查看某日研判闭环链条"""
    lines = [f"📋 {date_str} 研判闭环链条:"]
    for stage, ftype in [("晚盘(针对当日)", fs.TYPE_POST_MARKET),
                         ("盘前", fs.TYPE_PRE_MARKET),
                         ("午间", fs.TYPE_MIDDAY)]:
        r = fs.load_forecast_by_date(date_str, ftype)
        if r:
            v = r.get("验证列表") or ([r["验证"]] if r.get("验证") else [])
            vstr = " | ".join(f"{x.get('命中情景')}({x.get('实际涨跌幅')})" for x in v) if v else "未印证"
            lines.append(f"  {stage}: 生成{r.get('生成时间','')[:16]} → 印证[{vstr}]")
        else:
            lines.append(f"  {stage}: 无记录")
    return "\n".join(lines)


def cleanup(retain_days_reports=14, retain_forecasts=200, retain_mx_days=7):
    """
    日志清理(控制磁盘占用):
    - data/reports/ 只保留最近 N 天
    - forecast_history.jsonl 只保留最近 N 条
    - data/mx_output/ 清理 N 天前的选股中间文件
    - data/ 下 *_snapshot.json / *_index.json 等缓存清理 N 天前
    """
    removed = {"reports": 0, "mx_output": 0, "cache": 0, "forecast": 0}
    now = datetime.now()

    # 1. reports
    if os.path.isdir(REPORT_DIR):
        for fn in os.listdir(REPORT_DIR):
            fp = os.path.join(REPORT_DIR, fn)
            try:
                # 从文件名提取日期 YYYY-MM-DD
                d = fn[:10]
                dt = datetime.strptime(d, "%Y-%m-%d")
                if (now - dt).days > retain_days_reports:
                    os.remove(fp)
                    removed["reports"] += 1
            except (ValueError, OSError):
                pass

    # 2. forecast jsonl 裁剪(保留最近 N 条)
    fc_file = fs.FORECAST_FILE
    if os.path.exists(fc_file):
        with open(fc_file, "r", encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        if len(lines) > retain_forecasts:
            with open(fc_file, "w", encoding="utf-8") as f:
                f.writelines(lines[-retain_forecasts:])
            removed["forecast"] = len(lines) - retain_forecasts

    # 3. mx_output
    if os.path.isdir(MX_OUTPUT):
        for fn in os.listdir(MX_OUTPUT):
            fp = os.path.join(MX_OUTPUT, fn)
            try:
                if now - datetime.fromtimestamp(os.path.getmtime(fp)) > timedelta(days=retain_mx_days):
                    os.remove(fp)
                    removed["mx_output"] += 1
            except OSError:
                pass

    # 4. data/ 缓存 json(非账户/非CSV)
    if os.path.isdir(DATA_DIR):
        for fn in os.listdir(DATA_DIR):
            if not (fn.endswith(".json") and ("snapshot" in fn or "index" in fn or "sector" in fn
                    or "sentiment" in fn or "amount" in fn or "market_value" in fn)):
                continue
            fp = os.path.join(DATA_DIR, fn)
            try:
                if now - datetime.fromtimestamp(os.path.getmtime(fp)) > timedelta(days=retain_mx_days):
                    os.remove(fp)
                    removed["cache"] += 1
            except OSError:
                pass
    return removed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="研判闭环")
    sub = parser.add_subparsers(dest="cmd")
    p_rec = sub.add_parser("record")
    p_rec.add_argument("--date", required=True)
    p_rec.add_argument("--stage", choices=list(STAGE_TYPE.keys()), required=True)
    p_rec.add_argument("--json", required=True)
    p_rec.add_argument("--source", default="")
    p_vf = sub.add_parser("verify")
    p_vf.add_argument("--date", required=True)
    p_vf.add_argument("--stage", choices=["pre_market", "midday", "evening"], required=True)
    p_vf.add_argument("--hit", required=True)
    p_vf.add_argument("--chg", type=float, default=None)
    p_vf.add_argument("--note", default="")
    sub.add_parser("insight")
    p_ch = sub.add_parser("chain")
    p_ch.add_argument("--date", required=True)
    sub.add_parser("clean")
    args = parser.parse_args()

    if args.cmd == "record":
        r = record_judgment(args.date, args.stage, json.loads(args.json), args.source)
        print(r["msg"])
    elif args.cmd == "verify":
        r = verify_judgment(args.date, args.stage, args.hit, args.chg, args.note)
        print(r["msg"])
    elif args.cmd == "insight":
        print("✓ 认知已更新:", update_insight())
    elif args.cmd == "chain":
        print(chain_view(args.date))
    elif args.cmd == "clean":
        print("清理结果:", cleanup())
    else:
        parser.print_help()
