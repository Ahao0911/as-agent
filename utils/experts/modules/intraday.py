# -*- coding: utf-8 -*-
"""intraday 专家注册文件 — 规则自动加载自 intraday_rules.md"""
from utils.experts.base import Expert

class IntradayExpert(Expert):
    name = "日内分时"
    style = "稳健型"
    description = "盘中分时分析规范：分时图红绿砖+尾盘判断，区分盘中与盘后指标"
