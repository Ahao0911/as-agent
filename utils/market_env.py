# -*- coding: utf-8 -*-
"""
市场环境系数(对标高手看板「大盘研判完整框架」)
==========================================
MA5/10/20 分级控仓: 多头 1.0x / 中性 0.6x / 偏弱 0.4x / 熊市 0.25x 强制缩仓
熊市专属规则: 仓位上限15% / 单只上限20万 / 禁止左侧抄底 / 止损冷却 / 双确认进攻条件
"""
import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.indicators import ma


def market_env(df_index):
    """大盘环境判断(上证指数日K)

    返回: {环境, 系数, 仓位上限, 说明, 均线}
    """
    c = df_index["close"].astype(float)
    ma5 = float(ma(c, 5).iloc[-1])
    ma10 = float(ma(c, 10).iloc[-1])
    ma20 = float(ma(c, 20).iloc[-1])
    ma20_prev = float(ma(c, 20).iloc[-6]) if len(c) >= 25 else ma20  # 5日前MA20判断趋势

    ma20_up = ma20 > ma20_prev

    if ma5 > ma10 > ma20 and ma20_up:
        env = "多头"
        coef = 1.0
        cap = 1.0
        desc = "MA5>MA10>MA20 且 MA20 上行 → 满仓可做"
    elif ma5 > ma10 > ma20:
        env = "多头(待确认)"
        coef = 0.6
        cap = 0.6
        desc = "短期多头但 MA20 未上行 → 半仓谨慎"
    elif ma5 < ma10 < ma20 and ma20_up:
        env = "偏弱"
        coef = 0.4
        cap = 0.4
        desc = "空头排列但 MA20 仍上行 → 反弹对待,轻仓"
    elif ma5 < ma10 < ma20 and not ma20_up:
        env = "熊市"
        coef = 0.25
        cap = 0.15
        desc = "空头排列且 MA20 下行 → 强制缩仓(上限15%)"
    else:
        env = "中性"
        coef = 0.6
        cap = 0.6
        desc = "均线纠缠 → 观望,半仓"

    return {"环境": env, "系数": coef, "仓位上限": cap, "说明": desc,
            "MA5": round(ma5, 2), "MA10": round(ma10, 2), "MA20": round(ma20, 2)}


def double_confirm_check(df_index, foreign_fixed=True):
    """双确认进攻条件: 上证站稳 MA20 + 外围市场修复(美股/港股不再破位)"""
    c = df_index["close"].astype(float)
    ma20 = float(ma(c, 20).iloc[-1])
    close_now = float(c.iloc[-1])
    above_ma20 = close_now > ma20
    return {"上证站稳MA20": above_ma20,
            "外围修复": bool(foreign_fixed),
            "双确认": above_ma20 and bool(foreign_fixed)}


def bear_rules_check(env_info):
    """熊市专属规则 → 约束清单(供买入前调用)"""
    rules = {
        "熊市仓位上限15%": env_info["环境"] == "熊市",
        "禁止左侧抄底": env_info["环境"] in ("熊市", "偏弱"),
        "单只上限20万": env_info["环境"] in ("熊市", "偏弱"),
        "止损冷却(连续止损熔断观察一周)": env_info["环境"] == "熊市",
    }
    return rules


if __name__ == "__main__":
    # 自测: 用上证指数数据
    from utils.data_router import get_daily_bars
    r = get_daily_bars("000001", count=250, purpose="indicator")
    df = pd.DataFrame(r["data"]).set_index("date")
    df.index = pd.to_datetime(df.index)
    env = market_env(df)
    print("环境:", env)
    print("熊市规则:", bear_rules_check(env))
    print("双确认:", double_confirm_check(df))
