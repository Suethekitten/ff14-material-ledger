"""
把任意 stock.db 导出成「导入库存 CSV」能用的格式。

用法：
    python tools\\db_to_csv.py "D:\\path\\stock.db"
    或直接把 .db 拖到 导出库存CSV.bat 上

输出：与源文件同目录的 <文件名>-stock-export.csv
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/stock.db")
    if not src.exists():
        print(f"找不到文件：{src}")
        return 1

    out = src.with_name(f"{src.stem}-stock-export.csv")

    try:
        conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        print(f"打不开数据库：{exc}")
        return 1
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            """
            SELECT s.item_id, s.item_name, s.location, s.hq, s.qty, s.updated_at,
                   c.name AS character, c.world, c.account
            FROM stock s
            JOIN character c ON c.id = s.character_id
            ORDER BY s.item_name, c.name
            """
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"读取失败（可能不是本程序的数据库）：{exc}")
        conn.close()
        return 1
    conn.close()

    # 不写 BOM：0.7.3 的导入逻辑会把 BOM 算进第一列表头，导致认不出 material 列
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["material", "item_id", "character", "world", "account",
             "location", "hq", "qty", "updated_at"]
        )
        for r in rows:
            writer.writerow([
                r["item_name"],
                r["item_id"] or 0,
                r["character"] or "",
                r["world"] or "",
                r["account"] or "",
                r["location"] or "背包",
                "HQ" if r["hq"] else "NQ",
                r["qty"] or 0,
                r["updated_at"] or "",
            ])

    print(f"已导出 {len(rows)} 行")
    print(f"文件：{out}")
    print()
    print("下一步：把这份 CSV 发给对方，用「导入库存 CSV」导入（覆盖或追加均可）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
