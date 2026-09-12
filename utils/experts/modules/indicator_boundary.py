# -*- coding: utf-8 -*-
"""indicator_boundary 专家注册文件 — 规则自动加载自 indicator_boundary_rules.md"""
from utils.experts.base import Expert

class IndicatorBoundaryExpert(Expert):
    name = "指标边界"
    style = "稳健型"
    description = "指标使用边界强制约束：分层工具严格划分，禁止跨层乱用"
