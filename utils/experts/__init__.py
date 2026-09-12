"""
专家注册表 — 自动发现 experts/ 目录下所有专家
用法:
    from utils.experts import list_experts, get_expert
    experts = list_experts()        # -> ["pattern", "position"]
    pattern = get_expert("pattern")         # -> PatternExpert 实例
"""
import os
import importlib
import inspect

EXPERTS_DIR = os.path.dirname(__file__)


def _discover():
    """扫描目录下的 .py 文件，找出所有 Expert 子类"""
    experts = {}
    for fname in os.listdir(EXPERTS_DIR):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        module_name = fname[:-3]
        try:
            module = importlib.import_module(f"utils.experts.{module_name}")
        except ImportError:
            # 兼容直接运行时路径
            module = importlib.import_module(f"experts.{module_name}")
        except Exception as e:
            print(f"[expert] 加载 {module_name} 失败: {e}")
            continue

        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and hasattr(cls, "name"):
                if cls.__name__ != "Expert" and issubclass(cls, object):
                    # 找到 Expert 子类
                    from utils.experts.base import Expert
                    if issubclass(cls, Expert) and cls is not Expert:
                        try:
                            experts[module_name] = cls()
                        except Exception as e:
                            print(f"[expert] 实例化 {module_name} 失败: {e}")
    return experts


def list_experts():
    """返回所有专家 key 列表"""
    return list(_discover().keys())


def get_expert(name):
    """按名字获取专家实例，不存在返回 None"""
    return _discover().get(name)
