# -*- coding: utf-8 -*-
"""
三层风控引擎(对标高手看板)
========================
执行层: 止损 -3%~-5%(可配置) + 尾盘买入 T+1 灵活离场
止盈层: 阶梯止盈(固定涨幅分批) + 移动追踪止盈(最高价回撤 2%/4%/6% 分批)
系统层: 大盘系数据兜底(活跃市值/活跃ETF 阈值 → 整体降仓/清仓)

纯检查逻辑, 返回「动作建议」, 由 sim_portfolio 执行卖出。
"""
import os
import sys
from dataclasses import dataclass, field

# 支持两种运行方式: 包内 import(utils.risk_engine) 与 直接脚本(python utils/risk_engine.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.strategy_config import CONFIG


@dataclass
class RiskConfig:
    """风控配置(可调)"""
    # 执行层
    # 止损口径统一自 图形买点体系唯一参数源(strategy_config), 不再本地硬编码
    stop_loss_pct: float = CONFIG.STOP_LOSS_INTRADAY_PCT  # 止损 -4%(图形买点体系盘中硬止损)
    t1_loss_pct: float = CONFIG.T1_LOSS_PCT  # T+1 灵活离场: 买入次日跌破成本 -2% 即离场
    # 止盈层 · 移动追踪止盈(最高价回撤 → 减仓比例)
    trailing: list = field(default_factory=lambda: [
        (2.0, 1 / 3),   # 回撤 2% 减 1/3
        (4.0, 1 / 2),   # 回撤 4% 再减半(剩 1/3)
        (6.0, 1.0),     # 回撤 6% 清仓
    ])
    # 止盈层 · 阶梯止盈(固定涨幅 → 减仓比例)
    take_profit: list = field(default_factory=lambda: [
        (5.0, 1 / 3),   # 涨 5% 减 1/3
        (10.0, 1 / 3),  # 涨 10% 再减 1/3
        (15.0, 1 / 3),  # 涨 15% 再减 1/3(锁利)
    ])
    # 系统层 · 大盘兜底(活跃市值涨跌幅阈值)
    active_mv_exit: float = -2.3        # 活跃市值 <= -2.3% → 整体清仓(离场线)
    active_mv_warn: float = 0.0         # 活跃市值 <= 0% → 整体降仓(减半)
    # 系统层 · 熊市专属
    bear_position_cap: float = 0.15     # 熊市总仓位上限 15%
    single_position_cap: float = 200000 # 单只上限 20 万


def check_position(p, cur_price, cfg=None):
    """检查单只持仓, 返回动作 (reason, ratio) 或 None

    ratio: 减仓比例(0~1], 1.0=清仓
    优先级: 止损 > 移动追踪止盈 > 阶梯止盈
    """
    cfg = cfg or RiskConfig()
    cur_price = float(cur_price)
    cost = float(p.get("成本价", cur_price))

    # 追踪最高价(用于移动止盈回撤计算)
    high = max(float(p.get("最高价", cost)), cur_price)
    p["最高价"] = high

    # 1. 执行层 · 止损
    if cur_price <= float(p.get("止损价", cost * (1 - cfg.stop_loss_pct / 100))):
        return ("止损", 1.0)

    # 2. 止盈层 · 移动追踪止盈(最高价回撤)
    if high > cost:  # 有浮盈才追踪
        drawdown = (high - cur_price) / high * 100
        stage = int(p.get("止盈阶段", 0))
        if stage < len(cfg.trailing) and drawdown >= cfg.trailing[stage][0]:
            p["止盈阶段"] = stage + 1
            return (f"移动止盈-回撤{cfg.trailing[stage][0]:.0f}%", cfg.trailing[stage][1])

    # 3. 止盈层 · 阶梯止盈(固定涨幅)
    gain = (cur_price - cost) / cost * 100
    tp_stage = int(p.get("阶梯止盈阶段", 0))
    if tp_stage < len(cfg.take_profit) and gain >= cfg.take_profit[tp_stage][0]:
        p["阶梯止盈阶段"] = tp_stage + 1
        return (f"阶梯止盈+{cfg.take_profit[tp_stage][0]:.0f}%", cfg.take_profit[tp_stage][1])

    return None


def check_t1_exit(p, cur_price, cfg=None):
    """T+1 灵活离场: 买入次日跌破成本 -2% 即离场(短线不恋战)"""
    cfg = cfg or RiskConfig()
    cost = float(p.get("成本价", 0))
    if cost and cur_price <= cost * (1 - cfg.t1_loss_pct / 100):
        return ("T+1灵活离场", 1.0)
    return None


def check_system(active_mv_pct, cfg=None):
    """系统层 · 大盘兜底: 活跃市值阈值 → 整体动作

    返回 (reason, ratio) 或 None
    """
    cfg = cfg or RiskConfig()
    if active_mv_pct is None:
        return None
    if active_mv_pct <= cfg.active_mv_exit:
        return (f"大盘兜底-活跃市值{active_mv_pct:+.1f}%离场线", 1.0)
    if active_mv_pct <= cfg.active_mv_warn:
        return (f"大盘兜底-活跃市值{active_mv_pct:+.1f}%降仓", 0.5)
    return None


def position_cap_check(total_mv, cfg=None):
    """熊市专属: 单只/总仓位上限检查(供买入前调用)"""
    cfg = cfg or RiskConfig()
    return {
        "单只上限": cfg.single_position_cap,
        "熊市总仓位上限": cfg.bear_position_cap,
    }


if __name__ == "__main__":
    # 自测: 模拟一只持仓的完整风控路径
    p = {"名称": "测试", "数量": 1000, "成本价": 100.0, "止损价": 96.0,
         "最高价": 100.0, "止盈阶段": 0, "阶梯止盈阶段": 0}
    print("涨到 108(>5%):", check_position(p, 108.0))       # 阶梯止盈+5%
    print("回撤到 105(从108回撤2.7%):", check_position(p, 105.0))  # 移动止盈-回撤2%
    print("跌破止损 95:", check_position(p, 95.0))           # 止损
    print("系统层 活跃市值-2.5%:", check_system(-2.5))
    print("系统层 活跃市值-1%:", check_system(-1.0))
