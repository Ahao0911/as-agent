import os

def run():
    rule_path = os.path.join(os.path.dirname(__file__), "rules.md")
    with open(rule_path, "r", encoding="utf-8") as f:
        rules = f.read()
    
    prompt = f"""
接下来所有的分析和建议，必须严格遵循以下图形买点体系的规则，不能违背体系给出建议。
如果规则和行情有冲突，以规则为准。所有操作建议都要符合体系的仓位、买卖点、板块优先级要求。

【图形买点体系完整规则】
{rules}
    """
    return prompt
