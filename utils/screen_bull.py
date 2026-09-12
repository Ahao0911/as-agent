"""
图形买点体系黄白线选股筛选 — 只做多头区间（白线在黄线之上）
用法:
    python3 utils/screen_bull.py                 # 筛选内置股票池
    python3 utils/screen_bull.py 600519 000001  # 自定义代码列表
    python3 utils/screen_bull.py --file 股票池.txt  # 从文件读代码（每行一个）
输出: 多头区间票 / 空头区间票 分组，多头票按偏离度排序
"""
import sys
import os
import time
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

# 内置股票池：科技主线（半导体/AI/算力）+ 消费 + 创新药 + 新能源 混合
DEFAULT_POOL = [
    # 半导体/芯片
    "688981", "603986", "002371", "688012", "300661",
    # AI/算力/服务器
    "000977", "603019", "002230", "300308",
    # 消费
    "600519", "000858", "603288", "000568",
    # 创新药
    "600276", "300347", "603259",
    # 新能源/电力
    "300750", "601012", "600905",
    # 券商/金融
    "600030", "300059",
]


def fetch_kline(code, bars=250):
    """拉取日线K线（akshare优先，mootdx兜底）
    bars: 需要的K线根数（B1公式需要300+）"""
    import akshare as ak
    from utils.stock_data import _clear_proxy, _random_delay

    code = str(code).zfill(6)
    try:
        _random_delay()
        _clear_proxy()
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date="20250101", end_date="20261231", adjust="qfq")
        if df is not None and len(df) > 30:
            df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                    "最高": "high", "最低": "low", "成交量": "volume"})
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"[{code} akshare失败] {e}")

    # 兜底1: akshare 新浪日线（东财接口不通时使用）
    try:
        _random_delay()
        prefix = "sh" if code.startswith(("5", "6", "9")) else ("bj" if code.startswith(("4", "8")) else "sz")
        df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}",
                                 start_date="20250101", end_date="20261231", adjust="qfq")
        if df is not None and len(df) > 30:
            df = df.rename(columns={"date": "date", "open": "open", "close": "close",
                                    "high": "high", "low": "low", "volume": "volume"})
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"[{code} akshare新浪失败] {e}")

    # 兜底2: 腾讯日K线(股票/ETF通用,免费,web.ifzq.gtimg.cn)
    try:
        import requests
        import time
        import random
        prefix = "sh" if str(code).startswith(("5", "6", "9")) else "sz"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{bars},qfq"
        time.sleep(random.uniform(0.6, 1.0))
        r = requests.get(url, timeout=15)
        d = r.json()
        kdata = d.get("data", {}).get(f"{prefix}{code}", {})
        klines = kdata.get("qfqday") or kdata.get("day") or []
        rows = []
        for k in klines:
            rows.append({"date": pd.to_datetime(k[0]), "open": float(k[1]), "close": float(k[2]),
                         "high": float(k[3]), "low": float(k[4]), "volume": float(k[5])})
        if len(rows) > 30:
            df = pd.DataFrame(rows).set_index("date")
            return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"[{code} 腾讯K线失败] {e}")

    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std')
        df = client.bars(symbol=code, frequency=9, offset=bars)
        df = df.rename(columns={"datetime": "date", "open": "open", "high": "high",
                                "low": "low", "close": "close", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"])
        keep = {}
        for col in ["date", "open", "high", "low", "close", "volume"]:
            if col in df.columns:
                keep[col] = df[col] if col != "volume" else (df[col].iloc[:, 0] if isinstance(df[col], pd.DataFrame) else df[col])
        return pd.DataFrame(keep).set_index("date")
    except Exception as e:
        print(f"[{code} mootdx失败] {e}")
    return None


def screen(pool):
    """对股票池逐只分析黄白线，返回分类结果"""
    from utils.indicators import analyze_dual_line

    bulls, bears, errors = [], [], []
    for code in pool:
        code = str(code).zfill(6)
        df = fetch_kline(code)
        if df is None:
            errors.append((code, "无数据"))
            continue
        try:
            r = analyze_dual_line(df)
            if "error" in r:
                errors.append((code, r["error"]))
                continue
            item = {"代码": code, "收盘": r["收盘"], "白线": r["白线"],
                    "黄线": r["黄线"], "J值": r["J值"],
                    "偏离度": round((r["收盘"] - r["黄线"]) / r["黄线"] * 100, 2)}
            if r["多头区间"]:
                bulls.append(item)
            else:
                bears.append(item)
        except Exception as e:
            errors.append((code, str(e)[:50]))
        time.sleep(random.uniform(0.3, 0.8))  # 防反爬

    bulls.sort(key=lambda x: x["偏离度"], reverse=True)
    bears.sort(key=lambda x: x["偏离度"], reverse=True)
    return bulls, bears, errors


def main():
    args = sys.argv[1:]
    pool = DEFAULT_POOL
    if args:
        if args[0] == "--file" and len(args) > 1:
            with open(args[1], "r", encoding="utf-8") as f:
                pool = [line.strip() for line in f if line.strip()]
        else:
            pool = args

    print(f"股票池: {len(pool)} 只，开始黄白线筛选...\n")
    bulls, bears, errors = screen(pool)

    print("=" * 62)
    print(f"★ 多头区间（白线在黄线上，可做）: {len(bulls)} 只")
    print("=" * 62)
    if bulls:
        print(f"{'代码':<8}{'收盘':>8}{'白线':>8}{'黄线':>8}{'J值':>7}{'偏离黄线%':>9}")
        for b in bulls:
            print(f"{b['代码']:<8}{b['收盘']:>8.2f}{b['白线']:>8.2f}{b['黄线']:>8.2f}{b['J值']:>7.1f}{b['偏离度']:>9.2f}")
    else:
        print("(无)")

    print()
    print("=" * 62)
    print(f"✗ 空头区间（白线在黄线下，不做）: {len(bears)} 只")
    print("=" * 62)
    if bears:
        print(f"{'代码':<8}{'收盘':>8}{'白线':>8}{'黄线':>8}{'J值':>7}{'偏离黄线%':>9}")
        for b in bears:
            print(f"{b['代码']:<8}{b['收盘']:>8.2f}{b['白线']:>8.2f}{b['黄线']:>8.2f}{b['J值']:>7.1f}{b['偏离度']:>9.2f}")

    if errors:
        print(f"\n失败: {len(errors)} 只")
        for code, msg in errors[:10]:
            print(f"  {code}: {msg}")


if __name__ == "__main__":
    main()
