"""
交易助手 — 专家系统入口
用法:
    python main.py list                    # 列出所有专家
    python main.py prompt pattern              # 生成图形买点体系的提示词
    python main.py prompt all              # 生成所有专家的提示词
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from utils.experts import list_experts, get_expert


def main():
    args = sys.argv[1:]
    if not args or args[0] == "list":
        experts = list_experts()
        print(f"已发现 {len(experts)} 位专家:")
        for key in experts:
            e = get_expert(key)
            print(f"  - {key} ({e.name}) [{e.style}] {e.description}")
        return

    if args[0] == "prompt":
        target = args[1] if len(args) > 1 else "all"
        if target == "all":
            for key in list_experts():
                e = get_expert(key)
                print(f"\n{'='*50}\n[{e.name}] {e.style}\n{'='*50}")
                print(e.get_prompt())
        else:
            e = get_expert(target)
            if e:
                print(e.get_prompt())
            else:
                print(f"未找到专家: {target}")
        return

    print("用法: main.py [list|prompt <name|all>]")


if __name__ == "__main__":
    main()
