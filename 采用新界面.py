"""把实验室的新界面复制到正式版（bat 跑不动时用这个）。

用法：双击本文件，或在命令行执行
    python 采用新界面.py
"""

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
SRC = ROOT.parent / "ff14-stock-ui-lab" / "static" / "index.html"
DST = ROOT / "static" / "index.html"
LEGACY = ROOT / "static" / "index.legacy.html"


def main() -> int:
    if not SRC.exists():
        print(f"找不到新界面：{SRC}")
        print('请确认 "ff14-stock-ui-lab" 和 "ff14-stock" 在同一个文件夹里。')
        return 1
    if DST.exists():
        shutil.copyfile(DST, LEGACY)
        print(f"旧界面已备份为：{LEGACY.name}")
    shutil.copyfile(SRC, DST)
    print(f"已复制新界面：{SRC}")
    print(f"        → {DST}")
    print()
    print("重启程序，然后在浏览器按 Ctrl+F5 刷新。")
    return 0


if __name__ == "__main__":
    code = main()
    input("\n按回车退出…")
    sys.exit(code)
