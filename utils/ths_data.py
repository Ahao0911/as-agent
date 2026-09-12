"""
同花顺 iFinD HTTP API 接入 — 备用数据源
需要 access_token（同花顺量化平台注册申请：https://quantapi.51ifind.com 申请试用）

配置: 环境变量 THS_ACCESS_KEY 或 data/ths_config.json {"access_key": "..."}
用法:
    python3 utils/ths_data.py quote 300033.SZ      # 实时行情
    python3 utils/ths_data.py hist 300033.SZ       # 历史K线(close,volume)
    python3 utils/ths_data.py wencai "主力资金流向" # 问财自然语言
"""
import os
import sys
import json
import requests
import urllib3

urllib3.disable_warnings()

BASE = "https://quantapi.51ifind.com/api/v1"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def get_access_key():
    """从环境变量或配置文件读取 token"""
    key = os.environ.get("THS_ACCESS_KEY")
    if not key:
        cfg = os.path.join(DATA_DIR, "ths_config.json")
        if os.path.exists(cfg):
            with open(cfg, "r", encoding="utf-8") as f:
                key = json.load(f).get("access_key")
    return key


def _post(path, params):
    key = get_access_key()
    if not key:
        return {"errorcode": -9999, "errmsg": "未配置 access_token！注册申请: https://quantapi.51ifind.com 或设环境变量 THS_ACCESS_KEY"}
    url = f"{BASE}/{path}"
    headers = {"access_key": key}
    r = requests.post(url, json=params, headers=headers, timeout=20, verify=False)
    return r.json()


def quote(codes):
    """实时行情"""
    return _post("cmd_realtime_quote", {"codes": codes, "indicators": "latest,changeRatio,amount,volume"})


def hist(codes, indicators="close,volume", start="2025-01-01", end="2026-12-31"):
    """历史K线"""
    return _post("cmd_history_quotation", {
        "codes": codes, "indicators": indicators,
        "startDate": start, "endDate": end,
    })


def wencai(question, market="stock"):
    """问财自然语言选股"""
    return _post("cmd_wencai", {"question": question, "market": market})


def main():
    args = sys.argv[1:]
    if not args:
        print("用法: ths_data.py [quote|hist|wencai] <参数>")
        return
    cmd = args[0]
    key = get_access_key()
    if not key:
        print("✗ 未配置 access_token")
        print("  注册申请: https://quantapi.51ifind.com (申请试用)")
        print("  拿到后: export THS_ACCESS_KEY=xxx 或 写 data/ths_config.json")
        return

    if cmd == "quote" and len(args) > 1:
        print(json.dumps(quote(args[1]), ensure_ascii=False, indent=2))
    elif cmd == "hist" and len(args) > 1:
        print(json.dumps(hist(args[1]), ensure_ascii=False, indent=2)[:2000])
    elif cmd == "wencai" and len(args) > 1:
        print(json.dumps(wencai(" ".join(args[1:])), ensure_ascii=False, indent=2)[:2000])
    else:
        print("参数不足")


if __name__ == "__main__":
    main()
