"""
图形买点体系 量比开盘法（819系列完整版）
量比 = 开盘成交量 / 过去5日同时段均量
核心：量比+股价方向决定当天走势
"""
import numpy as np


def analyze_volume_ratio(open_vol_ratio, open_change_pct, is_low_open=False,
                         price_direction=None, vol_ratio_direction=None):
    """
    量比开盘法分析（完整版）
    open_vol_ratio: 开盘量比
    open_change_pct: 开盘涨跌幅（%）
    price_direction: 开盘后股价方向 'up'/'down'/'flat'
    vol_ratio_direction: 开盘后量比方向 'up'/'down'/'flat'
    返回: dict
    """
    signals = []
    verdict = ""

    # ===== 量比-股价 四象限判断 =====
    if price_direction and vol_ratio_direction:
        if vol_ratio_direction == 'up' and price_direction == 'up':
            verdict = "量价齐升 → 强势，做多"
            signals.append("量比往上+股价往上 = 强势信号，可追涨")
        elif vol_ratio_direction == 'down' and price_direction == 'down':
            verdict = "量价齐跌 → 震荡熄火，观望"
            signals.append("量比往下+股价往下 = 今天大概率震荡熄火")
        elif vol_ratio_direction == 'up' and price_direction == 'down':
            verdict = "量比上股价下 → 危险，往下砸"
            signals.append("量比往上+股价往下 = 放量下跌，危险信号")
        elif vol_ratio_direction == 'down' and price_direction == 'up':
            verdict = "量比下股价上 → 主力高控盘/缩量上涨"
            signals.append("量比往下+股价往上 = 缩量上涨，主力控盘强")

    # ===== 开盘量比绝对值判断 =====
    if open_vol_ratio >= 50 and open_change_pct > 2:
        verdict = "超高量比高开(>50x) → 时不我待，必须马上建仓"
        signals.append(f"量比{open_vol_ratio:.0f}x！从{open_change_pct:+.2f}%拉到8%+时不等人")
    elif open_vol_ratio >= 30 and open_change_pct > 0:
        verdict = "高量比高开(30x+) → 极强势，可追"
        signals.append(f"量比{open_vol_ratio:.0f}x → 极强的做多信号")
    elif open_vol_ratio >= 10 and open_change_pct > 0:
        verdict = "高量比高开(10x+) → 强势，可逢低介入"
        signals.append(f"量比{open_vol_ratio:.0f}x + 高开{open_change_pct:+.2f}% → 做多力量强")
    elif open_vol_ratio >= 10 and open_change_pct < -2:
        verdict = "高量比大幅低开(10x+) → 危险，大概率大跌"
        signals.append(f"量比{open_vol_ratio:.0f}x + 低开{open_change_pct:+.2f}% → 往下砸不需要量")
    elif open_vol_ratio >= 10 and open_change_pct < 0:
        verdict = "高量比小幅低开 → 多空博弈，看开盘5分钟方向"
        signals.append(f"量比{open_vol_ratio:.0f}x + 低开{open_change_pct:+.2f}% → 观察方向")
    elif open_vol_ratio < 1.0:
        verdict = "低量比(<1.0x) → 今天大概率震荡，低吸为主"
        signals.append(f"量比{open_vol_ratio:.2f}x → 没戏，适合低吸，不适合追高")
        signals.append("低量比突破(3-6x)：有些主力高控盘不需放量就能拉，可以干")
    elif open_vol_ratio < 1.5:
        verdict = "低量比(<1.5x) → 震荡行情，等回荡慢慢买"
        signals.append(f"量比{open_vol_ratio:.2f}x → 交投清淡")
    elif open_vol_ratio >= 3:
        verdict = f"中量比({open_vol_ratio:.0f}x) → 有资金关注，看方向"
        signals.append(f"量比{open_vol_ratio:.0f}x → 中量，观察股价方向配合")
    else:
        verdict = f"量比{open_vol_ratio:.1f}x → 正常水平，按原有信号执行"

    return {
        "量比": round(open_vol_ratio, 1),
        "开盘涨幅": round(open_change_pct, 2),
        "判断": verdict,
        "信号": signals,
    }


def volume_ratio_rule_of_thumb():
    """
    量比开盘法速查表（完整版）
    """
    return {
        "量比<1.0": "没戏，今天大概率震荡，低吸为主",
        "量比1.0-1.5": "交投清淡，等回荡慢慢买",
        "量比3-6": "中量，有资金关注，看方向",
        "量比10+高开": "强势，做多力量强",
        "量比30+高开": "极强势，可追",
        "量比50+高开": "时不我待，必须马上建仓",
        "量比10+大幅低开": "危险，大概率大跌",
        "量比上+股价上": "量价齐升，强势信号",
        "量比下+股价下": "量价齐跌，震荡熄火",
        "量比上+股价下": "放量下跌，危险信号",
        "量比下+股价上": "缩量上涨，主力高控盘",
    }


def stop_loss_three_rules():
    """
    图形买点体系止损三原则（819系列）
    """
    return [
        "① 高手买入后该涨不涨，成本区附近拍掉不等",
        "② 买入后三个交易日没脱离成本区（day3），多等一天（day4）",
        "③ 赚钱的票有过上涨行为后，马上又回到成本价，拍掉（说明弱）",
    ]