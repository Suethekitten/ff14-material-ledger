"""核心数据路径测试：隔离临时目录，不碰真实账本。

运行：
    python -m unittest discover -s tools/tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import http.client
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = Path(tempfile.mkdtemp(prefix="ff14-unittest-"))
os.environ["FF14_STOCK_DATA"] = str(TEST_DIR)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import datasource  # noqa: E402
import market  # noqa: E402


class CoreDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()
        if not app.INDEX.load():
            raise unittest.SkipTest("物品表未加载")
        cls.conn = app.connect()
        row = cls.conn.execute(
            "INSERT INTO character(owner, account, name, world, created_at)"
            " VALUES(?,?,?,?,?)",
            (app.OWNER, "test", "测试角色", "红玉海", app.now_iso()),
        )
        cls.char_id = int(row.lastrowid)
        cls.conn.commit()
        cls.conn.close()

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        c = app.connect()
        try:
            for table in ("stock", "ledger", "purchase", "alias", "stock_safety_line",
                          "recipe_list"):
                c.execute(f"DELETE FROM {table} WHERE owner = ?", (app.OWNER,))
            c.commit()
        finally:
            c.close()
        app.load_aliases()

    def add(self, name="青铜锭", qty=100, loc="背包", hq=0, **kw):
        return app.record_stock(self.char_id, name, loc, qty, hq, **kw)

    def stock_id(self) -> int:
        c = app.connect()
        try:
            return int(c.execute("SELECT id FROM stock LIMIT 1").fetchone()["id"])
        finally:
            c.close()

    def _list_payload(self, qty=10, hq=False):
        item_id, _ = app.resolve_item("青铜锭")
        return {
            "schema": 1,
            "targets": [{
                "item_id": item_id, "name": "青铜锭", "quantity": qty,
                "hq": hq, "can_hq": True,
            }],
            "overrides": {},
            "stockOn": True,
            "targetStock": True,
            "priceOn": False,
        }

    # ---- 库存 / 并发 ----
    def test_add_then_take_keeps_total(self):
        self.assertTrue(self.add(qty=100)["ok"])
        res = app.record_take(self.stock_id(), 40)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["remaining"], 60)
        self.assertEqual(res["total"], 60)

    def test_take_more_than_stock_is_rejected(self):
        self.add(qty=10)
        res = app.record_take(self.stock_id(), 11)
        self.assertFalse(res["ok"])
        self.assertIn("只有", res["error"])

    def test_move_merges_target(self):
        self.add(qty=100)
        first = app.move_stock(self.stock_id(), 20, self.char_id, "雇员1")
        self.assertTrue(first["ok"], first)
        second = app.move_stock(self.stock_id(), 10, self.char_id, "雇员1")
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["target_qty"], 30)
        self.assertTrue(second["merged"])

    def test_undo_removes_purchase_batch(self):
        self.add(qty=10, unit_price=50)
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM purchase").fetchone()[0], 1)
        finally:
            c.close()
        res = app.undo_last()
        self.assertTrue(res["ok"], res)
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM stock").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM purchase").fetchone()[0], 0)
        finally:
            c.close()

    # ---- 安全线 ----
    def test_safety_line_boundaries(self):
        item_id, _ = app.resolve_item("青铜锭")
        self.add(qty=999)
        res = app.safety_line_set(item_id, 1000)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["line"]["status"], "low")
        self.assertEqual(res["line"]["short_qty"], 1)
        res = app.safety_line_set(item_id, 999)
        self.assertEqual(res["line"]["status"], "ok")

    def test_safety_line_zero_stock_still_listed(self):
        item_id, _ = app.resolve_item("水之水晶")
        self.assertTrue(app.safety_line_set(item_id, 500)["ok"])
        payload = app.safety_lines()
        line = next(x for x in payload["lines"] if x["item_id"] == item_id)
        self.assertEqual(line["status"], "empty")
        self.assertEqual(line["current_qty"], 0)

    def test_safety_line_rejects_zero(self):
        item_id, _ = app.resolve_item("青铜锭")
        res = app.safety_line_set(item_id, 0)
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("invalid"))

    # ---- 别名 ----
    def test_alias_merges_stock_and_purchase(self):
        alias = "旧名测试"
        self.add(name=alias, qty=10, allow_unbound=True, unit_price=10)
        self.add(qty=5)
        res = app.set_alias(alias, app.resolve_item("青铜锭")[0])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["merged"], 1)
        c = app.connect()
        try:
            rows = c.execute(
                "SELECT item_name, SUM(qty) AS q FROM stock GROUP BY item_name"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["q"], 15)
            purchase = c.execute("SELECT item_name FROM purchase").fetchall()
            self.assertEqual(len(purchase), 1)
            self.assertEqual(purchase[0]["item_name"], "青铜锭")
        finally:
            c.close()

    # ---- 快照 ----
    def test_snapshot_restore_roundtrip(self):
        self.add(qty=100)
        snap = app.create_snapshot("manual", "unit-test")
        self.add(qty=50)  # set 覆盖为 50
        res = app.restore_snapshot(snap["name"])
        self.assertTrue(res["ok"], res)
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT SUM(qty) FROM stock").fetchone()[0], 100)
        finally:
            c.close()

    # ---- 市场 ----
    def test_buy_plan_flags_truncation(self):
        old = market._fetch_listings
        market._fetch_listings = lambda region, ids, hq: {
            5057: {
                "rows": [{"price": 10, "qty": 5, "world": "银泪湖"}],
                "count": 200,
                "upload": 1793540212996,
            }
        }
        try:
            res = market.buy_plan("豆豆柴", False, [{"item_id": 5057, "quantity": 10}])
        finally:
            market._fetch_listings = old
        self.assertEqual(res["coverage"]["listings_truncated"], 1)
        self.assertEqual(res["items"][0]["short"], 5)
        self.assertTrue(res["freshness"]["oldest_upload"])

    def test_selftest_refuses_without_isolated_env(self):
        old = os.environ.pop("FF14_STOCK_DATA", None)
        try:
            self.assertFalse(app._selftest_allowed())
        finally:
            if old is not None:
                os.environ["FF14_STOCK_DATA"] = old

    # ---- CSV / 审计 ----
    def test_csv_cell_neutralizes_formulas(self):
        self.assertEqual(app._csv_cell("=1+1"), "'=1+1")
        self.assertEqual(app._csv_cell("@cmd"), "'@cmd")
        self.assertEqual(app._csv_cell("-5"), "-5")
        self.assertEqual(app._csv_cell("5,000"), '"5,000"')

    def test_audit_totals_match_stock_and_void_count(self):
        self.add(qty=100)
        self.add(qty=20)
        app.record_take(self.stock_id(), 30)
        app.undo_last()
        summary = app.audit_summary()
        self.assertTrue(summary["ok"], summary)
        c = app.connect()
        try:
            stock_total = c.execute("SELECT COALESCE(SUM(qty),0) FROM stock").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(summary["totals"]["net"], stock_total)
        self.assertEqual(summary["void_count"], 1)

    # ---- 导入 / 成本 / 撤销搬移 ----
    def test_import_append_accumulates_same_cell(self):
        csv = ("material,character,world,account,location,hq,qty\n"
               "青铜锭,导入角色,红玉海,acc,背包,NQ,10\n"
               "青铜锭,导入角色,红玉海,acc,背包,NQ,5\n")
        res = app.import_apply(csv, "append")
        self.assertTrue(res["ok"], res)
        c = app.connect()
        try:
            total = c.execute(
                "SELECT COALESCE(SUM(qty),0) FROM stock WHERE item_name='青铜锭'"
            ).fetchone()[0]
            chars = c.execute(
                "SELECT COUNT(*) FROM character WHERE name='导入角色'"
            ).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(total, 15)
        self.assertEqual(chars, 1)

    def test_import_preview_counts_duplicate_cell_as_update(self):
        csv = ("material,character,world,account,location,hq,qty\n"
               "青铜锭,预览角色,红玉海,acc,背包,NQ,10\n"
               "青铜锭,预览角色,红玉海,acc,背包,NQ,5\n"
               "黄铜锭,预览角色,红玉海,acc,背包,NQ,7\n")
        prep = app.prepare_import(csv)
        self.assertTrue(prep["ok"], prep)
        self.assertEqual(prep["will_add"], 2)
        self.assertEqual(prep["will_update"], 1)

    def test_set_cost_refuses_when_batches_exist(self):
        self.add(qty=10, unit_price=50)
        res = app.set_cost("青铜锭", 0, 99)
        self.assertFalse(res["ok"])
        self.assertIn("采购批次", res["error"])

    def test_undo_move_is_refused(self):
        self.add(qty=100)
        move = app.move_stock(self.stock_id(), 20, self.char_id, "雇员1")
        self.assertTrue(move["ok"], move)
        res = app.undo_last()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "unsupported_move")
        self.assertIn("搬移", res["error"])

    def test_undo_position_edit_is_refused_even_after_note_edit(self):
        self.add(qty=100)
        sid = self.stock_id()
        edit = app.update_stock(sid, 100, self.char_id, "雇员1")
        self.assertTrue(edit["ok"], edit)
        c = app.connect()
        try:
            latest = c.execute(
                "SELECT id FROM ledger WHERE owner = ? ORDER BY id DESC LIMIT 1",
                (app.OWNER,),
            ).fetchone()["id"]
        finally:
            c.close()
        app.set_ledger_note(latest, "随便改备注")
        res = app.undo_last()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "unsupported_move")

    def test_undo_does_not_cross_pending_ops(self):
        self.add(qty=50)
        self.add(qty=100)  # set 覆盖成 100
        pending = app.delete_location_stock(self.char_id, "背包")
        self.assertTrue(pending["ok"], pending)
        res = app.undo_last()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "unsupported_op")
        c = app.connect()
        try:
            total = c.execute("SELECT COALESCE(SUM(qty),0) FROM stock").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(total, 100)

    def test_import_ambiguous_account_is_rejected_batch(self):
        c = app.connect()
        try:
            for acc, qty in (("A", 10), ("B", 20)):
                row = c.execute(
                    "INSERT INTO character(owner, account, name, world, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (app.OWNER, acc, "双胞胎", "红玉海", app.now_iso()),
                )
                cid = int(row.lastrowid)
                c.execute(
                    "INSERT INTO stock(owner, item_id, item_name, character_id,"
                    " location, hq, qty, source, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (app.OWNER, app.resolve_item("青铜锭")[0], "青铜锭", cid,
                     "背包", 0, qty, "manual", app.now_iso()),
                )
            c.commit()
        finally:
            c.close()
        csv = ("material,character,world,account,location,hq,qty\n"
               "青铜锭,双胞胎,红玉海,,背包,NQ,10\n")
        prep = app.prepare_import(csv)
        self.assertEqual(len(prep["ambiguous"]), 1)
        res = app.import_apply(csv, "append")
        self.assertFalse(res["ok"])
        self.assertIn("歧义", res["error"])
        c = app.connect()
        try:
            total = c.execute(
                "SELECT COALESCE(SUM(qty),0) FROM stock WHERE item_name='青铜锭'"
            ).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(total, 30)

    def test_import_with_account_keeps_two_characters(self):
        csv = ("material,character,world,account,location,hq,qty\n"
               "青铜锭,双胞胎,红玉海,A,背包,NQ,10\n"
               "青铜锭,双胞胎,红玉海,B,背包,NQ,20\n")
        res = app.import_apply(csv, "overwrite")
        self.assertTrue(res["ok"], res)
        c = app.connect()
        try:
            rows = c.execute(
                "SELECT qty FROM stock WHERE item_name='青铜锭' ORDER BY qty"
            ).fetchall()
        finally:
            c.close()
        self.assertEqual([r["qty"] for r in rows], [10, 20])

    def test_fc_stock_does_not_follow_character_after_switch(self):
        c = app.connect()
        try:
            ids = {}
            for name in ("A队", "B队"):
                row = c.execute(
                    "INSERT INTO fc(owner, name, world, created_at) VALUES(?,?,?,?)",
                    (app.OWNER, name, "红玉海", app.now_iso()),
                )
                ids[name] = int(row.lastrowid)
            c.execute("UPDATE character SET fc_id = ? WHERE id = ?",
                      (ids["A队"], self.char_id))
            c.commit()
        finally:
            c.close()
        self.add(qty=90, loc="部队柜")
        self.assertTrue(app.set_character_fc(self.char_id, ids["B队"])["ok"])
        try:
            a_total = sum(x["total"] for x in app.fc_stock(ids["A队"])["items"])
            b_total = sum(x["total"] for x in app.fc_stock(ids["B队"])["items"])
            self.assertEqual(a_total, 90)
            self.assertEqual(b_total, 0)
        finally:
            app.set_character_fc(self.char_id, 0)

    def test_reconcile_reports_missing_stock_row(self):
        self.add(qty=100)
        c = app.connect()
        try:
            c.execute("DELETE FROM stock WHERE owner = ?", (app.OWNER,))
            c.commit()
        finally:
            c.close()
        res = app.reconcile()
        self.assertTrue(any(i["type"] == "ledger_only" for i in res["issues"]), res)

    # ---- 成本总览 / 数量边界 ----
    def test_cost_overview_sums_all_stock(self):
        self.add(qty=100)
        self.assertEqual(app.set_cost("青铜锭", 0, 5)["ok"], True)
        res = app.cost_overview()
        self.assertTrue(res["ok"])
        self.assertEqual(res["items_total"], 1)
        self.assertEqual(res["items_with_cost"], 1)
        self.assertAlmostEqual(res["total_value"], 500.0)

    def test_qty_rejects_float_zero_and_negative(self):
        for bad in (2.5, "12", -1, None, True):
            res = self.add(qty=bad)
            self.assertFalse(res["ok"], bad)
            self.assertTrue(res.get("invalid"), bad)
        res = self.add(qty=0, mode="add")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("invalid"))
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM stock").fetchone()[0], 0)
        finally:
            c.close()

    def test_set_zero_clears_row(self):
        self.add(qty=100)
        res = self.add(qty=0)
        self.assertTrue(res["ok"], res)
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT qty FROM stock").fetchone()[0], 0)
        finally:
            c.close()

    def test_qty_rejects_above_max_and_merge_overflow(self):
        res = self.add(qty=app.MAX_ITEM_QTY + 1)
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("invalid"))
        self.assertTrue(self.add(qty=app.MAX_ITEM_QTY, mode="add")["ok"])
        res = self.add(qty=2, mode="add")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("invalid"))
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT qty FROM stock").fetchone()[0],
                             app.MAX_ITEM_QTY)
        finally:
            c.close()

    def test_take_move_update_reject_non_integer_qty(self):
        self.add(qty=100)
        sid = self.stock_id()
        for res in (
            app.record_take(sid, 2.5),
            app.move_stock(sid, 2.5, self.char_id, "雇员1"),
            app.update_stock(sid, 2.5, self.char_id, "背包"),
        ):
            self.assertFalse(res["ok"])
            self.assertTrue(res.get("invalid"))
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT qty FROM stock").fetchone()[0], 100)
        finally:
            c.close()

    def test_expand_recipe_rejects_float_target(self):
        saved = app.RECIPES
        app.RECIPES = {"1": [{"row": 1, "amount": 1, "level": 1, "ingredients": []}]}
        try:
            res = app.expand_recipe([{"item_id": 1, "quantity": 2.5}])
        finally:
            app.RECIPES = saved
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("invalid"))

    def test_import_skips_row_above_max(self):
        csv = ("material,character,world,account,location,hq,qty\n"
               f"青铜锭,测试角色,红玉海,test,背包,NQ,{app.MAX_ITEM_QTY + 1}\n")
        prep = app.prepare_import(csv)
        self.assertEqual(len(prep["skipped"]), 1)
        self.assertIn("上限", prep["skipped"][0]["reason"])

    # ---- 位置 / 批量 / 单实例 / 备注 ----
    def test_record_stock_rejects_unknown_new_location(self):
        res = self.add(qty=10, loc="月球基地")
        self.assertFalse(res["ok"])
        self.assertIn("位置不合法", res["error"])

    def test_record_stock_keeps_legacy_custom_location(self):
        c = app.connect()
        try:
            c.execute(
                "INSERT INTO stock(owner, item_id, item_name, character_id,"
                " location, hq, qty, source, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (app.OWNER, app.resolve_item("青铜锭")[0], "青铜锭", self.char_id,
                 "旧位置", 0, 3, "manual", app.now_iso()),
            )
            c.commit()
        finally:
            c.close()
        res = self.add(qty=8, loc="旧位置")
        self.assertTrue(res["ok"], res)

    def test_batch_accepts_employee_6_to_10(self):
        res = app._record_batch_line(self.char_id, "青铜锭 5 雇员7", "背包")
        self.assertTrue(res["ok"], res)
        c = app.connect()
        try:
            row = c.execute(
                "SELECT location, qty FROM stock WHERE owner = ?", (app.OWNER,)
            ).fetchone()
        finally:
            c.close()
        self.assertEqual((row["location"], row["qty"]), ("雇员7", 5))

    @unittest.skipUnless(os.name == "nt", "Windows 文件锁")
    def test_second_instance_is_rejected(self):
        import msvcrt
        app._release_data_lock()
        path = app.DATA_DIR / "stock.lock"
        path.touch(exist_ok=True)
        fh = open(path, "r+b")
        try:
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"x")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            self.assertIn("占用", app._acquire_data_lock())
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            fh.close()
            app._release_data_lock()

    def test_void_with_empty_note_keeps_original(self):
        self.add(qty=10)
        c = app.connect()
        try:
            lid = int(c.execute(
                "SELECT id FROM ledger WHERE owner = ? ORDER BY id DESC LIMIT 1",
                (app.OWNER,),
            ).fetchone()["id"])
        finally:
            c.close()
        app.set_ledger_note(lid, "原备注")
        self.assertTrue(app.set_ledger_void(lid, True, "")["ok"])
        c = app.connect()
        try:
            row = c.execute("SELECT note, void FROM ledger WHERE id = ?", (lid,)).fetchone()
        finally:
            c.close()
        self.assertEqual((row["note"], row["void"]), ("原备注", 1))

    # ---- 发布前复核 V01–V04 ----
    def test_legacy_move_undo_blocked_after_note_edit(self):
        self.add(qty=100)
        self.assertTrue(app.move_stock(self.stock_id(), 30, self.char_id, "雇员1")["ok"])
        c = app.connect()
        try:
            c.execute("ALTER TABLE ledger DROP COLUMN op_group")
            c.execute("UPDATE ledger SET note = '普通备注'")
            c.commit()
        finally:
            c.close()
        app.init_db()  # 旧结构升级
        res = app.undo_last()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "unsupported_move")
        c = app.connect()
        try:
            total = c.execute("SELECT COALESCE(SUM(qty),0) FROM stock").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(total, 100)

    def test_legacy_pair_without_group_is_refused_by_same_second_guard(self):
        self.add(qty=100)
        self.assertTrue(app.move_stock(self.stock_id(), 30, self.char_id, "雇员1")["ok"])
        c = app.connect()
        try:
            c.execute("UPDATE ledger SET op_group = '', note = '普通备注'")
            c.commit()
        finally:
            c.close()
        res = app.undo_last()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "unsupported_move")

    def test_restore_migrates_missing_stock_updated_at(self):
        self.add(qty=100)
        snap = app.create_snapshot("manual", "v02-updated-at")
        self.add(qty=150)
        bad = TEST_DIR / "candidate-no-updated-at.db"
        shutil.copyfile(app.SNAP_DIR / snap["name"], bad)
        c = sqlite3.connect(bad)
        try:
            c.execute("ALTER TABLE stock DROP COLUMN updated_at")
            c.commit()
        finally:
            c.close()
        info = app.inspect_db_file(bad)
        self.assertTrue(info["ok"], info)
        res = app._install_db(bad, info)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["replaced"])
        c = app.connect()
        try:
            row = c.execute("SELECT qty, updated_at FROM stock").fetchone()
        finally:
            c.close()
        self.assertEqual(row["qty"], 100)

    def test_restore_migrates_missing_stock_item_id(self):
        self.add(qty=100)
        snap = app.create_snapshot("manual", "v02-item-id")
        self.add(qty=150)
        bad = TEST_DIR / "candidate-no-item-id.db"
        shutil.copyfile(app.SNAP_DIR / snap["name"], bad)
        c = sqlite3.connect(bad)
        try:
            c.execute("DROP INDEX IF EXISTS idx_stock_owner_item")
            c.execute("ALTER TABLE stock DROP COLUMN item_id")
            c.commit()
        finally:
            c.close()
        info = app.inspect_db_file(bad)
        self.assertTrue(info["ok"], info)
        res = app._install_db(bad, info)
        self.assertTrue(res["ok"], res)
        c = app.connect()
        try:
            row = c.execute("SELECT item_name, item_id FROM stock").fetchone()
        finally:
            c.close()
        self.assertEqual(row["item_name"], "青铜锭")
        self.assertEqual(row["item_id"], 0)

    def test_restore_rejects_missing_core_column(self):
        self.add(qty=100)
        snap = app.create_snapshot("manual", "v02-core")
        self.add(qty=150)
        bad = TEST_DIR / "candidate-no-qty.db"
        shutil.copyfile(app.SNAP_DIR / snap["name"], bad)
        c = sqlite3.connect(bad)
        try:
            c.execute("ALTER TABLE stock DROP COLUMN qty")
            c.commit()
        finally:
            c.close()
        info = app.inspect_db_file(bad)
        self.assertFalse(info["ok"])
        res = app._install_db(bad, info)
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "not_replaced")
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT qty FROM stock").fetchone()["qty"], 150)
        finally:
            c.close()

    def test_reconcile_clean_after_pending_drop(self):
        self.add(qty=100, loc="雇员1")
        self.assertTrue(app.delete_location_stock(self.char_id, "雇员1")["ok"])
        self.assertEqual(app.reconcile()["issues"], [])
        c = app.connect()
        try:
            pid = int(c.execute(
                "SELECT id FROM stock WHERE owner = ? AND location = ?",
                (app.OWNER, app.PENDING_LOC),
            ).fetchone()["id"])
        finally:
            c.close()
        self.assertTrue(app.pending_drop(pid)["ok"], "pending_drop")
        self.assertEqual(app.reconcile()["issues"], [])

    def test_reconcile_clean_after_pending_move_back(self):
        self.add(qty=100, loc="雇员1")
        self.assertTrue(app.delete_location_stock(self.char_id, "雇员1")["ok"])
        c = app.connect()
        try:
            pid = int(c.execute(
                "SELECT id FROM stock WHERE owner = ? AND location = ?",
                (app.OWNER, app.PENDING_LOC),
            ).fetchone()["id"])
        finally:
            c.close()
        self.assertTrue(app.pending_move(pid, self.char_id, "背包")["ok"])
        self.assertEqual(app.reconcile()["issues"], [])

    def test_reconcile_clean_after_fc_disband(self):
        fc_id, char2 = self._make_two_fc_rows()
        self.assertTrue(app.fc_delete(fc_id, "void")["ok"])
        self.assertEqual(app.reconcile()["issues"], [])
        app.set_character_fc(char2, 0)

    def _make_two_fc_rows(self):
        import time as _time
        tag = str(_time.time_ns())
        c = app.connect()
        try:
            row = c.execute(
                "INSERT INTO fc(owner, name, world, created_at) VALUES(?,?,?,?)",
                (app.OWNER, f"测试部队-{tag}", "红玉海", app.now_iso()),
            )
            fc_id = int(row.lastrowid)
            row = c.execute(
                "INSERT INTO character(owner, account, name, world, created_at)"
                " VALUES(?,?,?,?,?)",
                (app.OWNER, f"test2-{tag}", f"测试角色2-{tag}", "红玉海", app.now_iso()),
            )
            char2 = int(row.lastrowid)
            c.execute("UPDATE character SET fc_id = ? WHERE id IN (?, ?)",
                      (fc_id, self.char_id, char2))
            c.commit()
        finally:
            c.close()
        self.add(qty=90, loc="部队柜")
        self.assertTrue(app.record_stock(char2, "青铜锭", "部队柜", 90)["ok"])
        return fc_id, char2

    def test_reconcile_clean_after_fc_merge(self):
        fc_id, char2 = self._make_two_fc_rows()
        c = app.connect()
        try:
            keep = int(c.execute(
                "SELECT id FROM stock WHERE character_id = ?", (self.char_id,)
            ).fetchone()["id"])
        finally:
            c.close()
        res = app.fc_merge_apply(fc_id, [{
            "item_name": "青铜锭", "hq": 0, "qty": 90, "keep_stock_id": keep,
        }])
        self.assertTrue(res["ok"], res)
        self.assertEqual(app.reconcile()["issues"], [])
        app.set_character_fc(char2, 0)

    def test_reconcile_accepts_legacy_fc_merged_summary(self):
        fc_id, char2 = self._make_two_fc_rows()
        c = app.connect()
        try:
            c.execute("DELETE FROM stock WHERE character_id = ?", (char2,))
            c.execute(
                "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
                " location, hq, fc_id, delta, qty_after, note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (app.OWNER, app.now_iso(), "fc_merged",
                 app.resolve_item("青铜锭")[0], "青铜锭", self.char_id, "", 0,
                 fc_id, -90, 90, "旧版归并汇总"),
            )
            c.commit()
        finally:
            c.close()
        self.assertEqual(app.reconcile()["issues"], [])
        app.set_character_fc(char2, 0)

    def test_pending_move_merge_over_max_is_rejected(self):
        self.add(qty=app.MAX_ITEM_QTY)
        self.add(qty=1, loc="雇员1")
        self.assertTrue(app.delete_location_stock(self.char_id, "雇员1")["ok"])
        c = app.connect()
        try:
            pid = int(c.execute(
                "SELECT id FROM stock WHERE owner = ? AND location = ?",
                (app.OWNER, app.PENDING_LOC),
            ).fetchone()["id"])
        finally:
            c.close()
        res = app.pending_move(pid, self.char_id, "背包")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("invalid"))
        c = app.connect()
        try:
            bag = c.execute(
                "SELECT qty FROM stock WHERE location = '背包'"
            ).fetchone()["qty"]
            pending = c.execute(
                "SELECT COUNT(*) FROM stock WHERE location = ?", (app.PENDING_LOC,)
            ).fetchone()[0]
            rows = c.execute(
                "SELECT COUNT(*) FROM ledger WHERE action = 'pending_move'"
            ).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(bag, app.MAX_ITEM_QTY)
        self.assertEqual(pending, 1)
        self.assertEqual(rows, 0)

    def test_delete_location_pending_merge_over_max_is_rejected(self):
        self.add(qty=app.MAX_ITEM_QTY, loc="雇员1")
        self.assertTrue(app.delete_location_stock(self.char_id, "雇员1")["ok"])
        self.add(qty=1, loc="雇员1")
        res = app.delete_location_stock(self.char_id, "雇员1")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("invalid"))
        c = app.connect()
        try:
            pending = c.execute(
                "SELECT qty FROM stock WHERE location = ?", (app.PENDING_LOC,)
            ).fetchone()["qty"]
            source = c.execute(
                "SELECT qty FROM stock WHERE location = '雇员1'"
            ).fetchone()["qty"]
        finally:
            c.close()
        self.assertEqual(pending, app.MAX_ITEM_QTY)
        self.assertEqual(source, 1)

    def test_security_guards_reject_bad_host_and_missing_csrf(self):
        import http.client
        import threading

        httpd = app.FastHTTPServer(("127.0.0.1", 0), app.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/api/state")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertIs(data["local"], True)
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/api/state", headers={"Host": "evil.example"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 403)
            resp.read()
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request(
                "POST", "/api/db/restore", body=b"",
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 403)
            resp.read()
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_local_only_routes_are_declared(self):
        for route in ("/api/db/preview", "/api/db/restore",
                      "/api/snapshot/restore", "/api/snapshot/delete",
                      "/api/snapshot/cleanup", "/api/data/backup"):
            self.assertIn(route, app.LOCAL_ONLY_POST)
        for route in ("/api/snapshot/download", "/api/data-dir"):
            self.assertIn(route, app.LOCAL_ONLY_GET)
        handler = app.Handler.__new__(app.Handler)
        handler.client_address = ("192.168.1.50", 1234)
        self.assertFalse(handler._is_local_client())

    def _serve_http(self, handler_cls):
        httpd = app.FastHTTPServer(("127.0.0.1", 0), handler_cls)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, thread, port

    def _stop_http(self, httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)

    def _http(self, port, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def _remote_handler(self):
        class RemoteHandler(app.Handler):
            def setup(self):
                super().setup()
                # 测试替身：只模拟服务端识别到的对端地址，不伪造 Host / X-Forwarded-For
                self.client_address = ("192.0.2.50", 54321)

        return RemoteHandler

    def _headers(self, port):
        return {
            "Host": f"127.0.0.1:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/json",
            "X-CSRF-Token": app.CSRF_TOKEN,
        }

    def test_remote_api_blocked_when_lan_disabled(self):
        saved = app.LAN_ENABLED
        app.LAN_ENABLED = False
        httpd, thread, port = self._serve_http(self._remote_handler())
        try:
            status, _ = self._http(port, "GET", "/api/state",
                                   {"Host": f"127.0.0.1:{port}"})
            self.assertEqual(status, 403)
        finally:
            app.LAN_ENABLED = saved
            self._stop_http(httpd, thread)

    def test_remote_local_only_routes_blocked(self):
        saved = app.LAN_ENABLED
        app.LAN_ENABLED = True
        httpd, thread, port = self._serve_http(self._remote_handler())
        patched = ("set_lan", "backup_now", "build_recipe_index",
                   "build_npc_shop_index", "restore_snapshot",
                   "cleanup_legacy_snapshots", "delete_snapshot",
                   "preview_db_file", "_install_db")
        originals = {name: getattr(app, name) for name in patched}
        calls: list[str] = []

        def no_side_effect(name):
            def call(*_args, **_kwargs):
                calls.append(name)
                return {"ok": True}

            return call

        startfile = getattr(app.os, "startfile", None)
        try:
            for name in patched:
                setattr(app, name, no_side_effect(name))
            if startfile is not None:
                app.os.startfile = no_side_effect("startfile")
            headers = self._headers(port)
            for route in sorted(app.LOCAL_ONLY_GET):
                status, _ = self._http(port, "GET", route, headers)
                self.assertEqual(status, 403, route)
            for route in sorted(app.LOCAL_ONLY_POST):
                status, _ = self._http(port, "POST", route, headers, b"{}")
                self.assertEqual(status, 403, route)
            self.assertEqual(calls, [], "危险处理被执行")
        finally:
            for name, func in originals.items():
                setattr(app, name, func)
            if startfile is not None:
                app.os.startfile = startfile
            app.LAN_ENABLED = saved
            self._stop_http(httpd, thread)

    def test_security_guards_host_origin_csrf(self):
        httpd, thread, port = self._serve_http(app.Handler)
        try:
            headers = self._headers(port)
            status, _ = self._http(port, "GET", "/api/state",
                                   {"Host": "evil.example"})
            self.assertEqual(status, 403)

            bad_origin = dict(headers, Origin="http://evil.example")
            status, _ = self._http(port, "POST", "/api/undo", bad_origin, b"{}")
            self.assertEqual(status, 403)

            body = json.dumps({
                "character_id": self.char_id,
                "item_name": "青铜锭",
                "location": "背包",
                "qty": 1,
            }).encode("utf-8")
            csrf_calls: list[int] = []
            saved_record = app.record_stock
            app.record_stock = lambda *a, **k: csrf_calls.append(1) or {"ok": True}
            try:
                for token in (None, "wrong-token"):
                    hdrs = dict(headers)
                    if token is None:
                        hdrs.pop("X-CSRF-Token")
                    else:
                        hdrs["X-CSRF-Token"] = token
                    status, _ = self._http(port, "POST", "/api/stock", hdrs, body)
                    self.assertEqual(status, 403)
                self.assertEqual(csrf_calls, [])
            finally:
                app.record_stock = saved_record

            status, data = self._http(port, "POST", "/api/undo", headers, b"{}")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(data.decode("utf-8"))["ok"])
        finally:
            self._stop_http(httpd, thread)

    def test_root_serves_v2_and_v1_direct_urls(self):
        httpd, thread, port = self._serve_http(app.Handler)
        try:
            status, body = self._http(port, "GET", "/", {"Host": f"127.0.0.1:{port}"})
            self.assertEqual(status, 200)
            self.assertIn(b"refreshAfterWrite", body)
            self.assertIn(b"mgr-drawer", body)

            status, body = self._http(port, "GET", "/index.html", {"Host": f"127.0.0.1:{port}"})
            self.assertEqual(status, 200)
            self.assertIn(b"qty-mode", body)

            status, body = self._http(port, "GET", "/calculator.html", {"Host": f"127.0.0.1:{port}"})
            self.assertEqual(status, 200)
            self.assertIn(b"dragListeners", body)
        finally:
            self._stop_http(httpd, thread)

    def test_lan_state_uses_actual_port(self):
        saved = app._PORT
        try:
            app._PORT = 9123
            self.assertEqual(app.lan_state()["port"], 9123)
        finally:
            app._PORT = saved

    def test_data_sources_are_pinned(self):
        self.assertEqual(datasource.SAINT_GH, "thewakingsands/ffxiv-datamining-cn")
        self.assertRegex(datasource.SAINT_REF, r"^[0-9a-f]{40}$")
        for url in (app.ITEMS_URL, app.RECIPE_URL, app.RECIPE_LEVEL_URL):
            self.assertIn(datasource.SAINT_GH, url)
            self.assertIn(datasource.SAINT_REF, url)
            self.assertNotIn("@master", url)
        for url in datasource.saint_mirrors("Recipe.csv"):
            self.assertIn(datasource.SAINT_GH, url)
            self.assertIn(datasource.SAINT_REF, url)
            self.assertNotIn("/master/", url)

    # ---- 清单同步 S2 ----
    def test_recipe_list_save_create_update_and_meta(self):
        res = app.recipe_list_save("清单A", 0, self._list_payload())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["revision"], 1)
        got = app.recipe_list_get("清单A")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["payload"]["targets"][0]["quantity"], 10)

        res = app.recipe_list_save("清单A", 1, self._list_payload(20), "phone")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["revision"], 2)
        got = app.recipe_list_get("清单A")
        self.assertEqual(got["payload"]["targets"][0]["quantity"], 20)
        meta = app.recipe_list_meta()
        self.assertEqual(meta["lists"], [{
            "name": "清单A", "revision": 2,
            "updated_at": meta["lists"][0]["updated_at"], "updated_by": "phone",
        }])

    def test_recipe_list_conflict_returns_server_payload(self):
        self.assertTrue(app.recipe_list_save("冲突清单", 0, self._list_payload(10))["ok"])
        res = app.recipe_list_save("冲突清单", 0, self._list_payload(99))
        self.assertFalse(res["ok"])
        self.assertTrue(res["conflict"])
        self.assertEqual(res["revision"], 1)
        self.assertEqual(res["payload"]["targets"][0]["quantity"], 10)
        self.assertEqual(
            app.recipe_list_get("冲突清单")["payload"]["targets"][0]["quantity"], 10
        )

    def test_recipe_list_delete_idempotent_and_working_protected(self):
        self.assertTrue(app.recipe_list_save("删我", 0, self._list_payload())["ok"])
        self.assertTrue(app.recipe_list_delete("删我", 1)["ok"])
        self.assertTrue(app.recipe_list_delete("删我")["ok"])
        self.assertFalse(app.recipe_list_get("删我")["ok"])

        self.assertTrue(app.recipe_list_save("__working__", 0, self._list_payload())["ok"])
        res = app.recipe_list_delete("__working__")
        self.assertFalse(res["ok"])
        self.assertIn("不可删除", res["error"])

    def test_recipe_list_delete_conflict_returns_server_payload(self):
        self.assertTrue(app.recipe_list_save("删冲突", 0, self._list_payload())["ok"])
        res = app.recipe_list_delete("删冲突", 0)
        self.assertFalse(res["ok"])
        self.assertTrue(res["conflict"])
        self.assertEqual(res["revision"], 1)
        self.assertEqual(res["payload"]["targets"][0]["quantity"], 10)

    def test_recipe_list_validation_rejects_bad_payloads(self):
        good = self._list_payload()
        bad_payloads = []
        p = dict(good, schema=2)
        bad_payloads.append(p)
        p = dict(good, targets=[dict(good["targets"][0], quantity=0)])
        bad_payloads.append(p)
        p = dict(good, targets=[dict(good["targets"][0], quantity=2.5)])
        bad_payloads.append(p)
        p = dict(good, targets=[dict(good["targets"][0], hq=1)])
        bad_payloads.append(p)
        p = dict(good, overrides={"5056": "foo"})
        bad_payloads.append(p)
        p = dict(good, overrides={"x": "buy"})
        bad_payloads.append(p)
        p = dict(good, targets=[dict(good["targets"][0], name="x" * 300000)])
        bad_payloads.append(p)
        for payload in bad_payloads:
            res = app.recipe_list_save("校验", 0, payload)
            self.assertFalse(res["ok"], payload)
            self.assertTrue(res.get("invalid"), res)
        self.assertFalse(app.recipe_list_save("x" * 61, 0, good)["ok"])
        self.assertFalse(app.recipe_list_save("", 0, good)["ok"])
        self.assertFalse(app.recipe_list_save("校验", 0, good, "x" * 65)["ok"])

    def test_recipe_list_http_conflict_is_409(self):
        self.assertTrue(app.recipe_list_save("HTTP冲突", 0, self._list_payload())["ok"])
        httpd, thread, port = self._serve_http(app.Handler)
        try:
            body = json.dumps({
                "name": "HTTP冲突",
                "base_revision": 0,
                "payload": self._list_payload(99),
            }).encode("utf-8")
            status, data = self._http(
                port, "POST", "/api/recipe/list/save", self._headers(port), body
            )
            self.assertEqual(status, 409)
            result = json.loads(data.decode("utf-8"))
            self.assertTrue(result["conflict"])
            self.assertEqual(result["revision"], 1)
            self.assertEqual(result["payload"]["targets"][0]["quantity"], 10)
        finally:
            self._stop_http(httpd, thread)

    def test_recipe_list_http_routes(self):
        httpd, thread, port = self._serve_http(app.Handler)
        try:
            headers = self._headers(port)
            body = json.dumps({
                "name": "HTTPList",
                "base_revision": 0,
                "payload": self._list_payload(),
            }).encode("utf-8")
            status, data = self._http(
                port, "POST", "/api/recipe/list/save", headers, body
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data.decode("utf-8"))["revision"], 1)

            status, data = self._http(port, "GET", "/api/recipe/lists", headers)
            self.assertEqual(status, 200)
            names = [x["name"] for x in json.loads(data.decode("utf-8"))["lists"]]
            self.assertIn("HTTPList", names)

            status, data = self._http(
                port, "GET", "/api/recipe/list?name=HTTPList", headers
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data.decode("utf-8"))["payload"]["schema"], 1)

            body = json.dumps({"name": "HTTPList", "base_revision": 1}).encode("utf-8")
            status, data = self._http(
                port, "POST", "/api/recipe/list/delete", headers, body
            )
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(data.decode("utf-8"))["ok"])

            status, data = self._http(
                port, "GET", "/api/recipe/list?name=HTTPList", headers
            )
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(data.decode("utf-8"))["ok"])
        finally:
            self._stop_http(httpd, thread)

    def test_recipe_list_survives_snapshot_restore(self):
        self.assertTrue(app.recipe_list_save("快照清单", 0, self._list_payload(10))["ok"])
        snap = app.create_snapshot("manual", "recipe-list-test")
        self.assertTrue(app.recipe_list_save("快照清单", 1, self._list_payload(20))["ok"])
        self.assertTrue(app.restore_snapshot(snap["name"])["ok"])
        got = app.recipe_list_get("快照清单")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["revision"], 1)
        self.assertEqual(got["payload"]["targets"][0]["quantity"], 10)

    def test_recipe_list_table_is_created_for_old_db(self):
        c = app.connect()
        try:
            c.execute("DROP TABLE recipe_list")
            c.commit()
        finally:
            c.close()
        app.init_db()
        c = app.connect()
        try:
            count = c.execute("SELECT COUNT(*) FROM recipe_list").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(count, 0)
        self.assertTrue(app.inspect_db_file(app.DB_PATH, strict=True)["ok"])

    # ---- 恢复 ----
    def test_restore_rejects_incomplete_schema(self):
        self.add(qty=100)
        bad = TEST_DIR / "bad-schema.db"
        bad.unlink(missing_ok=True)
        c = sqlite3.connect(bad)
        try:
            c.execute("CREATE TABLE character(id INTEGER)")
            c.execute("CREATE TABLE stock(id INTEGER)")
            c.execute("CREATE TABLE ledger(id INTEGER)")
            c.commit()
        finally:
            c.close()
        info = app.inspect_db_file(bad)
        self.assertFalse(info["ok"])
        res = app._install_db(bad, info)
        self.assertFalse(res["ok"])
        self.assertFalse(res["replaced"])
        self.assertEqual(res["stage"], "not_replaced")
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT COALESCE(SUM(qty),0) FROM stock").fetchone()[0], 100)
        finally:
            c.close()

    def test_restore_rolls_back_if_init_fails_after_replace(self):
        self.add(qty=100)
        snap = app.create_snapshot("manual", "r02-test")
        self.add(qty=150)
        original = app.init_db
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.Error("injected init failure")
            return original()

        app.init_db = flaky
        try:
            res = app.restore_snapshot(snap["name"])
        finally:
            app.init_db = original
        self.assertFalse(res["ok"])
        self.assertTrue(res["replaced"])
        self.assertTrue(res["rolled_back"])
        self.assertEqual(res["stage"], "rolled_back")
        c = app.connect()
        try:
            self.assertEqual(c.execute("SELECT COALESCE(SUM(qty),0) FROM stock").fetchone()[0], 150)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
