# Logo 素材（ff14-stock）

## 成品（已接入项目）

| 文件 | 用途 |
|---|---|
| `ff14-stock-logo.svg` | 主版，512 视窗，透明底。用于大尺寸、更新长图、群头像 |
| `logo-dark.svg` | 主版暗色（封面 #35485A） |
| `logo-16.svg` | 小尺寸版，16 视窗：羽毛压成一根粗斜杠，26px 以下用这个 |
| `logo-16-dark.svg` | 小尺寸版暗色 |
| `favicon.ico` | 16/32/48 三帧，带 1px 米色描边 |
| `icon.ico` | 16/24/32/48/64/128/256 七帧，给 exe / 任务栏 |
| `png/` | 各尺寸 PNG 导出（透明底） |

项目里的落点：

- `static/logo-16.svg`、`static/logo-16-dark.svg`、`static/logo-dark.svg`、`static/logo.svg`、`static/favicon.ico`
- 根目录 `icon.ico`（PyInstaller 用）
- `static/index.html`：`<link rel="icon">` + 标题栏 `<picture>`
- `ff14-stock.spec` / `build_exe.bat`：`icon='icon.ico'` / `--icon "icon.ico"`

## 设计

深蓝账本封面 + 旧金书脊 + 羊皮纸页边 + 金色羽毛笔。透明底，无文字。

色值：`#202C38` 封面 / `#35485A` 暗色封面 / `#C5A46C` 旧金 / `#F1E8D5` 羊皮纸 / `#16202A` 后封面。

## 测试结论（真渲染，逐尺寸）

- 128px：书 + 羽毛清楚
- 64px：羽毛仍可辨
- 48px：羽毛糊成一条金线
- 32px：只剩书 + 金书脊 + 一道斜光 → 用 `logo-16.svg`
- 16px：主版不可用；小尺寸版清楚（书 + 金脊 + 斜杠）
- 深色任务栏：深蓝封面会和背景粘住，ICO 帧内烘焙了 1px 米色描边
- 暗色标题栏：封面 #35485A 比 #2B3A47 略清楚，仍偏含蓄

## 复现

```powershell
python logo\_build_assets.py   # 需要先把 Edge 渲染结果放在 %TEMP%\logotest2
```

渲染脚本 `_make_svg_test.py` / `_make_test_sheet.py` 依赖 Edge headless（逐尺寸渲染后裁切）。
