# DeepSeek 联网搜索与网页抓取（MaiBot 插件）

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

给 [MaiBot](https://docs.mai-mai.org/) 增加**联网搜索**与**网页抓取**能力的第三方插件：底层直接调用 DeepSeek 官方的 Search / Fetch API，把「搜索」和「读网页」变成麦麦可以自己调用的工具。

- 插件 ID：`blackbee114514.deepseek-search`
- 作者：**BlackBee114514** · [哔哩哔哩个人主页](https://space.bilibili.com/1041748349)
- 仓库：[Blackwindy2333/DeepSeekSearchPlugin](https://github.com/Blackwindy2333/DeepSeekSearchPlugin)

## 功能

| 能力 | 形态 | 说明 |
| --- | --- | --- |
| `search_web` | Tool | LLM 自主联网搜索，支持 `site:` `after:` `before:` `lang:` 等语法（`core_tool`，模型每轮直接可见） |
| `fetch_webpage` | Tool | LLM 抓取给定链接的 Markdown 正文（`core_tool`，模型每轮直接可见） |
| `/search`、`/搜索` | Command | 用户直接搜索，最多展示 N 条 |
| `/fetch`、`/抓取` | Command | 用户直接抓取链接正文 |
| `search`、`get_stats` | API | 供其它插件 RPC 调用（结构化结果 / 运行统计） |
| 首页卡片 | HomeCard | 插件状态与配置入口 |

配套能力：令牌桶限速（默认 4 QPS，低于官方 5 QPS 上限）、TTL + LRU 结果缓存、失败重试（408/429/5xx/网络异常）、密钥脱敏日志、可选滚动文件日志。

## 安装

1. 把本仓库放到 MaiBot 的 `plugins/blackbee114514.deepseek-search/` 目录（目录名可自取，只要包含 `_manifest.json` 与 `plugin.py`）；
2. 在 MaiBot WebUI 的插件页启用插件，等待宿主自动安装依赖 `httpx>=0.27.0`；
3. 打开插件配置，填入 DeepSeek API Key（[申请地址](https://platform.deepseek.com/api_keys)）；
4. 重载插件，在聊天里发送 `/search 今天的科技新闻` 验证。

> 依赖安装默认走宿主的镜像配置；手动安装时请优先使用镜像源，例如
> `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple httpx>=0.27.0`。

## 配置

配置由 SDK 的 `config_model` 生成，WebUI 里按 `[plugin]` `[api]` `[search]` `[fetch]` `[ratelimit]` `[cache]` `[logging]` `[commands]` 分组展示。常用项：

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `api.api_key` | 空 | DeepSeek API Key；为空时工具返回配置提示 |
| `api.base_url` | `https://api.deepseek.com` | 可改为代理网关 |
| `api.proxy` | 空 | 形如 `http://127.0.0.1:7890` |
| `search.default_count` | `8` | 默认返回条数（1–50） |
| `search.need_content` | `false` | 是否默认附带正文 |
| `fetch.max_content_chars` | `4000` | 抓取正文回填上限 |
| `ratelimit.qps` | `4.0` | 令牌桶速率，官方上限 5 |
| `cache.search_ttl_seconds` | `600` | 搜索结果缓存 TTL |
| `logging.file_enabled` | `false` | 是否写 `data/plugins/<id>/logs/dsp.log` |

环境变量可覆盖敏感项：`MAIBOT_DSP_API_KEY`、`MAIBOT_DSP_BASE_URL`、`MAIBOT_DSP_PROXY`、`MAIBOT_DSP_LOG_LEVEL` 等，详见 [`docs/plugin-design.md`](docs/plugin-design.md)。

## 使用

- **让麦麦自己搜**：正常对话即可，模型会在需要实时信息时调用 `search_web`；需要细节时再对最相关的链接调用 `fetch_webpage`。
- **手动搜索**：`/search 关键词`；**手动抓取**：`/fetch https://example.com/article`。
- **检索语法**（直接写进关键词）：

```text
site:example.com 关键词      只在指定站点内搜索
-site:example.com 关键词     排除指定站点
"精确短语" 关键词            精确匹配
lang:zh,en 关键词            限定语言
after:2025-01-01 关键词      限定时间范围
```

## 开发

```powershell
# 安装开发依赖（优先镜像源）
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-dev.txt

# 单元测试（全部离线，不消耗 API 额度）
.\.venv\Scripts\python.exe -m pytest -q

# 本地冒烟（会真实请求 DeepSeek，消耗额度）
$env:MAIBOT_DSP_API_KEY = "sk-..."
.\.venv\Scripts\python.exe scripts/smoke_search.py "DeepSeek 搜索 API"
```

目录结构：

```text
plugin.py                    # 组件声明 + 生命周期（唯一入口）
deepseek_search/             # 纯 Python 实现，可独立测试
  config.py models.py errors.py settings.py
  http.py client.py ratelimit.py cache.py metrics.py
  formatting.py services.py logging_utils.py
tests/                       # pytest，全部离线
scripts/smoke_search.py      # 真实 API 冒烟脚本
docs/                        # 开发指南与设计文档
```

## 文档

- [MaiBot 插件开发 + DeepSeek 搜索 API 完全指南](docs/maibot-plugin-and-deepseek-api-guide.md)
- [插件设计说明](docs/plugin-design.md)

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)。
