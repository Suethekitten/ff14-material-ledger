"""市场板价格查询（Universalis v2），同步标准库实现。

设计要点：
- 批量查询：一次请求最多 100 个物品 ID，26 种材料一次查完；
- 结果缓存 5 分钟，避免反复打接口；
- 只读：不写库、不影响台账；
- 失败按物品粒度返回，不拖垮整个清单。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

API_BASE = "https://universalis.app/api/v2"
USER_AGENT = "FF14-Stock-Ledger/0.12 (Universalis price lookup)"
CACHE_TTL_SECONDS = 300
TIMEOUT_SECONDS = 12
MAX_IDS = 100

CN_DATACENTERS = ["陆行鸟", "莫古力", "猫小胖", "豆豆柴"]

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}
_worlds_cache: dict = {"at": 0.0, "data": {}}
WORLDS_TTL_SECONDS = 24 * 60 * 60


def regions() -> list[str]:
    return list(CN_DATACENTERS)


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _prune_expired(cache: dict) -> None:
    """清掉过期缓存，避免长时间运行只增不减。"""
    now = time.monotonic()
    for key, (at, _data) in list(cache.items()):
        if now - at >= CACHE_TTL_SECONDS:
            cache.pop(key, None)


def worlds_of(dc: str) -> list[str]:
    """返回某大区下的服务器名列表（缓存 24 小时）。"""
    dc = (dc or "").strip()
    if not dc:
        return []
    now = time.monotonic()
    with _lock:
        data = _worlds_cache.get("data") or {}
        if data and now - _worlds_cache["at"] < WORLDS_TTL_SECONDS and dc in data:
            return data[dc]
    try:
        dcs = _get_json(API_BASE + "/data-centers")
        world_list = _get_json(API_BASE + "/worlds")
        names = {int(w["id"]): w["name"] for w in world_list}
        mapping = {}
        for entry in dcs:
            if entry.get("region") != "中国":
                continue
            mapping[entry["name"]] = [names.get(int(wid), "") for wid in entry.get("worlds", []) if names.get(int(wid))]
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        return []
    with _lock:
        _worlds_cache["at"] = time.monotonic()
        _worlds_cache["data"] = mapping
    return mapping.get(dc, [])


def _now_iso(ms) -> str:
    if not ms:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))
    except (ValueError, OSError, OverflowError):
        return ""


def _normalize(item: dict) -> dict:
    listings = item.get("listings") or []
    dc_name = str(item.get("dcName") or "").strip()
    world = dc_name or (listings[0].get("worldName") if listings else "")
    return {
        "min_nq": int(item.get("minPriceNQ") or 0),
        "min_hq": int(item.get("minPriceHQ") or 0),
        "avg_nq": round(float(item.get("currentAveragePriceNQ") or 0)),
        "avg_hq": round(float(item.get("currentAveragePriceHQ") or 0)),
        "listings": int(item.get("listingsCount") or 0),
        "units": int(item.get("unitsForSale") or 0),
        "world": world or "",
        "scope": "dc" if dc_name else "world",
        "updated": _now_iso(item.get("lastUploadTime")),
        "has_data": bool(item.get("hasData")),
    }


def _fetch_batch(region: str, ids: list[int]) -> dict:
    """一次请求查一批 ID；返回 {item_id: normalized}。"""
    id_text = ",".join(str(i) for i in ids)
    url = f"{API_BASE}/{urllib.parse.quote(region, safe='')}/{id_text}?listings=1&entries=0"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    # 单个 ID 时 Universalis 可能直接返回该项，而不是 items 字典
    items = payload.get("items")
    if items is None:
        items = {str(payload.get("itemID")): payload}
    out = {}
    for key, item in items.items():
        try:
            out[int(key)] = _normalize(item)
        except (TypeError, ValueError):
            continue
    return out


def fetch_prices(region: str, item_ids) -> dict:
    """批量查价，带 5 分钟缓存。

    Returns: {"ok": bool, "region": str, "prices": {id: {...}}, "error": str,
              "requested": int, "returned": int, "truncated": bool}
    """
    region = (region or "").strip() or CN_DATACENTERS[-1]
    unique_ids = []
    for i in item_ids or []:
        try:
            value = int(i)
        except (TypeError, ValueError):
            continue
        if value > 0:
            unique_ids.append(value)
    wanted = sorted(set(unique_ids))
    ids = wanted[:MAX_IDS]
    base = {
        "requested": len(wanted),
        "returned": len(ids),
        "truncated": len(wanted) > len(ids),
    }
    if not ids:
        return {"ok": True, "region": region, "prices": {}, "error": "", **base}

    key = (region, tuple(ids))
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return {"ok": True, "region": region, "prices": hit[1], "error": "",
                    "cached": True, **base}

    try:
        prices = _fetch_batch(region, ids)
    except urllib.error.HTTPError as exc:
        msg = "该大区/服务器没有数据" if exc.code == 404 else f"Universalis 返回 HTTP {exc.code}"
        return {"ok": False, "region": region, "prices": {}, "error": msg,
                "failed_items": ids, **base}
    except urllib.error.URLError as exc:
        return {"ok": False, "region": region, "prices": {}, "error": f"网络请求失败：{exc.reason}",
                "failed_items": ids, **base}
    except (TimeoutError, ValueError, KeyError) as exc:
        return {"ok": False, "region": region, "prices": {}, "error": f"查询失败：{exc}",
                "failed_items": ids, **base}

    with _lock:
        _prune_expired(_cache)
        _cache[key] = (time.monotonic(), prices)
    return {"ok": True, "region": region, "prices": prices, "error": "", **base}


# ── 买齐成本：按挂单深度逐档累加 ───────────────────────────
# 不能拿「最低价 × 数量」——最低价那条可能只有 1 件。

LISTINGS_PER_ITEM = 100      # 每个物品最多取多少条挂单
BUY_PLAN_BATCH = 20          # 一次请求查多少个物品（避免响应过大）
BUY_PLAN_MAX_ITEMS = 50
_listing_cache: dict[tuple, tuple[float, dict]] = {}


def _fetch_listings(region: str, ids: list[int], hq: bool) -> dict:
    """取挂单明细：{item_id: {rows: [{price,qty,world}], count, upload}}。"""
    id_text = ",".join(str(i) for i in ids)
    url = (
        f"{API_BASE}/{urllib.parse.quote(region, safe='')}/{id_text}"
        f"?listings={LISTINGS_PER_ITEM}&entries=0"
    )
    if hq:
        url += "&hq=1"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    items = payload.get("items")
    if items is None:
        items = {str(payload.get("itemID")): payload}

    out: dict = {}
    for key, item in items.items():
        try:
            item_id = int(key)
        except (TypeError, ValueError):
            continue
        rows = []
        for entry in item.get("listings") or []:
            try:
                if bool(entry.get("hq")) != bool(hq):
                    continue
                price = int(entry.get("pricePerUnit") or 0)
                qty = int(entry.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0 and qty > 0:
                rows.append(
                    {"price": price, "qty": qty, "world": entry.get("worldName") or ""}
                )
        rows.sort(key=lambda r: r["price"])
        out[item_id] = {
            "rows": rows,
            "count": int(item.get("listingsCount") or 0),
            "upload": int(item.get("lastUploadTime") or 0),
        }
    return out


def buy_plan(region: str, hq: bool, items: list) -> dict:
    """按挂单深度算「买齐这些要多少金」。

    items: [{"item_id": int, "quantity": int}]
    Returns: {"ok", "region", "hq", "updated_at", "items": [...], "total",
              "unresolved": [...], "coverage": {...}}
    """
    region = (region or "").strip() or CN_DATACENTERS[-1]
    hq = bool(hq)

    wanted: list[tuple[int, int]] = []
    for row in items or []:
        try:
            item_id = int(row.get("item_id") or 0)
            qty = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "物品或数量格式不对"}
        if item_id > 0 and qty > 0:
            wanted.append((item_id, qty))
    if not wanted:
        return {"ok": False, "error": "至少需要一个物品"}
    if len(wanted) > BUY_PLAN_MAX_ITEMS:
        return {"ok": False, "error": f"一次最多 {BUY_PLAN_MAX_ITEMS} 个物品"}
    if len({i for i, _ in wanted}) != len(wanted):
        merged: dict = {}
        for item_id, qty in wanted:
            merged[item_id] = merged.get(item_id, 0) + qty
        wanted = list(merged.items())

    ids = sorted({i for i, _ in wanted})
    key = (region, hq, tuple(ids))
    now = time.monotonic()
    with _lock:
        hit = _listing_cache.get(key)
        listings_cached = hit[1] if hit and now - hit[0] < CACHE_TTL_SECONDS else None

    listings: dict = dict(listings_cached) if listings_cached is not None else {}
    fetch_failed = ""
    failed_items: list[int] = []
    if listings_cached is None:
        for start in range(0, len(ids), BUY_PLAN_BATCH):
            chunk = ids[start:start + BUY_PLAN_BATCH]
            try:
                listings.update(_fetch_listings(region, chunk, hq))
            except urllib.error.HTTPError as exc:
                fetch_failed = (
                    "该大区/服务器没有数据" if exc.code == 404 else f"Universalis 返回 HTTP {exc.code}"
                )
                failed_items.extend(chunk)
            except urllib.error.URLError as exc:
                fetch_failed = f"网络请求失败：{exc.reason}"
                failed_items.extend(chunk)
            except (TimeoutError, ValueError, KeyError) as exc:
                fetch_failed = f"查询失败：{exc}"
                failed_items.extend(chunk)
        if not failed_items:
            with _lock:
                _prune_expired(_listing_cache)
                _listing_cache[key] = (time.monotonic(), listings)

    out_items = []
    unresolved = []
    truncated_items: list[int] = []
    uploads: list[int] = []
    total = 0
    priced = 0

    for item_id, need in wanted:
        info = listings.get(item_id) or {}
        rows = info.get("rows") or []
        listings_total = int(info.get("count") or 0)
        truncated = listings_total > len(rows)
        if truncated:
            truncated_items.append(item_id)
        upload = int(info.get("upload") or 0)
        if upload:
            uploads.append(upload)
        left = need
        cost = 0
        used = []
        for row in rows:
            if left <= 0:
                break
            take = min(left, row["qty"])
            if take <= 0:
                continue
            cost += take * row["price"]
            used.append({"price": row["price"], "qty": take, "world": row["world"]})
            left -= take
        filled = need - left
        entry = {
            "item_id": item_id,
            "need": need,
            "filled": filled,
            "short": max(0, left),
            "cost": cost,
            "avg_unit": round(cost / filled) if filled else 0,
            "listings_total": listings_total,
            "listings_truncated": truncated,
            "listings_used": used,
        }
        if filled > 0:
            priced += 1
        if left > 0:
            unresolved.append(
                {"item_id": item_id, "need": need, "filled": filled, "short": left}
            )
        total += cost
        out_items.append(entry)

    if failed_items and len(failed_items) >= len(ids):
        status = "failed"
        ok = False
        fetch_failed = fetch_failed or "全部物品查询失败"
    elif failed_items:
        status = "partial"
        ok = True
        fetch_failed = fetch_failed or "部分物品查询失败"
    else:
        status = "ok"
        ok = True

    return {
        "ok": ok,
        "status": status,
        "region": region,
        "hq": hq,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": out_items,
        "total": total,
        "unresolved": unresolved,
        "failed_items": failed_items,
        "coverage": {
            "items": len(wanted),
            "priced": priced,
            "short_items": len(unresolved),
            "failed_items": len(failed_items),
            "listings_truncated": len(truncated_items),
            "truncated_items": truncated_items,
        },
        "freshness": {
            "oldest_upload": _now_iso(min(uploads)) if uploads else "",
            "newest_upload": _now_iso(max(uploads)) if uploads else "",
        },
        "price_kind": "listing",
        "tax_note": "买齐总价不含税；税为卖家侧扣除",
        "error": fetch_failed,
        "cached": listings_cached is not None,
    }
