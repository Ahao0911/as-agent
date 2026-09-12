"""
报告存储 — Markdown给人读，JSON给Agent继续计算
reports/YYYY-MM-DD_pre_market.md/.json
reports/YYYY-MM-DD_post_market.md/.json

用法:
    python3 utils/report_store.py save --type pre_market --json '{"..."}' --md '...' [--date 2026-08-03]
    python3 utils/report_store.py load --type pre_market [--date 2026-08-03]   # 返回 json 路径+内容
    python3 utils/report_store.py list
"""
import os
import sys
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")


def save_report(ftype, report_json, report_md="", date_str=""):
    """
    保存报告（md + json 双份）。
    ftype: pre_market / post_market
    date_str: 报告对应交易日；默认今天
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    prefix = f"{date_str}_{ftype}"
    md_path = os.path.join(REPORTS_DIR, f"{prefix}.md")
    json_path = os.path.join(REPORTS_DIR, f"{prefix}.json")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_json, f, ensure_ascii=False, indent=2, default=str)

    if report_md:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(report_md)
    else:
        # 没有md时，JSON转markdown简易版（供"推送上一份"兜底）
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(report_json, ensure_ascii=False, indent=2, default=str))
    return {"md": md_path, "json": json_path}


def load_report(ftype, date_str="", want="json"):
    """
    读取报告。want: json / md / md_content
    返回 dict {路径, 内容} 或 None
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    prefix = f"{date_str}_{ftype}"
    json_path = os.path.join(REPORTS_DIR, f"{prefix}.json")
    md_path = os.path.join(REPORTS_DIR, f"{prefix}.md")

    if want == "md":
        if os.path.exists(md_path):
            with open(md_path, "r", encoding="utf-8") as f:
                return {"path": md_path, "content": f.read()}
        return None
    if want == "md_content":
        if os.path.exists(md_path):
            with open(md_path, "r", encoding="utf-8") as f:
                return f.read()
        return None
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            return {"path": json_path, "content": json.load(f)}
    return None


def load_latest_report(ftype, want="json"):
    """最近一份某类型报告（可能跨日期）"""
    if not os.path.exists(REPORTS_DIR):
        return None
    files = [f for f in os.listdir(REPORTS_DIR) if f.endswith(f"_{ftype}.json")]
    if not files:
        return None
    latest = sorted(files)[-1]
    date_str = latest.split("_")[0]
    return load_report(ftype, date_str, want)


def list_reports():
    if not os.path.exists(REPORTS_DIR):
        return []
    return sorted(os.listdir(REPORTS_DIR))


def main():
    parser = argparse.ArgumentParser(description="报告存储")
    sub = parser.add_subparsers(dest="cmd")

    p_save = sub.add_parser("save", help="保存报告")
    p_save.add_argument("--type", choices=["pre_market", "post_market"], required=True)
    p_save.add_argument("--json", required=True)
    p_save.add_argument("--md", default="")
    p_save.add_argument("--date", default="")

    p_load = sub.add_parser("load", help="读取报告")
    p_load.add_argument("--type", choices=["pre_market", "post_market"], required=True)
    p_load.add_argument("--date", default="")

    sub.add_parser("list", help="列出全部报告")

    args = parser.parse_args()
    if args.cmd == "save":
        paths = save_report(args.type, json.loads(args.json), args.md, args.date)
        print(f"✓ 报告已保存:\n  md: {paths['md']}\n  json: {paths['json']}")
    elif args.cmd == "load":
        r = load_report(args.type, args.date)
        if r:
            print(f"路径: {r['path']}")
            print(json.dumps(r["content"], ensure_ascii=False, indent=2, default=str))
        else:
            print("无此报告")
    elif args.cmd == "list":
        for f in list_reports():
            print(f"  {f}")


if __name__ == "__main__":
    main()
