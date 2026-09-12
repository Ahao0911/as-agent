# -*- coding: utf-8 -*-
"""needle20 专家注册文件 — 规则自动加载自 needle20_rules.md"""
from utils.experts.base import Expert

class Needle20Expert(Expert):
    name = "单针下20"
    style = "稳健型"
    description = "知行单针下20四线指标：J≤20低位低吸找位置"
