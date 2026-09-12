import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from utils.stock_data import get_stock_detail

def run(stock_code):
    data = get_stock_detail(stock_code)
    
    prompt = f"""
你是专业的个股分析师，请基于以下标的的当日行情数据，生成一份简洁的个股复盘分析。

【标的数据】
代码：{data['代码']}
昨收：{data['昨收']}
今开：{data['今开']}
现价：{data['现价']}
涨跌幅：{data['涨跌幅']}%
最高：{data['最高']}
最低：{data['最低']}
成交额：{round(data['成交额']/100000000, 2)}亿

【输出结构】
1. 当日走势总结
2. 量价与资金分析
3. 关键支撑位与压力位
4. 短期操作建议
    """
    
    return prompt
