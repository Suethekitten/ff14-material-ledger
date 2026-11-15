"""
配方展开算法自测：用固定的假配方验证恒等式、多产出、覆盖、库存抵扣、目标聚合。

不需要联网，也不碰正式数据。
用法：python tools\\test_recipe.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ff14-recipe-test-"))
os.environ["FF14_STOCK_DATA"] = str(TMP)
sys.path.insert(0, str(ROOT))

import app as A  # noqa: E402

FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAIL
    if cond:
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def setup_fake() -> None:
    # 100 = 2×200 + 1×300 ；200 一批产 3（用 4×300）；500 一批产 3（用 1×300）
    A.RECIPES = {
        100: [{"row": 1, "amount": 1, "level": 50, "ings": [[200, 2], [300, 1]]}],
        200: [{"row": 2, "amount": 3, "level": 60, "ings": [[300, 4]]}],
        500: [{"row": 3, "amount": 3, "level": 60, "ings": [[300, 1]]}],
    }
    A.RECIPE_META = {"version": "test", "recipes": 3, "items": 3}


def main() -> int:
    setup_fake()

    print("① 基础展开：做 5 个 100")
    r = A.expand_recipe([{"item_id": 100, "quantity": 5}])
    check("ok", r.get("ok") is True, str(r))
    base = {b["item_id"]: b for b in r["base"]}
    inter = {i["item_id"]: i for i in r["intermediate"]}
    check("中间品只有 200（目标自身不重复列出）", sorted(inter) == [200], str(sorted(inter)))
    check("基础料只有 300", list(base) == [300], str(list(base)))
    check("300 需要 21", base[300]["need"] == 21, str(base[300]["need"]))
    check("200 做 4 批（每批 3）",
          inter[200]["craft_count"] == 4 and inter[200]["per_batch"] == 3,
          f"{inter[200]['craft_count']}批/{inter[200]['per_batch']}")
    check("200 产 12 多 2",
          inter[200]["produced"] == 12 and inter[200]["surplus"] == 2,
          f"{inter[200]['produced']}/{inter[200]['surplus']}")
    check("100 不算中间品（只是目标）", 100 not in inter, str(sorted(inter)))

    print("\n② 恒等式")
    ok = True
    for node in r["intermediate"]:
        if node["source"] == "craft":
            if node["need"] != node["from_stock"] + node["to_craft"]:
                ok = False
                print("   自制节点不成立:", node)
            if node["to_craft"] > 0 and node["produced"] != node["craft_count"] * node["per_batch"]:
                ok = False
                print("   产出不成立:", node)
            if node["surplus"] != (node["produced"] or 0) - node["to_craft"]:
                ok = False
                print("   结余不成立:", node)
        else:
            if node["need"] != node["from_stock"] + node["to_buy"]:
                ok = False
                print("   购买节点不成立:", node)
    for node in r["base"]:
        if node["need"] != node["from_stock"] + node["to_get"]:
            ok = False
            print("   基础料不成立:", node)
        if sum(x["qty"] for x in node["where"]) != node["from_stock"]:
            ok = False
            print("   where 不成立:", node)
    for node in r["targets"]:
        if node["need"] != node["from_stock"] + node["to_craft"]:
            ok = False
            print("   目标不成立:", node)
    check("全部恒等式成立", ok)

    print("\n③ 合并口径：两个目标各 1 个 200，总需求 2 → 只做 1 批（不是 2 批）")
    r2 = A.expand_recipe([{"item_id": 200, "quantity": 1},
                          {"item_id": 200, "quantity": 1}])
    t2 = {t["item_id"]: t for t in r2["targets"]}
    check("目标合并成一条", len(r2["targets"]) == 1, str(len(r2["targets"])))
    check("数量合并成 2", t2[200]["quantity"] == 2, str(t2[200]["quantity"]))
    check("只做 1 批", t2[200]["craft_count"] == 1, str(t2[200]["craft_count"]))
    check("产出 3 多 1",
          t2[200]["produced"] == 3 and t2[200]["surplus"] == 1,
          f"{t2[200]['produced']}/{t2[200]['surplus']}")
    b2 = {b["item_id"]: b for b in r2["base"]}
    check("300 需要 4（1 批 × 4）", b2[300]["need"] == 4, str(b2[300]["need"]))

    print("\n③b 一个目标 2 个 200，结果必须与上面完全一致")
    r2b = A.expand_recipe([{"item_id": 200, "quantity": 2}])
    t2b = {t["item_id"]: t for t in r2b["targets"]}
    check("同样只做 1 批", t2b[200]["craft_count"] == 1, str(t2b[200]["craft_count"]))
    b2b = {b["item_id"]: b for b in r2b["base"]}
    check("300 同样需要 4", b2b[300]["need"] == 4, str(b2b[300]["need"]))

    print("\n④ 覆盖：把 200 改成直接购买")
    r3 = A.expand_recipe([{"item_id": 100, "quantity": 5}], overrides={"200": "buy"})
    base3 = {b["item_id"]: b for b in r3["base"]}
    inter3 = {i["item_id"]: i for i in r3["intermediate"]}
    check("200 仍在中间品里", 200 in inter3, str(list(inter3)))
    check("200 source=buy", inter3.get(200, {}).get("source") == "buy", str(inter3.get(200)))
    check("200 需购买 10", inter3.get(200, {}).get("to_buy") == 10, str(inter3.get(200)))
    check("200 无批次字段", inter3.get(200, {}).get("craft_count") is None, str(inter3.get(200)))
    check("200 不进基础料", 200 not in base3, str(list(base3)))
    check("300 只来自 100 的 5 个", base3.get(300, {}).get("need") == 5, str(base3.get(300)))

    print("\n⑤ 目标成品抵扣库存（方案 A）：库存里 6 个 500，要做 20 个")
    orig = A._stock_by_item
    A._stock_by_item = lambda: {
        500: [{"character": "阿辉", "world": "豆豆柴", "location": "背包", "qty": 6}],
    }
    try:
        r4 = A.expand_recipe([{"item_id": 500, "quantity": 20}], use_stock=True)
        r4b = A.expand_recipe([{"item_id": 500, "quantity": 20}], use_stock=False)
    finally:
        A._stock_by_item = orig
    t4 = r4["targets"][0]
    check("quantity 还是 20", t4["quantity"] == 20, str(t4["quantity"]))
    check("from_stock=6", t4["from_stock"] == 6, str(t4["from_stock"]))
    check("to_craft=14", t4["to_craft"] == 14, str(t4["to_craft"]))
    check("craft_count=5（ceil(14/3)）", t4["craft_count"] == 5, str(t4["craft_count"]))
    check("produced=15 surplus=1",
          t4["produced"] == 15 and t4["surplus"] == 1,
          f"{t4['produced']}/{t4['surplus']}")
    t4b = r4b["targets"][0]
    check("stock=false 时 from_stock=0", t4b["from_stock"] == 0, str(t4b["from_stock"]))
    check("stock=false 时 craft_count=7", t4b["craft_count"] == 7, str(t4b["craft_count"]))

    print("\n⑥ 库存抵扣 + 只扣一次：库存 4 个 200、5 个 300，做 5 个 100")
    orig = A._stock_by_item
    A._stock_by_item = lambda: {
        200: [{"character": "阿辉", "world": "豆豆柴", "location": "背包", "qty": 4}],
        300: [{"character": "阿辉", "world": "豆豆柴", "location": "背包", "qty": 5}],
    }
    try:
        r5 = A.expand_recipe([{"item_id": 100, "quantity": 5}], use_stock=True)
    finally:
        A._stock_by_item = orig
    inter5 = {i["item_id"]: i for i in r5["intermediate"]}
    base5 = {b["item_id"]: b for b in r5["base"]}
    check("200 抵扣 4", inter5[200]["from_stock"] == 4, str(inter5[200]["from_stock"]))
    check("200 还需自制 6", inter5[200]["to_craft"] == 6, str(inter5[200]["to_craft"]))
    check("200 做 2 批", inter5[200]["craft_count"] == 2, str(inter5[200]["craft_count"]))
    check("300 需要 13", base5[300]["need"] == 13, str(base5[300]["need"]))
    check("300 抵扣 5", base5[300]["from_stock"] == 5, str(base5[300]["from_stock"]))
    check("300 还差 8", base5[300]["to_get"] == 8, str(base5[300]["to_get"]))
    check("where 之和 == from_stock",
          sum(x["qty"] for x in base5[300]["where"]) == 5, str(base5[300]["where"]))

    print("\n⑦ 异常输入")
    check("数量 0 被拒", A.expand_recipe([{"item_id": 100, "quantity": 0}])["ok"] is False)
    check("超上限被拒",
          A.expand_recipe([{"item_id": 100, "quantity": 10_000_000}])["ok"] is False)
    check("空目标被拒", A.expand_recipe([])["ok"] is False)

    print("\n⑨ HQ 目标：库存 HQ 2 + NQ 6，要做 5 个 HQ")
    orig = A._stock_by_item
    A._stock_by_item = lambda: {
        500: [
            {"character": "阿辉", "world": "豆豆柴", "location": "背包", "qty": 2, "hq": 1},
            {"character": "阿辉", "world": "豆豆柴", "location": "鞍囊", "qty": 6, "hq": 0},
        ],
    }
    try:
        r7 = A.expand_recipe([{"item_id": 500, "quantity": 5, "hq": True}], use_stock=True)
        r7b = A.expand_recipe([{"item_id": 500, "quantity": 2, "hq": True}], use_stock=True)
        r7c = A.expand_recipe([{"item_id": 500, "quantity": 5, "hq": False}], use_stock=True)
    finally:
        A._stock_by_item = orig
    t7 = r7["targets"][0]
    check("HQ 目标只抵扣 HQ 库存 from_stock=2", t7["from_stock"] == 2, str(t7["from_stock"]))
    check("HQ 目标 to_craft=3", t7["to_craft"] == 3, str(t7["to_craft"]))
    check("回传 NQ 可用量 from_stock_nq=6", t7.get("from_stock_nq") == 6, str(t7.get("from_stock_nq")))
    check("where 标了 hq",
          all(x.get("hq") == 1 for x in t7["where"]) and sum(x["qty"] for x in t7["where"]) == 2,
          str(t7["where"]))
    t7b = r7b["targets"][0]
    check("需求 2 时全部由 HQ 库存覆盖 to_craft=0", t7b["to_craft"] == 0, str(t7b["to_craft"]))
    t7c = r7c["targets"][0]
    check("hq=false 时 NQ+HQ 一起抵扣（需求 5 全被覆盖）from_stock=5",
          t7c["from_stock"] == 5, str(t7c["from_stock"]))
    check("hq=false 时 to_craft=0", t7c["to_craft"] == 0, str(t7c["to_craft"]))
    check("hq=false 时 where 之和 = from_stock",
          sum(x["qty"] for x in t7c["where"]) == 5, str(t7c["where"]))

    print("\n⑨b HQ 目标与普通原料混合：HQ 需求只吃 HQ，原料需求可用 NQ")
    A.RECIPES[600] = [{"row": 4, "amount": 1, "level": 60, "ings": [[500, 1]]}]
    orig = A._stock_by_item
    A._stock_by_item = lambda: {
        500: [
            {"character": "阿辉", "world": "豆豆柴", "location": "背包", "qty": 2, "hq": 1},
            {"character": "阿辉", "world": "豆豆柴", "location": "鞍囊", "qty": 6, "hq": 0},
        ],
        300: [{"character": "阿辉", "world": "豆豆柴", "location": "背包", "qty": 10, "hq": 0}],
    }
    try:
        r8 = A.expand_recipe(
            [{"item_id": 500, "quantity": 5, "hq": True}, {"item_id": 600, "quantity": 1}],
            use_stock=True,
        )
        r8b = A.expand_recipe(
            [{"item_id": 500, "quantity": 3, "hq": True},
             {"item_id": 500, "quantity": 2, "hq": False}],
            use_stock=True,
        )
    finally:
        A._stock_by_item = orig
        A.RECIPES.pop(600, None)
    t8 = {t["item_id"]: t for t in r8["targets"]}
    check("混合需求 total=6", t8[500]["need"] == 6, str(t8[500]["need"]))
    check("HQ 2 + NQ 1 抵扣 3", t8[500]["from_stock"] == 3, str(t8[500]["from_stock"]))
    check("仍需自制 3（不被 HQ 限制成 4）", t8[500]["to_craft"] == 3,
          str(t8[500]["to_craft"]))
    check("where 2 HQ + 1 NQ",
          sorted((x["qty"], x["hq"]) for x in t8[500]["where"]) == [(1, 0), (2, 1)],
          str(t8[500]["where"]))
    t8b = {t["item_id"]: t for t in r8b["targets"]}
    check("HQ 3 + NQ 2：HQ 库存只够 2，NQ 2 正常抵扣，只差 1 个 HQ",
          t8b[500]["from_stock"] == 4 and t8b[500]["to_craft"] == 1,
          f"{t8b[500]['from_stock']}/{t8b[500]['to_craft']}")

    print("\n⑩ 买齐成本：按挂单深度逐档累加")
    import market  # noqa: E402

    fake_listings = {
        9001: [
            {"price": 46, "qty": 1, "world": "伊修加德"},
            {"price": 60, "qty": 12, "world": "太阳海岸"},
            {"price": 75, "qty": 30, "world": "豆豆柴"},
        ],
        9002: [],
    }
    orig_fetch = market._fetch_listings
    market._fetch_listings = lambda region, ids, hq: {
        i: {"rows": fake_listings.get(i, []),
            "count": len(fake_listings.get(i, [])), "upload": 0}
        for i in ids
    }
    market._listing_cache.clear()
    try:
        p1 = market.buy_plan("豆豆柴", False, [{"item_id": 9001, "quantity": 20}])
        p2 = market.buy_plan("豆豆柴", False, [{"item_id": 9001, "quantity": 100}])
        p3 = market.buy_plan("豆豆柴", False, [{"item_id": 9001, "quantity": 20},
                                              {"item_id": 9002, "quantity": 5}])
    finally:
        market._fetch_listings = orig_fetch
        market._listing_cache.clear()
    it1 = p1["items"][0]
    check("需 20 时成本 1291", it1["cost"] == 1291, str(it1["cost"]))
    check("需 20 时全部填满", it1["filled"] == 20 and it1["short"] == 0,
          f"{it1['filled']}/{it1['short']}")
    check("用了 3 档挂单", len(it1["listings_used"]) == 3, str(it1["listings_used"]))
    it2 = p2["items"][0]
    check("需 100 时只填 43", it2["filled"] == 43, str(it2["filled"]))
    check("需 100 时缺 57", it2["short"] == 57, str(it2["short"]))
    check("需 100 时成本 3016", it2["cost"] == 46 + 720 + 2250, str(it2["cost"]))
    check("无挂单进 unresolved", len(p3["unresolved"]) == 1, str(p3["unresolved"]))
    check("总价不含无价物品", p3["total"] == 1291, str(p3["total"]))
    check("覆盖率 priced=1", p3["coverage"]["priced"] == 1, str(p3["coverage"]))
    check("报价状态 ok", p1["status"] == "ok" and p1["ok"] is True, str(p1.get("status")))
    check("成功时无失败项", p1["failed_items"] == [], str(p1.get("failed_items")))

    import urllib.error
    orig_batch = market.BUY_PLAN_BATCH
    orig_fetch = market._fetch_listings

    def flaky(region, ids, hq):
        if 9002 in ids:
            raise urllib.error.URLError("down")
        return {i: {"rows": fake_listings.get(i, []),
                    "count": len(fake_listings.get(i, [])), "upload": 0} for i in ids}

    market._fetch_listings = flaky
    market.BUY_PLAN_BATCH = 1
    market._listing_cache.clear()
    try:
        pp = market.buy_plan("豆豆柴", False, [{"item_id": 9001, "quantity": 20},
                                              {"item_id": 9002, "quantity": 5}])
    finally:
        market._fetch_listings = orig_fetch
        market._listing_cache.clear()
    check("部分失败 status=partial", pp["status"] == "partial" and pp["ok"] is True,
          str(pp.get("status")))
    check("部分失败记 failed_items", pp["failed_items"] == [9002], str(pp["failed_items"]))

    market._fetch_listings = lambda region, ids, hq: (_ for _ in ()).throw(
        urllib.error.URLError("down"))
    market._listing_cache.clear()
    try:
        pf = market.buy_plan("豆豆柴", False, [{"item_id": 9001, "quantity": 20},
                                              {"item_id": 9002, "quantity": 5}])
    finally:
        market._fetch_listings = orig_fetch
        market.BUY_PLAN_BATCH = orig_batch
        market._listing_cache.clear()
    check("全部失败 ok=false,status=failed",
          pf["ok"] is False and pf["status"] == "failed", str(pf.get("status")))
    check("全部失败记全部 failed_items",
          sorted(pf["failed_items"]) == [9001, 9002], str(pf["failed_items"]))

    print("\n⑪ NPC 金币商店价")
    gi_header = ["key", "Item", "a", "b", "c",
                 "Quest{Required}[0]", "Quest{Required}[1]",
                 "Achievement{Required}", "d", "State{Required}", "Patch"]
    gi_rows = [
        ["262144.0", "4594", "False", "0", "0", "0", "0", "0", "0", "0", "0"],
        ["262145.2", "4552", "False", "0", "0", "66646", "0", "1558", "0", "0", "0"],
        ["262146.0", "9999", "False", "0", "0", "0", "0", "0", "0", "0", "0"],
    ]
    gs_header = ["key", "Name"]
    gs_rows = [["262144", "伊修加德杂货商"], ["262145", "某奇货商"]]
    it_header = ["key", "Name", "PriceMid"]
    it_rows = [["4594", "秘银锭", "514"], ["4552", "某道具", "80"], ["9999", "无价物", "0"]]

    def fake_csv(url: str, timeout: int = 300):
        if "GilShopItem" in url:
            return gi_header, gi_rows
        if "GilShop.csv" in url:
            return gs_header, gs_rows
        return it_header, it_rows

    orig_csv = A._read_saint_csv
    A._read_saint_csv = fake_csv
    try:
        res = A.build_npc_shop_index()
    finally:
        A._read_saint_csv = orig_csv

    check("构建成功", res.get("ok") is True, str(res))
    check("收集到 2 个物品", res.get("items") == 2, str(res.get("items")))
    check("无价物品被跳过", res.get("skipped_no_price") == 1, str(res.get("skipped_no_price")))
    e1 = A.NPC_SHOPS.get(4594) or {}
    check("秘银锭价格 514", e1.get("price") == 514, str(e1))
    check("商店 ID 取整数部分",
          [s["id"] for s in e1.get("shops") or []] == [262144], str(e1.get("shops")))
    check("商店名 join 上了",
          (e1.get("shops") or [{}])[0].get("name") == "伊修加德杂货商", str(e1.get("shops")))
    e2 = A.NPC_SHOPS.get(4552) or {}
    check("任务前置 66646", e2.get("quest_required") == [66646], str(e2.get("quest_required")))
    check("成就前置 1558", e2.get("achievement_required") == [1558], str(e2.get("achievement_required")))
    check("无价物品不在表里", 9999 not in A.NPC_SHOPS, str(list(A.NPC_SHOPS)))

    look = A.npc_shop_lookup([4594, 9999])
    check("查询返回 ok", look.get("ok") is True, str(look))
    check("没数据的物品不出现", list(look["items"]) == ["4594"], str(look["items"]))

    saved_shops = A.NPC_SHOPS
    A.NPC_SHOPS = {}
    try:
        empty = A.npc_shop_lookup([4594])
    finally:
        A.NPC_SHOPS = saved_shops
    check("数据未就绪时 ok:false", empty.get("ok") is False, str(empty))

    print("\n⑫ 配方表缺失")
    saved = A.RECIPES
    A.RECIPES = {}
    try:
        r6 = A.expand_recipe([{"item_id": 100, "quantity": 1}])
    finally:
        A.RECIPES = saved
    check("返回 ok:false 而不是异常",
          r6.get("ok") is False and "配方" in str(r6.get("error")), str(r6))

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
        print("（已清理临时目录）")
    sys.exit(code)
