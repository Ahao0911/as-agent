# -*- coding: utf-8 -*-
"""market_value 专家注册文件 — 规则自动加载自 market_value_rules.md"""
from utils.experts.base import Expert

class MarketValueExpert(Expert):
    name = "活跃市值"
    style = "稳健型"
    description = "活跃市值战法：0AmV定大盘择时与仓位管理(4%入场/-2.3%离场)"
