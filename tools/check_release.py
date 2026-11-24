"""核对发版包：版本号、F3 拖动修复、S1 增量录入、静态资源与数据文件。

用法：
    python tools/check_release.py release/ff14-stock-v0.12.2-win64
    python tools/check_release.py release/ff14-stock-v0.12.2-win64.zip

全部通过退出码 0；有 FAIL 退出码 1；只有警告退出码 0。
不修改任何文件。
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
EXPECT_VERSION = json.loads(
    (PROJECT / "version.json").read_text(encoding="utf-8-sig")
)["version"]


def _entry(folder: str) -> str:
    return folder.replace("\\", "/").strip("/")


class Source:
    def __init__(self, path: Path):
        self.path = path
        self.zip = None
        if path.is_dir():
            self.root = path
            self.names = [str(p.relative_to(path)).replace("\\", "/") for p in path.rglob("*") if p.is_file()]
        elif path.is_file() and path.suffix.lower() == ".zip":
            self.zip = zipfile.ZipFile(path)
            self.names = self.zip.namelist()
            self.root = None
        else:
            raise SystemExit(f"找不到目录或 zip：{path}")

    def find(self, suffix: str) -> str | None:
        suffix = _entry(suffix)
        hits = [n for n in self.names if n == suffix or n.endswith("/" + suffix)]
        return sorted(hits, key=len)[0] if hits else None

    def read_text(self, suffix: str) -> str | None:
        name = self.find(suffix)
        if not name:
            return None
        if self.zip:
            return self.zip.read(name).decode("utf-8", errors="replace")
        return (self.root / name).read_text(encoding="utf-8", errors="replace")

    def mtime(self, suffix: str):
        name = self.find(suffix)
        if not name:
            return None
        if self.zip:
            return self.zip.getinfo(name).date_time
        return (self.root / name).stat().st_mtime


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = Source(Path(argv[1]))
    results: list[tuple[str, str, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(("PASS" if ok else "FAIL", name, detail))

    def warn(name: str, ok: bool, detail: str = "") -> None:
        results.append(("PASS" if ok else "WARN", name, detail))

    index = src.read_text("_internal/static/index.html")
    calc = src.read_text("_internal/static/calculator.html")
    v2 = src.read_text("_internal/static/ff14-stock-v2.html")
    planner = src.read_text("_internal/static/recipe-planner.html")
    css = src.read_text("_internal/static/recipe-test.css")

    check("包内 index.html 存在", index is not None, src.find("_internal/static/index.html") or "缺失")
    check("包内 calculator.html 存在", calc is not None, src.find("_internal/static/calculator.html") or "缺失")
    check("包内 ff14-stock-v2.html 存在", v2 is not None,
          src.find("_internal/static/ff14-stock-v2.html") or "缺失")
    check("包内 recipe-planner.html 存在", planner is not None,
          src.find("_internal/static/recipe-planner.html") or "缺失")
    check("包内 recipe-test.css 存在", css is not None, src.find("_internal/static/recipe-test.css") or "缺失")
    check("包内 ff14-stock.exe 存在", src.find("ff14-stock.exe") is not None,
          src.find("ff14-stock.exe") or "缺少 exe，包不可运行")

    if index:
        import re

        m = re.search(r'const BUILD = "([^"]+)"', index)
        build = m.group(1) if m else "(未找到)"
        check(f"index.html BUILD = {EXPECT_VERSION}", build == EXPECT_VERSION, f"实际 {build}")
        check("S1：指数增量录入切换（qty-mode）", "qty-mode" in index)
        check("S1：批量增量开关（batch-add-mode）", "batch-add-mode" in index)
        check("F5：搜索请求序号（searchSeq）", "searchSeq" in index)

    if calc:
        import re

        check("F3：window 级拖动（dragListeners）", "dragListeners" in calc)
        check("F3：onDragMove/endDrag 已存在", "onDragMove" in calc and "endDrag" in calc)
        check("F4：基础材料折叠（data-toggle-base）", "data-toggle-base" in calc)
        check("F1：单行删除确认（syncRowDeleteButtons）", "syncRowDeleteButtons" in calc)
        m = re.search(r'<span class="tag">([^<]+)</span>', calc)
        tag = m.group(1) if m else "(未找到)"
        check(f"算料页角标 v{EXPECT_VERSION}", tag == f"v{EXPECT_VERSION}", f"实际 {tag}")

    if v2:
        check("V2：写入后刷新（refreshAfterWrite）", "refreshAfterWrite" in v2)
        check("V2：管理抽屉（mgr-drawer）", "mgr-drawer" in v2)

    if planner:
        check("V2 算料：清单快照（snapshotList）", "snapshotList" in planner)
        check("V2 算料：已保存清单下拉（saved-lists）", "saved-lists" in planner)

    if css:
        source_css = (PROJECT / "static" / "recipe-test.css").read_text(encoding="utf-8")
        check("recipe-test.css 与当前源码一致", sha256_text(css) == sha256_text(source_css))

    for rel in ("_internal/static/heartbeat.js", "_internal/static/hotkeys.js",
                "_internal/static/pagenav.css", "_internal/data/recipe.json",
                "_internal/data/npc_shop.json", "_internal/data/items.json"):
        check(f"资源存在：{rel.split('/')[-1]}", src.find(rel) is not None)

    # 时间：包内 static 不应早于源码最后一次 F3 修改
    src_mtime = (PROJECT / "static" / "calculator.html").stat().st_mtime
    import datetime as _dt
    entry_time = src.mtime("_internal/static/calculator.html")
    if isinstance(entry_time, tuple):
        packed = _dt.datetime(*entry_time).timestamp()
    elif entry_time:
        packed = entry_time
    else:
        packed = 0
    warn(
        "包内静态文件不早于源码 F3 修改",
        packed + 1 >= src_mtime,
        f"包 {_dt.datetime.fromtimestamp(packed):%Y-%m-%d %H:%M} / 源码 {_dt.datetime.fromtimestamp(src_mtime):%Y-%m-%d %H:%M}",
    )

    width = max(len(n) for _, n, _ in results)
    fails = 0
    for status, name, detail in results:
        if status == "FAIL":
            fails += 1
        print(f"[{status}] {name.ljust(width)}  {detail}")

    print()
    if fails:
        print(f"结果：{fails} 项 FAIL，这个包不能发。")
        return 1
    print("结果：全部通过（WARN 请人工确认）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
