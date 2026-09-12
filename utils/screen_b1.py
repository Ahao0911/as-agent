"""
B1启动选股 — 批量筛选
用法:
    python3 utils/screen_b1.py                 # 内置股票池
    python3 utils/screen_b1.py 600519 000858  # 自定义
    python3 utils/screen_b1.py --file 股票池.txt
"""
import sys
import os
import time
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.screen_bull import DEFAULT_POOL, fetch_kline
from utils.b1_select import b1_signal
from utils.indicators import analyze_dual_line


def get_liutong_map(codes):
    """逐只获取流通股本（mootdx finance 只支持单代码）"""
    from mootdx.quotes import Quotes
    result = {}
    try:
        client = Quotes.factory(market='std')
        for code in codes:
            try:
                fin = client.finance(symbol=code)
                if fin is not None and len(fin):
                    result[code] = float(fin.iloc[0]['liutongguben'])
            except Exception:
                pass
    except Exception as e:
        print(f"[流通股本获取失败] {e}")
    return result


def main():
    args = sys.argv[1:]
    pool = DEFAULT_POOL
    if args:
        if args[0] == "--file" and len(args) > 1:
            with open(args[1], "r", encoding="utf-8") as f:
                pool = [line.strip() for line in f if line.strip()]
        else:
            pool = args
    pool = [str(c).zfill(6) for c in pool]

    print(f"股票池: {len(pool)} 只，B1启动信号扫描...\n")
    print("获取流通股本...")
    mv_map = get_liutong_map(pool)
    print(f"  获取到 {len(mv_map)} 只\n")

    hits, misses, errors = [], [], []
    for code in pool:
        df = fetch_kline(code, bars=400)  # B1公式需要MA250，至少300+根
        if df is None:
            errors.append((code, "无数据"))
            continue
        try:
            r = b1_signal(df, liutong_guben=mv_map.get(code))
            dual = analyze_dual_line(df)
            item = {
                "代码": code,
                "收盘": r["收盘"],
                "J值": r["J值"],
                "QL": r["QL"],
                "多头区间": dual.get("多头区间", False),
            }
            if r["B1"]:
                hits.append(item)
            else:
                misses.append((code, r.get("信号说明", ["?"])[:1]))
        except Exception as e:
            errors.append((code, str(e)[:50]))
        time.sleep(random.uniform(0.2, 0.4))

    print("═" * 60)
    print(f"★ B1启动信号命中: {len(hits)} 只")
    print("═" * 60)
    if hits:
        print(f"{'代码':<8}{'收盘':>9}{'J值':>7}{'QL成本':>10}{'黄白线':>8}")
        for h in hits:
            bull = "✓多头" if h["多头区间"] else "✗空头"
            print(f"{h['代码']:<8}{h['收盘']:>9.2f}{h['J值']:>7.1f}{h['QL']:>10.2f}{bull:>8}")
        # 黄白线二次过滤
        strong = [h for h in hits if h["多头区间"]]
        print()
        if strong:
            print(f"◆ B1+黄白线多头（首选）: {len(strong)} 只")
            for h in strong:
                print(f"  {h['代码']}  收盘{h['收盘']}  J{h['J值']}")
        else:
            print("⚠ B1命中但无一处于黄白线多头区间 → 按体系应放弃或降仓")

    print()
    print("═" * 60)
    print(f"✗ 未命中: {len(misses)} 只")
    print("═" * 60)
    for code, why in misses[:15]:
        print(f"  {code}: {why[0] if why else '?'}")

    if errors:
        print(f"\n⚠ 失败: {len(errors)} 只")
        print("  " + "  ".join(f"{c}" for c, _ in errors[:10]))


if __name__ == "__main__":
    main()
