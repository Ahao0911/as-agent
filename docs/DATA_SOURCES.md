# 数据源说明

本仓库**不含**任何第三方数据源包或真实凭据。`utils/data_router.py` 实现的 4 级优先级网关按以下顺序调度,断源自动降级。

| 优先级 | 数据源 | 类型 | 关键依赖 | 备注 |
|---:|---|---|---|---|
| 0 | 腾讯行情 | 免费 | `requests` | 默认首选,日内实时报价 |
| 1 | 通达信·mootdx | 免费 | `mootdx` | 日 K / 分时 |
| 2 | akshare | 免费 | `akshare` | 宏观/板块/公告 |
| 3 | 同花顺·iFinD | 收费 | `THS_ACCESS_KEY` (env) + `mcp_config.json` (本地) | 高质量基本面/财务 |
| 3 | 东方财富·妙想 MX | 收费 | `MX_APIKEY` (env) | 新闻/选股/财务 |

## 配置需 KEY 的源

### 东方财富·妙想 MX(可选)

```bash
# Windows
set MX_APIKEY=<your_dongfang_caifu_mx_key>
# macOS / Linux
export MX_APIKEY=<your_dongfang_caifu_mx_key>
```

获取地址:<https://emcreative.eastmoney.com/>

### 同花顺 iFinD(可选)

`iFinD` 走 MCP 协议,需要在 `data-sources-ifind/mcp_config.json` 中填入 token:

```json
{
  "auth_token": "<your_ifind_token>"
}
```

⚠️ `mcp_config.json` 已加入 `.gitignore`,请勿提交到任何仓库。  
获取地址:<https://quant.10jqka.com.cn/>

## 限流与容错

`utils/data_router.py` 内置:

- 限速:每个源单独维护 token 桶,默认 ≤ 10 req/s
- 防封:失败计数 + 指数退避,自动切换备用 User-Agent
- 缓存:日 K 拉取结果落 `utils/kline_cache.py`,重复请求直接返回
- 降级:3 次失败后自动跳过该源,直至下次 24:00 复位

## 数据使用约定

- 所有行情数据仅用于**本地研究**,不得用于任何商业用途
- 新闻与公告数据版权归原数据方所有
- 模拟盘与回测结果**不构成投资建议**(详见根目录 `README.md` 免责声明)
