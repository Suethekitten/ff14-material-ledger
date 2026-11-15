"""
「未确认库存」后端自测：在临时目录里跑完整流程，不碰你的正式数据。

用法：python tools\\test_pending.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ff14-pending-test-"))
os.environ["FF14_STOCK_DATA"] = str(TMP)
sys.path.insert(0, str(ROOT))

import app as A  # noqa: E402  （必须在设置环境变量之后导入）

FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAIL
    if cond:
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    print(f"临时数据目录：{TMP}")
    print(f"后端版本：{A.VERSION}\n")

    A.init_db()

    A.add_character("测试账号", "阿辉", "豆豆柴")
    A.add_character("测试账号", "老陈", "豆豆柴")
    conn = A.connect()
    try:
        cid1 = conn.execute(
            "SELECT id FROM character WHERE name = ? AND owner = ?", ("阿辉", A.OWNER)
        ).fetchone()["id"]
        cid2 = conn.execute(
            "SELECT id FROM character WHERE name = ? AND owner = ?", ("老陈", A.OWNER)
        ).fetchone()["id"]
    finally:
        conn.close()

    print("① 给雇员3 录两条库存")
    A.record_stock(cid1, "黑铁锭", "雇员3", 200, 0, allow_unbound=True)
    A.record_stock(cid1, "亚麻", "雇员3", 60, 0, allow_unbound=True)
    st = A.state()
    check("总库存 2 行", st["stock_rows"] == 2, str(st["stock_rows"]))
    check("未确认为空", st["pending_rows"] == 0, str(st["pending_rows"]))

    print("\n② 删雇员3 栏位 → 应转入未确认，而不是删除")
    conn = A.connect()
    try:
        before_updated = conn.execute(
            "SELECT updated_at FROM stock WHERE character_id = ? AND location = '雇员3'"
            " ORDER BY id LIMIT 1", (cid1,)
        ).fetchone()["updated_at"]
    finally:
        conn.close()

    res = A.delete_location_stock(cid1, "雇员3")
    check("接口返回成功", res.get("ok") is True, str(res))
    check("标记为转入未确认", res.get("moved_to_pending") is True, str(res.get("moved_to_pending")))
    check("转移 2 行 / 260 件", res.get("rows") == 2 and res.get("qty") == 260,
          f"{res.get('rows')}行 {res.get('qty')}件")
    st = A.state()
    check("主库存归零", st["stock_rows"] == 0, str(st["stock_rows"]))
    check("合计归零", st["total_qty"] == 0, str(st["total_qty"]))
    check("未确认 2 行 260 件",
          st["pending_rows"] == 2 and st["pending_qty"] == 260,
          f"{st['pending_rows']}行 {st['pending_qty']}件")

    print("\n③ 搜索结果不应包含未确认行")
    hit = A.search(query="")
    names = [i["name"] for i in hit["items"]]
    check("搜索里没有黑铁锭/亚麻", not names, str(names))

    print("\n④ 未确认列表")
    pend = A.pending_stock()
    check("2 组材料", len(pend["items"]) == 2, str(len(pend["items"])))
    origins = {r["origin"] for item in pend["items"] for r in item["rows"]}
    check("原位置记为「雇员3」", origins == {"雇员3"}, str(origins))
    kept = {r["updated_at"] for item in pend["items"] for r in item["rows"]}
    check("updated_at 未被迁移时间覆盖", before_updated in kept, f"{before_updated} vs {kept}")
    since = {bool(r.get("pending_since")) for item in pend["items"] for r in item["rows"]}
    check("记录了 pending_since", since == {True}, str(since))

    print("\n⑤ 转移到老陈 / 背包")
    row = pend["items"][0]["rows"][0]
    move = A.pending_move(row["stock_id"], cid2, "背包")
    check("转移成功", move.get("ok") is True, str(move))
    check("目标数量正确", move.get("target_qty") == row["qty"], str(move.get("target_qty")))
    st = A.state()
    check("未确认剩 1 行", st["pending_rows"] == 1, str(st["pending_rows"]))
    check("主库存 1 行", st["stock_rows"] == 1, str(st["stock_rows"]))

    print("\n⑤b 流水备注应写「原属 阿辉 / 雇员3」，不是目标")
    conn = A.connect()
    try:
        note = conn.execute(
            "SELECT note FROM ledger WHERE action = 'pending_move' ORDER BY id DESC LIMIT 1"
        ).fetchone()["note"]
    finally:
        conn.close()
    check("备注含原角色", "阿辉" in note, note)
    check("备注含原位置", "雇员3" in note, note)
    check("备注不含目标位置", "背包" not in note, note)

    print("\n⑤c 非法目标位置应被拒绝（雇员1..10 合法，11 以上不行）")
    pend = A.pending_stock()
    left_row = pend["items"][0]["rows"][0]
    ok9 = A.pending_move(left_row["stock_id"], cid2, "雇员9")
    check("雇员9 允许（v0.11.2 放宽）", ok9.get("ok") is True, str(ok9))
    check("转移后未确认少一条", A.state()["pending_rows"] == 0, str(A.state()["pending_rows"]))

    # 再造一条未确认，用来测拒绝
    A.record_stock(cid1, "白钨矿", "雇员3", 12, 0, allow_unbound=True)
    A.delete_location_stock(cid1, "雇员3")
    pend = A.pending_stock()
    left_row = pend["items"][0]["rows"][0]
    bad = A.pending_move(left_row["stock_id"], cid2, "雇员11")
    check("雇员11 被拒绝", bad.get("ok") is False, str(bad))
    bad2 = A.pending_move(left_row["stock_id"], cid2, "../../etc")
    check("乱写位置被拒绝", bad2.get("ok") is False, str(bad2))
    check("拒绝后未确认仍在", A.state()["pending_rows"] == 1, str(A.state()["pending_rows"]))

    print("\n⑥ 转移到已有格子应相加")
    A.record_stock(cid2, "白钨矿", "背包", 100, 0, allow_unbound=True)
    pend = A.pending_stock()
    row2 = pend["items"][0]["rows"][0]
    before = A.search(query="白钨矿")["items"][0]["total"]
    move2 = A.pending_move(row2["stock_id"], cid2, "背包")
    after = A.search(query="白钨矿")["items"][0]["total"]
    check("merged 标记为真", move2.get("merged") is True, str(move2.get("merged")))
    check(f"数量相加（{before} + {row2['qty']} = {after}）", after == before + row2["qty"],
          f"{before}→{after}")

    print("\n⑦ 再删一次雇员3（已空）应拒绝")
    res = A.delete_location_stock(cid1, "雇员3")
    check("空位置返回失败", res.get("ok") is False, str(res))

    print("\n⑧ 丢弃")
    A.record_stock(cid1, "白钨矿", "雇员3", 12, 0, allow_unbound=True)
    A.delete_location_stock(cid1, "雇员3")
    pend = A.pending_stock()
    target = pend["items"][0]["rows"][0]
    drop = A.pending_drop(target["stock_id"], "测试丢弃")
    check("丢弃成功", drop.get("ok") is True, str(drop))
    check("有快照", bool(drop.get("snapshot")), str(drop.get("snapshot")))
    st = A.state()
    check("未确认清空", st["pending_rows"] == 0, str(st["pending_rows"]))

    print("\n⑨ 快照目录")
    snaps = list(A.SNAP_DIR.glob("*.db")) if A.SNAP_DIR.exists() else []
    check("至少生成 3 份快照", len(snaps) >= 3, f"{len(snaps)} 份")

    print()
    if FAIL:
        print(f"结果：{FAIL} 项失败")
    else:
        print("结果：全部通过 ✅")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        print(f"（已清理临时目录）")
    sys.exit(code)
