# 策略模块

仓库内信号引擎由 10 个自研 Python 模块组成,分布于 `utils/experts/modules/` 与部分独立工具。

## 选用的策略(正期望)

| 文档标签 | 代码模块 | 触发条件概要 |
|---|---|---|
| S1 超跌反抽型 | `utils/experts/modules/b1.py` | 区间累计跌幅 + 双均线 + 振幅过滤 |
| S2 回踩蓄势型 | `utils/experts/modules/needle20.py` | 20 日回踩 + 关键位支撑确认 |
| S4 关键位突破型 | `utils/experts/modules/brick.py` | 连续平台蓄势 + 关键位放量突破 |

> 详细买入/卖出条件请见对应模块源码,运行时通过同名 `_rules.md` 加载到专家系统 Prompt。

## 备选策略(实验/负期望)

| 文档标签 | 代码模块 | 备注 |
|---|---|---|
| S3 突破延续型 | `utils/experts/modules/brick_xg.py` | 回测年化 -28.2%,已不进入模拟盘 |
| S5 长下影探针型 | `utils/experts/modules/needle20.py`(同模块不同用法) | 探索性质,回测年化 -18.7% |

> 保留代码以供学习与对照,**未自动启用**。

## 通用模块

| 模块 | 职责 |
|---|---|
| `utils/experts/modules/trade_discipline.py` | 仓位上限、单笔敞口、停手规则 |
| `utils/experts/modules/turnover.py` | 量比与流动性过滤 |
| `utils/experts/modules/intraday.py` | 日内确认与破止损退出 |
| `utils/experts/modules/indicator_boundary.py` | 双均线边界与状态判定 |
| `utils/experts/modules/market_value.py` | 活跃市值(资金面)信号 |

## 接入方式

`utils/experts/__init__.py` 启动时自动扫描 `experts/` 目录所有 `.py`,找到 `Expert` 子类后即注册到全局。**新增模块**只需:

1. 在 `utils/experts/modules/` 下创建 `xxx.py`
2. 在同目录创建 `xxx_rules.md` 描述规则文本
3. 类继承 `Expert`,设置 `name` / `description` / `style` 三个属性

无需修改其它代码。
