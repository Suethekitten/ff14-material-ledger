# 给 AI agent 的约定

本项目目前由一名维护者（Suethekitten）指挥两个 agent 开发：**后端 agent** 和 **前端 agent**。下面每条每次都要遵守。

## 分工

- **后端**只改 `app.py`、`market.py`、`tools/`（含测试）
- **前端**只改 `static/`
- 要改对方的文件时，先停下来写清楚需要什么，交给维护者转达，不要自己动手

每个文件归谁：

| 文件 | 负责方 |
|---|---|
| `app.py`、`market.py`、`tools/`（含 `tools/tests/`） | 后端 |
| 根目录所有 `.bat`（启动、恢复、导出、诊断、清空账本等） | 后端 |
| 打包和发布：`pack.py`、`pack.bat`、`pack_release.py`、`build_exe.bat`、`ff14-stock.spec`、`发布打包.bat` | 后端 |
| `version.json`（只经 `tools/sync_version.py` 同步） | 后端 |
| `采用新界面.py`、`采用新界面.bat`（脚本本身归后端；运行它会覆盖 `static/index.html`，由维护者决定何时运行） | 后端 |
| `docs/API.md` | 后端写，前端只读；前端发现不一致时提给维护者 |
| `static/` | 前端 |
| `CHANGELOG.md` | 双方都可在当前版本那一节追加自己的条目，不改对方写的 |
| `README.md`、`SECURITY.md`、`LICENSE`、`AGENTS.md`、`.gitignore`、`.gitattributes` | 维护者 |
| `logo/`、`icon.ico`、`功能项总表-*.md`、`测试说明.md`、`批量录入示例.txt` | 维护者 |

表里没列的新文件，先问维护者归谁再建。

## 接口约定

- 前后端之间唯一的约定是 [docs/API.md](docs/API.md)，以它为准，不以聊天记录或 `交接文档/` 为准
- **改接口先改文档**：新增、删除、改名接口或字段时，后端在**同一个提交**里同时改代码和 `docs/API.md`
- 只加字段不删字段。要废弃一个字段，先在文档里标「废弃」，等前端不再使用后再删
- 前端用到文档里没有的字段，视为 bug：要么让后端补文档，要么前端别用
- 危险操作（覆盖数据库、删快照、开关手机访问等）必须在 `LOCAL_ONLY_GET` / `LOCAL_ONLY_POST` 里，并在文档里标「本机」

## 提交前

- 后端改动后在隔离目录跑测试，全部通过才提交：

  ```powershell
  $env:FF14_STOCK_DATA="$env:TEMP\ff14-test"
  python -m unittest tools.tests.test_core
  ```

- 不要用真实的 `data\` 目录跑测试或自检，`--selftest` 会写演示数据
- 版本号只改 `version.json`，再用 `tools/sync_version.py` 同步
- 用户能感觉到的变化，写进 `CHANGELOG.md` 当前版本那一节

## 不要做

- 不要 `git push`，不要改远端；推送由维护者决定
- 不要提交 `.gitignore` 里排除的东西（数据库、备份、打包产物、`交接文档/` 等）
- 不要加第三方依赖：后端只用 Python 标准库，前端不引入构建工具或框架
- 不要顺手重构或加没要求的功能
