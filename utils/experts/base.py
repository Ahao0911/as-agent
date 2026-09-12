"""
专家系统基类 — 所有交易专家统一接口
新增专家：在 experts/ 下新建 .py 文件，继承 Expert 类即可，注册表自动发现
"""

class Expert:
    """交易专家基类"""
    name = "unnamed"            # 显示名称
    style = "稳健"               # 风格标签：激进 / 稳健 / 保守
    description = ""            # 一句话介绍

    def __init__(self):
        self.rules_text = self.load_rules()

    def load_rules(self):
        """从同目录 <模块名>_rules.md 加载规则文本（若无则返回空）"""
        import os
        import inspect
        module_name = inspect.getmodule(self).__name__.split(".")[-1]
        rule_path = os.path.join(os.path.dirname(__file__), f"{module_name}_rules.md")
        if os.path.exists(rule_path):
            with open(rule_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def get_prompt(self, market_data=None):
        """
        生成该专家的分析提示词模板
        子类可重写，实现各自的思维逻辑
        """
        return f"""你是一位{self.style}风格的交易专家【{self.name}】。
{self.description}

【交易体系规则】
{self.rules_text}

请严格遵循以上体系规则进行分析和操作建议，规则与行情冲突时以规则为准。
"""

    def analyze(self, data):
        """
        纯代码分析函数（可选实现）
        用于技术指标计算、信号判断等确定性逻辑
        data: dict，包含指数/个股行情数据
        返回: dict 分析结果
        """
        raise NotImplementedError
