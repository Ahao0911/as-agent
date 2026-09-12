# -*- coding: utf-8 -*-
"""b1 专家注册文件 — 规则自动加载自 b1_rules.md"""
from utils.experts.base import Expert

class B1Expert(Expert):
    name = "B1战法"
    style = "进攻型"
    description = "图形买点体系B1战法：三买三卖六节点(B1B2B3/S1S2S3)+量价+J值+分时，捕捉主升起点"
