"""
把 ff14-stock 打成一个干净的分发包。

排除你自己的数据：data/stock.db、data/icon_map.json、pack.py、pack.bat。
保留 data/items.json（约 2MB），测试者首次启动就不用下载 19MB。

用法：python pack.py
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def read_version() -> str:
    """版本号直接从 app.py 读，避免两处手改漏掉一处。"""
    try:
        text = (APP_DIR / "app.py").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


VERSION = read_version()
ZIP_NAME = f"ff14-stock-v{VERSION}.zip"

ALWAYS = [
    "app.py",
    "market.py",
    "datasource.py",
    "npc_shop.py",
    "启动.bat",
    "README.md",
    "测试说明.md",
    "恢复数据库.bat",
    "紧急拆封CSV.bat",
    "导出库存CSV.bat",
]
FOLDERS = ["static", "tools"]
OPTIONAL = ["data/items.json", "data/recipe.json"]
EXCLUDE_NAMES = {"pack.py", "pack.bat", ZIP_NAME}


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # 控制台编码不友好时也不要崩
    except Exception:  # noqa: BLE001
        pass

    out = APP_DIR / ZIP_NAME
    if out.exists():
        out.unlink()

    added: list[str] = []
    missing: list[str] = []

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in ALWAYS:
            path = APP_DIR / name
            if path.exists():
                zf.write(path, arcname=name)
                added.append(name)
            else:
                missing.append(name)

        for folder in FOLDERS:
            base = APP_DIR / folder
            if not base.is_dir():
                missing.append(folder + "/")
                continue
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                if path.name in EXCLUDE_NAMES:
                    continue
                arc = path.relative_to(APP_DIR).as_posix()
                zf.write(path, arcname=arc)
                added.append(arc)

        for name in OPTIONAL:
            path = APP_DIR / name
            if path.exists():
                zf.write(path, arcname=name)
                added.append(name)

    size_mb = out.stat().st_size / 1048576
    print(f"打包完成：{out.name}  ({size_mb:.1f} MB, {len(added)} 个文件)")
    for name in added:
        print("  + " + name)
    if missing:
        print("缺少（已跳过）：" + ", ".join(missing))
        if "data/items.json" in missing:
            print("注意：没有 data/items.json，测试者首次启动会下载约 19MB 物品表")
    print()
    print("下一步：解压到另一个空目录试跑一次，确认能启动，再发给别人。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
