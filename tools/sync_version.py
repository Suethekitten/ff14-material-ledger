"""单一版本源：读取 version.json，同步到 app.py / 页面。

用法：
    python tools/sync_version.py            # 同步
    python tools/sync_version.py --check    # 只检查，不一致退出 1

构建脚本应先跑同步；失败必须非零退出，不能“改了一半”。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "version.json"

TARGETS = [
    (ROOT / "app.py", re.compile(r'^VERSION\s*=\s*"[^"]+"', re.MULTILINE),
     'VERSION = "{v}"'),
    (ROOT / "static" / "index.html", re.compile(r'const BUILD\s*=\s*"[^"]+"'),
     'const BUILD = "{v}"'),
    (ROOT / "static" / "calculator.html",
     re.compile(r'<span class="tag">v[0-9][^<]*</span>'),
     '<span class="tag">v{v}</span>'),
]


def read_version() -> str:
    data = json.loads(VERSION_FILE.read_text(encoding="utf-8-sig"))
    version = str(data.get("version") or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"version.json 里的版本号不合法：{version!r}")
    return version


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    version = read_version()
    changed: list[str] = []
    stale: list[str] = []

    for path, pattern, template in TARGETS:
        if not path.exists():
            print(f"[失败] 找不到 {path.relative_to(ROOT)}")
            return 1
        text = path.read_text(encoding="utf-8")
        wanted = template.format(v=version)
        match = pattern.search(text)
        if not match:
            print(f"[失败] {path.relative_to(ROOT)} 里没找到版本号位置")
            return 1
        if match.group(0) == wanted:
            continue
        if check_only:
            stale.append(str(path.relative_to(ROOT)))
            continue
        new_text = text[:match.start()] + wanted + text[match.end():]
        path.write_text(new_text, encoding="utf-8", newline="")
        changed.append(str(path.relative_to(ROOT)))

    if check_only:
        if stale:
            print("[不一致] " + ", ".join(stale))
            return 1
        print(f"[一致] 全部为 {version}")
        return 0

    if changed:
        print(f"[同步] {version} → " + ", ".join(changed))
    else:
        print(f"[一致] 全部已经是 {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
