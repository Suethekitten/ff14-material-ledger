"""为 v0.12.2 准备隔离测试数据（安全线 / S3 / S2 基线）。

用法（必须先设置隔离数据目录）：
    $env:FF14_STOCK_DATA='C:\\Users\\你\\AppData\\Local\\Temp\\ff14-0122-test'
    python tools/seed_v0122_testdata.py --yes

安全约束：
  * 没有 FF14_STOCK_DATA 或指向真实安装目录时拒绝执行；
  * 只清空测试目录里的 2 个测试角色及其库存/流水，不动其他数据；
  * 安全线表不存在时（D1 未完成）只准备库存，并提示先做 D1。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="准备 v0.12.2 隔离测试数据")
    parser.add_argument("--yes", action="store_true", help="确认写入隔离目录")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw = os.environ.get("FF14_STOCK_DATA")
    if not raw:
        print("[拒绝] 先设置 FF14_STOCK_DATA 指向隔离目录")
        return 1
    data_dir = Path(raw).expanduser().resolve()
    if "ff14材料台账" in str(data_dir) or data_dir == (ROOT / "data").resolve():
        print(f"[拒绝] {data_dir} 看起来是真实数据目录")
        return 1
    if not args.yes:
        print(f"[预演] 将写入 {data_dir}；确认后加 --yes")
        return 1
    data_dir.mkdir(parents=True, exist_ok=True)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import app  # noqa: E402 - 必须在设置环境变量之后导入

    app.init_db()
    if not app.INDEX.load():
        print("[失败] 物品表未加载：" + (app.INDEX.error or "未知原因"))
        return 1

    conn = app.connect()
    try:
        chars = {}
        for name in ("测试甲", "测试乙"):
            conn.execute(
                "INSERT OR IGNORE INTO character(owner, account, name, world, created_at)"
                " VALUES(?,?,?,?,?)",
                (app.OWNER, "v0122", name, "红玉海", app.now_iso()),
            )
            row = conn.execute(
                "SELECT id FROM character WHERE owner=? AND name=?",
                (app.OWNER, name),
            ).fetchone()
            chars[name] = int(row["id"])

        for cid in chars.values():
            conn.execute("DELETE FROM stock WHERE owner=? AND character_id=?", (app.OWNER, cid))
            conn.execute("DELETE FROM ledger WHERE owner=? AND character_id=?", (app.OWNER, cid))

        def item(name: str) -> tuple[int, str]:
            item_id, canonical = app.resolve_item(name)
            if not item_id:
                raise SystemExit(f"物品表里找不到：{name}")
            return item_id, canonical

        a = chars["测试甲"]
        cases = [
            # name, location, hq, qty
            ("土之水晶", "背包", 0, 999),      # 安全线 1000 → low，差 1
            ("火之水晶", "背包", 0, 1000),     # 安全线 1000 → ok
            ("冰之水晶", "背包", 0, 600),      # NQ + HQ 合计 1000 → ok
            ("冰之水晶", "背包", 1, 400),
            ("雷之水晶", "未确认", 0, 5000),   # 未确认，不计入 → empty
            ("青铜锭", "背包", 0, 100),        # S3 行内「加」用
        ]
        ts = app.now_iso()
        for name, location, hq, qty in cases:
            item_id, canonical = item(name)
            pending_from = ""
            pending_since = ""
            if location == app.PENDING_LOC:
                pending_from = "背包"
                pending_since = ts
            conn.execute(
                "INSERT INTO stock(owner, item_id, item_name, character_id, location,"
                " hq, qty, source, fc_id, pending_from, pending_since, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (app.OWNER, item_id, canonical, a, location, hq, qty, "manual",
                 0, pending_from, pending_since, ts),
            )

        lines = [
            ("土之水晶", 1000),   # 999 → low，差 1
            ("火之水晶", 1000),   # 1000 → ok
            ("冰之水晶", 1000),   # 600 + 400 → ok
            ("雷之水晶", 100),    # 只有未确认 5000 → empty
            ("风之水晶", 1000),   # 零库存已设线 → empty
            ("水之水晶", 500),    # 零库存已设线 → empty
        ]
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_safety_line'"
        ).fetchone()
        if has_table:
            conn.execute("DELETE FROM stock_safety_line WHERE owner=?", (app.OWNER,))
            for name, min_qty in lines:
                item_id, canonical = item(name)
                conn.execute(
                    "INSERT INTO stock_safety_line(owner, item_id, item_name, min_qty, updated_at)"
                    " VALUES(?,?,?,?,?)",
                    (app.OWNER, item_id, canonical, min_qty, ts),
                )

        # S2：清单同步的冲突夹具（表存在才写；S2 未开工时跳过）
        has_lists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recipe_list'"
        ).fetchone()
        if has_lists:
            item_id, canonical = item("青铜锭")
            payload = {
                "schema": 1,
                "targets": [{"item_id": item_id, "name": canonical, "quantity": 10, "hq": False}],
                "overrides": {},
                "stockOn": True,
                "targetStock": True,
                "priceOn": False,
                "tax": 5,
            }
            import json as _json

            for name, revision in (("__working__", 3), ("冲突测试", 2)):
                conn.execute(
                    "INSERT OR REPLACE INTO recipe_list(owner, name, payload, revision,"
                    " updated_at, updated_by) VALUES(?,?,?,?,?,?)",
                    (app.OWNER, name, _json.dumps(payload, ensure_ascii=False),
                     revision, ts, "seed"),
                )
        conn.commit()
    finally:
        conn.close()

    print(f"[完成] 隔离目录：{data_dir}")
    print("  角色：测试甲 / 测试乙（红玉海）")
    print("  库存：土 999、火 1000、冰 NQ600+HQ400、雷 5000 未确认、青铜锭 100")
    if has_table:
        print("  安全线：土1000(low) 火1000(ok) 冰1000(ok) 雷100(empty) 风1000(empty) 水500(empty)")
        print("  预期 low_count = 4（土/雷/风/水）")
    else:
        print("  安全线表不存在：先完成 D1，再重跑本脚本")
    if has_lists:
        print("  S2 夹具：__working__ rev3、冲突测试 rev2（payload 都是青铜锭 ×10）")
        print("  冲突复现：设备 A 读 rev2 → 设备 B 用 base_revision=2 写成功(rev3) → A 再用 2 写应 409")
    else:
        print("  S2 表不存在：先完成 S2 建表，再重跑本脚本")
    print("  S3：对「青铜锭 100」执行行内「加」，验证 100→N、流水 add、撤销回 100")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
