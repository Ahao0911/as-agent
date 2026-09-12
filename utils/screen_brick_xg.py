"""
砖型图绿翻强红选股 — 知行体系标准择时器（B2末端动量启动信号）
原版公式逻辑:
1. 昨天绿柱 + 今天红柱 = 绿翻红拐点
2. 红柱高度 ≥ 前一根绿柱高度×2/3 = 绿翻强红
3. 收盘价 > 知行多空线(黄线) = 中期多头环境
4. 板块剔除: 非科创(688)、非创业(300/301)、非ST

优化版（推荐）: 增加 白线>黄线 强制条件（原版漏洞1修复）

⚠ 只能作为观察预警，不能无脑自动买入！
流程: B1选股初选池 → 自选等B2调整 → 本公式扫描XG → 人工复核单针下20共振

用法:
    python3 utils/screen_brick_xg.py                 # 全市场扫描（较慢）
    python3 utils/screen_brick_xg.py 600519 000858  # 自定义代码
"""
import sys
import os
import time
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from utils.indicators import white_line, yellow_line
from utils.brick import brick_chart
from utils.screen_bull import fetch_kline
from utils.strategy_config import CONFIG


def get_stock_names():
    """获取全市场股票名称（用于ST判断）"""
    from mootdx.quotes import Quotes
    names = {}
    try:
        client = Quotes.factory(market='std')
        for mkt in [0, 1]:  # 沪深
            df = client.stocks(market=mkt)
            for _, row in df.iterrows():
                code = str(row['code']).zfill(6)
                name = str(row['name']).replace('\x00', '').strip()
                if code.isdigit() and len(code) == 6 and name and '指数' not in name and 'A股' not in name and 'B股' not in name:
                    names[code] = name
    except Exception as e:
        print(f"[名称获取失败] {e}")
    return names


def check_brick_xg(df, code="", name="", exclude_gem=True, exclude_star=True, exclude_st=True, require_bull=True):
    """
    砖型图绿翻强红选股信号
    require_bull=True: 用优化版（白线>黄线）；False: 原版（仅C>黄线）
    返回: dict
    """
    if df is None or len(df) < 60:
        return {"XG": False, "原因": "数据不足"}

    c = df["close"].astype(float)
    wl = white_line(c)          # 白线
    yl = yellow_line(c)         # 黄线

    # 1. 黄线达标: C > 黄线（中期多头）
    c_val = float(c.iloc[-1])
    yl_val = float(yl.iloc[-1])
    wl_val = float(wl.iloc[-1])
    yellow_ok = c_val > yl_val
    # 2. 白线>黄线（优化版硬性前提）
    bull_ok = wl_val > yl_val

    # 3. 砖型图绿翻强红
    b = brick_chart(df)
    last = len(b) - 1
    cur = float(b["砖型图"].iloc[last])
    prev1 = float(b["砖型图"].iloc[last - 1]) if last > 0 else 0
    prev2 = float(b["砖型图"].iloc[last - 2]) if last > 1 else prev1

    today_red = cur > prev1                      # 今天红柱
    yest_green = prev1 < prev2                   # 昨天绿柱
    red_h = cur - prev1                          # 红柱高度
    green_h = prev2 - prev1                      # 绿柱高度
    strong = green_h > 0 and red_h >= green_h * CONFIG.BRICK_RATIO   # 高度达标(口径统一自 CONFIG)

    xg_base = today_red and yest_green and strong and yellow_ok

    # 4. 板块剔除
    board_ok = True
    if exclude_star and code.startswith("688"):
        board_ok = False
    if exclude_gem and (code.startswith("300") or code.startswith("301")):
        board_ok = False
    if exclude_st and name and ("ST" in name.upper() or "退" in name):
        board_ok = False

    # 5. 最终
    if require_bull:
        xg = xg_base and bull_ok and board_ok
    else:
        xg = xg_base and board_ok

    reasons = []
    if not today_red or not yest_green:
        reasons.append("非绿翻红拐点")
    elif not strong:
        reasons.append(f"强红不达标(红{red_h:.1f}/绿{green_h:.1f}，需≥2/3)")
    elif not yellow_ok:
        reasons.append(f"收盘{c_val:.2f}未站上黄线{yl_val:.2f}")
    elif require_bull and not bull_ok:
        reasons.append(f"白线{wl_val:.2f}<黄线{yl_val:.2f}，趋势未多头")
    elif not board_ok:
        reasons.append("板块剔除(科创/创业/ST)")

    return {
        "XG": bool(xg),
        "代码": code,
        "名称": name,
        "收盘": round(c_val, 2),
        "白线": round(wl_val, 2),
        "黄线": round(yl_val, 2),
        "砖型图": round(cur, 2),
        "红柱高": round(red_h, 2),
        "绿柱高": round(green_h, 2),
        "今天红柱": bool(today_red),
        "昨天绿柱": bool(yest_green),
        "强红": bool(strong),
        "黄线达标": bool(yellow_ok),
        "白线多头": bool(bull_ok),
        "原因": reasons[0] if reasons else "",
    }


def main():
    args = sys.argv[1:]
    names = get_stock_names()
    print(f"股票名称库: {len(names)} 只\n")

    if args:
        pool = [str(c).zfill(6) for c in args]
    else:
        # 全市场扫描（沪深A股，排除指数/北交所）
        pool = []
        for code in names:
            if code.startswith(("60", "00", "30", "68")):
                pool.append(code)
        print(f"全市场扫描: {len(pool)} 只（可能较慢，Ctrl+C可中断）\n")

    hits, misses = [], []
    for code in pool:
        df = fetch_kline(code, bars=120)
        if df is None:
            continue
        try:
            r = check_brick_xg(df, code=code, name=names.get(code, ""))
            if r["XG"]:
                hits.append(r)
            else:
                misses.append(r)
        except Exception:
            pass
        time.sleep(random.uniform(0.15, 0.4))

    print("═" * 78)
    print(f"★ 砖型图绿翻强红信号命中: {len(hits)} 只")
    print("═" * 78)
    if hits:
        print(f"{'代码':<8}{'名称':<10}{'收盘':>8}{'白线':>8}{'黄线':>8}{'砖型图':>8}{'红高':>6}{'绿高':>6}")
        for h in hits:
            print(f"{h['代码']:<8}{h['名称']:<10}{h['收盘']:>8.2f}{h['白线']:>8.2f}{h['黄线']:>8.2f}{h['砖型图']:>8.2f}{h['红柱高']:>6.1f}{h['绿柱高']:>6.1f}")
        print("\n※ 下一步: 人工复核【单针下20】是否低位共振，肉眼确认是连续绿调整后的第一根红砖")
    else:
        print("  (今日无命中)")

    print(f"\n✗ 未命中: {len(misses)} 只（前10个原因）")
    for m in misses[:10]:
        print(f"  {m['代码']} {m['名称']}: {m['原因']}")


if __name__ == "__main__":
    main()
