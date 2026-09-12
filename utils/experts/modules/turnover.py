# -*- coding: utf-8 -*-
"""turnover 专家注册文件 — 规则自动加载自 turnover_rules.md"""
from utils.experts.base import Expert

class TurnoverExpert(Expert):
    name = "换手率"
    style = "稳健型"
    description = "换手率辅助过滤：相对换手优先，最后一关过滤"
