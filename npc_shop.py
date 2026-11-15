"""NPC 金币商店数据：构建、加载、查询。

只依赖标准库和 datasource.py；不导入 app.py，避免循环导入。
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

import datasource

NPC_API_BASE = "https://xivapi-v2.xivcdn.com/api/sheet"
NPC_API_PAGE = 500

NPC_SHOPS: dict = {}
NPC_META: dict = {"version": "", "items": 0}


def _api_get(url: str, user_agent: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _npc_from_api(progress=None, *, user_agent: str) -> dict:
    """备用数据源：XIVAPI v2 的 GilShopItem 表（自带嵌套 Item.PriceMid）。

    CSV 镜像都取不到时用这条。商店名单独从 GilShop 表取，取不到留空。
    """

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    say("从 XIVAPI 读取金币商店关联表…")
    shop_names: dict[int, str] = {}
    try:
        after = None
        while True:
            url = f"{NPC_API_BASE}/GilShop?fields=Name&limit={NPC_API_PAGE}"
            if after is not None:
                url += f"&after={after}"
            payload = _api_get(url, user_agent)
            rows = payload.get("rows") or []
            if not rows:
                break
            for r in rows:
                try:
                    sid = int(r.get("row_id") or 0)
                except (TypeError, ValueError):
                    continue
                name = ((r.get("fields") or {}).get("Name") or "").strip()
                if sid and name:
                    shop_names[sid] = name
            last = rows[-1]
            after = f'{last.get("row_id")}:{last.get("subrow_id") or 0}'
            if len(rows) < NPC_API_PAGE:
                break
    except Exception:  # noqa: BLE001 - 商店名拿不到不影响价格
        shop_names = {}

    items: dict = {}
    skipped = 0
    after = None
    pages = 0
    while True:
        url = f"{NPC_API_BASE}/GilShopItem?fields=Item.PriceMid&limit={NPC_API_PAGE}"
        if after is not None:
            url += f"&after={after}"
        payload = _api_get(url, user_agent)
        rows = payload.get("rows") or []
        if not rows:
            break
        pages += 1
        for r in rows:
            fields = r.get("fields") or {}
            item = fields.get("Item") or {}
            try:
                item_id = int(item.get("value") or 0)
                price = int((item.get("fields") or {}).get("PriceMid") or 0)
            except (TypeError, ValueError):
                continue
            if item_id <= 0:
                continue
            if price <= 0:
                skipped += 1
                continue
            try:
                shop_id = int(r.get("row_id") or 0)
            except (TypeError, ValueError):
                shop_id = 0
            entry = items.setdefault(
                str(item_id),
                {
                    "price": price,
                    "shops": [],
                    "quest_required": [],
                    "achievement_required": [],
                    "state_required": [],
                },
            )
            if shop_id and all(s["id"] != shop_id for s in entry["shops"]):
                entry["shops"].append({"id": shop_id, "name": shop_names.get(shop_id, "")})
        last = rows[-1]
        after = f'{last.get("row_id")}:{last.get("subrow_id") or 0}'
        if progress and pages % 5 == 0:
            say(f"已读取 {len(items)} 种物品…（第 {pages} 页）")
        if len(rows) < NPC_API_PAGE:
            break

    return {"items": items, "shops": len(shop_names), "skipped_no_price": skipped}


def load_npc_shops(data_dir: Path, ensure_file) -> None:
    """载入 data/npc_shop.json（没有就留空，功能降级但不报错）。"""
    global NPC_SHOPS, NPC_META
    ensure_file("npc_shop.json")
    path = data_dir / "npc_shop.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        NPC_SHOPS = {}
        NPC_META = {"version": "", "items": 0}
        return
    raw = data.get("items") or {}
    out: dict = {}
    for key, val in raw.items():
        try:
            out[int(key)] = val
        except (TypeError, ValueError):
            continue
    NPC_SHOPS = out
    NPC_META = {"version": str(data.get("version") or ""), "items": len(out)}


def _col_index(header: list[str], names: tuple) -> int:
    """按候选列名找下标（忽略大小写和 {}）。"""
    norm = [h.strip().strip("{}").replace(" ", "").lower() for h in header]
    for want in names:
        key = want.strip("{}").replace(" ", "").lower()
        if key in norm:
            return norm.index(key)
    return -1


def build_npc_shop_index(data_dir: Path, ensure_file, progress=None, *,
                         user_agent: str) -> dict:
    """生成 data/npc_shop.json 并热加载。

    优先走 CSV 镜像（信息最全：含任务/成就/状态前置）；
    镜像全挂时退回 XIVAPI v2（只有价格和商店，没有前置标记）。
    """

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    path = data_dir / "npc_shop.json"
    say("下载金币商店关联表…")
    gi_header, gi_rows, gi_err = datasource.read_saint_csv_multi("GilShopItem.csv")
    if not gi_header:
        say(f"CSV 镜像都不可用（{gi_err}），改用 XIVAPI…")
        try:
            api = _npc_from_api(progress, user_agent=user_agent)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"CSV 与 XIVAPI 都失败。CSV：{gi_err}；XIVAPI：{exc}",
            }
        payload = {"version": datetime.now().strftime("%Y-%m-%d"),
                   "items": api["items"], "source": "xivapi"}
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        load_npc_shops(data_dir, ensure_file)
        say(f"NPC 金币商店物品 {len(api['items'])} 种，已保存（来源 XIVAPI）")
        return {
            "ok": True,
            "version": payload["version"],
            "items": len(api["items"]),
            "shops": api["shops"],
            "skipped_no_price": api["skipped_no_price"],
            "source": "xivapi",
            "path": str(path),
        }

    def gcol(*names) -> int:
        return _col_index(gi_header, names)

    idx_item = gcol("Item")
    idx_q0 = gcol("Quest{Required}[0]")
    idx_q1 = gcol("Quest{Required}[1]")
    idx_ach = gcol("Achievement{Required}")
    idx_state = gcol("State{Required}")
    if idx_item < 0:
        return {"ok": False, "error": "GilShopItem.csv 缺少 Item 列"}

    say("下载商店名表…")
    shop_names: dict[int, str] = {}
    gs_header, gs_rows, _gs_err = datasource.read_saint_csv_multi("GilShop.csv")
    gs_name = _col_index(gs_header, ("Name",)) if gs_header else -1
    if gs_name >= 0:
        for r in gs_rows:
            try:
                sid = int(float(r[0]))
                name = (r[gs_name] or "").strip()
            except (IndexError, ValueError):
                continue
            if sid and name:
                shop_names[sid] = name

    say("下载物品表（约 19MB，只取 PriceMid）…")
    prices: dict[int, int] = {}
    it_header, it_rows, it_err = datasource.read_saint_csv_multi("Item.csv")
    if not it_header:
        return {"ok": False, "error": f"Item.csv 下载失败：{it_err}"}
    price_col = _col_index(it_header, ("PriceMid", "Price{Mid}", "PriceMid{Item}"))
    if price_col >= 0:
        for r in it_rows:
            try:
                item_id = int(float(r[0]))
                price = int(float(r[price_col] or 0))
            except (IndexError, ValueError):
                continue
            if item_id > 0 and price > 0:
                prices[item_id] = price
    if not prices:
        return {"ok": False, "error": "Item.csv 里找不到 PriceMid 列或价格全为 0"}

    def to_ids(*idxs) -> list[int]:
        out = []
        for i in idxs:
            if i < 0:
                continue
            try:
                val = r[i]
            except IndexError:
                continue
            try:
                num = int(float(val or 0))
            except (TypeError, ValueError):
                continue
            if num > 0 and num not in out:
                out.append(num)
        return out

    items: dict = {}
    skipped_no_price = 0
    for r in gi_rows:
        try:
            key = float(r[0])
            item_id = int(float(r[idx_item]))
        except (IndexError, ValueError):
            continue
        if item_id <= 0:
            continue
        price = prices.get(item_id)
        if not price:
            skipped_no_price += 1
            continue
        shop_id = int(key)          # key = "262144.0" → 商店 ID 取整数部分
        entry = items.setdefault(
            str(item_id),
            {
                "price": price,
                "shops": [],
                "quest_required": [],
                "achievement_required": [],
                "state_required": [],
            },
        )
        if shop_id and all(s["id"] != shop_id for s in entry["shops"]):
            entry["shops"].append({"id": shop_id, "name": shop_names.get(shop_id, "")})
        for num in to_ids(idx_q0, idx_q1):
            if num not in entry["quest_required"]:
                entry["quest_required"].append(num)
        for num in to_ids(idx_ach):
            if num not in entry["achievement_required"]:
                entry["achievement_required"].append(num)
        for num in to_ids(idx_state):
            if num not in entry["state_required"]:
                entry["state_required"].append(num)

    payload = {
        "version": datetime.now().strftime("%Y-%m-%d"),
        "items": items,
        "source": "csv",
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)

    load_npc_shops(data_dir, ensure_file)
    result = {
        "ok": True,
        "version": payload["version"],
        "items": len(items),
        "shops": len(shop_names),
        "skipped_no_price": skipped_no_price,
        "source": "csv",
        "path": str(path),
    }
    say(f"NPC 金币商店物品 {len(items)} 种，已保存")
    return result


def npc_shop_lookup(ids: list[int]) -> dict:
    """按物品 ID 查 NPC 金币价；没有数据的物品不出现在结果里。"""
    if not NPC_SHOPS:
        return {"ok": False, "error": "NPC 商店数据未就绪", "version": "", "items": {}}
    out: dict = {}
    for item_id in ids[:100]:
        entry = NPC_SHOPS.get(item_id)
        if entry:
            out[str(item_id)] = entry
    return {"ok": True, "version": NPC_META.get("version", ""), "items": out}
