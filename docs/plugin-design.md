# 插件设计说明

本文说明 `blackbee114514.deepseek-search` 的分层、关键取舍与扩展方式。面向两类读者：想改这个插件的人，以及想照着写另一个「外部 API 工具型插件」的人。

## 1. 目标与非目标

**目标**

- 让 MaiBot 能自主联网：LLM 通过 Tool 搜索、再按需抓取网页正文；
- 让用户能手动触发：`/search`、`/fetch` 两条命令；
- 让其它插件能复用：两个 `public` API；
- 稳定、可观测、可热更新：限速、缓存、重试、脱敏日志、配置热加载；
- 全部业务逻辑与 MaiBot 解耦，离线可测。

**非目标**

- 不做搜索引擎聚合/自建索引（只转发 DeepSeek 官方 Search / Fetch）；
- 不做网页渲染、JS 执行、登录态抓取（Fetch API 只返回可读正文）；
- 不实现聊天平台适配、LLM Provider、回复扩展、自定义 WebUI 页；
- 不代替用户申请 Key、不做计费统计之外的额度管理。

## 2. 分层与模块职责

```text
┌──────────────────────────────────────────────┐
│ plugin.py（唯一入口）                        │
│  @Tool search_web / fetch_webpage            │
│  @Command search / fetch                     │
│  @API search / get_stats                     │
│  @HomeCard status                            │
│  生命周期 on_load / on_unload / on_config_update │
└───────────────┬──────────────────────────────┘
                │ 只做参数兜底与返回值适配
┌───────────────▼──────────────────────────────┐
│ services.DeepSeekSearchService（业务编排）    │
│  缓存读写 → 限速等待 → 调客户端 → 失败归一    │
│  格式化 →SearchOutcome/FetchOutcome          │
└───┬──────────┬──────────┬──────────┬─────────┘
    │          │          │          │
┌───▼───┐ ┌────▼────┐ ┌───▼───┐ ┌────▼──────┐
│cache  │ │ratelimit│ │client │ │formatting │
│TTL+LRU│ │TokenBucket│ │httpx  │ │文本裁剪   │
└───────┘ └─────────┘ └───┬───┘ └───────────┘
                          │
              ┌───────────▼───────────┐
              │ http（URL 校验）      │
              │ models（响应模型）    │
              │ errors（错误分类）    │
              │ settings（环境变量）  │
              │ metrics（计数器）     │
              │ logging_utils（脱敏） │
              └───────────────────────┘
```

| 模块 | 职责 | 不做什么 |
| --- | --- | --- |
| `config.py` | Pydantic 配置模型、默认值、范围校验、WebUI 分组元数据 | 不读文件、不读环境变量 |
| `settings.py` | 进程级环境变量（`MAIBOT_DSP_*`）与密钥脱敏 | 不写配置 |
| `errors.py` | 14 个异常类 + HTTP 状态映射 + 用户文案 | 不打日志、不发请求 |
| `models.py` | `SearchItem`/`SearchResult`/`FetchResult` 宽容解析 | 不做网络与格式化 |
| `http.py` | URL 白名单校验（协议、长度、内网地址、用户名密码） | 不调 API |
| `client.py` | httpx 客户端、Bearer 鉴权、超时、重试、解析、错误归一 | 不管缓存、限速、文本裁剪 |
| `ratelimit.py` | 异步令牌桶（`qps`/`burst`，可热更新） | 不做全局进程间协调 |
| `cache.py` | TTL + LRU，线程安全，统计命中率 | 不落盘 |
| `pages.py` | 网页正文按 URL 落盘（`sha256` 文件名、原子写、条数 + 字节双限额、TTL 清理） | 不做网络请求、不格式化文本 |
| `metrics.py` | 请求数、失败数、缓存、重试、耗时、最后错误 | 不导出 Prometheus |
| `formatting.py` | 搜索结果/抓取结果/错误/状态行的中文排版 | 不发消息 |
| `services.py` | 串联缓存→限速→客户端→格式化，返回 `*Outcome` | 不依赖 `ctx`、不读 `config.toml` |
| `logging_utils.py` | 脱敏过滤器、子日志器、滚动文件句柄 | 不决定业务日志内容 |
| `plugin.py` | 组件声明、生命周期、日志接线、依赖注入 | 不写业务逻辑 |

## 3. 一次搜索的数据流

```text
LLM/用户
  │ search_web(query, count?, need_content?, ori_question?) / "/search 关键词"
  ▼
plugin.search_web / plugin.cmd_search
  │ 参数兜底（None、字符串数字、"false" 等）
  ▼
service.search()
  │ 1. 未配置 Key → 立即返回 ConfigurationError 文案（无网络）
  │ 2. 生成 rid（8 位），记录 search.request
  │ 3. 查缓存（键为 query/count/need_content/ori_question/localization 五元组）
  │ 4. 未命中 → 令牌桶 acquire() → 记录 ratelimit.wait
  │ 5. client.search() → POST /v1/tools/search
  │ 6. 失败：按异常类型分别记 search.failed，返回可读文案
  │ 7. 成功：格式化中文正文 → 写缓存 → 记 search.success
  ▼
SearchOutcome.to_dict() → {"success", "content", ...} → Host 取 content 喂给 LLM
```

抓取同理，键为 `url`，正文按 `fetch.max_content_chars` 截断并标记 `truncated`；完整正文同时落盘（`pages.*`），返回窗口末尾会附带 `offset`，再次调用即可续读。

## 4. 关键设计取舍

### 4.1 为什么不用相对导入

MaiBot 的 Runner 用 `importlib.util.spec_from_file_location(..., submodule_search_locations=[插件目录])` 加载 `plugin.py`，理论上支持相对导入；但 `plugin.py` 同时要被 pytest、冒烟脚本直接 import，且不同插件的同名子包（例如都叫 `dsp`）会在 `sys.modules` 撞车。因此：

- `plugin.py` 顶部把自身目录插入 `sys.path`，再用绝对名 `deepseek_search.*` 导入；
- 子包名带插件前缀语义（`deepseek_search`），避免与其它插件冲突；
- 所有业务模块零 `ctx` 依赖，`pytest` 直接导入即可。

### 4.2 为什么 Components 只在 `plugin.py`

SDK 的 `collect_components(instance)` 遍历插件实例的属性找装饰器元数据，因此组件必须是**插件类的方法**。把 Tool/Command/API 方法留在 `plugin.py`、把逻辑下沉到 `services.py`，既满足 SDK 契约，又让业务可独立测试。组件方法只做三件事：类型兜底、调 service、转字典。

### 4.3 为什么只声明 `capabilities: []`

本插件只用三样东西：`ctx.logger`（无需能力）、`ctx.paths`（无需能力）、自己的 `httpx`（SDK 不提供 HTTP 能力）。manifest 里不申请任何能力代理，插件在只读、受限的宿主环境下也能工作。日志能力在 SDK 2.14 中已移除说明，声明反而会校验失败。

### 4.4 重试策略

| 情形 | 处理 |
| --- | --- |
| 400/401/402/403/404/413 | 不重试，直接归一到对应异常（重试也不会变好） |
| 408 / 429 / 5xx / 网络异常 | 指数退避 + 抖动，最多 `api.max_retries` 次；429 优先使用响应头 `Retry-After` |
| 403 与 404 的差异 | 403 = 目标站点拒绝抓取（换来源）；404 = 抓不到可用正文（换链接） |

退避上限 `retry_max_backoff_seconds` 防止长尾挂死；每次重试都记 `search.retry`，包含 `attempt`、`status`、`backoff_ms`。

### 4.5 限速与缓存

- 官方限速是**每账号** Search 5 QPS、Fetch 5 QPS，且与 API Key 无关。插件默认 `qps=4.0`、`burst=2`，给同账号的其它客户端留余量；高并发部署可调到 5 以下任意值，不建议超过 5。
- 缓存是进程内的 TTL + LRU：搜索默认 10 分钟、抓取默认 30 分钟。相同查询在 TTL 内直接复用，命令与 Tool 共享同一份缓存；配置热更新时只 `resize` 不重建，避免瞬间击穿。
- 缓存键包含所有影响结果的参数，`need_content` 不同不会错误复用。
- 另有一层**磁盘缓存**（`pages.py`）：抓取成功的完整正文按 URL 落盘到 `data/plugins/<id>/pages/`，文件名取 `sha256(url)` 前 32 位；内存未命中时先查磁盘，命中就完全跳过 Fetch API，因此长网页可以在进程重启后继续按 `offset` 读取。条数与总字节双限额淘汰，TTL 默认 24 小时，所有磁盘异常只降级为「不缓存」，不影响抓取主流程。

### 4.6 配置热更新

`on_config_update(scope, config_data, version)` 只响应 `scope == "self"`：

1. 先用新配置重配日志（级别、文件开关）；
2. 调 `service.apply_config(config)`：就地更新限速参数与缓存容量；若 `base_url`/`api_key`/`proxy`/超时发生变化，则关闭旧客户端，下次请求按新参数懒创建；
3. 返回 `changes` 摘要写日志，失败不抛给 Host（避免整个插件被禁用）。

### 4.7 日志与脱敏

- 基日志器 `self.ctx.logger`（宿主命名为 `plugin.<plugin_id>`），子日志器 `getChild("dsp.client")` 等；
- 所有日志经过 `SecretMaskFilter`：`sk-` 开头的 Key → `sk-***abcd`，`Bearer <token>` → `Bearer ***tail`；
- 请求级日志带 `rid`，搜索/抓取/缓存/限速/重试各自成事件，便于 grep 串起一次调用；
- 文件日志默认关闭；打开后写 `ctx.paths.data_dir/logs/dsp.log`（`RotatingFileHandler`，UTF-8，2MB × 3 备份），卸载时移除 handler 并关闭句柄，避免重载泄漏。

### 4.8 返回给 LLM 的文本预算

搜索结果的 `snippet` 100–200 字、`summary` 600–800 字、`content` 可能很长。插件按配置分别截断（默认 200/500/1200 字），并且默认只展开 `max_results_in_text=8` 条，避免一次工具调用吃掉上下文。抓取正文默认 4000 字，超出加「已截断」说明。

## 5. 扩展指南

**加一个 Tool**：在 `plugin.py` 加 `@Tool(...)` 方法，参数用 `ToolParameterInfo` 声明，内部调 `service` 或新模块；同步在 `tests/test_manifest.py` 的组件计数与 `README.md` 组件表里登记。

**加一个命令**：加 `@Command(name, pattern=r"^/(?:xx)(?:\s+(?P<arg>.+))?$")`，处理器返回三元组；注意 pattern 对整条消息匹配，建议带 `^...$`。

**接入新上游 API**：在 `client.py` 加方法并复用 `_request()`，在 `services.py` 加编排方法，新增 dataclass Outcome；错误走 `errors.py` 映射，不要在各层抛裸 `ValueError`。

**换缓存后端（Redis 等）**：只替换 `cache.TTLCache` 接口（`get/set/clear/resize/stats`），`services.py` 不需要改。

**新增日志字段**：在 `logging_utils.py` 保持脱敏过滤器为唯一出口；结构化字段建议用 `key=value` 形式，方便 grep 与后续接入日志平台。

## 6. 测试策略

- **全部离线**：网络层用 `httpx.MockTransport` 注入预置响应；业务层用 `FakeClient` 记录调用参数。
- `tests/conftest.py` 提供 `configure()`（用 SDK 默认配置 + 覆盖项构造合法配置）、`sample_search_result()`、`FakeClient`、临时 `ctx`（`paths.data_dir → tmp_path`）。
- 覆盖点：manifest 契约、配置默认值/边界、URL 校验、请求体与鉴权头、错误状态映射、重试与 `Retry-After`、解析失败、缓存命中/失效、限速、脱敏、文件日志、热更新、命令文案、API 结构。
- 不覆盖：真实 DeepSeek 网络行为、宿主 IPC、WebUI 渲染——这三类交给 `scripts/smoke_search.py` 与真机联调。

## 7. 已知限制与路线

| 限制 | 影响 | 后续方向 |
| --- | --- | --- |
| 缓存为进程内 | 多 Runner 实例不共享 | 抽 `CacheBackend` 接口接 Redis |
| 令牌桶不跨进程 | 多实例总和可能超过 5 QPS | 接入宿主级限速或 Redis 令牌桶 |
| ~~Fetch 正文不落盘~~ **已支持** | 大页面正文落盘到 `data/pages`，`fetch_webpage` 支持 `offset` 分段续读，返回末尾附 `next_offset` 提示 | 后续：首页卡片展示缓存占用、按站点配置 TTL |
| 无结果排序策略 | 完全信任官方 `rerank_score` | 增加去重与站点多样性重排 |
| 首页卡片为静态内容 | 不展示实时计数 | 宿主支持动态内容后接 `get_stats` |
| 热重载会关闭进行中的连接 | 极端并发下正在执行的请求报一次网络错误 | 引用计数或延迟关闭旧客户端 |

## 8. 日志事件约定（速查）

| 事件 | 级别 | 字段 |
| --- | --- | --- |
| `search.request` | INFO | `rid` `query` `count` `need_content` |
| `search.cache_hit` | DEBUG | `rid` `results` `age_s` |
| `search.retry` | WARNING | `rid` `attempt` `status` `backoff_ms` |
| `search.success` | INFO | `rid` `results` `cache` `elapsed_ms` |
| `search.failed` | ERROR | `rid` `error` `status` |
| `fetch.request` | INFO | `rid` `url` `max_chars` `offset` |
| `fetch.disk_hit` | INFO | `rid` `chars` |
| `fetch.window` | INFO | `rid` `offset` `total` `returned` |
| `fetch.success` | INFO | `rid` `chars` `elapsed_ms` |
| `fetch.failed` | ERROR | `rid` `url` `error` |
| `config.reload` | INFO | `scope` `version` `changes` |

磁盘缓存模块（`pages.py`）另有 `pages.hit` / `pages.save` / `pages.expired` / `pages.evict` / `pages.corrupt` / `pages.save_failed` 事件，字段含 `url` `path` `chars` `age` 等，便于按关键字检索缓存生命周期。
