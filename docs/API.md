# 接口约定（v0.12.3）

前后端之间唯一的约定。规则见 [AGENTS.md](../AGENTS.md)：**改接口的提交必须同时改这份文档**。

> 初稿由 `app.py` 的 `Handler._do_get` / `do_POST` 路由整理而来。请求参数已逐项对过代码；返回字段已按当前实现补全。

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
| `/api/state` | — | 页面初始化状态；完整字段见下方「`/api/state` 返回字段」 | 页面加载时先调 |
| `/api/characters` | — | `characters: [...]` | |
| `/api/diag` | — | 物品表条数、名称解析样例、图标缓存数 | 排查用 |
| `/api/lan` | — | `enabled`、`addresses`、`listening`、`port` | **本机** |
| `/api/data-dir` | — | `dir`、`db`、`db_exists`、`db_size`、`sidecars` | **本机** |

#### `/api/state` 返回字段

| 字段 | 类型 / 说明 |
|---|---|
| `ok` | `true` |
| `local` | `bool`：请求来自本机为 `true`。**只给前端隐藏按钮用**，服务端权限仍以 `LOCAL_ONLY_GET/POST` 为准 |
| `version` | 当前版本号 |
| `csrf_token` | 写请求要带的 `X-CSRF-Token`；每次启动重新生成 |
| `character_count` | 角色数 |
| `stock_rows` | 正常库存行数（不含未确认） |
| `total_qty` | 正常库存总数量 |
| `has_demo` | 是否有示例数据 |
| `items_loaded` | 已加载物品数 |
| `items_error` | 物品表错误（无为空串） |
| `items_file` | 物品表路径 |
| `stale_days` | 超过多少天标黄 |
| `locations` | 默认位置列表 |
| `data_dir` | 数据目录路径 |
| `db_size` | 数据库文件字节数 |
| `snapshot_count` | 快照数量 |
| `recovered_hint` | 启动时库不存在但手上有快照 |
| `db_created_new` | 本次启动新建了空库 |
| `retainer_max` | `{character_id: 最大雇员编号}`，用于生成雇员下拉 |
| `pending_rows` | 未确认库存行数 |
| `pending_qty` | 未确认库存总数量 |

### 库存与搜索

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/search` | `q`、`account`、`character`、`location`、`hq`、`limit_items`（整数，0 用默认 40，最大 200） | 分层搜索结果；完整字段见下方「`/api/search` 返回字段」 |
| `/api/stock/lookup` | `character_id`、`location`（缺省「背包」）、`item_name`、`hq` | `found`、`stock_id`、`qty`、`hq`、`stack`、`updated_at`、`item{id, name, matched}`；`matched` 为 `exact` / `alias` / `unbound` |
| `/api/stock/export` | — | `count`、`csv`（表头 `material,item_id,character,world,account,location,hq,qty,groups,slots,updated_at`）、`text`（制表符分隔，可直接粘贴） |
| `/api/stock/safety-lines` | — | `scope`、`lines[]`、`low_count`、`unresolved`；每行含 `item_id`、`name`、`min_qty`、`current_qty`、`short_qty`、`status`（`ok` / `low` / `empty`）、`updated_at` |
| `/api/pending` | — | `summary{rows, qty}`、`items[]`；每项含 `item_name`、`item_id`、`hq`、`total`、`rows[]`；`rows` 每行含 `stock_id`、`character_id`、`character`、`world`、`qty`、`location`（固定「未确认」）、`origin`、`updated_at`、`age_text`、`pending_since`、`moved_text` |
| `/api/unmapped` | — | `items`：未收录的材料 |
| `/api/reconcile` | — | `checked`、`issues[]`；每条含 `type`（`ledger_only` 只有流水没有库存 / `stock_only` 只有库存没有流水 / `qty_diff` 两边都有但数量不一致）、`character`、`world`、`item_name`、`item_id`、`location`、`hq_any`、`stock_qty`、`ledger_qty`、`diff` |

#### `/api/search` 返回字段

| 字段 | 说明 |
|---|---|
| `ok` | `true` |
| `query` | 原搜索词 |
| `total_rows` | 命中库存行数（截断前） |
| `total_items` | 命中材料种数（截断前） |
| `truncated` | 材料种数是否被 `limit_items` 截断 |
| `items[]` | 材料，按匹配度、总量排序 |
| `characters[]` | 角色维度命中：`id`、`name`、`world`、`account`、`kinds`（材料种数）、`total`（总数量） |
| `suggestions[]` | 无库存时的联想：`id`、`name`、`icon`、`score`、`stack`、`can_be_hq`、`matched_alias`、`aliases[]` |
| `hint` | 空结果原因：`no_stock`（物品库有、库存没有）/ `no_item`（物品库也没有）/ 空串 |
| `stats` | 同 `/api/state` 的完整字段（**不含 `local`**） |

`items[]` 每项：

| 字段 | 说明 |
|---|---|
| `name`、`item_id`、`icon`、`stack`、`hq` | 材料基本信息；`hq` 为 0/1 |
| `total` | 该材料总量 |
| `holders[]` | 持有行，按数量降序 |
| `aliases` | 备注名，最多 4 个 |
| `purchase_count` | 采购批次数 |
| `avg_cost` | 加权均价 |
| `spent` | `total × avg_cost`，四舍五入 2 位 |
| `match_score` | 匹配分；无搜索词为 0，仅靠角色/位置命中为 300 |
| `matched_alias` | 命中的备注名，没有为空串 |

`holders[]` 每项：`stock_id`、`character_id`、`character`、`account`、`world`、`location`、`qty`、`hq`、`age_days`、`age_text`、`stale`。

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
| `/api/audit` | `from`、`to`、`check`、`character`、`account`；旧参数 `only_checked=1` 等于 `check=1` | `items[]`（`name`、`item_id`、`stack`、`added`、`took`、`adjusted`、`cleared`、`net`、`characters[]`）、`totals`（同五项 + `kinds`）、`ops`、`void_count`；日期格式错误返回 `ok:false` |
| `/api/audit/export` | 同 `/api/audit` | `text`（可直接复制）、`csv`（表头 `material,added,took,adjusted,cleared,net,characters`） |

### 部队

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/fc` | — | `fcs`、`orphan` |
| `/api/fc/stock` | `fc_id` | `items[]`（`item_name`、`item_id`、`hq`、`total`、`name`、`qty`、`rows[]`；`rows` 含 `stock_id`、`character`、`world`、`location`、`qty`、`updated_at`）、`rows`、`qty`、`total_qty` |
| `/api/fc/conflicts` | `fc_id` | `conflicts[]`（`item_name`、`item_id`、`hq`、`entries[]`/`rows[]`、`suggested_qty`/`suggested`、`suggested_from`）、`groups`（同 `conflicts`）、`count` |

### 成本

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/cost/overview` | — | 全量库存成本：`total_value`（库存数量 × 加权均价）、`items_total`、`items_with_cost`、`items_without_cost`、`basis`、`generated_at`。不受搜索 40/200 截断影响 |
| `/api/purchase` | `item_name`（必填）、`hq` | `item_name`、`hq`、`count`、`avg_cost`、`total_qty`、`total_cost`、`rows`。`purchases`、`batches` 与 `rows` 内容相同，是旧名，新代码用 `rows` |

### 配方、NPC、市场

| 接口 | 参数 | 返回要点 |
|---|---|---|
| `/api/recipe/meta` | — | `version`、`recipes`（配方条数）、`items`（有配方的物品数） |
| `/api/npc/meta` | — | `version`、`items`（NPC 商店物品数） |
| `/api/npc/shop` | `ids` 或 `id`（逗号分隔，最多 100） | `version`、`items`：`{item_id: {price, shops[], quest_required[], achievement_required[], state_required[]}}`；没有数据的物品不出现。数据未就绪返回 `ok:false` |
| `/api/market/regions` | — | `regions`：国服四个大区 |
| `/api/market/worlds` | `dc`（缺省「豆豆柴」） | `dc`、`worlds` |
| `/api/market/price` | `ids` 或 `id`（逗号分隔，最多 100）、`region`（缺省用默认服务器） | `prices`、`requested`、`returned`、`truncated`、`cached`；失败时 `ok:false`、`error`、`failed_items`。缓存 5 分钟 |

### 快照

| 接口 | 参数 | 返回要点 | 备注 |
|---|---|---|---|
| `/api/snapshots` | — | `snapshots`、`keep` | |
| `/api/snapshot/preview` | `name` | `name`、`items[]`（`name`、`item_id`、`hq`、`total`、`stack`、`holders[]`）、`ledger_count`；`holders` 含 `character`、`world`、`location`、`qty` | |
| `/api/snapshot/download` | `name` | **二进制文件**，不是 JSON；找不到 404 | **本机** |

## POST 接口

字段名后的 `?` 表示可选。返回要点只列前端常用的，标「快照」的接口写成功后自动打快照。

### 页面心跳（不校验 CSRF）

| 接口 | 请求体 | 返回 |
|---|---|---|
| `/api/page/open` | `sid` | `ok` |
| `/api/page/ping` | `sid`、`visible?`（缺省 true） | `live` |
| `/api/page/closed` | `sid` | `ok` |
| `/api/exit` | — | `ok`；**本机**，非本机、恢复中会拒绝 |

### 库存

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/stock` | `character_id`、`item_name`、`location?`（缺省「背包」）、`qty`、`hq?`、`mode?`（`set` 覆盖 / `add` 累加，缺省 `set`）、`unit_price?`、`allow_unbound?` | 记一笔。返回 `qty_after`、`total`、`unbound`、`unchanged` 等。快照 |
| `/api/stock/update` | `id`、`qty`、`character_id`、`location?`、`item_name`、`hq?` | 改一行（同时改位置即搬货）。快照 |
| `/api/stock/take` | `id`、`qty`、`note?` | 取出。快照 |
| `/api/stock/move` | `id`、`qty`、`character_id`、`location?` | 搬货，源和目标同时成功。快照 |
| `/api/stock/delete` | `id` | 删一行。快照 |
| `/api/stock/batch` | `character_id`、`text`、`location?`、`mode?` | 批量粘贴，最多 500 行 |
| `/api/stock/import/preview` | `text`（CSV 全文） | 只预览不写库：`total`、`will_add`、`will_update`、`skipped`、`new_characters`、`rows`；`ambiguous[]` 每项含 `line`、`character`、`world`、`candidates[]{account, character_id, qty}` |
| `/api/stock/import/apply` | `text`、`mode?`（`overwrite` / `append`，缺省 `overwrite`） | 导入前自动打快照。存在 `ambiguous` 时整批中止：`{ok:false, error, ambiguous}`，不写库 |
| `/api/stock/safety-line` | `item_id`、`min_qty` | 设安全线 |
| `/api/stock/safety-line/delete` | `item_id` | 删安全线 |
| `/api/location/delete` | `character_id`、`location` | 清空一个位置，库存转入未确认 |
| `/api/pending/move` | `stock_id`、`character_id`、`location?` | 未确认库存转移 |
| `/api/pending/drop` | `stock_id`、`note?` | 未确认库存丢弃 |
| `/api/undo` | — | 撤销最近一条库存操作。快照。失败时带 `code`：`none`（没有可撤销/已撤销）、`voided`（最新一条已标异常）、`unsupported_move`（搬移/改位置不能单条撤销）、`unsupported_op`（未确认/部队类操作挡在前面）、`crossed_ops`（该格已被后续操作改动） |

### 角色、部队、名字

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/character` | `account`、`name`、`world` | 新建角色 |
| `/api/clear_demo` | — | 删除示例数据，返回 `removed` |
| `/api/fc` | `name`、`world`、`id?`（有则修改） | 建或改部队 |
| `/api/fc/adopt` | — | 收编无主的部队柜记录 |
| `/api/character/fc` | `character_id`、`fc_id` | 角色加入部队 |
| `/api/fc/merge/apply` | `fc_id`、`items: [{item_name, hq, qty, keep_stock_id?}]` | 归并部队柜，`qty` 为用户确认后的总数，`keep_stock_id` 缺省保留第一行；返回 `merged`（成功条数）、`snapshot`。每行 `qty` 超过上限时跳过 |
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
| `/api/recipe/expand` | `targets: [{item_id, quantity}]`（旧写法：顶层 `item_id`、`quantity`）、`stock?`（使用库存）、`overrides?`、`with_alternatives?`、`target_stock?`（缺省 true，成品本身也抵扣库存） | 展开配方。数量非法（小数、字符串、≤0、>9,999,999）返回 HTTP 400：`{ok:false, invalid:true, error:"数量必须是 1–9999999 的整数"}`。`unresolvable_items[]` 每项含 `item_id`、`name`、`reason` |
| `/api/market/buy-plan` | `region?`、`hq?`、`items: [...]` | 按挂单逐档算买齐成本。返回 `status`（`ok` / `partial` / `failed`）、`failed_items`、`coverage`（`items`、`priced`、`short_items`、`failed_items`、`listings_truncated`、`truncated_items`）、`freshness`（`oldest_upload`、`newest_upload`）、`price_kind`、`tax_note`、`cached`、`error`。全部批次失败时 `ok:false, status:"failed"`；部分失败 `ok:true, status:"partial"`；`items`、`unresolved` 每行带 `name` |

#### `/api/recipe/expand` 请求与返回结构

请求：

- `targets: [{item_id, quantity, hq?}]`（旧写法：顶层 `item_id`、`quantity`）。`hq` 为布尔，缺省 `false`；请求 HQ 但物品没有 HQ 版本时按 NQ 计算，并在返回里保留 `hq_requested:true`。
- `stock?`：使用库存抵扣；`overrides?`：`{ "<item_id>": "buy" | "craft" }` 或 `{ "<item_id>": {source: "buy"|"craft"} }`；`with_alternatives?`：预留；`target_stock?`：缺省 `true`，目标成品本身也抵扣库存。
- 数量非法（小数、字符串、≤0、>9,999,999，或同一目标合并后超限）返回 HTTP 400：`{ok:false, invalid:true, error:"数量必须是 1–9999999 的整数"}`。

成功返回：

```json
{
  "ok": true,
  "data_version": "…",
  "targets": [],
  "intermediate": [],
  "base": [],
  "summary": {},
  "unresolvable_items": []
}
```

`targets[]` 每项：

| 字段 | 说明 |
|---|---|
| `item_id`、`name`、`quantity` | 目标物品与请求数量 |
| `hq` | 实际按 HQ 计算为 `true`；物品不能 HQ 时为 `false` |
| `hq_requested` | 请求里是否要过 HQ |
| `can_be_hq` | `1` 有 HQ / `0` 无 / `-1` 物品表未知 |
| `need` | 合并后的总需求 |
| `from_stock`、`from_stock_nq` | 库存抵扣数；`from_stock_nq` 是 HQ 目标额外提示的 NQ 可用量 |
| `to_craft`、`craft_count`、`per_batch`、`produced`、`surplus` | 需自制数、批数、每批产量、总产出、结余；无配方时为 `0` / `null` |
| `where` | 实际抵扣的库存明细：`character`、`world`、`location`、`qty`、`hq` |

`intermediate[]` 与 `base[]` 每项：

| 字段 | 说明 |
|---|---|
| `item_id`、`name`、`depth` | 物品、层级 |
| `need`、`from_stock` | 总需求、库存抵扣 |
| `source` | `craft` 自制 / `buy` 购买 / `gather` 采集 |
| `to_craft`、`craft_count`、`per_batch`、`produced`、`surplus` | 自制节点字段；非自制为 `0` / `null` |
| `to_buy` | `source=buy` 时需购买数 |
| `to_get` | `base[]` 采集节点还差的数量 |
| `where` | 库存抵扣明细，字段同 `targets[].where` |
| `used_by` | 被哪些父节点使用：`[{item_id, name, qty}]` |
| `alternative` | 当前为 `null`（预留） |

`summary`：`base_kinds`、`intermediate_kinds`、`unknown_cost`（当前 `0`）、`unresolvable`、`max_depth`。

`unresolvable_items[]`：`item_id`、`name`、`reason`（如配方成环、超过递归深度）。

### 本机专用

以下接口其他设备调用一律 403。

| 接口 | 请求体 | 说明 |
|---|---|---|
| `/api/lan` | `enabled` | 开关手机访问 |
| `/api/open-data-dir` | — | 在资源管理器里打开数据目录 |
| `/api/data/backup` | — | 立即备份 |
| `/api/snapshot/restore` | `name` | 恢复快照，恢复前先打「恢复前」快照。返回 `replaced`（是否已替换）、`rolled_back`（失败后是否回滚成功）、`backup`（备份文件名）、`stage`（`not_replaced` / `replaced` / `rolled_back` / `rollback_failed`） |
| `/api/snapshot/delete` | `name` | 删一份快照 |
| `/api/snapshot/cleanup` | — | 清理早期没有摘要的自动快照 |
| `/api/db/preview` | `.db` 文件字节 | 预览要导入的数据库 |
| `/api/db/restore` | `.db` 文件字节 | 整份覆盖。先校验结构，原库自动备份，失败回滚。返回字段同 `/api/snapshot/restore`：`replaced`、`rolled_back`、`backup`、`stage` |
| `/api/recipe/rebuild` | — | 重新下载并构建配方表 |
| `/api/npc/rebuild` | — | 重新下载并构建 NPC 商店表 |

## 变更记录

| 日期 | 版本 | 改了什么 |
|---|---|---|
| 2026-11-15 | 0.12.3 | 初稿，从 `app.py` 路由整理 |
| 2026-11-15 | 0.12.3 | 补齐前端在用字段：买齐报价状态/覆盖/新鲜度、导入歧义、撤销 `code`、恢复四字段与 `stage`、对账 `type`、成本总览字段、配方 `invalid`/`unresolvable_items`、`/api/exit` 标本机 |
| 2026-11-15 | 0.12.3 | `/api/state` 新增 `local`（仅用于前端隐藏本机按钮），补全 `/api/state`、`/api/search` 返回字段 |
| 2026-11-15 | 0.12.4（准备中） | 补完全部剩余「待补」接口：库存预览/导出/安全线/未确认、审计、部队库存与冲突、配方/NPC 元数据、快照预览、部队归并 items；数据源改为固定 commit |
| 2026-11-24 | 0.12.5 | 补 `/api/recipe/expand` 的 `targets[].hq` 请求字段与 `targets/intermediate/base/summary` 返回结构 |
