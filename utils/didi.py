"""
图形买点体系 滴滴战法（地铁战法）

滴滴 = 逃顶信号

规则：两根阴线，第二根收盘价 < 第一根最低价
→ 全跑，或者至少跑1/2
"""
import numpy as np
import pandas as pd


def detect_didi(df):
    """
    检测滴滴（逃顶信号）

    规则：两根阴线，第二根收盘价 < 第一根最低价
    → 全跑，或者至少跑1/2

    df: DataFrame (open/high/low/close/volume，升序)
    返回: dict {'is_escape': bool, 'action': str, 'details': dict}
    """
    if df is None or len(df) < 3:
        return {'is_escape': False, 'action': '无信号', 'details': {}}

    c1 = df.iloc[-2]
    c0 = df.iloc[-1]

    # 条件：两根都是阴线
    if not (c1['close'] < c1['open'] and c0['close'] < c0['open']):
        return {'is_escape': False, 'action': '无信号', 'details': {'reason': '不是两根阴线'}}

    # 核心条件：第二根收盘价 < 第一根最低价
    if not (c0['close'] < c1['low']):
        return {'is_escape': False, 'action': '无信号', 'details': {
            'reason': '第二根收盘未跌破第一根最低价',
            'c0_close': c0['close'], 'c1_low': c1['low']
        }}

    return {
        'is_escape': True,
        'action': '全跑或至少跑1/2',
        'details': {
            '阴线1': {'日期': str(c1.get('date', '')), '开': c1['open'], '收': c1['close'], '低': c1['low']},
            '阴线2': {'日期': str(c0.get('date', '')), '开': c0['open'], '收': c0['close'], '低': c0['low']},
            '条件': f'第二根收盘{c0["close"]:.2f} < 第一根最低{c1["low"]:.2f}',
            '提示': '滴滴信号，全跑或至少跑1/2！',
        }
    }