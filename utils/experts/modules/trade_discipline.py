# -*- coding: utf-8 -*-
"""trade_discipline 专家注册文件 — 规则自动加载自 trade_discipline_rules.md"""
from utils.experts.base import Expert

class TradeDisciplineExpert(Expert):
    name = "交易纪律"
    style = "保守型"
    description = "统一交易纪律：拒绝下跌途中逆势加仓/追高/计划外交易"
