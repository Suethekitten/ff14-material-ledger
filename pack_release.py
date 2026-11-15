"""
打发布包：把 dist\\ff14-stock 复制一份（去掉 data 目录），压成带版本号的 zip。

用法：双击「发布打包.bat」，或 python pack_release.py
先决条件：已经用 build_exe.bat 构建过，dist\\ff14-stock\\ff14-stock.exe 存在。
"""

from __future__ import annotations

import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "dist" / "ff14-stock"
OUT_DIR = ROOT / "release"


def read_version() -> str:
    try:
        text = (ROOT / "app.py").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def main() -> int:
    if not SRC.is_dir() or not (SRC / "ff14-stock.exe").exists():
        print("找不到 dist\\ff14-stock\\ff14-stock.exe")
        print("先双击 build_exe.bat 构建一次，再回来打包。")
        return 1

    version = read_version()
    name = f"ff14-stock-v{version}-win64"
    OUT_DIR.mkdir(exist_ok=True)
    zip_path = OUT_DIR / f"{name}.zip"
    stage = OUT_DIR / name

    if stage.exists():
        shutil.rmtree(stage)
    if zip_path.exists():
        zip_path.unlink()

    def ignore_top_level(dir_path: str, names: list[str]) -> set[str]:
        """只忽略顶层 data 目录（那是本地账本），_internal 里的 data 要保留。"""
        if Path(dir_path).resolve() == SRC.resolve():
            return {n for n in names if n in ("data", "error.log", "console.log")}
        return set()

    shutil.copytree(SRC, stage, ignore=ignore_top_level)

    # 附上说明文档与恢复工具，方便对方直接用
    for doc in ("README.md", "测试说明.md", "恢复数据.bat",
                "批量录入示例.txt", "FF14材料台账-新手引导.pptx"):
        src_doc = ROOT / doc
        if src_doc.exists():
            shutil.copyfile(src_doc, stage / doc)

    removed = 0
    data_dir = stage / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
        removed += 1

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in stage.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(OUT_DIR).as_posix())

    size_mb = zip_path.stat().st_size / 1048576
    shutil.rmtree(stage, ignore_errors=True)

    print(f"发布包已生成：{zip_path}")
    print(f"版本：v{version}　大小：{size_mb:.1f} MB")
    print(f"已排除本地数据目录：{'是' if removed else '（原本就没有）'}")
    print()
    print("发给别人后：解压 → 双击 ff14-stock.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
