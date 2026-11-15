"""SaintCoinach CSV 下载与解析。

只依赖标准库；不导入 app.py，避免循环导入。
"""

from __future__ import annotations

import csv
import io
import urllib.request

# 数据源固定到 commit；需要更新游戏数据时改这里并重跑构建/测试。
SAINT_GH = "thewakingsands/ffxiv-datamining-cn"
SAINT_REF = "ae29be8b21a32329e3356709422427540bf7e364"


def saint_mirrors(name: str) -> tuple:
    """同一份 SaintCoinach CSV 的多个下载地址，按顺序尝试。

    jsdelivr 有几个平行域名（fastly / gcore / testingcf），
    某条线上取不到某个文件时换一条通常就好了；raw 作为最后兜底。
    """
    gh = SAINT_GH
    ref = SAINT_REF
    return (
        f"https://fastly.jsdelivr.net/gh/{gh}@{ref}/{name}",
        f"https://gcore.jsdelivr.net/gh/{gh}@{ref}/{name}",
        f"https://testingcf.jsdelivr.net/gh/{gh}@{ref}/{name}",
        f"https://cdn.jsdelivr.net/gh/{gh}@{ref}/{name}",
        f"https://raw.githubusercontent.com/{gh}/{ref}/{name}",
    )


def read_saint_csv(url: str, timeout: int = 300) -> tuple[list[str], list[list[str]]]:
    """读 SaintCoinach 导出的 CSV：跳过键行、列名行(#开头)、类型行。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        text = resp.read().decode("utf-8-sig", errors="replace")

    reader = csv.reader(io.StringIO(text))
    header: list[str] = []
    for row in reader:
        if not row:
            continue
        if row[0].startswith("#"):
            header = [c.lstrip("#").strip() for c in row]
            break
    if not header:
        return [], []

    type_words = {"int32", "int16", "int8", "uint32", "uint16", "uint64",
                  "byte", "sbyte", "bool", "single", "str"}
    body: list[list[str]] = []
    for row in reader:
        if not row:
            continue
        cell = row[0].strip()
        if cell in type_words:
            continue
        # key 可能是 262144 或 262144.0（GilShopItem 就带小数），都要接受
        if not cell.lstrip("-").replace(".", "", 1).isdigit():
            continue
        body.append(row)
    return header, body


def read_saint_csv_multi(name: str, timeout: int = 300):
    """依次尝试多个镜像，返回第一个成功的 (header, rows)。"""
    errors = []
    for url in saint_mirrors(name):
        try:
            header, rows = read_saint_csv(url, timeout)
        except Exception as exc:  # noqa: BLE001 - 换下一个镜像
            errors.append(f"{url.rsplit('/', 1)[-1]}@{url.split('/')[2]}：{exc}")
            continue
        if header:
            return header, rows, ""
    return [], [], "；".join(errors)
