"""
紧急拆封：把 stock.db 里的内容全部导成 CSV，作为升级前的兜底备份。

用途：
    · 0.7.3 → 新版本升级前，先导一份
    · 万一新版本读不了旧库，还能用「导入库存 CSV」把数据倒回去

用法：
    python tools\\emergency_export.py "D:\\path\\stock.db"
    或把 stock.db 拖到「紧急拆封CSV.bat」上

输出：与源文件同目录的 emergency-<名字>-<时间>/ 文件夹
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

TABLES = ("character", "stock", "ledger", "alias", "purchase")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def write_csv(path: Path, header: list[str], rows: list[list]) -> int:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        src = Path("data/stock.db")

    if not src.exists():
        print(f"找不到数据库：{src}")
        print("用法：python tools\\emergency_export.py \"D:\\path\\stock.db\"")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = src.with_name(f"emergency-{src.stem}-{stamp}")
    out.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        print(f"打不开数据库：{exc}")
        return 1
    conn.row_factory = sqlite3.Row

    found = {t for t in TABLES if table_exists(conn, t)}
    totals: dict[str, int] = {}

    try:
        # 1) 库存 —— 这份是「导入库存 CSV」能直接吃回去的格式
        if "stock" in found:
            rows = conn.execute(
                """
                SELECT s.item_name, s.item_id, s.location, s.hq, s.qty, s.updated_at,
                       c.name AS character, c.world, c.account
                FROM stock s
                LEFT JOIN character c ON c.id = s.character_id
                ORDER BY s.item_name, c.name
                """
            ).fetchall()
            data = [
                [
                    r["item_name"], r["item_id"] or 0, r["character"] or "",
                    r["world"] or "", r["account"] or "", r["location"] or "背包",
                    "HQ" if r["hq"] else "NQ", r["qty"] or 0, r["updated_at"] or "",
                ]
                for r in rows
            ]
            totals["库存"] = write_csv(
                out / "01-库存（可直接导入）.csv",
                ["material", "item_id", "character", "world", "account",
                 "location", "hq", "qty", "updated_at"],
                data,
            )
        else:
            totals["库存"] = 0

        # 2) 角色
        if "character" in found:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(character)")}
            fc = "fc_id" if "fc_id" in cols else "0 AS fc_id"
            rows = conn.execute(
                f"SELECT account, name, world, {fc} FROM character ORDER BY account, name"
            ).fetchall()
            totals["角色"] = write_csv(
                out / "02-角色.csv",
                ["account", "name", "world", "fc_id"],
                [[r["account"], r["name"], r["world"], r["fc_id"]] for r in rows],
            )
        else:
            totals["角色"] = 0

        # 3) 备注名（名字映射）
        if "alias" in found:
            rows = conn.execute(
                "SELECT alias, item_name, item_id FROM alias ORDER BY alias"
            ).fetchall()
            totals["备注名"] = write_csv(
                out / "03-备注名.csv",
                ["alias", "item_name", "item_id"],
                [[r["alias"], r["item_name"], r["item_id"]] for r in rows],
            )
        else:
            totals["备注名"] = 0

        # 4) 流水（备查，不能直接导入）
        if "ledger" in found:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger)")}

            def pick(name: str, default: str = "''") -> str:
                expr = f"l.{name}" if name in cols else default
                return f"{expr} AS {name}"

            select = ", ".join(
                [
                    "l.ts", "l.action", "l.item_name", "l.location",
                    "l.delta", "l.qty_after",
                    pick("hq", "0"), pick("void", "0"),
                    pick("checked", "0"), pick("note"),
                    "c.name AS character",
                ]
            )
            rows = conn.execute(
                f"SELECT {select} FROM ledger l"
                " LEFT JOIN character c ON c.id = l.character_id"
                " ORDER BY l.ts"
            ).fetchall()
            totals["流水"] = write_csv(
                out / "04-流水（仅备查）.csv",
                ["ts", "action", "item_name", "location", "delta", "qty_after",
                 "hq", "void", "checked", "note", "character"],
                [
                    [
                        r["ts"], r["action"], r["item_name"], r["location"],
                        r["delta"], r["qty_after"], r["hq"], r["void"],
                        r["checked"], r["note"], r["character"] or "",
                    ]
                    for r in rows
                ],
            )
        else:
            totals["流水"] = 0

        # 5) 买入批次（新版本才有）
        if "purchase" in found:
            rows = conn.execute(
                "SELECT item_name, hq, qty, unit_price, location, created_at"
                " FROM purchase ORDER BY created_at"
            ).fetchall()
            totals["成本批次"] = write_csv(
                out / "05-成本批次（仅备查）.csv",
                ["item_name", "hq", "qty", "unit_price", "location", "created_at"],
                [
                    [r["item_name"], r["hq"], r["qty"], r["unit_price"],
                     r["location"], r["created_at"]]
                    for r in rows
                ],
            )
        else:
            totals["成本批次"] = 0
    except sqlite3.Error as exc:
        conn.close()
        print(f"读取失败：{exc}")
        print("这个文件可能不是本程序的数据库，或者已经损坏。")
        return 1
    finally:
        conn.close()

    print("拆封完成：")
    for name, count in totals.items():
        print(f"  {name}：{count} 条")
    print()
    print(f"输出目录：{out}")
    print()
    print("说明：")
    print("  01-库存（可直接导入）.csv  → 新版本里用「导入库存 CSV」就能倒回去")
    print("  02~05 是备查用的，不能直接导入，出问题时给我看")
    return 0


if __name__ == "__main__":
    sys.exit(main())
