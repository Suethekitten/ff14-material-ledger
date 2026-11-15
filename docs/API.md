# 接口约定（v0.12.3）

前后端之间唯一的约定。规则见 [AGENTS.md](../AGENTS.md)：**改接口的提交必须同时改这份文档**。

> 初稿由 `app.py` 的 `Handler._do_get` / `do_POST` 路由整理而来。请求参数已逐项对过代码；返回字段大多只写了要点，标「待补」的由后端在下次碰到这个接口时补全。

## 通用规则

### 地址与访问

- 服务地址 `http://127.0.0.1:<端口>/`，默认 8765，被占用时自动往后找，实际端口见 `GET /api/lan` 的 `port`
- 非本机访问要先开启「手机访问」，否则一律 403 `手机访问未开启`
- 标 **本机** 的接口只允许本机调用，其他设备返回 403 `该功能只允许在本机使用`（见 `app.py` 的 `LOCAL_ONLY_GET` / `LOCAL_ONLY_POST`）
- 校验 Host / Origin，不通过返回 403 `请求来源不受信任，已拒绝`

### 写请求（POST）

- 请求体 `Content-Type: application/json`，上限 2MB；否则 415 / 413
  - 例外：`/api/db/preview`、`/api/db/restore` 的请求体是 `.db` 文件原始字节，上限 100MB
- 请求头带 `X-CSRF-Token`，值取自 `GET /api/state` 的 `csrf_token`。每次服务重启都会变，失效返回 403，前端应提示刷新页面
  - 例外：`/api/page/*` 不校验 token
- 正在恢复数据库时，除 `/api/page/ping`、`/api/page/closed` 外都返回 503

### 返回格式

- 一律 JSON：成功 `{"ok": true, ...}`，失败 `{"ok": false, "error": "给用户看的中文说明"}`
- **业务失败多数仍是 HTTP 200**，前端必须看 `ok` 字段，不能只看状态码
- 参数校验失败时带 `"invalid": true`，HTTP 400
- 未知接口 404；服务内部异常 500，`error` 以「处理失败：」开头，详情写进 `data\error.log`
- 写成功后，这些接口会自动打快照：`/api/stock`、`/api/stock/take`、`/api/stock/move`、`/api/stock/update`、`/api/stock/delete`、`/api/undo`、`/api/alias`、`/api/alias/delete`、`/api/cost`（返回 `unchanged: true` 时不打）

### 数量

- 数量一律是整数总数，组和格只在前端显示时换算
- 上限见后端 `MAX_ITEM_QTY`，超限或小数、负数返回 `invalid: true`

## GET 接口

查询参数都是字符串，缺省为空。

### 基础

| 接口 | 参数 | 返回要点 | 备注 |
|---|---|---|---|
| `/api/state` | — | `csrf_token` 及页面初始化所需状态（待补） | 页面加载时先调 |
| `/api/characters` | — | `characters: [...]` | |
| `/api/diag` | — | 物品表条数、名称解析样例、图标缓存数 | 排查用 |
| `/api/lan` | — | `enabled`、`addresses`、`listening`、`port` | **本机** |
| `/api/data-dir` | — | `dir`、`db`、`db_exists`、`db_size`、`sidecars` | **本机** |

### 库存与搜索

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/search` | `q`、`account`、`character`、`location`、`hq`、`limit_items`（整数） | 搜索结果（待补） |
| `/api/stock/lookup` | `character_id`、`location`（缺省「背包」）、`item_name`、`hq` | 这一格现有数量（待补） |
| `/api/stock/export` | — | 导出用的 CSV 数据（待补） |
| `/api/stock/safety-lines` | — | 安全线列表（待补） |
| `/api/pending` | — | 未确认库存（待补） |
| `/api/unmapped` | — | `items`：未收录的材料 |
| `/api/reconcile` | — | 对账差异（待补） |

### 物品与名字

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/item_search` | `q` | `items`：最多 8 条联想 |
| `/api/item_search_en` | `q` | `items`：XIVAPI 英文名结果 |
| `/api/item_by_id` | `id` | 物品信息；缺 `id` 返回 400 |
| `/api/icons` | `ids`（逗号分隔） | `icons` |
| `/api/aliases` | — | `aliases: [{alias, item_id, item_name, created_at}]` |

### 流水与审计

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/ledger` | `limit`（缺省 20） | `rows` |
| `/api/audit` | `from`、`to`、`check`、`character`、`account`；旧参数 `only_checked=1` 等于 `check=1` | 审计汇总（待补） |
| `/api/audit/export` | 同 `/api/audit` | 审计导出（待补） |

### 部队

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/fc` | — | `fcs`、`orphan` |
| `/api/fc/stock` | `fc_id` | 部队柜库存（待补） |
| `/api/fc/conflicts` | `fc_id` | 多人重复记录的冲突（待补） |

### 成本

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/cost/overview` | — | 全部库存的成本汇总（待补） |
| `/api/purchase` | `item_name`（必填）、`hq` | `item_name`、`hq`、`count`、`avg_cost`、`total_qty`、`total_cost`、`rows`。`purchases`、`batches` 与 `rows` 内容相同，是旧名，新代码用 `rows` |

### 配方、NPC、市场

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/recipe/meta` | — | 配方表状态（待补） |
| `/api/npc/meta` | — | NPC 商店表状态（待补） |
| `/api/npc/shop` | `ids` 或 `id`（逗号分隔） | NPC 金币价（待补） |
| `/api/market/regions` | — | `regions`：国服四个大区 |
| `/api/market/worlds` | `dc`（缺省「豆豆柴」） | `dc`、`worlds` |
| `/api/market/price` | `ids` 或 `id`（逗号分隔，最多 100）、`region`（缺省用默认服务器） | Universalis 价格，缓存 5 分钟（待补） |

### 快照

| 接口 | 参数 | 返回要点 | 备注 |
|---|---|---|---|
| `/api/snapshots` | — | `snapshots`、`keep` | |
| `/api/snapshot/preview` | `name` | 只读查看这份快照（待补） | |
| `/api/snapshot/download` | `name` | **二进制文件**，不是 JSON；找不到 404 | **本机** |

## POST 接口

字段名后的 `?` 表示可选。返回要点只列前端常用的，标「快照」的接口写成功后自动打快照。

### 页面心跳（不校验 CSRF）

| 接口 | 请求体 | 返回 |
|---|---|---|
| `/api/page/open` | `sid` | `ok` |
| `/api/page/ping` | `sid`、`visible?`（缺省 true） | `live` |
| `/api/page/closed` | `sid` | `ok` |
| `/api/exit` | — | `ok`；非本机、恢复中会拒绝 |

### 库存

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/stock` | `character_id`、`item_name`、`location?`（缺省「背包」）、`qty`、`hq?`、`mode?`（`set` 覆盖 / `add` 累加，缺省 `set`）、`unit_price?`、`allow_unbound?` | 记一笔。返回 `qty_after`、`total`、`unbound`、`unchanged` 等。快照 |
| `/api/stock/update` | `id`、`qty`、`character_id`、`location?`、`item_name`、`hq?` | 改一行（同时改位置即搬货）。快照 |
| `/api/stock/take` | `id`、`qty`、`note?` | 取出。快照 |
| `/api/stock/move` | `id`、`qty`、`character_id`、`location?` | 搬货，源和目标同时成功。快照 |
| `/api/stock/delete` | `id` | 删一行。快照 |
| `/api/stock/batch` | `character_id`、`text`、`location?`、`mode?` | 批量粘贴，最多 500 行 |
| `/api/stock/import/preview` | `text`（CSV 全文） | 只预览不写库：`total`、`will_add`、`will_update`、`skipped`、`new_characters`、`rows` |
| `/api/stock/import/apply` | `text`、`mode?`（`overwrite` / `append`，缺省 `overwrite`） | 导入前自动打快照 |
| `/api/stock/safety-line` | `item_id`、`min_qty` | 设安全线 |
| `/api/stock/safety-line/delete` | `item_id` | 删安全线 |
| `/api/location/delete` | `character_id`、`location` | 清空一个位置，库存转入未确认 |
| `/api/pending/move` | `stock_id`、`character_id`、`location?` | 未确认库存转移 |
| `/api/pending/drop` | `stock_id`、`note?` | 未确认库存丢弃 |
| `/api/undo` | — | 撤销最近一条库存操作。快照 |

### 角色、部队、名字

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/character` | `account`、`name`、`world` | 新建角色 |
| `/api/clear_demo` | — | 删除示例数据，返回 `removed` |
| `/api/fc` | `name`、`world`、`id?`（有则修改） | 建或改部队 |
| `/api/fc/adopt` | — | 收编无主的部队柜记录 |
| `/api/character/fc` | `character_id`、`fc_id` | 角色加入部队 |
| `/api/fc/merge/apply` | `fc_id`、`items: [...]` | 归并部队柜，数量由用户确认（items 结构待补） |
| `/api/fc/delete` | `id`、`mode?`（`void` 库存作废并留流水 / `empty` 已搬空直接解散；缺省及其他值都按 `void`） | 解散部队 |
| `/api/alias` | `alias`、`item_id` | 加备注名。快照 |
| `/api/alias/delete` | `alias` | 删备注名。快照 |

### 流水

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/ledger/note` | `id`、`note` | 改流水备注 |
| `/api/ledger/void` | `id`、`void`、`note?` | 标记 / 取消异常 |
| `/api/ledger/check` | `id`、`checked` | 勾选已核对 |
| `/api/ledger/check_batch` | `ids: [...]`、`checked` | 批量勾选 |

### 成本

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/cost` | `item_name`、`hq`、`unit_price` | 给没有批次的旧库存设基线单价。快照 |
| `/api/purchase/delete` | `id` | 删一个采购批次 |

### 快照

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/snapshot/create` | `note?` | 手动打快照。手机也能调用；恢复、删除快照见下面「本机专用」 |

### 配方与市场

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/recipe/expand` | `targets: [{item_id, quantity}]`（旧写法：顶层 `item_id`、`quantity`）、`stock?`（使用库存）、`overrides?`、`with_alternatives?`、`target_stock?`（缺省 true，成品本身也抵扣库存） | 展开配方（返回结构待补） |
| `/api/market/buy-plan` | `region?`、`hq?`、`items: [...]` | 按挂单逐档算买齐成本；返回的 `items`、`unresolved` 每行带 `name`（items 结构待补） |

### 本机专用

以下接口其他设备调用一律 403。

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/lan` | `enabled` | 开关手机访问 |
| `/api/open-data-dir` | — | 在资源管理器里打开数据目录 |
| `/api/data/backup` | — | 立即备份 |
| `/api/snapshot/restore` | `name` | 恢复快照，恢复前先打「恢复前」快照 |
| `/api/snapshot/delete` | `name` | 删一份快照 |
| `/api/snapshot/cleanup` | — | 清理早期没有摘要的自动快照 |
| `/api/db/preview` | `.db` 文件字节 | 预览要导入的数据库 |
| `/api/db/restore` | `.db` 文件字节 | 整份覆盖。先校验结构，原库自动备份，失败回滚 |
| `/api/recipe/rebuild` | — | 重新下载并构建配方表 |
| `/api/npc/rebuild` | — | 重新下载并构建 NPC 商店表 |

## 变更记录

| 日期 | 版本 | 改了什么 |
|---|---|---|
| 2026-11-15 | 0.12.3 | 初稿，从 `app.py` 路由整理 |
