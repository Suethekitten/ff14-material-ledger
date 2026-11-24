"""
FF14 材料台账  v0.1（单文件原型）

用途：记录材料分散在各个角色/仓库里的数量，之后按名字广域搜索。
运行：python app.py          （启动本地服务并打开浏览器）
自检：python app.py --selftest
清理示例：python app.py --clear-demo

设计约束（v0.1 刻意保留的）：
  * 零第三方依赖，只用标准库 —— 便于以后打包 EXE 或搬进 bot
  * 所有 SQL 只在本文件内出现
  * 搜索返回结构化 JSON，不返回 HTML 片段（为将来 bot 复用留路）
"""

from __future__ import annotations

import argparse
import csv
import datasource
import http.client
import io
import json
import market
import npc_shop
import os
import re
import secrets
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

VERSION = "0.12.4"
PENDING_LOC = "未确认"   # 删雇员栏位后，库存转进这个暂存位置
CSRF_TOKEN = secrets.token_urlsafe(32)   # 每次启动重新生成；写请求需带 X-CSRF-Token

def _app_dir() -> Path:
    """程序所在目录：正常运行时是脚本目录，打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _res_dir() -> Path:
    """只读资源目录：PyInstaller onefile 会解包到 _MEIPASS。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else _app_dir()


def _pick_data_dir() -> Path:
    """可写数据目录：优先 exe 旁边的 data/（便携），不可写就退回用户目录。"""
    override = os.environ.get("FF14_STOCK_DATA")
    if override:
        return Path(override).expanduser()

    if getattr(sys, "frozen", False):
        portable = _app_dir() / "data"
        try:
            portable.mkdir(parents=True, exist_ok=True)
            probe = portable / ".writable"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return portable
        except OSError:
            pass
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "ff14-stock"

    return _app_dir() / "data"


APP_DIR = _app_dir()
RES_DIR = _res_dir()
STATIC_DIR = RES_DIR / "static"
BUNDLED_ITEMS = RES_DIR / "data" / "items.json"

DATA_DIR = _pick_data_dir()
CACHE_DIR = DATA_DIR / "icon_cache"
DB_PATH = DATA_DIR / "stock.db"
ITEMS_PATH = DATA_DIR / "items.json"
FROZEN = bool(getattr(sys, "frozen", False))
SEED_DEMO = False   # 纯净版默认不灌示例数据，需要时加 --seed-demo
DB_WAS_MISSING = False   # 启动时数据库不存在但手上有快照 → 提示用户去恢复
DB_CREATED_NEW = False   # 本次启动新建了空库（数据可能在旧文件夹）

# ── 页面存活检测：关掉页面后自动退出，免得用户忘了关黑窗口 ──
_HTTPD = None
_DATA_LOCK = None
_PAGES: set[str] = set()
_PAGE_LOCK = threading.Lock()
_QUIT_TIMER = None
QUIT_MIN_UPTIME = 8.0  # 启动后前几秒不自动退出，避免页面还没登记完就误判
AUTO_QUIT = False     # 默认不自动退出；--auto-exit 可开启，--keep-alive 强制关闭
STARTED_AT = datetime.now(timezone.utc)
_LAST_HIT = 0.0       # 最近一次收到请求的时间（monotonic）
_CLOSED_AT = 0.0      # 页面注销的时间

# 启动分段计时：数据初始化、端口绑定、端口就绪、窗口已开
_BOOT_T0 = time.perf_counter()


def boot_stage(label: str) -> None:
    """打印启动阶段耗时（从进程进入 main 开始算的累计值）。"""
    print(f"[启动] {label} {1000 * (time.perf_counter() - _BOOT_T0):.0f}ms", flush=True)


def note_request() -> None:
    """每个请求进来都记一笔：用来判断「页面关了」是不是真的关（而不是跳到了别的页面）。"""
    global _LAST_HIT
    _LAST_HIT = time.monotonic()


def page_open(sid: str) -> None:
    global _QUIT_TIMER
    with _PAGE_LOCK:
        _PAGES.add(sid)
        if _QUIT_TIMER is not None:
            _QUIT_TIMER.cancel()
            _QUIT_TIMER = None
        live = len(_PAGES)
    print(f"[窗口] 页面已登记（在线 {live}）", flush=True)


def page_closed(sid: str) -> None:
    global _CLOSED_AT
    now = time.monotonic()
    with _PAGE_LOCK:
        _PAGES.discard(sid)
        _CLOSED_AT = now
        # 不立刻作废心跳：切页面时旧页先发 closed、新页首个 ping 还没到，
        # 立刻 pop 会让看门狗误判“全关”。改成留一段宽限。
        entry = _PINGS.get(sid)
        if entry is not None:
            vis = entry[1]
            _PINGS[sid] = (now - _timeout_for(vis) + CLOSE_GRACE, vis)
        left = len(_PAGES)
        if left or not AUTO_QUIT:
            print(f"[窗口] 页面已注销（剩 {left}，不退出）", flush=True)
            return
        if LAN_ENABLED:
            print("[窗口] 页面已注销（手机访问开着，不自动退出）", flush=True)
            return
    print(f"[窗口] 页面已注销（宽限 {CLOSE_GRACE:.0f} 秒，等新页心跳）", flush=True)


# ── 心跳式存活检测（v0.12.1）──
# 页面每 3 秒 POST /api/page/ping；连续 PING_TIMEOUT 秒没有心跳就认为全关了。
# 比 page_open/page_closed 配对可靠：切页面、多个页面、页面崩溃都能正确处理。
PING_TIMEOUT_VISIBLE = 12.0   # 前台页面：关掉后 12 秒左右退出
PING_TIMEOUT_HIDDEN = 90.0    # 后台标签页（会被浏览器节流到 ~1 分钟）：给足余量
CLOSE_GRACE = 6.0             # 明确注销后的宽限：> 2 个看门狗周期 + 3 秒心跳间隔
_PINGS: dict[str, tuple[float, bool]] = {}
_SEEN_PAGE = False


def page_ping(sid: str, visible: bool = True) -> int:
    """记录一次心跳，返回当前活跃页面数。

    前台页面超时短（12 秒，关掉很快就退）；
    后台标签页会被浏览器节流到 ~1 分钟一次，给长超时（90 秒）免得误杀。
    """
    global _SEEN_PAGE
    now = time.monotonic()
    with _PAGE_LOCK:
        first = sid not in _PINGS
        _PINGS[sid] = (now, bool(visible))
        _PAGES.add(sid)
        if first:
            _SEEN_PAGE = True
        live = sum(1 for ts, vis in _PINGS.values() if now - ts < _timeout_for(vis))
    if first:
        print(f"[窗口] 页面心跳开始（在线 {live}）", flush=True)
    return live


def _timeout_for(visible: bool) -> float:
    return PING_TIMEOUT_VISIBLE if visible else PING_TIMEOUT_HIDDEN


def _reaper_loop() -> None:
    """后台线程：心跳全部超时就退出。"""
    global _SEEN_PAGE
    while True:
        time.sleep(2.0)
        try:
            if not AUTO_QUIT or LAN_ENABLED or not _SEEN_PAGE:
                continue
            if (datetime.now(timezone.utc) - STARTED_AT).total_seconds() < QUIT_MIN_UPTIME:
                continue
            now = time.monotonic()
            with _PAGE_LOCK:
                stale = [
                    s for s, (ts, vis) in _PINGS.items()
                    if now - ts >= _timeout_for(vis)
                ]
                for sid in stale:
                    _PINGS.pop(sid, None)
                    _PAGES.discard(sid)
                if stale:
                    left = len(_PINGS)
                    print(f"[窗口] 清理超时页面 {len(stale)} 个（在线 {left}）", flush=True)
                alive = [
                    s for s, (ts, vis) in _PINGS.items()
                    if now - ts < _timeout_for(vis)
                ]
            if alive:
                continue
            # 双重确认：可能正好卡在「旧页已注销、新页首个心跳未到」的窗口。
            # 刚有请求进来（含新页首次 ping）就先不退，等下一轮。
            if time.monotonic() - _LAST_HIT < 3.0:
                continue
            time.sleep(1.5)
            with _PAGE_LOCK:
                again = [
                    s for s, (ts, vis) in _PINGS.items()
                    if time.monotonic() - ts < _timeout_for(vis)
                ]
            if again:
                print("[窗口] 复核发现新页心跳，取消退出", flush=True)
                continue
            print("\n[窗口] 心跳超时（所有页面都已关闭），程序自动退出", flush=True)
            server = _HTTPD
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()
            return
        except Exception:  # noqa: BLE001 - 看门狗自己不能挂
            continue

# 数据源固定 commit 在 datasource.py；这里只拼地址。
ITEMS_URL = f"https://cdn.jsdelivr.net/gh/{datasource.SAINT_GH}@{datasource.SAINT_REF}/Item.csv"

# ── 配方展开（v0.12）──
RECIPE_PATH = DATA_DIR / "recipe.json"
RECIPE_URL = f"https://cdn.jsdelivr.net/gh/{datasource.SAINT_GH}@{datasource.SAINT_REF}/Recipe.csv"
RECIPE_LEVEL_URL = f"https://cdn.jsdelivr.net/gh/{datasource.SAINT_GH}@{datasource.SAINT_REF}/RecipeLevelTable.csv"
RECIPE_MAX_DEPTH = 12
RECIPES: dict = {}
RECIPE_META: dict = {"version": "", "recipes": 0, "items": 0}

# ── 手机访问（局域网）──
LAN_ENABLED = False      # 是否允许非本机访问；状态存在 meta 表里
LAN_HOST = "0.0.0.0"     # 始终监听所有网卡，靠请求来源判断放不放行

OWNER = "local"          # 单用户版本固定值，将来 bot 版换成真实用户键
STALE_DAYS = 14          # 超过这么多天未更新就标黄
DEFAULT_PORT = 8765
PORT_TRIES = 12
_PORT = DEFAULT_PORT     # 实际监听端口（可能因占用或 --port 改变）

# 手机访问只读/记账，这些接口只允许本机调用
LOCAL_ONLY_GET = {
    "/api/snapshot/download",
    "/api/data-dir",
    "/api/lan",
}
LOCAL_ONLY_POST = {
    "/api/db/preview",
    "/api/db/restore",
    "/api/snapshot/restore",
    "/api/snapshot/delete",
    "/api/snapshot/cleanup",
    "/api/lan",
    "/api/open-data-dir",
    "/api/data/backup",
    "/api/recipe/rebuild",
    "/api/npc/rebuild",
}

DEFAULT_LOCATIONS = [
    "背包", "鞍囊", "雇员1", "雇员2", "雇员3",
    "雇员4", "雇员5", "部队柜", "房屋",
]

DEMO_PREFIX = "示例"

SEARCH_LIMIT_ITEMS = 40
SEARCH_LIMIT_ROWS = 300
MAX_ITEM_QTY = 9_999_999
JSON_BODY_LIMIT = 2 * 1024 * 1024          # 普通 JSON 请求体上限
DB_UPLOAD_LIMIT = 100 * 1024 * 1024        # 数据库上传上限
UPLOAD_CHUNK = 64 * 1024


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _op_group() -> str:
    """成对操作（搬移/改位置）的分组键；撤销靠它识别，不依赖可编辑备注。"""
    return secrets.token_hex(8)


def days_since(iso_text: str) -> int:
    try:
        dt = datetime.fromisoformat(iso_text)
    except (TypeError, ValueError):
        return 9999
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 86400))


def human_age(days: int) -> str:
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    return f"{days} 天前"


def norm(text: str) -> str:
    """全角转半角、去首尾空白、统一小写。"""
    if not text:
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).strip()


def like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


ERROR_LOG_PATH = DATA_DIR / "error.log"
ERROR_LOG_MAX = 200_000


def log_error(where: str, exc: BaseException) -> None:
    """把异常写进 data\\error.log，超过 200KB 就裁掉前半段。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if ERROR_LOG_PATH.exists() and ERROR_LOG_PATH.stat().st_size > ERROR_LOG_MAX:
            tail = ERROR_LOG_PATH.read_text(encoding="utf-8", errors="replace")[-ERROR_LOG_MAX // 2:]
            ERROR_LOG_PATH.write_text(tail, encoding="utf-8")
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[{now_iso()}] {where}\n")
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass
    try:
        print(f"[错误] {where}: {type(exc).__name__}: {exc}", flush=True)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner      TEXT NOT NULL DEFAULT 'local',
    account    TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,
    world      TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    fc_id      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(owner, account, name, world)
);

CREATE TABLE IF NOT EXISTS fc (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner      TEXT NOT NULL DEFAULT 'local',
    name       TEXT NOT NULL,
    world      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(owner, name, world)
);

CREATE TABLE IF NOT EXISTS stock (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner        TEXT NOT NULL DEFAULT 'local',
    item_id      INTEGER NOT NULL DEFAULT 0,
    item_name    TEXT NOT NULL,
    character_id INTEGER NOT NULL,
    location     TEXT NOT NULL DEFAULT '背包',
    hq           INTEGER NOT NULL DEFAULT 0,
    qty          INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'manual',
    sync_id      TEXT NOT NULL DEFAULT '',
    fc_id        INTEGER NOT NULL DEFAULT 0,
    pending_from TEXT NOT NULL DEFAULT '',
    pending_since TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    UNIQUE(owner, character_id, item_name, location, hq)
);

CREATE TABLE IF NOT EXISTS ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner        TEXT NOT NULL DEFAULT 'local',
    ts           TEXT NOT NULL,
    action       TEXT NOT NULL,
    item_id      INTEGER NOT NULL DEFAULT 0,
    item_name    TEXT NOT NULL DEFAULT '',
    character_id INTEGER NOT NULL,
    location     TEXT NOT NULL DEFAULT '',
    delta        INTEGER NOT NULL DEFAULT 0,
    qty_after    INTEGER,
    note         TEXT NOT NULL DEFAULT '',
    checked      INTEGER NOT NULL DEFAULT 0,
    checked_at   TEXT NOT NULL DEFAULT '',
    void         INTEGER NOT NULL DEFAULT 0,
    hq           INTEGER NOT NULL DEFAULT 0,
    fc_id        INTEGER NOT NULL DEFAULT 0,
    op_group     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_stock_item   ON stock(owner, item_name);
CREATE INDEX IF NOT EXISTS idx_stock_char   ON stock(owner, character_id);
CREATE INDEX IF NOT EXISTS idx_ledger_time  ON ledger(owner, ts);

CREATE TABLE IF NOT EXISTS alias (
    owner      TEXT NOT NULL DEFAULT 'local',
    alias      TEXT NOT NULL,
    item_id    INTEGER NOT NULL,
    item_name  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner, alias)
);

CREATE TABLE IF NOT EXISTS purchase (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner      TEXT NOT NULL DEFAULT 'local',
    item_id    INTEGER NOT NULL DEFAULT 0,
    item_name  TEXT NOT NULL,
    hq         INTEGER NOT NULL DEFAULT 0,
    qty        INTEGER NOT NULL,
    unit_price INTEGER NOT NULL,
    location   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    ledger_id  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stock_safety_line (
    owner      TEXT NOT NULL DEFAULT 'local',
    item_id    INTEGER NOT NULL,
    item_name  TEXT NOT NULL DEFAULT '',
    min_qty    INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, item_id)
);

CREATE TABLE IF NOT EXISTS recipe_list (
    owner      TEXT NOT NULL,
    name       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    PRIMARY KEY (owner, name)
);

CREATE INDEX IF NOT EXISTS idx_stock_owner_item ON stock(owner, item_id);
"""

# 分两步建：先表、再补列、最后索引（旧库可能缺索引引用的列）
_SCHEMA_TABLES = re.sub(r"(?m)^CREATE INDEX.*$", "", SCHEMA)
_SCHEMA_INDEXES = "\n".join(
    line for line in SCHEMA.splitlines() if line.strip().startswith("CREATE INDEX")
)

# 旧库升级：缺这些列可以直接补默认值，不会丢数据
DB_BASE_COLUMNS = {
    "meta": {"key", "value"},
    "character": {"id", "owner", "name", "world", "created_at"},
    "stock": {"id", "owner", "item_name", "character_id",
              "location", "hq", "qty"},
    "ledger": {"id", "owner", "ts", "action", "item_id", "item_name",
               "character_id", "location", "delta"},
    "alias": {"owner", "alias", "item_id", "created_at"},
    "purchase": {"id", "owner", "item_name", "qty",
                 "unit_price", "created_at"},
    "fc": {"id", "owner", "name", "world", "created_at"},
    "stock_safety_line": {"owner", "item_id", "min_qty", "updated_at"},
}

# 迁移完成后运行所需的全部列（缺任何一个安装后都会报错）
DB_ALL_COLUMNS = {
    "meta": {"key", "value"},
    "character": {"id", "owner", "account", "name", "world", "is_default",
                  "fc_id", "created_at"},
    "stock": {"id", "owner", "item_id", "item_name", "character_id", "location",
              "hq", "qty", "source", "sync_id", "fc_id", "pending_from",
              "pending_since", "updated_at"},
    "ledger": {"id", "owner", "ts", "action", "item_id", "item_name",
               "character_id", "location", "delta", "qty_after", "note",
               "checked", "checked_at", "void", "hq", "fc_id", "op_group"},
    "alias": {"owner", "alias", "item_id", "item_name", "created_at"},
    "purchase": {"id", "owner", "item_id", "item_name", "hq", "qty",
                 "unit_price", "location", "created_at", "ledger_id"},
    "fc": {"id", "owner", "name", "world", "created_at"},
    "stock_safety_line": {"owner", "item_id", "item_name", "min_qty", "updated_at"},
    "recipe_list": {"owner", "name", "payload", "revision", "updated_at", "updated_by"},
}

CORE_TABLES = {"character", "stock", "ledger"}


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def normalize_journal() -> None:
    """把数据库从 WAL 切回普通模式。

    WAL 会把最近的写入暂存在 stock.db-wal 里，用户只拷 stock.db 就会丢数据。
    这个工具是单机单人使用，用普通模式更安全：数据永远在主文件里。
    """
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            if mode and str(mode[0]).lower() == "wal":
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
        finally:
            conn.close()
        for suffix in ("-wal", "-shm"):
            side = Path(str(DB_PATH) + suffix)
            if side.exists():
                try:
                    side.unlink()
                except OSError:
                    pass
    except sqlite3.Error:
        pass


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    """给旧库补列；只做可安全迁移的字段。"""
    _ensure_column(conn, "ledger", "checked", "checked INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "ledger", "checked_at", "checked_at TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "ledger", "void", "void INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "ledger", "hq", "hq INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "ledger", "fc_id", "fc_id INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "ledger", "op_group", "op_group TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "ledger", "note", "note TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "character", "account", "account TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "character", "fc_id", "fc_id INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "character", "is_default", "is_default INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "stock", "fc_id", "fc_id INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "stock", "item_id", "item_id INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "stock", "source", "source TEXT NOT NULL DEFAULT 'manual'")
    _ensure_column(conn, "stock", "sync_id", "sync_id TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "stock", "pending_from", "pending_from TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "stock", "pending_since", "pending_since TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "stock", "updated_at", "updated_at TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "purchase", "ledger_id", "ledger_id INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "purchase", "location", "location TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "purchase", "item_id", "item_id INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "purchase", "hq", "hq INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "alias", "item_name", "item_name TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "stock_safety_line", "item_name",
                   "item_name TEXT NOT NULL DEFAULT ''")


def _backfill_legacy_op_groups(conn: sqlite3.Connection) -> int:
    """给旧搬移/改位置流水补 op_group，避免用户改备注后绕过撤销保护。

    只处理可可靠识别的同一秒 take+store 对：同物品同品质，或备注带搬移前缀。
    其它情况由 undo_last 的保守检查兜底。
    """
    rows = conn.execute(
        "SELECT id, ts, action, item_id, hq, delta, note FROM ledger"
        " WHERE op_group = '' AND action IN ('take','store') ORDER BY id"
    ).fetchall()
    by_ts: dict[str, list] = {}
    for r in rows:
        by_ts.setdefault(r["ts"], []).append(r)
    marked = 0
    for items in by_ts.values():
        if len(items) != 2:
            continue
        a, b = items
        if {a["action"], b["action"]} != {"take", "store"}:
            continue
        if not (int(a["delta"]) < 0 < int(b["delta"])
                or int(b["delta"]) < 0 < int(a["delta"])):
            continue
        same_item = int(a["item_id"] or 0) == int(b["item_id"] or 0) \
            and int(a["hq"] or 0) == int(b["hq"] or 0)
        prefixed = any(
            str(x["note"] or "").startswith(("搬入", "搬出", "改：搬入", "改：搬出"))
            for x in (a, b)
        )
        if not (same_item or prefixed):
            continue
        group = f"hist-{min(int(a['id']), int(b['id']))}"
        conn.execute(
            "UPDATE ledger SET op_group = ? WHERE id IN (?, ?)",
            (group, a["id"], b["id"]),
        )
        marked += 1
    return marked


def _migrate_conn(conn: sqlite3.Connection) -> None:
    """对当前连接做结构迁移；用于启动和恢复前的候选副本。"""
    conn.executescript(_SCHEMA_TABLES)
    _ensure_schema_columns(conn)
    conn.executescript(_SCHEMA_INDEXES)
    _backfill_legacy_op_groups(conn)


def init_db() -> None:
    normalize_journal()
    conn = connect()
    try:
        _migrate_conn(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '3')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('app_version', ?)", (VERSION,)
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 物品索引（内置 items.json，缺失时从国服 CSV 构建）
# --------------------------------------------------------------------------

class ItemIndex:
    """item_id -> 名称 / 图标 ID。内存里只留这两样，体积小、匹配快。"""

    def __init__(self) -> None:
        self._by_id: dict[int, dict] = {}
        self._by_name: dict[str, int] = {}
        self._names: list[tuple[int, str]] = []
        self._norm_names: list[tuple[int, str, str]] = []
        self.loaded = False
        self.error = ""
        self.icon_column = ""
        self.count = 0
        self.has_stack = False
        self.has_hq = False

    # ---- 载入 / 构建 ----

    def load(self) -> bool:
        if not ITEMS_PATH.exists() and BUNDLED_ITEMS.exists() and BUNDLED_ITEMS != ITEMS_PATH:
            # 打包版把物品表放在只读资源里，首次运行复制到可写数据目录
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(BUNDLED_ITEMS, ITEMS_PATH)
            except OSError:
                pass
        if not ITEMS_PATH.exists():
            self.error = "缺少 data/items.json（首次启动会自动下载构建）"
            return False
        try:
            raw = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.error = f"items.json 读取失败：{exc}"
            return False

        by_id: dict[int, dict] = {}
        for key, value in raw.items():
            try:
                item_id = int(key)
            except ValueError:
                continue
            if isinstance(value, list):
                name = str(value[0]) if value else ""
                icon = int(value[1] or 0) if len(value) > 1 else 0
                stack = int(value[2] or 0) if len(value) > 2 else 0
                # 第 4 位是 can_be_hq（-1 = 旧表没有这个字段，含义未知）
                can_hq = int(value[3]) if len(value) > 3 else -1
                by_id[item_id] = {"name": name, "icon": icon, "stack": stack,
                                  "can_be_hq": can_hq}
            elif isinstance(value, dict):
                raw_hq = value.get("can_be_hq")
                by_id[item_id] = {
                    "name": str(value.get("name") or ""),
                    "icon": int(value.get("icon") or 0),
                    "stack": int(value.get("stack") or 0),
                    "can_be_hq": -1 if raw_hq is None else int(raw_hq),
                }
        self._by_id = by_id
        self._by_name = {}
        for item_id, data in by_id.items():
            name = data.get("name") or ""
            if name:
                self._by_name.setdefault(norm(name), item_id)
        self._names = [(i, d["name"]) for i, d in by_id.items() if d["name"]]
        self._names.sort(key=lambda pair: pair[1])
        # 预归一化一份，供联想时逐条打分用（5 万条时能省掉大量重复的字符转换）
        self._norm_names = [(i, n, norm(n)) for i, n in self._names]
        self.count = len(by_id)
        self.has_stack = any(d.get("stack") for d in by_id.values())
        self.has_hq = any(int(d.get("can_be_hq", -1)) >= 0 for d in by_id.values())
        self.loaded = True
        self.error = ""
        return True

    def build_from_csv(self, url: str = ITEMS_URL, progress=None) -> bool:
        """下载国服 Item.csv，裁剪成 items.json。只跑一次。"""
        if progress:
            progress("下载物品表（约 19MB，仅首次）…")
        try:
            with urllib.request.urlopen(url, timeout=180) as resp:
                payload = resp.read()
        except Exception as exc:  # noqa: BLE001 - 网络错误一律降级
            self.error = f"物品表下载失败：{exc}"
            return False

        text = payload.decode("utf-8-sig", errors="replace")
        if progress:
            progress(f"已下载 {len(payload) / 1048576:.1f} MB，解析中…")

        reader = csv.reader(io.StringIO(text))
        # SaintCoinach 导出格式：第 1 行是 key 行（key,0,1,2…），
        # 真正的列名在下一行（以 # 开头），再下一行是类型行。两种都要能处理。
        header: list[str] | None = None
        rows: list[list[str]] = []
        for row in reader:
            if not row:
                continue
            if header is None:
                first = row[0].strip()
                if first.startswith("#"):
                    header = [c.lstrip("#").strip() for c in row]
                    continue
                # key 行：首格是 key（或数字），后面几格也是数字
                rest = [c.strip() for c in row[1:6]]
                if first.lower() in ("key", "id") or (
                    rest and all((not c) or c.lstrip("-").replace(".", "", 1).isdigit() for c in rest)
                ):
                    continue
                header = row          # 普通 CSV：自带列名
                continue
            rows.append(row)
        if header is None:
            self.error = "物品表为空"
            return False

        type_words = {"int32", "int16", "int8", "uint32", "uint16", "uint64",
                      "byte", "sbyte", "bool", "single", "str"}
        rows = [r for r in rows if r and r[0].strip() not in type_words]

        key_idx, name_idx, icon_idx, stack_idx, hq_idx = self._pick_columns(header, iter(rows))
        if name_idx is None:
            self.error = "物品表里找不到名称列"
            return False

        result: dict[str, list] = {}
        missing_icon = 0
        missing_stack = 0
        total = 0
        for row in rows:
            if not row:
                continue
            try:
                raw_id = row[key_idx].strip()
                if not raw_id or not raw_id.lstrip("-").isdigit():
                    continue
                item_id = int(raw_id)
            except (IndexError, ValueError):
                continue
            try:
                name = row[name_idx].strip()
            except IndexError:
                continue
            if not name or name.startswith("dummy") or name == "0":
                continue
            icon = 0
            if icon_idx is not None:
                try:
                    icon = int(float(row[icon_idx] or 0))
                except (IndexError, ValueError):
                    icon = 0
            if not icon:
                missing_icon += 1
            stack = 0
            if stack_idx is not None:
                try:
                    stack = int(float(row[stack_idx] or 0))
                except (IndexError, ValueError):
                    stack = 0
            if stack <= 0:
                missing_stack += 1
            can_hq = -1
            if hq_idx is not None:
                try:
                    cell = (row[hq_idx] or "").strip().lower()
                except IndexError:
                    cell = ""
                if cell in ("true", "1"):
                    can_hq = 1
                elif cell in ("false", "0"):
                    can_hq = 0
            result[str(item_id)] = [name, icon, stack, can_hq]
            total += 1

        if not result:
            self.error = "物品表解析后没有任何条目（列名可能变了）"
            return False

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ITEMS_PATH.write_text(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        if missing_icon:
            (DATA_DIR / "unmatched.txt").write_text(
                f"共 {total} 条，其中 {missing_icon} 条缺图标 ID（用占位图）\n",
                encoding="utf-8",
            )
        if progress:
            progress(
                f"物品表已生成：{total} 条（缺图标 {missing_icon} 条，"
                f"缺堆叠上限 {missing_stack} 条）"
            )
        return self.load()

    @staticmethod
    def _pick_columns(
        header: list[str], reader
    ) -> tuple[int, int | None, int | None, int | None]:
        heads = [h.strip().lower() for h in header]
        key_idx = 0
        for cand in ("key", "id", "row_id", "rowid", "#"):
            if cand in heads:
                key_idx = heads.index(cand)
                break

        name_idx = None
        for cand in ("name", "名称", "名前"):
            if cand in heads:
                name_idx = heads.index(cand)
                break
        if name_idx is None:
            # 兜底：看前若干行，取中文占比最高的列
            sample: list[list[str]] = []
            for _ in range(30):
                try:
                    sample.append(next(reader))
                except StopIteration:
                    break
            best_idx, best_score = None, 0.0
            width = max((len(r) for r in sample), default=0)
            for col in range(width):
                cells = [r[col] for r in sample if len(r) > col and r[col].strip()]
                if not cells:
                    continue
                cjk = sum(1 for c in cells if re.search(r"[\u4e00-\u9fff]", c))
                score = cjk / len(cells)
                if score > best_score:
                    best_idx, best_score = col, score
            if best_idx is not None and best_score >= 0.6:
                name_idx = best_idx

        icon_idx = None
        for cand in ("icon", "icon_id", "iconid"):
            if cand in heads:
                icon_idx = heads.index(cand)
                break

        stack_idx = None
        for cand in ("stacksize", "stack_size", "stacksizelimit", "堆叠上限", "stack"):
            if cand in heads:
                stack_idx = heads.index(cand)
                break

        hq_idx = None
        for cand in ("canbehq", "can_be_hq", "canbehq{bool}", "可否hq"):
            if cand in heads:
                hq_idx = heads.index(cand)
                break
        return key_idx, name_idx, icon_idx, stack_idx, hq_idx

    # ---- 查询 ----

    def by_id(self, item_id: int) -> dict | None:
        return self._by_id.get(item_id)

    def can_be_hq(self, item_id: int) -> int:
        """该物品有没有 HQ 版本：1=有，0=没有，-1=物品表没这个字段（旧表）。"""
        info = self._by_id.get(item_id)
        if not info:
            return -1
        return int(info.get("can_be_hq", -1))

    def canonical_name(self, name: str) -> str:
        """把用户输入换成索引里的标准名；找不到就原样返回。"""
        target = norm(name)
        if not target:
            return name
        item_id = self._by_name.get(target)
        if item_id is not None:
            return self._by_id[item_id]["name"]
        return name.strip()

    def id_of(self, name: str) -> int:
        """名字 → 物品 ID；找不到返回 0。"""
        return self._by_name.get(norm(name), 0)

    def suggest(self, query: str, limit: int = 8) -> list[dict]:
        q = norm(query)
        if not q:
            return []
        exact: list[dict] = []
        prefix: list[dict] = []
        contains: list[dict] = []
        for item_id, item_name in self._names:
            name = norm(item_name)
            entry = {"id": item_id, "name": item_name, "icon": self._by_id[item_id]["icon"]}
            if name == q:
                exact.append(entry)
            elif name.startswith(q):
                prefix.append(entry)
            elif q in name:
                contains.append(entry)
            if len(exact) + len(prefix) + len(contains) >= limit * 4:
                break
        merged = (exact + prefix + contains)[:limit]
        return merged


INDEX = ItemIndex()

# 用户自定义别名：游戏里/习惯的叫法 → 真实物品 ID
_alias_cache: dict[str, tuple[int, str]] = {}
_alias_loaded = False


def load_aliases() -> dict[str, tuple[int, str]]:
    global _alias_loaded
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT alias, item_id, item_name FROM alias WHERE owner = ?", (OWNER,)
        ).fetchall()
        _alias_cache.clear()
        for r in rows:
            _alias_cache[norm(r["alias"])] = (r["item_id"], r["item_name"])
        _alias_loaded = True
    finally:
        conn.close()
    return _alias_cache


def aliases_by_item() -> dict[int, list[str]]:
    """item_id → 它挂着的备注名列表（用于联想与命中标注）。"""
    if not _alias_loaded:
        load_aliases()
    out: dict[int, list[str]] = {}
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT alias, item_id FROM alias WHERE owner = ? ORDER BY alias", (OWNER,)
        ).fetchall()
        for r in rows:
            out.setdefault(r["item_id"], []).append(r["alias"])
    finally:
        conn.close()
    return out


def resolve_item(name: str) -> tuple[int, str]:
    """名字 → (item_id, 标准名)。先查别名表，再查物品库。"""
    if not _alias_loaded:
        load_aliases()
    clean = (name or "").strip()
    hit = _alias_cache.get(norm(clean))
    if hit:
        item_id, stored_name = hit
        info = INDEX.by_id(item_id) if INDEX.loaded else None
        return item_id, ((info or {}).get("name") or stored_name or clean)
    if INDEX.loaded:
        item_id = INDEX.id_of(clean)
        if item_id:
            return item_id, INDEX.by_id(item_id)["name"]
    return 0, clean


def set_alias(alias: str, item_id: int) -> dict:
    """把一个叫法绑定到真实物品 ID，并把库存/采购批次合并到标准名。"""
    alias = (alias or "").strip()
    if not alias:
        return {"ok": False, "error": "别名为空"}
    if item_id <= 0:
        return {"ok": False, "error": "物品 ID 不合法"}
    info = INDEX.by_id(item_id) if INDEX.loaded else None
    # 物品库里没有这一条也允许绑定：恰好「铁锭」这种情况就要靠手动指定 ID
    stored_name = (info or {}).get("name") or alias
    conn = connect()
    ts = now_iso()
    merged = renamed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO alias(owner, alias, item_id, item_name, created_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(owner, alias) DO UPDATE SET"
            " item_id = excluded.item_id, item_name = excluded.item_name",
            (OWNER, alias, item_id, stored_name, ts),
        )
        if stored_name != alias:
            rows = conn.execute(
                "SELECT id, character_id, location, hq, qty FROM stock"
                " WHERE owner = ? AND item_name = ?",
                (OWNER, alias),
            ).fetchall()
            for row in rows:
                target = conn.execute(
                    "SELECT id FROM stock WHERE owner = ? AND character_id = ?"
                    " AND item_name = ? AND location = ? AND hq = ? AND id <> ?",
                    (OWNER, row["character_id"], stored_name, row["location"],
                     row["hq"], row["id"]),
                ).fetchone()
                if target:
                    conn.execute(
                        "UPDATE stock SET qty = qty + ?, item_id = ?, updated_at = ? WHERE id = ?",
                        (int(row["qty"]), item_id, ts, target["id"]),
                    )
                    conn.execute("DELETE FROM stock WHERE id = ?", (row["id"],))
                    merged += 1
                else:
                    conn.execute(
                        "UPDATE stock SET item_name = ?, item_id = ?, updated_at = ? WHERE id = ?",
                        (stored_name, item_id, ts, row["id"]),
                    )
                    renamed += 1
        else:
            conn.execute(
                "UPDATE stock SET item_id = ? WHERE owner = ? AND item_name = ?",
                (item_id, OWNER, alias),
            )
        conn.execute(
            "UPDATE purchase SET item_name = ?, item_id = ? WHERE owner = ? AND item_name = ?",
            (stored_name, item_id, OWNER, alias),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        log_error("绑定别名", exc)
        return {"ok": False, "error": f"绑定失败：{exc}"}
    finally:
        conn.close()
    load_aliases()
    return {
        "ok": True,
        "alias": alias,
        "item_id": item_id,
        "item_name": stored_name,
        "merged": merged,
        "renamed": renamed,
    }


def lookup_item_by_id(item_id: int) -> dict:
    """按 ID 查物品：本地表里有名字就用本地的，同时去 XIVAPI 取英文名与图标。"""
    info = INDEX.by_id(item_id) if INDEX.loaded else None
    result: dict = {
        "item_id": item_id,
        "in_table": bool(info),
        "name": (info or {}).get("name", ""),
        "name_en": "",
        "icon_url": "",
        "error": "",
    }
    conn = None
    try:
        conn = http.client.HTTPSConnection("beta.xivapi.com", timeout=6.0)
        conn.request(
            "GET",
            f"/api/1/sheet/Item/{item_id}?fields=Name,Icon",
            headers={"User-Agent": f"ff14-stock/{VERSION}", "Connection": "keep-alive"},
        )
        resp = conn.getresponse()
        body = resp.read()
        if resp.status == 200:
            fields = (json.loads(body.decode("utf-8")).get("fields") or {})
            result["name_en"] = fields.get("Name") or ""
            icon = fields.get("Icon") or {}
            result["icon_url"] = tex_to_url(icon.get("path_hr1") or icon.get("path") or "")
        else:
            result["error"] = f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return result


def search_items_en(query: str, limit: int = 8) -> list[dict]:
    """按英文名搜物品（走 XIVAPI）。用于「不知道 ID」时反查，中文表没收录的名字靠它。"""
    q = (query or "").strip()
    if not q or not re.search(r"[A-Za-z]", q):
        return []
    path = (
        "/api/1/search?sheets=Item&fields=Name,Icon&query="
        + quote(f'Name~"{q}"')
    )
    out: list[dict] = []
    conn = None
    try:
        conn = http.client.HTTPSConnection("beta.xivapi.com", timeout=6.0)
        conn.request(
            "GET", path,
            headers={"User-Agent": f"ff14-stock/{VERSION}", "Connection": "keep-alive"},
        )
        resp = conn.getresponse()
        body = resp.read()
        if resp.status == 200:
            data = json.loads(body.decode("utf-8"))
            for item in (data.get("results") or [])[:limit]:
                fields = item.get("fields") or {}
                icon = fields.get("Icon") or {}
                out.append(
                    {
                        "id": item.get("row_id"),
                        "name_en": fields.get("Name") or "",
                        "score": round(float(item.get("score") or 0), 2),
                        "icon_url": tex_to_url(icon.get("path_hr1") or icon.get("path") or ""),
                    }
                )
    except Exception:  # noqa: BLE001 - 查不到就算了，界面还有手动 ID 这条路
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return out


def unmapped_items() -> list[dict]:
    """库存里存在、但物品库（或别名）认不出名字的材料。"""
    if not _alias_loaded:
        load_aliases()
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT item_name, COALESCE(SUM(qty), 0) AS total, COUNT(*) AS row_count
            FROM stock
            WHERE owner = ? AND item_id = 0
            GROUP BY item_name
            ORDER BY total DESC, item_name
            """,
            (OWNER,),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        name = r["item_name"]
        if norm(name) in _alias_cache:
            continue
        if resolve_item(name)[0]:
            continue
        out.append({"name": name, "total": r["total"], "row_count": r["row_count"]})
    return out


def icon_url(icon_id: int) -> str:
    if not icon_id:
        return ""
    folder = f"{(icon_id // 1000) * 1000:06d}"
    file_name = f"{icon_id:06d}"
    return (
        "https://beta.xivapi.com/api/1/asset"
        f"?path=ui/icon/{folder}/{file_name}_hr1.tex&format=png"
    )


# --------------------------------------------------------------------------
# 名称匹配打分：精确 > 前缀 > 结尾 > 包含，按多出来的长度与出现位置扣分
# --------------------------------------------------------------------------

def _score_norm(q: str, t: str) -> int:
    """两端都已是归一化文本时的打分，避免重复转换。"""
    if not q or not t:
        return 0
    if t == q:
        return 1000
    ln, lq = len(t), len(q)
    extra = max(0, ln - lq) / max(ln, 1)
    len_pen = int(extra * 120)
    if t.startswith(q):
        return max(1, 900 - len_pen)
    pos = t.find(q)
    if pos >= 0:
        pos_pen = int(pos / max(ln, 1) * 40)
        if t.endswith(q):
            return max(1, 850 - len_pen)
        return max(1, 800 - len_pen - pos_pen)
    return 0


def match_score(query: str, text: str) -> int:
    """0 = 不匹配。分值越高越接近。"""
    return _score_norm(norm(query), norm(text))


def _lev_within_one(a: str, b: str) -> bool:
    """只在长度差 ≤1 时调用；判断编辑距离是否 ≤1（低分兜底用）。"""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = 0
    diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        diff += 1
        if diff > 1:
            return False
        if la == lb:
            i += 1
            j += 1
        else:
            j += 1
    return True


def best_match(query: str, name: str, aliases: list[str]) -> tuple[int, str]:
    """在标准名和各备注名里取最高分；返回 (分数, 命中的备注名或空串)。"""
    score = match_score(query, name)
    hit = ""
    for alias in aliases:
        s = int(match_score(query, alias) * 0.95)  # 备注名略低于标准名
        if s > score:
            score, hit = s, alias
    return score, hit


def suggest_items(query: str, limit: int = 8) -> list[dict]:
    """联想候选：标准名 + 备注名一起打分排序；全无结果时用编辑距离兜底。"""
    q = norm(query)
    if not q or not INDEX.loaded:
        return []
    alias_map = aliases_by_item()

    scored: list[tuple[int, int, str, int, str]] = []
    for item_id, name, nn in INDEX._norm_names:  # noqa: SLF001 - 同模块内部使用
        score = _score_norm(q, nn)
        hit = ""
        for alias in alias_map.get(item_id, ()):
            alias_score = int(match_score(q, alias) * 0.95)
            if alias_score > score:
                score, hit = alias_score, alias
        if score:
            scored.append((score, len(name), name, item_id, hit))

    if not scored:
        # 低分兜底：允许打错一个字，排在正常结果之后
        for item_id, name, nn in INDEX._norm_names:  # noqa: SLF001
            if abs(len(nn) - len(q)) <= 1 and _lev_within_one(q, nn):
                scored.append((400, len(name), name, item_id, ""))

    scored.sort(key=lambda row: (-row[0], row[1], row[2]))

    out: list[dict] = []
    for score, _ln, name, item_id, hit in scored[:limit]:
        info = INDEX.by_id(item_id) or {}
        out.append(
            {
                "id": item_id,
                "name": name,
                "icon": icon_url(info.get("icon", 0)),
                "score": score,
                "stack": stack_of(item_id),
                "can_be_hq": INDEX.can_be_hq(item_id),
                "matched_alias": hit,
                "aliases": alias_map.get(item_id, [])[:4],
            }
        )
    return out


# --------------------------------------------------------------------------
# 业务逻辑
# --------------------------------------------------------------------------

def list_characters() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.account, c.name, c.world, c.fc_id,
                   COUNT(s.id) AS kinds, COALESCE(SUM(s.qty), 0) AS total
            FROM character c
            LEFT JOIN stock s ON s.character_id = c.id AND s.owner = c.owner
                 AND s.location <> ?
            WHERE c.owner = ?
            GROUP BY c.id
            ORDER BY c.account, c.name
            """,
            (PENDING_LOC, OWNER),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def state() -> dict:
    conn = connect()
    try:
        chars = conn.execute(
            "SELECT COUNT(*) AS n FROM character WHERE owner = ?", (OWNER,)
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS q FROM stock"
            " WHERE owner = ? AND location <> ?",
            (OWNER, PENDING_LOC),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(qty), 0) AS q FROM stock"
            " WHERE owner = ? AND location = ?",
            (OWNER, PENDING_LOC),
        ).fetchone()
        demo = conn.execute(
            "SELECT COUNT(*) AS n FROM character WHERE owner = ? AND account LIKE ?",
            (OWNER, DEMO_PREFIX + "%"),
        ).fetchone()["n"]
        snaps = 0
        if SNAP_DIR.exists():
            snaps = sum(1 for _ in SNAP_DIR.glob("*.db"))
        # 每个角色用过第几个雇员：前端据此生成「雇员1..N」下拉，老用户不用手动加
        retainer_max: dict[str, int] = {}
        for r in conn.execute(
            "SELECT character_id, location FROM stock"
            " WHERE owner = ? AND location LIKE '雇员%'",
            (OWNER,),
        ):
            m = re.match(r"^雇员(\d+)$", (r["location"] or "").strip())
            if not m:
                continue
            cid = str(r["character_id"])
            slot = int(m.group(1))
            if slot > retainer_max.get(cid, 0):
                retainer_max[cid] = slot
        return {
            "version": VERSION,
            "csrf_token": CSRF_TOKEN,
            "character_count": chars,
            "stock_rows": rows["n"],
            "total_qty": rows["q"],
            "has_demo": demo > 0,
            "items_loaded": INDEX.count,
            "items_error": INDEX.error,
            "items_file": str(ITEMS_PATH),
            "stale_days": STALE_DAYS,
            "locations": DEFAULT_LOCATIONS,
            "data_dir": str(DATA_DIR),
            "db_size": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            "snapshot_count": snaps,
            "recovered_hint": bool(DB_WAS_MISSING and snaps),
            "db_created_new": bool(DB_CREATED_NEW),
            "retainer_max": retainer_max,
            "pending_rows": pending["n"],
            "pending_qty": pending["q"],
        }
    finally:
        conn.close()


SAFETY_MAX_QTY = 9_999_999


def _safety_positive_int(value) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _safety_current_qty(conn: sqlite3.Connection) -> tuple[dict[int, int], int]:
    """按 item_id 聚合正常库存，排除未确认；历史 item_id=0 的行按名称补解析。"""
    qty: dict[int, int] = {}
    for row in conn.execute(
        "SELECT item_id, COALESCE(SUM(qty), 0) AS q FROM stock"
        " WHERE owner = ? AND location <> ? AND item_id > 0 GROUP BY item_id",
        (OWNER, PENDING_LOC),
    ):
        qty[int(row["item_id"])] = int(row["q"] or 0)
    unresolved = 0
    for row in conn.execute(
        "SELECT item_name, COALESCE(SUM(qty), 0) AS q FROM stock"
        " WHERE owner = ? AND location <> ? AND item_id = 0 GROUP BY item_name",
        (OWNER, PENDING_LOC),
    ):
        item_id, _ = resolve_item(row["item_name"] or "")
        if item_id:
            qty[item_id] = qty.get(item_id, 0) + int(row["q"] or 0)
        else:
            unresolved += 1
    return qty, unresolved


def _safety_line(item_id: int, item_name: str, min_qty: int,
                 updated_at: str, qty: dict[int, int]) -> dict:
    current = int(qty.get(item_id, 0))
    if current <= 0:
        status = "empty"
    elif current < min_qty:
        status = "low"
    else:
        status = "ok"
    info = INDEX.by_id(item_id) if INDEX.loaded else None
    return {
        "item_id": item_id,
        "name": (info or {}).get("name") or item_name or str(item_id),
        "min_qty": int(min_qty),
        "current_qty": current,
        "short_qty": max(0, int(min_qty) - current),
        "status": status,
        "updated_at": updated_at or "",
    }


def safety_lines() -> dict:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT item_id, item_name, min_qty, updated_at FROM stock_safety_line"
            " WHERE owner = ? ORDER BY item_id",
            (OWNER,),
        ).fetchall()
        qty, unresolved = _safety_current_qty(conn)
    finally:
        conn.close()
    lines = [
        _safety_line(int(r["item_id"]), r["item_name"] or "", int(r["min_qty"]),
                     r["updated_at"] or "", qty)
        for r in rows
    ]
    rank = {"empty": 0, "low": 1, "ok": 2}
    lines.sort(key=lambda x: (rank[x["status"]], -x["short_qty"], x["name"]))
    return {
        "ok": True,
        "scope": "all_confirmed_stock_nq_hq",
        "lines": lines,
        "low_count": sum(1 for x in lines if x["status"] != "ok"),
        "unresolved": unresolved,
    }


def _safety_invalid(message: str) -> dict:
    return {"ok": False, "invalid": True, "error": message}


def safety_line_set(item_id, min_qty) -> dict:
    item_id = _safety_positive_int(item_id)
    min_qty = _safety_positive_int(min_qty)
    if not item_id:
        return _safety_invalid("item_id 必须是正整数")
    if not min_qty:
        return _safety_invalid("安全线必须是正整数；取消设置请用删除接口")
    if min_qty > SAFETY_MAX_QTY:
        return _safety_invalid("安全线数值过大")
    if not INDEX.loaded:
        return _safety_invalid("物品表未就绪，暂时无法设置安全线")
    info = INDEX.by_id(item_id)
    if not info:
        return _safety_invalid("物品表里没有这个 item_id，请先完成物品匹配")

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO stock_safety_line(owner, item_id, item_name, min_qty, updated_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(owner, item_id) DO UPDATE SET"
            " item_name = excluded.item_name, min_qty = excluded.min_qty,"
            " updated_at = excluded.updated_at",
            (OWNER, item_id, info["name"], min_qty, now_iso()),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        log_error("设置安全线", exc)
        return {"ok": False, "error": f"保存失败：{exc}"}
    finally:
        conn.close()

    payload = safety_lines()
    line = next((x for x in payload["lines"] if x["item_id"] == item_id), None)
    return {"ok": True, "line": line, "low_count": payload["low_count"]}


def safety_line_delete(item_id) -> dict:
    item_id = _safety_positive_int(item_id)
    if not item_id:
        return _safety_invalid("item_id 必须是正整数")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM stock_safety_line WHERE owner = ? AND item_id = ?",
            (OWNER, item_id),
        )
        deleted = cur.rowcount > 0
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        log_error("取消安全线", exc)
        return {"ok": False, "error": f"删除失败：{exc}"}
    finally:
        conn.close()
    payload = safety_lines()
    return {
        "ok": True,
        "item_id": item_id,
        "deleted": deleted,
        "low_count": payload["low_count"],
    }


def _valid_item_qty(value, allow_zero: bool = False):
    """数量入口统一校验：只接受 JSON 整数，范围 1…9,999,999（允许清零时含 0）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    low = 0 if allow_zero else 1
    return value if low <= value <= MAX_ITEM_QTY else None


def cost_overview() -> dict:
    """全量成本合计：不受 /api/search 的 40/200 截断影响。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT item_name, hq, COALESCE(SUM(qty), 0) AS q FROM stock"
            " WHERE owner = ? AND location <> ? GROUP BY item_name, hq",
            (OWNER, PENDING_LOC),
        ).fetchall()
    finally:
        conn.close()
    cost_map = purchase_map()
    total = 0.0
    with_cost = 0
    for r in rows:
        qty = int(r["q"] or 0)
        cost = cost_map.get((r["item_name"], int(r["hq"] or 0)))
        if not cost or not cost.get("avg_cost"):
            continue
        with_cost += 1
        total += qty * float(cost["avg_cost"])
    return {
        "ok": True,
        "total_value": round(total, 2),
        "items_total": len(rows),
        "items_with_cost": with_cost,
        "items_without_cost": len(rows) - with_cost,
        "basis": "purchase_weighted_avg",
        "generated_at": now_iso(),
    }


def search(
    query: str = "",
    account: str = "",
    character: str = "",
    location: str = "",
    hq: str = "",
    limit_items: int = 0,
) -> dict:
    """广域搜索：材料名 / 角色 / 账号 / 服务器 / 位置 一次匹配，结果分层。

    limit_items 控制最多返回多少「种」材料（0 用默认 40，最大 200）；
    行数上限随它一起放宽，否则点两次「显示更多」就会被行数卡住。
    """
    item_limit = SEARCH_LIMIT_ITEMS if not limit_items else max(1, min(int(limit_items), 200))
    row_limit = max(SEARCH_LIMIT_ROWS, item_limit * 8)
    q = norm(query)
    clauses = ["s.owner = ?", "s.location <> ?"]
    params: list = [OWNER, PENDING_LOC]

    if q:
        pat = like_pattern(q)
        clauses.append(
            "(s.item_name LIKE ? ESCAPE '\\' OR s.location LIKE ? ESCAPE '\\'"
            " OR c.name LIKE ? ESCAPE '\\' OR c.account LIKE ? ESCAPE '\\'"
            " OR c.world LIKE ? ESCAPE '\\'"
            " OR s.item_id IN (SELECT item_id FROM alias"
            "                 WHERE owner = ? AND alias LIKE ? ESCAPE '\\'))"
        )
        params.extend([pat, pat, pat, pat, pat, OWNER, pat])
    if account:
        clauses.append("c.account = ?")
        params.append(account)
    if character:
        clauses.append("c.name = ?")
        params.append(character)
    if location:
        clauses.append("s.location = ?")
        params.append(location)
    if hq == "1":
        clauses.append("s.hq = 1")
    elif hq == "0":
        clauses.append("s.hq = 0")

    where = " AND ".join(clauses)

    conn = connect()
    try:
        total_rows = conn.execute(
            f"SELECT COUNT(*) AS n FROM stock s JOIN character c ON c.id = s.character_id"
            f" WHERE {where}",
            params,
        ).fetchone()["n"]

        rows = conn.execute(
            f"""
            SELECT s.id AS stock_id, s.item_id, s.item_name, s.location, s.hq, s.qty,
                   s.updated_at, c.id AS character_id, c.name AS character_name,
                   c.world, c.account
            FROM stock s
            JOIN character c ON c.id = s.character_id
            WHERE {where}
            ORDER BY s.updated_at DESC
            LIMIT {row_limit}
            """,
            params,
        ).fetchall()

        items: dict[tuple, dict] = {}
        for r in rows:
            key = (r["item_name"], r["hq"])
            # 老数据里 item_id 可能是 0（物品表晚于录入才就绪），这里按名字补解析
            resolved_id = r["item_id"] or resolve_item(r["item_name"])[0]
            bucket = items.setdefault(
                key,
                {
                    "name": r["item_name"],
                    "item_id": resolved_id,
                    "icon": icon_url((INDEX.by_id(resolved_id) or {}).get("icon", 0)),
                    "stack": stack_of(resolved_id),
                    "hq": r["hq"],
                    "total": 0,
                    "holders": [],
                },
            )
            bucket["total"] += r["qty"]
            age = days_since(r["updated_at"])
            bucket["holders"].append(
                {
                    "stock_id": r["stock_id"],
                    "character_id": r["character_id"],
                    "character": r["character_name"],
                    "account": r["account"],
                    "world": r["world"],
                    "location": r["location"],
                    "qty": r["qty"],
                    "hq": r["hq"],
                    "age_days": age,
                    "age_text": human_age(age),
                    "stale": age > STALE_DAYS,
                }
            )

        alias_map = aliases_by_item()
        cost_map = purchase_map()
        for item in items.values():
            item["aliases"] = alias_map.get(item["item_id"], [])[:4]
            cost = cost_map.get((item["name"], item["hq"]))
            if cost:
                item["purchase_count"] = cost["count"]
                item["avg_cost"] = cost["avg_cost"]
                item["spent"] = round(cost["avg_cost"] * item["total"], 2)
            else:
                item["purchase_count"] = 0
                item["avg_cost"] = 0
                item["spent"] = 0
            if q:
                score, hit = best_match(q, item["name"], item["aliases"])
                if not score:
                    # 只靠角色/位置命中的行：给基础分，排在名称命中之后
                    score = 300
                item["match_score"] = score
                item["matched_alias"] = hit
            else:
                item["match_score"] = 0
                item["matched_alias"] = ""

        all_items = sorted(
            items.values(),
            key=lambda x: (-x["match_score"], -x["total"], x["name"]),
        )
        total_items = len(all_items)
        item_list = all_items[:item_limit]
        for item in item_list:
            item["holders"].sort(key=lambda h: -h["qty"])

        # 角色维度的命中（按名字/账号/服务器）
        char_params = [OWNER]
        char_clause = "c.owner = ?"
        if q:
            char_clause += (
                " AND (c.name LIKE ? ESCAPE '\\' OR c.account LIKE ? ESCAPE '\\'"
                " OR c.world LIKE ? ESCAPE '\\')"
            )
            pat = like_pattern(q)
            char_params.extend([pat, pat, pat])
        if account:
            char_clause += " AND c.account = ?"
            char_params.append(account)
        characters = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT c.id, c.name, c.world, c.account,
                       COUNT(s.id) AS kinds, COALESCE(SUM(s.qty), 0) AS total
                FROM character c
                LEFT JOIN stock s ON s.character_id = c.id AND s.owner = c.owner
                WHERE {char_clause}
                GROUP BY c.id
                ORDER BY total DESC
                """,
                char_params,
            ).fetchall()
        ]

        # 空结果分两类：物品库里有但没有库存 / 物品库里也没有
        suggestions: list[dict] = []
        hint = ""
        if not item_list and q:
            suggestions = suggest_items(q, 6)
            hint = "no_stock" if suggestions else "no_item"
        return {
            "query": query,
            "total_rows": total_rows,
            "total_items": total_items,
            "truncated": total_items > len(item_list),
            "items": item_list,
            "characters": characters,
            "suggestions": suggestions,
            "hint": hint,
            "stats": state(),
        }
    finally:
        conn.close()


def stock_lookup(character_id: int, location: str, item_name: str, hq: int) -> dict:
    """录入时的实时预览：这一格现在有多少，保存后会变成多少。"""
    query = (item_name or "").strip()
    item_id, canonical = resolve_item(query)
    matched = "exact"
    if item_id and norm(canonical) != norm(query):
        matched = "alias"
    elif not item_id:
        matched = "unbound"

    row = None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, qty, hq, updated_at FROM stock"
            " WHERE owner = ? AND character_id = ? AND item_name = ?"
            " AND location = ? AND hq = ?",
            (OWNER, character_id, canonical, location or "背包", 1 if hq else 0),
        ).fetchone()
    finally:
        conn.close()

    result = {
        "ok": True,
        "found": bool(row),
        "stock_id": row["id"] if row else 0,
        "qty": row["qty"] if row else 0,
        "hq": row["hq"] if row else (1 if hq else 0),
        "stack": stack_of(item_id),
        "updated_at": row["updated_at"] if row else "",
        "item": {"id": item_id, "name": canonical, "matched": matched},
    }
    return result


def _add_purchase(
    conn: sqlite3.Connection,
    item_name: str,
    item_id: int,
    hq: int,
    qty: int,
    unit_price: int,
    location: str,
    ts: str,
    ledger_id: int = 0,
) -> dict:
    """记一条买入批次（同一连接，调用方负责提交）。ledger_id 用于撤销时回查。"""
    cur = conn.execute(
        "INSERT INTO purchase(owner, item_id, item_name, hq, qty, unit_price,"
        " location, created_at, ledger_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (OWNER, item_id, item_name, hq, qty, unit_price, location, ts, int(ledger_id or 0)),
    )
    return {
        "id": cur.lastrowid,
        "qty": qty,
        "unit_price": unit_price,
        "cost": qty * unit_price,
    }


def purchase_rows(item_name: str, hq: int = 0) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, item_id, item_name, hq, qty, unit_price, location, created_at"
            " FROM purchase WHERE owner = ? AND item_name = ? AND hq = ?"
            " ORDER BY created_at",
            (OWNER, item_name, int(hq),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        qty = int(r["qty"])
        price = int(r["unit_price"])
        out.append(
            {
                "id": r["id"],
                "item_id": r["item_id"],
                "item_name": r["item_name"],
                "hq": r["hq"],
                "qty": qty,
                "unit_price": price,
                "cost": qty * price,
                "location": r["location"],
                "created_at": r["created_at"],
                "date_text": (r["created_at"] or "")[:10],
            }
        )
    return out


def purchase_summary(item_name: str, hq: int = 0) -> dict:
    rows = purchase_rows(item_name, hq)
    total_qty = sum(r["qty"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    avg = round(total_cost / total_qty, 2) if total_qty else 0
    return {
        "rows": rows,
        "count": len(rows),
        "total_qty": total_qty,
        "total_cost": total_cost,
        "avg_cost": avg,
    }


def purchase_map() -> dict:
    """一次查出所有材料的加权均价，给搜索结果批量用。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT item_name, hq, COUNT(*) AS n, COALESCE(SUM(qty), 0) AS q,"
            " COALESCE(SUM(qty * unit_price), 0) AS cost"
            " FROM purchase WHERE owner = ? GROUP BY item_name, hq",
            (OWNER,),
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        q = int(r["q"])
        cost = int(r["cost"])
        out[(r["item_name"], r["hq"])] = {
            "count": int(r["n"]),
            "total_qty": q,
            "total_cost": cost,
            "avg_cost": round(cost / q, 2) if q else 0,
        }
    return out


def set_cost(item_name: str, hq: int, unit_price: int) -> dict:
    """给没有批次的库存直接设一个基线单价（等同于一条「基线」批次）。"""
    name = (item_name or "").strip()
    if not name:
        return {"ok": False, "error": "缺少材料名"}
    if unit_price < 0:
        return {"ok": False, "error": "单价不能是负数"}
    item_id, canonical = resolve_item(name)
    hq = 1 if hq else 0
    conn = connect()
    try:
        total = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS q FROM stock"
            " WHERE owner = ? AND item_name = ? AND hq = ?",
            (OWNER, canonical, hq),
        ).fetchone()["q"]
        batches = conn.execute(
            "SELECT COUNT(*) AS n FROM purchase"
            " WHERE owner = ? AND item_name = ? AND hq = ?",
            (OWNER, canonical, hq),
        ).fetchone()["n"]
        if batches:
            return {
                "ok": False,
                "error": f"已有 {batches} 条采购批次，不能设基线；如需改价请先删除批次",
            }
        conn.execute(
            "DELETE FROM purchase WHERE owner = ? AND item_name = ? AND hq = ?",
            (OWNER, canonical, hq),
        )
        if total > 0:
            conn.execute(
                "INSERT INTO purchase(owner, item_id, item_name, hq, qty, unit_price,"
                " location, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (OWNER, item_id, canonical, hq, int(total), unit_price, "基线", now_iso()),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "item_name": canonical,
        "hq": hq,
        "unit_price": unit_price,
        "qty": int(total),
        "avg_cost": unit_price,
    }


def delete_purchase(purchase_id: int) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM purchase WHERE id = ? AND owner = ?", (purchase_id, OWNER)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "找不到这条买入记录"}
        conn.execute("DELETE FROM purchase WHERE id = ?", (purchase_id,))
        conn.commit()
        return {"ok": True, "id": purchase_id}
    finally:
        conn.close()


def _fc_anchor(conn: sqlite3.Connection, fc_id: int) -> int:
    """部队柜的“记账代表”：部队里 id 最小的成员。

    部队柜是共享中转站，不挂在具体某个人名下；统一记在代表角色上，
    这样不管谁搬进去，都只改同一行，不会出现重复。
    """
    if not fc_id:
        return 0
    row = conn.execute(
        "SELECT id FROM character WHERE owner = ? AND fc_id = ? ORDER BY id LIMIT 1",
        (OWNER, fc_id),
    ).fetchone()
    return int(row["id"]) if row else 0


def record_stock(
    character_id: int,
    item_name: str,
    location: str,
    qty: int,
    hq: int = 0,
    allow_unbound: bool = False,
    unit_price: int = 0,
    mode: str = "set",
) -> dict:
    """写入一条库存。同一 (角色, 材料, 位置, HQ) 默认覆盖式更新。

    mode="set"（默认）：qty 是改完后的总数，旧行为；
    mode="add"：qty 是本次增量，在现有数量上累加，行不存在则新建。

    allow_unbound=True 是安全路口：转储数据还没收录的新物品可以先用原名记下来，
    item_id 留 0，之后在「未收录的材料」里补绑。
    """
    mode = (mode or "set").strip().lower()
    if mode not in ("set", "add"):
        return {"ok": False, "error": "mode 只能是 set 或 add"}
    name = (item_name or "").strip()
    if not name:
        return {"ok": False, "error": "材料名不能为空"}
    if not location:
        return {"ok": False, "error": "位置不能为空"}
    qty = _valid_item_qty(qty, allow_zero=(mode == "set"))
    if qty is None:
        return {"ok": False, "invalid": True,
                "error": f"数量必须是 0–{MAX_ITEM_QTY} 的整数" if mode == "set"
                else f"数量必须是 1–{MAX_ITEM_QTY} 的整数"}

    item_id, canonical = resolve_item(name)

    if INDEX.loaded and not item_id and not allow_unbound:
        return {
            "ok": False,
            "hint": "pick",
            "error": f"库里没有「{name}」这个名字",
            "suggestions": [s["name"] for s in suggest_items(name, 6)],
        }

    conn = connect()
    try:
        chr_row = conn.execute(
            "SELECT id, name, world, account, fc_id FROM character WHERE id = ? AND owner = ?",
            (character_id, OWNER),
        ).fetchone()
        if chr_row is None:
            return {"ok": False, "error": "角色不存在"}

        # 位置白名单；老数据里的自定义位置（库存里已有）继续兼容
        legacy_location = conn.execute(
            "SELECT 1 FROM stock WHERE owner = ? AND location = ? LIMIT 1",
            (OWNER, location),
        ).fetchone()
        if not legacy_location and not _valid_target_location(
            conn, character_id, location, set(DEFAULT_LOCATIONS)
        ):
            return {"ok": False, "error": f"位置不合法：{location}"}

        ts = now_iso()
        # 部队柜的库存带上部队归属，方便归并与解散时识别
        stock_fc = 0
        if location == "部队柜" and (chr_row["fc_id"] or 0):
            stock_fc = int(chr_row["fc_id"])

        existing = conn.execute(
            "SELECT id, qty FROM stock WHERE owner = ? AND character_id = ?"
            " AND item_name = ? AND location = ? AND hq = ?",
            (OWNER, character_id, canonical, location, hq),
        ).fetchone()
        if existing and mode == "add" and int(existing["qty"]) + qty > MAX_ITEM_QTY:
            return {"ok": False, "invalid": True,
                    "error": f"累加后超过上限 {MAX_ITEM_QTY}"}
        if existing:
            if mode == "set" and qty == existing["qty"]:
                # 数量没变：不写流水、不打快照，避免"覆盖 +0"这种噪音
                return {
                    "ok": True,
                    "unchanged": True,
                    "mode": mode,
                    "item_name": canonical,
                    "qty": qty,
                    "character": f"{chr_row['name']}@{chr_row['world']}" if chr_row["world"] else chr_row["name"],
                    "location": location,
                    "total": conn.execute(
                        "SELECT COALESCE(SUM(qty), 0) AS q FROM stock WHERE owner = ? AND item_name = ?",
                        (OWNER, canonical),
                    ).fetchone()["q"],
                }
            if mode == "add":
                # 原子累加：UPDATE ... qty = qty + ?，避免两个窗口同时“先读后写”丢更新
                conn.execute(
                    "UPDATE stock SET qty = qty + ?, item_id = ?, fc_id = ?, updated_at = ?"
                    " WHERE id = ?",
                    (qty, item_id, stock_fc, ts, existing["id"]),
                )
                qty_after = int(conn.execute(
                    "SELECT qty FROM stock WHERE id = ?", (existing["id"],)
                ).fetchone()["qty"])
                delta = qty
                action = "add"
            else:
                conn.execute(
                    "UPDATE stock SET qty = ?, item_id = ?, fc_id = ?, updated_at = ? WHERE id = ?",
                    (qty, item_id, stock_fc, ts, existing["id"]),
                )
                delta = qty - existing["qty"]
                action = "set"
                qty_after = qty
        else:
            try:
                conn.execute(
                    "INSERT INTO stock(owner, item_id, item_name, character_id, location,"
                    " hq, qty, source, fc_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (OWNER, item_id, canonical, character_id, location, hq, qty, "manual",
                     stock_fc, ts),
                )
            except sqlite3.IntegrityError:
                if mode != "add":
                    raise
                # 另一个窗口刚插入了同一格：退回原子累加
                conn.execute(
                    "UPDATE stock SET qty = qty + ?, item_id = ?, fc_id = ?, updated_at = ?"
                    " WHERE owner = ? AND character_id = ? AND item_name = ? AND location = ? AND hq = ?",
                    (qty, item_id, stock_fc, ts, OWNER, character_id, canonical, location, hq),
                )
            qty_after = int(conn.execute(
                "SELECT qty FROM stock WHERE owner = ? AND character_id = ?"
                " AND item_name = ? AND location = ? AND hq = ?",
                (OWNER, character_id, canonical, location, hq),
            ).fetchone()["qty"])
            delta = qty
            action = "add" if mode == "add" else "store"

        ledger_cur = conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (OWNER, ts, action, item_id, canonical, character_id, location, hq, delta, qty_after),
        )

        # 填了买入单价、且这次是「数量增加」→ 追加一条采购批次
        # 库存、流水、采购批次同一事务提交：任一步失败都整体回滚。
        purchase = None
        if unit_price > 0 and delta > 0:
            purchase = _add_purchase(
                conn, canonical, item_id, hq, delta, unit_price, location, ts,
                ledger_id=int(ledger_cur.lastrowid or 0),
            )

        conn.commit()

        total = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS q FROM stock WHERE owner = ? AND item_name = ?",
            (OWNER, canonical),
        ).fetchone()["q"]
        return {
            "ok": True,
            "item_name": canonical,
            "input_name": name,
            "renamed": norm(name) != norm(canonical),
            "item_id": item_id,
            "unbound": item_id == 0,
            "mode": mode,
            "added": delta if mode == "add" else 0,
            "qty": qty_after,
            "qty_after": qty_after,
            "total": total,
            "character": f"{chr_row['name']}@{chr_row['world']}" if chr_row["world"] else chr_row["name"],
            "location": location,
            "action": action,
            "purchase": purchase,
        }
    except sqlite3.Error as exc:
        conn.rollback()
        log_error("写入库存", exc)
        return {"ok": False, "error": f"保存失败，库存未改动：{exc}"}
    finally:
        conn.close()


def move_stock(stock_id: int, qty: int, character_id: int, location: str) -> dict:
    """搬货：从一条库存里移出 qty，并入目标角色+位置（同材料同品质则相加）。

    一个事务完成“减源 + 加目标”，不会出现旧行删了、新行没进来的半截状态。
    流水写两条（源 -qty / 目标 +qty），审计净变化为 0。
    """
    if not _valid_item_qty(qty):
        return {"ok": False, "invalid": True,
                "error": f"数量必须是 1–{MAX_ITEM_QTY} 的整数"}
    location = (location or "").strip()
    if not location:
        return {"ok": False, "error": "请选择目标位置"}

    conn = connect()
    try:
        src = conn.execute(
            "SELECT id, item_id, item_name, hq, qty, character_id, location FROM stock"
            " WHERE id = ? AND owner = ?",
            (stock_id, OWNER),
        ).fetchone()
        if src is None:
            return {"ok": False, "error": "找不到这条记录（可能已被改动，刷新看看）"}
        if qty > int(src["qty"]):
            return {"ok": False, "error": f"这格只有 {src['qty']}，搬不了 {qty}"}
        if int(src["character_id"]) == int(character_id) and src["location"] == location:
            return {"ok": False, "error": "目标位置和原位置相同，没有需要搬的"}

        chr_row = conn.execute(
            "SELECT id, name, world, fc_id FROM character WHERE id = ? AND owner = ?",
            (character_id, OWNER),
        ).fetchone()
        if chr_row is None:
            return {"ok": False, "error": "目标角色不存在"}

        allowed = set(DEFAULT_LOCATIONS)
        if not _valid_target_location(conn, character_id, location, allowed):
            return {"ok": False, "error": f"目标位置不合法：{location}"}

        ts = now_iso()
        fc_id = int(chr_row["fc_id"] or 0) if location == "部队柜" else 0
        target = conn.execute(
            "SELECT id, qty FROM stock WHERE owner = ? AND character_id = ?"
            " AND location = ? AND item_name = ? AND hq = ?",
            (OWNER, character_id, location, src["item_name"], src["hq"]),
        ).fetchone()
        if target:
            conn.execute(
                "UPDATE stock SET qty = qty + ?, fc_id = ?, updated_at = ? WHERE id = ?",
                (qty, fc_id, ts, target["id"]),
            )
            new_qty = int(conn.execute(
                "SELECT qty FROM stock WHERE id = ?", (target["id"],)
            ).fetchone()["qty"])
            if new_qty > MAX_ITEM_QTY:
                conn.rollback()
                return {"ok": False, "invalid": True,
                        "error": f"合并后超过上限 {MAX_ITEM_QTY}"}
            merged = True
        else:
            try:
                conn.execute(
                    "INSERT INTO stock(owner, item_id, item_name, character_id, location,"
                    " hq, qty, source, fc_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (OWNER, src["item_id"], src["item_name"], character_id, location,
                     src["hq"], qty, "move", fc_id, ts),
                )
                new_qty = qty
            except sqlite3.IntegrityError:
                # 另一个窗口刚插入了目标格：改成原子累加
                conn.execute(
                    "UPDATE stock SET qty = qty + ?, fc_id = ?, updated_at = ?"
                    " WHERE owner = ? AND character_id = ? AND location = ?"
                    " AND item_name = ? AND hq = ?",
                    (qty, fc_id, ts, OWNER, character_id, location,
                     src["item_name"], src["hq"]),
                )
                new_qty = int(conn.execute(
                    "SELECT qty FROM stock WHERE owner = ? AND character_id = ?"
                    " AND location = ? AND item_name = ? AND hq = ?",
                    (OWNER, character_id, location, src["item_name"], src["hq"]),
                ).fetchone()["qty"])
            merged = False

        cur = conn.execute(
            "UPDATE stock SET qty = qty - ?, updated_at = ?"
            " WHERE id = ? AND owner = ? AND qty >= ?",
            (qty, ts, src["id"], OWNER, qty),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return {"ok": False, "error": "源库存已被改动，刷新后重试"}
        remaining = int(conn.execute(
            "SELECT qty FROM stock WHERE id = ?", (src["id"],)
        ).fetchone()["qty"])
        if remaining <= 0:
            conn.execute("DELETE FROM stock WHERE id = ?", (src["id"],))

        who = chr_row["name"] + (f"@{chr_row['world']}" if chr_row["world"] else "")
        src_chr = conn.execute(
            "SELECT name, world FROM character WHERE id = ?", (src["character_id"],)
        ).fetchone()
        from_who = "?"
        if src_chr:
            from_who = src_chr["name"] + (f"@{src_chr['world']}" if src_chr["world"] else "")
        group = _op_group()
        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after, note, op_group)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (OWNER, ts, "take", src["item_id"], src["item_name"], src["character_id"],
             src["location"], src["hq"], -qty, remaining, f"搬出 → {who} / {location}", group),
        )
        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after, note, op_group)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (OWNER, ts, "store", src["item_id"], src["item_name"], character_id,
             location, src["hq"], qty, new_qty, f"搬入 ← {src['location']}", group),
        )
        conn.commit()
        return {
            "ok": True,
            "item_name": src["item_name"],
            "item_id": src["item_id"],
            "qty": qty,
            "from_character": from_who,
            "from_location": src["location"],
            "to_character": chr_row["name"],
            "to_location": location,
            "target_qty": new_qty,
            "remaining": remaining,
            "merged": merged,
        }
    finally:
        conn.close()


def update_stock(
    stock_id: int,
    qty: int,
    character_id: int,
    location: str,
    item_name: str = "",
    hq: int = 0,
) -> dict:
    """「改」= 改这一行的数据（数量 / 位置 / 角色 / 材料 / 品质）。

    语义：改完之后**还是同一份货**，不允许留下旧行。
    - 目标格与原格相同 → 直接改数量
    - 目标格不同（位置/角色/材料/品质变了）→ 合并进目标格，原行减掉或删除
    全程一个事务，流水写「搬出/搬入」两条，审计净变化为 0（除纯数量调整）。
    """
    qty = _valid_item_qty(qty, allow_zero=True)
    if qty is None:
        return {"ok": False, "invalid": True,
                "error": f"数量必须是 0–{MAX_ITEM_QTY} 的整数"}
    location = (location or "").strip()
    if not location:
        return {"ok": False, "error": "请选择位置"}

    name = (item_name or "").strip()
    item_id, canonical = resolve_item(name) if name else (0, "")
    if name and INDEX.loaded and not item_id:
        return {
            "ok": False,
            "hint": "pick",
            "error": f"库里没有「{name}」这个名字",
            "suggestions": [s["name"] for s in suggest_items(name, 6)],
        }
    hq = 1 if hq else 0

    conn = connect()
    try:
        src = conn.execute(
            "SELECT id, item_id, item_name, hq, qty, character_id, location FROM stock"
            " WHERE id = ? AND owner = ?",
            (stock_id, OWNER),
        ).fetchone()
        if src is None:
            return {"ok": False, "error": "找不到这条记录（可能已被改动，刷新看看）"}

        new_name = canonical or src["item_name"]
        new_item_id = item_id or src["item_id"]
        new_hq = hq
        new_char = int(character_id) or int(src["character_id"])

        chr_row = conn.execute(
            "SELECT id, name, world, fc_id FROM character WHERE id = ? AND owner = ?",
            (new_char, OWNER),
        ).fetchone()
        if chr_row is None:
            return {"ok": False, "error": "目标角色不存在"}

        allowed = set(DEFAULT_LOCATIONS)
        if not _valid_target_location(conn, new_char, location, allowed):
            return {"ok": False, "error": f"位置不合法：{location}"}

        same_place = (
            int(src["character_id"]) == new_char
            and src["location"] == location
            and src["item_name"] == new_name
            and int(src["hq"]) == new_hq
        )
        ts = now_iso()

        if same_place:
            conn.execute(
                "UPDATE stock SET qty = ?, fc_id = ?, pending_from = ?, pending_since = ?,"
                " updated_at = ? WHERE id = ?",
                (
                    qty,
                    int(chr_row["fc_id"] or 0) if location == "部队柜" else 0,
                    src["pending_from"] if "pending_from" in src.keys() else "",
                    src["pending_since"] if "pending_since" in src.keys() else "",
                    ts,
                    src["id"],
                ),
            )
            conn.execute(
                "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
                " location, hq, delta, qty_after, note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (OWNER, ts, "set", new_item_id, new_name, new_char, location, new_hq,
                 qty - int(src["qty"]), qty, "改数量"),
            )
            conn.commit()
            return {
                "ok": True,
                "mode": "qty",
                "item_name": new_name,
                "qty": qty,
                "target_qty": qty,
            }

        # 目标格不同：合并进目标，原行减掉/删除
        fc_id = int(chr_row["fc_id"] or 0) if location == "部队柜" else 0
        target = conn.execute(
            "SELECT id, qty FROM stock WHERE owner = ? AND character_id = ?"
            " AND location = ? AND item_name = ? AND hq = ?",
            (OWNER, new_char, location, new_name, new_hq),
        ).fetchone()
        if target:
            new_qty = int(target["qty"]) + qty
            if new_qty > MAX_ITEM_QTY:
                conn.rollback()
                return {"ok": False, "invalid": True,
                        "error": f"合并后超过上限 {MAX_ITEM_QTY}"}
            conn.execute(
                "UPDATE stock SET qty = ?, fc_id = ?, pending_from = '',"
                " pending_since = '', updated_at = ? WHERE id = ?",
                (new_qty, fc_id, ts, target["id"]),
            )
            merged = True
        else:
            new_qty = qty
            conn.execute(
                "INSERT INTO stock(owner, item_id, item_name, character_id, location,"
                " hq, qty, source, fc_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (OWNER, new_item_id, new_name, new_char, location, new_hq, qty,
                 "edit", fc_id, ts),
            )
            merged = False
        conn.execute("DELETE FROM stock WHERE id = ?", (src["id"],))

        who = chr_row["name"] + (f"@{chr_row['world']}" if chr_row["world"] else "")
        group = _op_group()
        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after, note, op_group)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (OWNER, ts, "take", src["item_id"], src["item_name"], src["character_id"],
             src["location"], src["hq"], -int(src["qty"]), 0, f"改：搬出 → {who} / {location}", group),
        )
        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after, note, op_group)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (OWNER, ts, "store", new_item_id, new_name, new_char, location, new_hq,
             qty, new_qty, f"改：搬入 ← {src['location']}", group),
        )
        conn.commit()
        return {
            "ok": True,
            "mode": "move",
            "item_name": new_name,
            "qty": qty,
            "target_qty": new_qty,
            "merged": merged,
            "from": {"character_id": src["character_id"], "location": src["location"]},
            "to": {"character_id": new_char, "character": who, "location": location},
        }
    finally:
        conn.close()


def record_take(stock_id: int, qty: int, note: str = "") -> dict:
    """取货：从某条记录里减掉数量；减到 0 就删行。全程写流水。"""
    if not _valid_item_qty(qty):
        return {"ok": False, "invalid": True,
                "error": f"数量必须是 1–{MAX_ITEM_QTY} 的整数"}
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, character_id, item_id, item_name, location, hq, qty"
            " FROM stock WHERE id = ? AND owner = ?",
            (stock_id, OWNER),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "找不到这条记录（可能已被改动，刷新看看）"}
        if qty > row["qty"]:
            return {"ok": False, "error": f"这格只有 {row['qty']}，取不了 {qty}"}

        ts = now_iso()
        cur = conn.execute(
            "UPDATE stock SET qty = qty - ?, updated_at = ?"
            " WHERE id = ? AND owner = ? AND qty >= ?",
            (qty, ts, stock_id, OWNER, qty),
        )
        if cur.rowcount == 0:
            conn.rollback()
            now_row = conn.execute(
                "SELECT qty FROM stock WHERE id = ? AND owner = ?", (stock_id, OWNER)
            ).fetchone()
            if now_row is None:
                return {"ok": False, "error": "这条记录已被删除，刷新看看"}
            return {"ok": False, "error": f"这格现在只有 {now_row['qty']}，取不了 {qty}"}
        remaining = int(conn.execute(
            "SELECT qty FROM stock WHERE id = ?", (stock_id,)
        ).fetchone()["qty"])
        if remaining <= 0:
            conn.execute("DELETE FROM stock WHERE id = ?", (stock_id,))
        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after, note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (OWNER, ts, "take", row["item_id"], row["item_name"], row["character_id"],
             row["location"], row["hq"], -qty, remaining, (note or "").strip()),
        )
        conn.commit()

        total = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS q FROM stock WHERE owner = ? AND item_name = ?",
            (OWNER, row["item_name"]),
        ).fetchone()["q"]
        chr_row = conn.execute(
            "SELECT name, world FROM character WHERE id = ?", (row["character_id"],)
        ).fetchone()
        who = chr_row["name"] if chr_row else "?"
        if chr_row and chr_row["world"]:
            who = f"{chr_row['name']}@{chr_row['world']}"
        return {
            "ok": True,
            "item_name": row["item_name"],
            "took": qty,
            "remaining": remaining,
            "total": total,
            "character": who,
            "location": row["location"],
            "removed": remaining <= 0,
            "stack": stack_of(row["item_id"]),
        }
    finally:
        conn.close()


def delete_stock(stock_id: int) -> dict:
    """删掉一条库存明细，并写流水。用于记错时纠正。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, character_id, item_id, item_name, location, hq, qty"
            " FROM stock WHERE id = ? AND owner = ?",
            (stock_id, OWNER),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "找不到这条记录（可能已经删过了）"}
        conn.execute("DELETE FROM stock WHERE id = ?", (stock_id,))
        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (OWNER, now_iso(), "clear", row["item_id"], row["item_name"],
             row["character_id"], row["location"], row["hq"], -row["qty"], 0),
        )
        conn.commit()
        return {"ok": True, "item_name": row["item_name"], "location": row["location"],
                "qty": row["qty"]}
    finally:
        conn.close()


def ledger_recent(limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT l.id, l.ts, l.action, l.item_id, l.item_name, l.location, l.delta,
                   l.qty_after, l.note, l.checked, l.checked_at, l.void,
                   c.name AS character_name, c.world
            FROM ledger l
            LEFT JOIN character c ON c.id = l.character_id
            WHERE l.owner = ?
            ORDER BY l.id DESC
            LIMIT ?
            """,
            (OWNER, max(1, min(limit, 100))),
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            row["stack"] = stack_of(row["item_id"])
            out.append(row)
        return out
    finally:
        conn.close()


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _date_bound(date_text: str, end: bool = False) -> str:
    """本地日期 → UTC 边界；end=True 取「次日 0 点」做开区间。"""
    dt = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=_local_tz())
    if end:
        dt = dt + timedelta(days=1)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _audit_where(date_from: str, date_to: str, check_state: str,
                 character: str, account: str):
    clauses = ["l.owner = ?"]
    params: list = [OWNER]
    if character:
        clauses.append("c.name = ?")
        params.append(character)
    if account:
        clauses.append("c.account = ?")
        params.append(account)
    if date_from:
        clauses.append("l.ts >= ?")
        params.append(_date_bound(date_from))
    if date_to:
        clauses.append("l.ts < ?")
        params.append(_date_bound(date_to, end=True))
    if check_state == "void":
        clauses.append("l.void = 1")
    else:
        # 默认把标了异常的行排除在统计之外
        clauses.append("l.void = 0")
        if check_state == "1":
            clauses.append("l.checked = 1")
        elif check_state == "0":
            clauses.append("l.checked = 0")
    return " AND ".join(clauses), params, bool(character or account)


def _audit_count(date_from: str = "", date_to: str = "", check_state: str = "",
                 character: str = "", account: str = "",
                 exclude_action: str = "") -> int:
    where, params, need_join = _audit_where(date_from, date_to, check_state,
                                            character, account)
    if exclude_action:
        where += " AND l.action <> ?"
        params.append(exclude_action)
    sql = "SELECT COUNT(*) AS n FROM ledger l"
    if need_join:
        sql += " LEFT JOIN character c ON c.id = l.character_id"
    sql += f" WHERE {where}"
    conn = connect()
    try:
        return int(conn.execute(sql, params).fetchone()["n"])
    finally:
        conn.close()


def _audit_rows(date_from: str, date_to: str, check_state: str = "",
                character: str = "", account: str = ""):
    where, params, _ = _audit_where(date_from, date_to, check_state,
                                    character, account)
    conn = connect()
    try:
        return conn.execute(
            f"""
            SELECT l.id, l.ts, l.action, l.item_id, l.item_name, l.delta,
                   l.checked, l.checked_at, l.void, l.note,
                   c.name AS character_name, c.world
            FROM ledger l
            LEFT JOIN character c ON c.id = l.character_id
            WHERE {where}
            ORDER BY l.id DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()


def audit_summary(date_from: str = "", date_to: str = "", check_state: str = "",
                  character: str = "", account: str = "") -> dict:
    """按物品汇总一段时间内的进出。同 ID 的不同叫法会合并成一行。"""
    try:
        rows = _audit_rows(date_from, date_to, check_state, character, account)
    except ValueError:
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}

    items: dict = {}
    ops = 0
    for r in rows:
        # rename 只是改名；undo 是补偿记录，原操作已被标 void，
        # 两者都不进对账汇总，否则 net 会被重复扣一次。
        if r["action"] in ("rename", "undo"):
            continue
        ops += 1

        item_id = r["item_id"] or 0
        if not item_id:
            # 历史流水可能没存 item_id（别名绑定前写入的），按名字补解析，
            # 这样才能和现行的标准名合并成同一行
            resolved, _ = resolve_item(r["item_name"] or "")
            if resolved:
                item_id = resolved
        key = item_id if item_id else ("name:" + (r["item_name"] or "?"))
        standard = (INDEX.by_id(item_id) or {}).get("name") if item_id else ""
        name = standard or r["item_name"] or "?"

        bucket = items.setdefault(
            key,
            {
                "name": name,
                "item_id": item_id,
                "stack": stack_of(item_id),
                "added": 0,
                "took": 0,
                "adjusted": 0,
                "cleared": 0,
                "net": 0,
                "characters": [],
            },
        )
        delta = r["delta"] or 0
        if r["action"] in ("store", "add"):
            bucket["added"] += delta
        elif r["action"] == "take":
            bucket["took"] += delta
        elif r["action"] == "set":
            bucket["adjusted"] += delta
        elif r["action"] == "clear":
            bucket["cleared"] += delta
        bucket["net"] += delta

        who = r["character_name"] or ""
        if r["world"]:
            who = f"{who}@{r['world']}" if who else r["world"]
        if who and who not in bucket["characters"]:
            bucket["characters"].append(who)

    item_list = sorted(items.values(), key=lambda x: (-abs(x["net"]), x["name"]))
    totals = {
        "added": sum(i["added"] for i in item_list),
        "took": sum(i["took"] for i in item_list),
        "adjusted": sum(i["adjusted"] for i in item_list),
        "cleared": sum(i["cleared"] for i in item_list),
        "net": sum(i["net"] for i in item_list),
        "kinds": len(item_list),
    }
    try:
        void_count = _audit_count(date_from, date_to, "void", character, account,
                                  exclude_action="rename")
    except ValueError:
        void_count = 0
    return {
        "ok": True,
        "items": item_list,
        "totals": totals,
        "ops": ops,
        "void_count": void_count,
    }


def audit_export(date_from: str = "", date_to: str = "", check_state: str = "",
                 character: str = "", account: str = "") -> dict:
    """把汇总结果转成可复制的文本和 CSV。"""
    data = audit_summary(date_from, date_to, check_state, character, account)
    if not data.get("ok"):
        return data

    label_parts = []
    label_parts.append(f"{date_from} 起" if date_from else "全部时间")
    if date_to:
        label_parts.append(f"{date_to} 止")
    if check_state == "1":
        label_parts.append("只看已核对")
    elif check_state == "0":
        label_parts.append("只看未核对")
    label = " · ".join(label_parts)

    def grp(qty, stack):
        if not qty:
            return "0"
        if stack == 1:
            return str(qty)
        per = stack if stack and stack > 1 else 999
        g, rest = divmod(abs(qty), per)
        sign = "-" if qty < 0 else ""
        if not g:
            return f"{sign}{abs(qty)}"
        return f"{sign}{g}组{rest}" if rest else f"{sign}{g}组"

    lines = [f"FF14 材料审计（{label}），共 {data['ops']} 条操作", ""]
    lines.append(f"{'材料':<14}{'新增':>12}{'取出':>12}{'调整':>10}{'删除':>10}{'净变化':>12}  涉及角色")
    for item in data["items"]:
        stack = item["stack"]
        lines.append(
            f"{item['name']:<14}"
            f"{grp(item['added'], stack):>12}"
            f"{grp(item['took'], stack):>12}"
            f"{grp(item['adjusted'], stack):>10}"
            f"{grp(item['cleared'], stack):>10}"
            f"{grp(item['net'], stack):>12}"
            f"  {'、'.join(item['characters'])}"
        )
    t = data["totals"]
    lines.append("")
    lines.append(f"材料种类：{t['kinds']}　净变化合计：{t['net']}")

    csv_lines = ["material,added,took,adjusted,cleared,net,characters"]
    for item in data["items"]:
        chars = "、".join(item["characters"])
        csv_lines.append(
            ",".join(
                _csv_cell(c)
                for c in (
                    item["name"],
                    item["added"],
                    item["took"],
                    item["adjusted"],
                    item["cleared"],
                    item["net"],
                    chars,
                )
            )
        )
    return {"ok": True, "text": "\n".join(lines), "csv": "\n".join(csv_lines)}


def set_ledger_void(ledger_id: int, is_void: bool, note: str = "") -> dict:
    """标记 / 取消标记某条流水为异常（备注写原因，原因会保留）。"""
    conn = connect()
    try:
        if is_void:
            reason = (note or "").strip()
            if reason:
                cur = conn.execute(
                    "UPDATE ledger SET void = 1, note = ? WHERE id = ? AND owner = ?",
                    (reason, ledger_id, OWNER),
                )
            else:
                cur = conn.execute(
                    "UPDATE ledger SET void = 1 WHERE id = ? AND owner = ?",
                    (ledger_id, OWNER),
                )
        else:
            cur = conn.execute(
                "UPDATE ledger SET void = 0 WHERE id = ? AND owner = ?",
                (ledger_id, OWNER),
            )
        conn.commit()
        if not cur.rowcount:
            return {"ok": False, "error": "找不到这条操作"}
        return {"ok": True, "id": ledger_id, "void": 1 if is_void else 0}
    finally:
        conn.close()


def set_ledger_checked(ledger_id: int, checked: bool) -> dict:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE ledger SET checked = ?, checked_at = ? WHERE id = ? AND owner = ?",
            (1 if checked else 0, now_iso() if checked else "", ledger_id, OWNER),
        )
        conn.commit()
        if not cur.rowcount:
            return {"ok": False, "error": "找不到这条操作"}
        return {"ok": True, "id": ledger_id, "checked": 1 if checked else 0}
    finally:
        conn.close()


def set_ledger_checked_batch(ids: list[int], checked: bool) -> dict:
    ids = [int(i) for i in ids if str(i).lstrip("-").isdigit()][:500]
    if not ids:
        return {"ok": False, "error": "没有要标记的记录"}
    placeholders = ",".join("?" for _ in ids)
    conn = connect()
    try:
        conn.execute(
            f"UPDATE ledger SET checked = ?, checked_at = ?"
            f" WHERE owner = ? AND id IN ({placeholders})",
            [1 if checked else 0, now_iso() if checked else "", OWNER] + ids,
        )
        conn.commit()
        return {"ok": True, "count": len(ids), "checked": 1 if checked else 0}
    finally:
        conn.close()


def delete_alias(alias: str) -> dict:
    alias = (alias or "").strip()
    if not alias:
        return {"ok": False, "error": "备注名为空"}
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM alias WHERE owner = ? AND alias = ?", (OWNER, alias)
        )
        conn.commit()
        if not cur.rowcount:
            return {"ok": False, "error": f"没有「{alias}」这条备注名"}
        return {"ok": True, "alias": alias}
    finally:
        conn.close()
        load_aliases()


def migrate_rename_iron() -> None:
    """一次性迁移：库存里的「铁锭」改成标准名「黑铁锭」，「铁锭」留作备注名。

    改名可能撞上已存在的「黑铁锭」行（同角色/位置/品质），此时把数量合并过去，
    而不是让整次迁移失败。
    """
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM meta WHERE key = 'migrate_heities'").fetchone():
            return

        rows = conn.execute(
            "SELECT id, character_id, location, hq, qty FROM stock"
            " WHERE owner = ? AND item_name = '铁锭'",
            (OWNER,),
        ).fetchall()

        renamed = 0
        merged = 0
        for row in rows:
            twin = conn.execute(
                "SELECT id, qty FROM stock WHERE owner = ? AND character_id = ?"
                " AND item_name = '黑铁锭' AND location = ? AND hq = ?",
                (OWNER, row["character_id"], row["location"], row["hq"]),
            ).fetchone()
            if twin:
                conn.execute(
                    "UPDATE stock SET qty = qty + ?, item_id = 5057, updated_at = ? WHERE id = ?",
                    (row["qty"], now_iso(), twin["id"]),
                )
                conn.execute("DELETE FROM stock WHERE id = ?", (row["id"],))
                merged += 1
            else:
                conn.execute(
                    "UPDATE stock SET item_name = '黑铁锭', item_id = 5057 WHERE id = ?",
                    (row["id"],),
                )
                renamed += 1

        if renamed or merged:
            # 只有真的改过历史数据，才把「铁锭」留成备注名；空库不凭空造这条
            conn.execute(
                "INSERT OR IGNORE INTO alias(owner, alias, item_id, item_name, created_at)"
                " VALUES(?, '铁锭', 5057, '黑铁锭', ?)",
                (OWNER, now_iso()),
            )
            conn.execute(
                "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
                " location, delta, qty_after, note)"
                " VALUES(?, ?, 'rename', 5057, '黑铁锭', 0, '', 0, 0, ?)",
                (OWNER, now_iso(), f"改名 {renamed} 行，与已有黑铁锭合并 {merged} 行"),
            )

        conn.execute("INSERT INTO meta(key, value) VALUES('migrate_heities', '1')")
        conn.commit()
        if renamed or merged:
            print(f"[迁移] 「铁锭」→「黑铁锭」：改名 {renamed} 行，合并 {merged} 行；「铁锭」保留为备注名")
        else:
            print("[迁移] 没有需要改名的「铁锭」记录，已登记迁移标记")
    except Exception as exc:  # noqa: BLE001 - 迁移失败不能拦住启动
        print(f"[迁移] 跳过（{type(exc).__name__}: {exc}），数据未改动")
    finally:
        conn.close()
        load_aliases()


# --------------------------------------------------------------------------
# 快照：每次写操作后存一份，保留最近 30 个自动快照，手动快照不自动删
# --------------------------------------------------------------------------

SNAP_DIR = DATA_DIR / "snapshots"
SNAP_META_PATH = SNAP_DIR / "meta.json"
SNAP_KEEP = 30          # 自动快照保留数量
SNAP_PRE_KEEP = 15      # 「恢复前」安全垫保留数量（手动快照不受限）
SNAP_AFTER = {
    "/api/stock",
    "/api/stock/take",
    "/api/stock/move",
    "/api/stock/update",
    "/api/stock/delete",
    "/api/undo",
    "/api/alias",
    "/api/alias/delete",
    "/api/cost",
}
_snap_lock = threading.RLock()
_restore_lock = threading.RLock()
_RESTORING = False


def _snap_meta() -> dict:
    if not SNAP_META_PATH.exists():
        return {}
    try:
        data = json.loads(SNAP_META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _snap_meta_write(meta: dict) -> None:
    tmp = SNAP_META_PATH.with_name(SNAP_META_PATH.name + ".tmp")
    try:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(meta, ensure_ascii=False, indent=0), encoding="utf-8"
        )
        os.replace(tmp, SNAP_META_PATH)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _ledger_max() -> int:
    try:
        conn = connect()
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger").fetchone()
            return int(row["m"])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0


def _snap_prune(meta: dict) -> None:
    for pattern, keep in (("auto-*.db", SNAP_KEEP), ("pre-restore-*.db", SNAP_PRE_KEEP)):
        files = sorted(
            (p for p in SNAP_DIR.glob(pattern)), key=lambda p: p.name, reverse=True
        )
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
            meta.pop(old.name, None)
    _snap_meta_write(meta)


LEGACY_NOTES = {"stock", "take", "delete", "alias"}


def _csv_cell(value) -> str:
    text = str(value if value is not None else "")
    # Excel 会把以 = + - @ 开头的单元格当公式；纯数字不受影响。
    if text[:1] in ("=", "+", "@") or (
        text[:1] == "-" and not re.fullmatch(r"-\d+(?:\.\d+)?", text)
    ):
        text = "'" + text
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def _fmt_qty_note(qty, stack: int) -> str:
    try:
        value = int(qty or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "0"
    if stack == 1:
        return str(value)
    per = stack if stack and stack > 1 else 999
    groups, rest = divmod(value, per)
    if not groups:
        return str(value)
    return f"{groups}组{rest}/{value}" if rest else f"{groups}组/{value}"


def fmt_slots(qty, stack: int) -> int:
    try:
        value = int(qty or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return 0
    per = stack if stack and stack > 0 else 999
    return (value + per - 1) // per


def snapshot_note(route: str, res: dict) -> str:
    """把这次操作的摘要写进快照备注，方便回溯时看清做了什么。"""
    key = route.rsplit("/", 1)[-1]
    if key == "stock":
        verb = "覆盖" if res.get("action") == "set" else "存入"
        stack = stack_of(int(res.get("item_id") or 0))
        return (
            f"{verb} {res.get('item_name', '?')} "
            f"{_fmt_qty_note(res.get('qty'), stack)} → "
            f"{res.get('character', '?')} / {res.get('location', '')}"
        )
    if key == "take":
        stack = int(res.get("stack") or 0)
        return (
            f"取出 {res.get('item_name', '?')} "
            f"{_fmt_qty_note(res.get('took'), stack)} → "
            f"{res.get('character', '?')} / {res.get('location', '')}"
            f"（剩 {res.get('remaining')}）"
        )
    if key == "move":
        stack = stack_of(int(res.get("item_id") or 0))
        return (
            f"搬移 {res.get('item_name', '?')} "
            f"{_fmt_qty_note(res.get('qty'), stack)} "
            f"{res.get('from_character', '?')} / {res.get('from_location', '')} → "
            f"{res.get('to_character', '?')} / {res.get('to_location', '')}"
            + (f"（原处剩 {res.get('remaining')}）" if res.get("remaining") else "")
        )
    if key == "delete":
        return (
            f"删除 {res.get('item_name', '?')} {res.get('qty')}"
            f"（{res.get('location', '')}）"
        )
    if key == "alias":
        return f"备注名 {res.get('alias', '?')} → {res.get('item_name', '?')} #{res.get('item_id')}"
    if key == "undo":
        return f"撤销 {res.get('item_name', '?')} → 恢复到 {res.get('before')}"
    return key


def _db_integrity_ok(path: Path) -> bool:
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _replace_with_retry(src: Path, dst: Path, attempts: int = 20) -> None:
    """Windows 下文件被短暂打开会 WinError 5；重试到所有连接释放。"""
    last: OSError | None = None
    for _ in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    raise last or OSError("替换数据库失败")


def _backup_current_to(dst: Path) -> None:
    """用 backup API 备份当前库：先写 .tmp，校验通过再原子替换。"""
    tmp = dst.with_name(dst.name + ".tmp")
    src = sqlite3.connect(DB_PATH, timeout=10)
    try:
        src.execute("PRAGMA busy_timeout=5000")
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
        finally:
            out.close()
        if not _db_integrity_ok(tmp):
            raise sqlite3.Error("备份完整性检查失败")
        os.replace(tmp, dst)
    finally:
        src.close()
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def create_snapshot(kind: str = "auto", note: str = "") -> dict:
    with _snap_lock:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = SNAP_DIR / f"{kind}-{stamp}.db"
        seq = 2
        while path.exists():
            path = SNAP_DIR / f"{kind}-{stamp}-{seq}.db"
            seq += 1

        # 用 sqlite 的 backup 接口，WAL 模式下直接拷文件可能拿到半截数据；
        # 先写 .tmp、校验完整性，再原子替换，避免留下坏快照。
        _backup_current_to(path)

        meta = _snap_meta()
        meta[path.name] = {
            "kind": kind,
            "note": note,
            "ts": now_iso(),
            "ledger_max": _ledger_max(),
        }
        _snap_meta_write(meta)
        _snap_prune(meta)
        return {"ok": True, "name": path.name, "kind": kind, "note": note}


def auto_snapshot(reason: str) -> None:
    """异步存快照：即使拷贝失败或变慢，也不能拖住这次保存的响应。"""

    def worker() -> None:
        try:
            create_snapshot("auto", reason)
        except Exception as exc:  # noqa: BLE001
            print(f"[快照] 失败（{type(exc).__name__}: {exc}）", flush=True)

    threading.Thread(target=worker, daemon=True).start()


def list_snapshots() -> list[dict]:
    if not SNAP_DIR.exists():
        return []
    meta = _snap_meta()
    current_max = _ledger_max()
    out: list[dict] = []
    for path in SNAP_DIR.glob("*.db"):
        try:
            stat = path.stat()
        except OSError:
            continue
        info = meta.get(path.name, {})
        note = info.get("note", "")
        kind = info.get("kind") or ("auto" if path.name.startswith("auto") else "manual")
        out.append(
            {
                "name": path.name,
                "kind": kind,
                "note": note,
                "legacy": kind == "auto" and note in LEGACY_NOTES,
                "size": stat.st_size,
                "ts": info.get("ts") or datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).replace(microsecond=0).isoformat(),
                "behind_ops": max(0, current_max - int(info.get("ledger_max") or 0)),
            }
        )
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


def restore_snapshot(name: str) -> dict:
    name = Path(name or "").name
    src = SNAP_DIR / name
    if not name or not src.exists() or src.suffix != ".db":
        return {"ok": False, "error": "找不到这个快照"}
    info = inspect_db_file(src)
    if not info["ok"]:
        return _restore_result(
            False, "这个快照不完整或已损坏，未替换", stage="not_replaced",
            name=name,
        )
    result = _install_db(src, info, pre_snapshot_note=f"恢复「{name}」之前的现场")
    result["name"] = name
    return result


def delete_snapshot(name: str) -> dict:
    name = Path(name or "").name
    path = SNAP_DIR / name
    if not name or not path.exists():
        return {"ok": False, "error": "找不到这个快照"}
    try:
        path.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"删除失败：{exc}"}
    meta = _snap_meta()
    meta.pop(name, None)
    _snap_meta_write(meta)
    return {"ok": True, "name": name}


def cleanup_legacy_snapshots() -> dict:
    """删掉早期没有操作摘要的自动快照，让列表保持干净。"""
    removed = 0
    for item in list_snapshots():
        if item.get("legacy"):
            res = delete_snapshot(item["name"])
            if res.get("ok"):
                removed += 1
    return {"ok": True, "removed": removed}


def _open_db_ro(path: Path) -> sqlite3.Connection:
    """只读打开一个数据库文件。

    优先用普通路径：Windows 上中文路径经 as_uri() 百分号编码后，
    SQLite 有时会报 "unable to open database file"，普通路径不受影响。
    """
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.execute("PRAGMA query_only = 1")
        return conn
    except sqlite3.Error:
        return sqlite3.connect(f"file:{path.as_uri()}?mode=ro", uri=True, timeout=5)


def preview_snapshot(name: str) -> dict:
    """只读打开某份快照，看看那一刻的库存长什么样。"""
    path = SNAP_DIR / Path(name or "").name
    if not path.exists() or path.suffix != ".db":
        return {"ok": False, "error": "找不到这个快照"}
    try:
        conn = _open_db_ro(path)
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"打不开快照：{exc}"}
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT s.item_id, s.item_name, s.location, s.hq, s.qty,
                   c.name AS character, c.world
            FROM stock s
            LEFT JOIN character c ON c.id = s.character_id
            ORDER BY s.item_name
            """
        ).fetchall()
        ledger_n = conn.execute("SELECT COUNT(*) AS n FROM ledger").fetchone()["n"]
    except sqlite3.Error as exc:
        conn.close()
        return {"ok": False, "error": f"读取失败：{exc}"}
    conn.close()

    items: dict = {}
    for r in rows:
        key = (r["item_name"], r["hq"])
        bucket = items.setdefault(
            key,
            {
                "name": r["item_name"],
                "item_id": r["item_id"],
                "hq": r["hq"],
                "total": 0,
                "holders": [],
            },
        )
        bucket["total"] += r["qty"] or 0
        bucket["holders"].append(
            {
                "character": r["character"] or "?",
                "world": r["world"] or "",
                "location": r["location"],
                "qty": r["qty"],
            }
        )
    out = sorted(items.values(), key=lambda x: (-x["total"], x["name"]))
    for item in out:
        item["stack"] = stack_of(item["item_id"])
        item["holders"].sort(key=lambda h: -h["qty"])
    return {
        "ok": True,
        "name": path.name,
        "items": out,
        "ledger_count": ledger_n,
    }


def _parse_qty_with_stack(token: str, stack: int) -> int | None:
    token = norm(token)
    m = re.fullmatch(r"(\d+)组(\d+)?", token)
    if m:
        per = stack if stack and stack > 1 else 999
        return int(m.group(1)) * per + int(m.group(2) or 0)
    if re.fullmatch(r"\d+", token):
        return int(token)
    return None


def _record_batch_line(character_id: int, raw: str, default_location: str, mode: str = "set") -> dict:
    tokens = [t for t in re.split(r"\s+", raw.strip()) if t]
    if not tokens:
        return {"line": raw, "ok": False, "message": "空行"}

    hq = 0
    if tokens[-1].lower() in ("hq", "hq1", "高质量"):
        hq = 1
        tokens = tokens[:-1]

    location = default_location or "背包"
    emp = re.fullmatch(r"雇员(\d+)", tokens[-1]) if tokens else None
    if len(tokens) >= 3 and (
        tokens[-1] in DEFAULT_LOCATIONS or (emp and 1 <= int(emp.group(1)) <= 10)
    ):
        location = tokens[-1]
        tokens = tokens[:-1]

    if len(tokens) < 2:
        return {"line": raw, "ok": False, "message": "至少要有「材料名 数量」"}

    name = " ".join(tokens[:-1]).strip()
    item_id, _canonical = resolve_item(name)
    stack = stack_of(item_id)
    qty = _parse_qty_with_stack(tokens[-1], stack)
    if qty is None:
        return {"line": raw, "ok": False, "message": "数量看不懂（例：524 或 1组524）"}
    if qty <= 0:
        return {"line": raw, "ok": False, "message": "数量要大于 0"}
    if qty > MAX_ITEM_QTY:
        return {"line": raw, "ok": False, "message": f"数量超过上限 {MAX_ITEM_QTY}"}

    res = record_stock(character_id, name, location, qty, hq, mode=mode)
    if not res.get("ok"):
        message = res.get("error") or "写入失败"
        if res.get("hint") == "pick":
            message = "物品库里没有这个名称"
        return {"line": raw, "ok": False, "message": message}

    return {
        "line": raw,
        "ok": True,
        "mode": mode,
        "unchanged": bool(res.get("unchanged")),
        "message": f"{res['item_name']} {_fmt_qty_note(qty, stack_of(res.get('item_id') or 0))}"
                   f"{' HQ' if hq else ''} → {location}",
    }


def record_batch(character_id: int, text: str, default_location: str = "背包", mode: str = "set") -> dict:
    """批量粘贴录入：一行一条 `材料名 数量 [位置] [hq]`。mode=add 时按增量累加。"""
    mode = "add" if str(mode or "").lower() == "add" else "set"
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        return {"ok": False, "error": "没有可解析的内容"}
    if len(lines) > 500:
        return {"ok": False, "error": "一次最多 500 行，分批来吧"}

    try:
        create_snapshot("manual", f"批量录入前（{len(lines)} 行）")
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "error": f"批量录入前快照失败，已中止：{exc}"}

    results = [_record_batch_line(character_id, ln, default_location, mode) for ln in lines]
    added = sum(1 for r in results if r.get("ok") and not r.get("unchanged"))
    unchanged = sum(1 for r in results if r.get("ok") and r.get("unchanged"))
    failed = len(results) - added - unchanged
    return {
        "ok": True,
        "mode": mode,
        "total": len(results),
        "added": added,
        "unchanged": unchanged,
        "failed": failed,
        "results": results,
    }


def export_stock() -> dict:
    """导出全部库存为 CSV / 文本。"""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.item_id, s.item_name, s.location, s.hq, s.qty, s.updated_at,
                   c.name AS character, c.world, c.account
            FROM stock s
            JOIN character c ON c.id = s.character_id
            WHERE s.owner = ?
            ORDER BY s.item_name, c.name
            """,
            (OWNER,),
        ).fetchall()
    finally:
        conn.close()

    csv_lines = ["material,item_id,character,world,account,location,hq,qty,groups,slots,updated_at"]
    text_lines = [f"FF14 材料库存导出，共 {len(rows)} 行", ""]
    for r in rows:
        stack = stack_of(r["item_id"])
        slots = fmt_slots(r["qty"], stack)
        groups = (r["qty"] // stack) if stack and stack > 1 else 0
        row_cells = [
            str(r["item_name"]),
            str(r["item_id"]),
            str(r["character"] or ""),
            str(r["world"] or ""),
            str(r["account"] or ""),
            str(r["location"] or ""),
            "HQ" if r["hq"] else "NQ",
            str(r["qty"]),
            str(groups),
            str(slots),
            str(r["updated_at"] or ""),
        ]
        csv_lines.append(",".join(_csv_cell(c) for c in row_cells))
        text_lines.append(
            f"{r['item_name']}\t{r['character']}@{r['world']}\t{r['location']}\t"
            f"{'HQ' if r['hq'] else 'NQ'}\t{_fmt_qty_note(r['qty'], stack)}"
        )
    return {
        "ok": True,
        "count": len(rows),
        "csv": "\n".join(csv_lines),
        "text": "\n".join(text_lines),
    }


def _char_id(conn, name: str, world: str, account: str = "") -> int:
    if account:
        row = conn.execute(
            "SELECT id FROM character WHERE owner = ? AND account = ?"
            " AND name = ? AND world = ?",
            (OWNER, account, name, world),
        ).fetchone()
        return int(row["id"]) if row else 0
    rows = conn.execute(
        "SELECT id FROM character WHERE owner = ? AND name = ? AND world = ? LIMIT 2",
        (OWNER, name, world),
    ).fetchall()
    return int(rows[0]["id"]) if len(rows) == 1 else 0


def prepare_import(text: str) -> dict:
    """解析导入用的 CSV（只解析不写库）。"""
    text = (text or "").lstrip("\ufeff")
    if not re.search(r"(?im)^\s*material\s*,", text):
        return {"ok": False, "error": "需要带表头的 CSV（material,character,location,hq,qty…）"}

    prepared: list[dict] = []
    skipped: list[dict] = []
    new_chars: list[str] = []
    ambiguous: list[dict] = []
    seen_qty: dict[tuple, int] = {}

    conn = connect()
    try:
        by_account: dict[tuple, int] = {}
        by_nameworld: dict[tuple, list[tuple[str, int]]] = {}
        for c in conn.execute(
            "SELECT id, account, name, world FROM character WHERE owner = ?", (OWNER,)
        ).fetchall():
            acc = str(c["account"] or "")
            key = (c["name"], c["world"])
            by_account[(acc, c["name"], c["world"])] = int(c["id"])
            by_nameworld.setdefault(key, []).append((acc, int(c["id"])))
        reader = csv.DictReader(io.StringIO(text))
        field_names = [f.strip().lower() for f in (reader.fieldnames or [])]
        if "qty" not in field_names:
            return {
                "ok": False,
                "error": "这份 CSV 没有 qty 列，看起来是「审计汇总」而不是库存明细。"
                         "请用搜索结果面板右上角的「导出库存 CSV」导出明细后再导入。",
            }
        for index, rec in enumerate(reader, start=2):
            name = (rec.get("material") or "").strip()
            if not name:
                continue

            raw_id = (rec.get("item_id") or "").strip()
            item_id = int(raw_id) if raw_id.isdigit() else 0
            if not item_id:
                item_id, _ = resolve_item(name)
            canonical = (INDEX.by_id(item_id) or {}).get("name") or name
            if not item_id:
                skipped.append({"line": index, "name": name, "reason": "物品库未收录，先绑定名称再来"})
                continue

            qty_raw = (rec.get("qty") or "0").strip()
            qty = int(qty_raw) if qty_raw.isdigit() else 0
            if qty <= 0:
                skipped.append({"line": index, "name": name, "reason": "数量为 0 或无法解析"})
                continue
            if qty > MAX_ITEM_QTY:
                skipped.append({"line": index, "name": name,
                                "reason": f"数量超过上限 {MAX_ITEM_QTY}"})
                continue

            character = (rec.get("character") or "").strip()
            if not character:
                skipped.append({"line": index, "name": name, "reason": "缺少角色名"})
                continue

            world = (rec.get("world") or "").strip()
            account = (rec.get("account") or "").strip()
            location = (rec.get("location") or "背包").strip() or "背包"
            hq = 1 if (rec.get("hq") or "").strip().upper() == "HQ" else 0

            char_id = 0
            is_new_char = False
            if account:
                char_id = by_account.get((account, character, world), 0)
                is_new_char = not char_id
            else:
                candidates = by_nameworld.get((character, world), [])
                if len(candidates) == 1:
                    char_id = candidates[0][1]
                elif len(candidates) > 1:
                    detail = []
                    for acc, cid in candidates:
                        q = conn.execute(
                            "SELECT qty FROM stock WHERE owner = ? AND character_id = ?"
                            " AND item_name = ? AND location = ? AND hq = ?",
                            (OWNER, cid, canonical, location, hq),
                        ).fetchone()
                        detail.append({"account": acc, "character_id": cid,
                                       "qty": int(q["qty"]) if q else 0})
                    ambiguous.append({
                        "line": index,
                        "character": character,
                        "world": world,
                        "candidates": detail,
                    })
                    skipped.append({
                        "line": index,
                        "name": name,
                        "reason": "同名同服有多个账号，请在 CSV 补 account 列",
                    })
                    continue
                else:
                    is_new_char = True
            stock_key = (char_id or f"new:{character}@{world}", canonical, location, hq)
            existing_qty = seen_qty.get(stock_key)
            if existing_qty is None and char_id:
                row = conn.execute(
                    "SELECT id, qty FROM stock WHERE owner = ? AND character_id = ?"
                    " AND item_name = ? AND location = ? AND hq = ?",
                    (OWNER, char_id, canonical, location, hq),
                ).fetchone()
                existing_qty = row["qty"] if row else None

            if is_new_char:
                label = f"{character}@{world}" if world else character
                if label not in new_chars:
                    new_chars.append(label)

            seen_qty[stock_key] = int(existing_qty or 0) + qty
            prepared.append(
                {
                    "line": index,
                    "name": canonical,
                    "item_id": item_id,
                    "character": character,
                    "world": world,
                    "account": account,
                    "location": location,
                    "hq": hq,
                    "qty": qty,
                    "char_id": char_id,
                    "new_char": is_new_char,
                    "existing_qty": existing_qty,
                }
            )
    finally:
        conn.close()

    will_add = sum(1 for r in prepared if r["existing_qty"] is None)
    will_update = len(prepared) - will_add

    rows_out = [
        {
            "line": r["line"],
            "text": f"{r['name']} {r['qty']} {r['location']}".strip(),
            "action": "update" if r["existing_qty"] is not None else "add",
            "item_name": r["name"],
            "location": r["location"],
            "hq": r["hq"],
            "qty_before": int(r["existing_qty"] or 0),
            "qty_after": int(r["qty"]),
            "error": "",
        }
        for r in prepared
    ]
    rows_out += [
        {
            "line": s["line"],
            "text": s.get("name", ""),
            "action": "error",
            "item_name": s.get("name", ""),
            "location": "",
            "hq": 0,
            "qty_before": 0,
            "qty_after": 0,
            "error": s.get("reason", ""),
        }
        for s in skipped
    ]
    rows_out.sort(key=lambda x: x["line"])

    return {
        "ok": True,
        "rows": rows_out,
        "prepared": prepared,
        "skipped": skipped,
        "new_characters": new_chars,
        "total": len(prepared) + len(skipped),
        "will_add": will_add,
        "will_update": will_update,
        "ambiguous": ambiguous,
    }


def import_apply(text: str, mode: str) -> dict:
    """执行导入：mode=overwrite 覆盖，append 追加。"""
    mode = "append" if mode == "append" else "overwrite"
    prep = prepare_import(text)
    if not prep.get("ok"):
        return prep
    if prep.get("ambiguous"):
        return {
            "ok": False,
            "error": "存在歧义：同名同服多个账号，请补 account 列后重试",
            "ambiguous": prep["ambiguous"],
        }

    snap = create_snapshot(
        "manual", "导入前自动存档（" + ("追加" if mode == "append" else "覆盖") + "）"
    )

    added = updated = created_chars = 0
    results: list[dict] = []
    char_cache: dict[tuple, int] = {}
    written: set[tuple] = set()

    for row in prep["prepared"]:
        char_id = row["char_id"]
        char_key = (row["account"], row["character"], row["world"])
        if not char_id and char_key in char_cache:
            char_id = char_cache[char_key]
        if not char_id:
            created = add_character(row["account"], row["character"], row["world"])
            conn = connect()
            try:
                char_id = _char_id(conn, row["character"], row["world"],
                                   row["account"])
                existing = conn.execute(
                    "SELECT qty FROM stock WHERE owner = ? AND character_id = ?"
                    " AND item_name = ? AND location = ? AND hq = ?",
                    (OWNER, char_id, row["name"], row["location"], row["hq"]),
                ).fetchone()
                row["existing_qty"] = existing["qty"] if existing else None
            finally:
                conn.close()
            if not char_id:
                results.append({"line": row["line"], "ok": False,
                                "message": created.get("error") or "角色创建失败"})
                continue
            if created.get("ok"):
                created_chars += 1
            char_cache[char_key] = char_id

        stock_key = (char_id, row["name"], row["location"], row["hq"])
        use_add = mode == "append" and (
            stock_key in written or row["existing_qty"] is not None
        )
        written.add(stock_key)

        if use_add:
            res = record_stock(char_id, row["name"], row["location"], row["qty"],
                               row["hq"], mode="add")
        else:
            res = record_stock(char_id, row["name"], row["location"], row["qty"],
                               row["hq"])
        if not res.get("ok"):
            results.append({"line": row["line"], "ok": False,
                            "message": res.get("error") or "写入失败"})
            continue

        if row["existing_qty"] is None and not use_add:
            added += 1
        else:
            updated += 1
        results.append(
            {
                "line": row["line"],
                "ok": True,
                "message": f"{row['name']} {row['qty']}{' HQ' if row['hq'] else ''}"
                           f" → {row['character']} / {row['location']}",
            }
        )

    return {
        "ok": True,
        "mode": mode,
        "added": added,
        "updated": updated,
        "skipped": len(prep["skipped"]),
        "created_characters": created_chars,
        "results": results,
        "skipped_rows": prep["skipped"],
        "snapshot": snap.get("name", "") if isinstance(snap, dict) else "",
    }


def undo_last() -> dict:
    """撤销最近一条库存操作：把那一格恢复到操作前的数量。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM ledger WHERE owner = ? AND action <> 'rename'"
            " ORDER BY id DESC LIMIT 1",
            (OWNER,),
        ).fetchone()
        if row is None:
            return {"ok": False, "code": "none", "error": "没有可撤销的操作"}
        if row["action"] == "undo":
            return {"ok": False, "code": "none",
                    "error": "上一步已经撤销过，没有可继续撤销的操作"}
        if row["void"]:
            return {"ok": False, "code": "voided",
                    "error": "最近一条已被标记异常，先点「恢复」再撤销"}
        note = (row["note"] or "").strip()
        undoable = {"store", "set", "take", "clear", "add"}
        if row["action"] not in undoable:
            return {"ok": False, "code": "unsupported_op",
                    "error": "最新一条是未确认/部队类操作，不能跨过它撤销；请先处理这条"}
        if row["op_group"] or note.startswith(("搬入", "搬出", "改：搬入", "改：搬出")):
            return {"ok": False, "code": "unsupported_move",
                    "error": "搬移/改位置不能单独撤销：请再搬一次，或到「管理 · 快照」恢复"}
        if row["action"] in ("take", "store"):
            # 旧库没补 op_group、备注也被改过时，用同一秒的成对动作保守兜底
            others = conn.execute(
                "SELECT action, delta FROM ledger WHERE owner = ? AND id <> ?"
                " AND ts = ? AND action IN ('take','store')",
                (OWNER, row["id"], row["ts"]),
            ).fetchall()
            row_delta = int(row["delta"] or 0)
            for other in others:
                other_delta = int(other["delta"] or 0)
                if (other["action"] != row["action"] and row_delta and other_delta
                        and (row_delta > 0) != (other_delta > 0)):
                    return {
                        "ok": False, "code": "unsupported_move",
                        "error": "这条历史流水可能是搬移/改位置的一半，不能单独撤销；"
                                 "请到「管理 · 快照」恢复",
                    }

        character_id = row["character_id"]
        item_name = row["item_name"]
        location = row["location"]
        hq = row["hq"] or 0
        before = int(row["qty_after"] or 0) - int(row["delta"] or 0)
        if before < 0:
            before = 0

        current = conn.execute(
            "SELECT id, qty FROM stock WHERE owner = ? AND character_id = ?"
            " AND item_name = ? AND location = ? AND hq = ?",
            (OWNER, character_id, item_name, location, hq),
        ).fetchone()
        current_qty = int(current["qty"]) if current else 0
        if current_qty != int(row["qty_after"] or 0):
            return {"ok": False, "code": "crossed_ops",
                    "error": "这一格已被后续操作改动，不能安全撤销；请用快照恢复"}

        ts = now_iso()
        if before <= 0:
            if current:
                conn.execute("DELETE FROM stock WHERE id = ?", (current["id"],))
        elif current:
            conn.execute(
                "UPDATE stock SET qty = ?, updated_at = ? WHERE id = ?",
                (before, ts, current["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO stock(owner, item_id, item_name, character_id, location,"
                " hq, qty, source, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (OWNER, row["item_id"], item_name, character_id, location, hq,
                 before, "manual", ts),
            )

        conn.execute(
            "UPDATE ledger SET void = 1,"
            " note = CASE WHEN note = '' THEN '已被撤销' ELSE note END WHERE id = ?",
            (row["id"],),
        )
        # 这次写入如果记过采购批次，撤销时一起删掉，避免成本留下幽灵记录
        conn.execute("DELETE FROM purchase WHERE ledger_id = ?", (row["id"],))
        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (OWNER, ts, "undo", row["item_id"], item_name, character_id, location, hq,
             before - current_qty, before, f"撤销 #{row['id']}"),
        )
        conn.commit()

        chr_row = conn.execute(
            "SELECT name, world FROM character WHERE id = ?", (character_id,)
        ).fetchone()
        who = "?"
        if chr_row:
            who = chr_row["name"] + ("@" + chr_row["world"] if chr_row["world"] else "")
        return {
            "ok": True,
            "action": row["action"],
            "item_name": item_name,
            "location": location,
            "character": who,
            "before": before,
            "current": current_qty,
            "stack": stack_of(row["item_id"]),
        }
    finally:
        conn.close()


def _ips_from_ipconfig() -> list[str]:
    """Windows 兜底：直接解析 ipconfig 的 IPv4 地址。

    比 getaddrinfo 可靠——不依赖主机名解析，也不依赖有外网路由。
    """
    out: list[str] = []
    try:
        proc = subprocess.run(
            ["ipconfig"], capture_output=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = proc.stdout or b""
    except (OSError, subprocess.SubprocessError):
        return out
    for enc in ("gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = raw.decode(enc, errors="ignore")
            break
    for match in re.finditer(r"IPv4[^:：]*[:：]\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", text):
        ip = match.group(1)
        if ip not in out and not ip.startswith("127."):
            out.append(ip)
    return out


_LAN_ADDR_CACHE: tuple = (0.0, [])


def lan_addresses(force: bool = False) -> list[str]:
    """本机所有可用的局域网 IPv4（多网卡都列出来，VPN/Tailscale 也包含）。

    注意：`getaddrinfo(主机名)` 在 Windows 上可能卡好几秒，所以结果缓存 60 秒，
    并且先走「UDP 探测默认出口」这条快路径。
    """
    global _LAN_ADDR_CACHE
    now = time.time()
    cached_at, cached = _LAN_ADDR_CACHE
    if not force and cached and now - cached_at < 60:
        return list(cached)

    seen: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
            if ip and not ip.startswith("127."):
                seen.append(ip)
        finally:
            probe.close()
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in seen:
                seen.append(ip)
    except OSError:
        pass

    # 前两种都可能落空（主机名解析失败 / 没有默认路由），再读一次 ipconfig
    if not seen:
        seen.extend(_ips_from_ipconfig())

    _LAN_ADDR_CACHE = (now, seen)
    return seen


def lan_state(force: bool = False) -> dict:
    return {
        "ok": True,
        "enabled": bool(LAN_ENABLED),
        "addresses": lan_addresses(force),
        "listening": True,   # 始终监听，靠来源判断是否放行
        "port": _PORT,
    }


def set_lan(enabled: bool) -> dict:
    global LAN_ENABLED
    LAN_ENABLED = bool(enabled)
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('lan_enabled', ?)",
            ("1" if LAN_ENABLED else "0",),
        )
        conn.commit()
    finally:
        conn.close()
    print(
        "[手机] " + ("已开启，局域网可访问" if LAN_ENABLED else "已关闭，只允许本机访问"),
        flush=True,
    )
    return lan_state(force=True)


def load_lan_state() -> None:
    global LAN_ENABLED
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'lan_enabled'"
        ).fetchone()
    finally:
        conn.close()
    LAN_ENABLED = bool(row and str(row["value"]) == "1")


def backup_now() -> dict:
    """数据急救 → 立即备份：在 data/ 里存一份独立的 .bak.db（不进快照轮换）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = DATA_DIR / f"stock.{stamp}.bak.db"
    seq = 2
    while path.exists():
        path = DATA_DIR / f"stock.{stamp}-{seq}.bak.db"
        seq += 1
    try:
        _backup_current_to(path)
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"备份失败：{exc}"}
    return {"ok": True, "file": path.name, "size": path.stat().st_size}


def set_ledger_note(ledger_id: int, note: str) -> dict:
    """给一条流水补/改备注（只动 note，不碰数量金额）。"""
    note = (note or "").strip()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM ledger WHERE id = ? AND owner = ?", (ledger_id, OWNER)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "这条流水已经不存在了"}
        conn.execute("UPDATE ledger SET note = ? WHERE id = ?", (note, ledger_id))
        conn.commit()
        return {"ok": True, "id": ledger_id, "note": note}
    finally:
        conn.close()


def reconcile() -> dict:
    """对账：逐格比较「当前数量」与「流水累计」。

    历史流水有两处坑：早期写的是别名（铁锭）而库存存的是标准名（黑铁锭），
    早期也没有品质字段。所以这里按「物品 ID + 角色 + 位置」匹配，并把 HQ/NQ 合并统计。
    """
    conn = connect()
    try:
        stock_rows = conn.execute(
            """
            SELECT s.character_id, s.item_name, s.location, s.hq, s.qty, s.item_id,
                   c.name AS character, c.world
            FROM stock s JOIN character c ON c.id = s.character_id
            WHERE s.owner = ? AND s.location <> ?
            """,
            (OWNER, PENDING_LOC),
        ).fetchall()
        ledger_rows = conn.execute(
            "SELECT l.character_id, l.item_id, l.item_name, l.location, l.hq,"
            " l.delta, l.action, l.fc_id, c.name AS character, c.world,"
            " c.fc_id AS char_fc_id"
            " FROM ledger l LEFT JOIN character c ON c.id = l.character_id"
            " WHERE l.owner = ? AND l.location <> ? AND l.action IN"
            " ('store','set','take','clear','undo','add','pending','pending_move',"
            "  'pending_drop','fc_merged','fc_disbanded')",
            (OWNER, PENDING_LOC),
        ).fetchall()
    finally:
        conn.close()

    def item_key(item_id, name):
        if item_id:
            return ("id", int(item_id))
        resolved, _ = resolve_item(name or "")
        if resolved:
            return ("id", resolved)
        return ("name", (name or "?"))

    stock_sums: dict = {}
    stock_info: dict = {}
    for r in stock_rows:
        key = (r["character_id"], item_key(r["item_id"], r["item_name"]), r["location"])
        stock_sums[key] = stock_sums.get(key, 0) + int(r["qty"])
        info = stock_info.setdefault(
            key,
            {
                "character": r["character"],
                "world": r["world"],
                "item_name": (INDEX.by_id(r["item_id"]) or {}).get("name") or r["item_name"],
                "item_id": r["item_id"],
                "location": r["location"],
                "hq_any": 0,
            },
        )
        if r["hq"]:
            info["hq_any"] = 1

    ledger_sums: dict = {}
    ledger_info: dict = {}
    legacy_fc_credit: dict = {}
    for r in ledger_rows:
        key = (r["character_id"], item_key(r["item_id"], r["item_name"]), r["location"])
        if r["action"] == "fc_merged" and not (r["location"] or "").strip():
            # 旧版归并只写一条 location 为空的总账；按部队+物品记成可抵销量，
            # 抵掉被删除重复行留下的正数流水，避免旧账本永久误报。
            fc_key = (int(r["fc_id"] or 0), key[1])
            if fc_key[0]:
                legacy_fc_credit[fc_key] = legacy_fc_credit.get(fc_key, 0) + max(
                    0, -int(r["delta"] or 0)
                )
            continue
        ledger_sums[key] = ledger_sums.get(key, 0) + int(r["delta"] or 0)
        info = ledger_info.setdefault(
            key,
            {
                "character": r["character"] or "",
                "world": r["world"] or "",
                "item_name": (INDEX.by_id(r["item_id"]) or {}).get("name")
                or r["item_name"] or "",
                "item_id": r["item_id"] or 0,
                "location": r["location"],
                "fc_id": int(r["fc_id"] or 0) or int(r["char_fc_id"] or 0),
                "hq_any": 0,
            },
        )
        if r["hq"]:
            info["hq_any"] = 1

    # 旧 fc_merged 只对「部队柜里、只有流水没有库存」的格子生效，最多抵到 0
    for key in list(ledger_sums):
        if key in stock_sums or ledger_sums.get(key, 0) <= 0:
            continue
        info = ledger_info.get(key) or {}
        if info.get("location") != "部队柜" or not info.get("fc_id"):
            continue
        fc_key = (info["fc_id"], key[1])
        credit = legacy_fc_credit.get(fc_key, 0)
        if credit <= 0:
            continue
        used = min(credit, ledger_sums[key])
        ledger_sums[key] -= used
        legacy_fc_credit[fc_key] = credit - used
        if ledger_sums[key] == 0:
            del ledger_sums[key]

    issues = []
    for key in set(stock_sums) | set(ledger_sums):
        stock_qty = int(stock_sums.get(key, 0))
        ledger_qty = int(ledger_sums.get(key, 0))
        if stock_qty == ledger_qty:
            continue
        info = stock_info.get(key) or ledger_info.get(key) or {
            "character": "", "world": "", "item_name": "?",
            "item_id": 0, "location": "", "hq_any": 0,
        }
        if key not in stock_sums:
            issue_type = "ledger_only"
        elif key not in ledger_sums:
            issue_type = "stock_only"
        else:
            issue_type = "qty_diff"
        issues.append(
            {
                "type": issue_type,
                "character": info["character"],
                "world": info["world"],
                "item_name": info["item_name"],
                "item_id": info["item_id"],
                "location": info["location"],
                "hq_any": info["hq_any"],
                "stock_qty": stock_qty,
                "ledger_qty": ledger_qty,
                "current": stock_qty,
                "expected": ledger_qty,
                "diff": stock_qty - ledger_qty,
                "stack": stack_of(info["item_id"]),
            }
        )
    issues.sort(key=lambda x: -abs(x["diff"]))
    return {"ok": True, "checked": len(set(stock_sums) | set(ledger_sums)),
            "issues": issues}


def _db_detail(path: Path) -> dict:
    """读一个数据库的版本、日志模式、各表行数（只读）。"""
    conn = _open_db_ro(path)
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        counts = {}
        for table in ("character", "stock", "ledger", "alias"):
            if table in tables:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        try:
            journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        except sqlite3.Error:
            journal = ""
        version = ""
        if "meta" in tables:
            row = conn.execute("SELECT value FROM meta WHERE key = 'app_version'").fetchone()
            version = row[0] if row else ""
        return {"tables": tables, "counts": counts, "journal": journal, "version": version}
    finally:
        conn.close()


def preview_db_file(path: Path, name: str = "", size: int = 0) -> dict:
    """预检一个已落盘的 .db：只读打开、不替换。"""
    size = int(size or 0) or (path.stat().st_size if path.exists() else 0)
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return {
            "ok": True,
            "valid": False,
            "file": {"name": name, "size": size, "sqlite": False},
            "warnings": [f"读不了文件：{exc}"],
        }
    if not head.startswith(b"SQLite format 3\x00"):
        return {
            "ok": True,
            "valid": False,
            "file": {"name": name, "size": size, "sqlite": False},
            "warnings": ["这不是 SQLite 数据库文件（文件头不对）"],
        }

    warnings: list[str] = []
    try:
        incoming = _db_detail(path)
    except sqlite3.Error as exc:
        return {
            "ok": True,
            "valid": False,
            "file": {"name": name, "size": size, "sqlite": True},
            "warnings": [f"读不了这个数据库：{exc}"],
        }

    missing = [
        t for t in ("character", "stock", "ledger", "alias", "meta")
        if t not in incoming["tables"]
    ]
    if missing:
        warnings.append("缺少数据表：" + "、".join(missing))

    if incoming["journal"] == "wal":
        warnings.append(
            "这份文件是 WAL 模式：如果同目录还有 stock.db-wal，请一并提供，"
            "否则最近写入可能丢失"
        )

    try:
        current = _db_detail(DB_PATH) if DB_PATH.exists() else {"counts": {}, "version": ""}
    except sqlite3.Error:
        current = {"counts": {}, "version": ""}

    cur_ver = current.get("version") or VERSION
    if incoming["version"] and cur_ver and incoming["version"] != cur_ver:
        warnings.append(f"版本不同（当前 {cur_ver}，文件 {incoming['version']}），导入后请重启程序")

    return {
        "ok": True,
        "valid": not missing,
        "file": {"name": name, "size": size, "sqlite": True},
        "incoming": {
            "version": incoming["version"] or "",
            "characters": incoming["counts"].get("character", 0),
            "stock_rows": incoming["counts"].get("stock", 0),
            "ledger_rows": incoming["counts"].get("ledger", 0),
        },
        "current": {
            "version": cur_ver,
            "characters": current.get("counts", {}).get("character", 0),
            "stock_rows": current.get("counts", {}).get("stock", 0),
            "ledger_rows": current.get("counts", {}).get("ledger", 0),
        },
        "warnings": warnings,
    }


def inspect_db_file(path: Path, strict: bool = False) -> dict:
    """检查 .db 是否可读、是否本程序账本。

    strict=False 只查基础列（旧库可迁移）；strict=True 要求迁移后的全部列都在。
    """
    conn = _open_db_ro(path)
    required_columns = DB_ALL_COLUMNS if strict else DB_BASE_COLUMNS
    try:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = bool(row and str(row[0]).lower() == "ok")
        except sqlite3.Error:
            integrity = False
        missing_columns: dict[str, list[str]] = {}
        for table, cols in required_columns.items():
            if table not in tables:
                continue
            actual = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            missing = sorted(cols - actual)
            if missing:
                missing_columns[table] = missing
        counts = {}
        for table in ("character", "stock", "ledger", "alias"):
            if table in tables:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        missing_tables = sorted(CORE_TABLES - tables)
        if strict:
            missing_tables = sorted(set(required_columns) - tables)
        return {
            "ok": integrity and not missing_tables and not missing_columns,
            "integrity": integrity,
            "missing": missing_tables,
            "missing_columns": missing_columns,
            "tables": sorted(tables),
            "counts": counts,
        }
    finally:
        conn.close()


def _restore_result(ok: bool, error: str = "", *, replaced: bool = False,
                    rolled_back: bool = False, backup: str = "",
                    stage: str = "not_replaced", **extra) -> dict:
    result = {
        "ok": ok,
        "replaced": replaced,
        "rolled_back": rolled_back,
        "backup": backup,
        "stage": stage,
        "error": error,
    }
    result.update(extra)
    return result


def _install_db(src: Path, info: dict, pre_snapshot_note: str = "") -> dict:
    global _RESTORING
    with _restore_lock:
        _RESTORING = True
        tmp = DB_PATH.with_name("stock.restore.tmp")
        rollback_tmp = DB_PATH.with_name("stock.rollback.tmp")
        backup = ""
        backup_path: Path | None = None
        replaced = False
        rolled_back = False
        try:
            if pre_snapshot_note:
                create_snapshot("pre-restore", pre_snapshot_note)

            check = inspect_db_file(src)
            if not check["ok"]:
                detail = []
                if check.get("missing"):
                    detail.append("缺表：" + "、".join(check["missing"]))
                if check.get("missing_columns"):
                    detail.append("缺列：" + str(check["missing_columns"]))
                if not check.get("integrity"):
                    detail.append("完整性检查失败")
                return _restore_result(
                    False,
                    "候选数据库结构不完整或已损坏，未替换：" + "；".join(detail),
                    stage="not_replaced",
                )

            if DB_PATH.exists():
                backup_path = DB_PATH.with_name(
                    f"stock.bak-{datetime.now():%Y%m%d-%H%M%S}.db"
                )
                seq = 2
                while backup_path.exists():
                    backup_path = DB_PATH.with_name(
                        f"stock.bak-{datetime.now():%Y%m%d-%H%M%S}-{seq}.db"
                    )
                    seq += 1
                try:
                    _backup_current_to(backup_path)
                except (OSError, sqlite3.Error) as exc:
                    return _restore_result(
                        False, f"恢复前备份失败，未替换：{exc}", stage="not_replaced"
                    )
                backup = backup_path.name

            shutil.copyfile(src, tmp)
            if not _db_integrity_ok(tmp):
                return _restore_result(
                    False, "新数据库完整性检查失败，未替换", backup=backup,
                    stage="not_replaced",
                )
            try:
                cand = sqlite3.connect(tmp)
                try:
                    cand.row_factory = sqlite3.Row
                    _migrate_conn(cand)
                    cand.commit()
                finally:
                    cand.close()
            except sqlite3.Error as exc:
                return _restore_result(
                    False, f"候选数据库无法迁移到当前结构，未替换：{exc}",
                    backup=backup, stage="not_replaced",
                )
            check = inspect_db_file(tmp, strict=True)
            if not check["ok"]:
                detail = []
                if check.get("missing"):
                    detail.append("缺表：" + "、".join(check["missing"]))
                if check.get("missing_columns"):
                    detail.append("缺列：" + str(check["missing_columns"]))
                return _restore_result(
                    False, "新数据库迁移后仍缺必需表/字段，未替换：" + "；".join(detail),
                    backup=backup, stage="not_replaced",
                )
            _replace_with_retry(tmp, DB_PATH)
            replaced = True
            for suffix in ("-wal", "-shm"):
                side = Path(str(DB_PATH) + suffix)
                if side.exists():
                    try:
                        side.unlink()
                    except OSError:
                        pass
            init_db()
            load_aliases()
            return _restore_result(
                True, backup=backup, replaced=True, stage="replaced",
                counts=info["counts"], db=str(DB_PATH),
            )
        except (OSError, sqlite3.Error) as exc:
            if replaced and backup_path and backup_path.exists():
                try:
                    shutil.copyfile(backup_path, rollback_tmp)
                    _replace_with_retry(rollback_tmp, DB_PATH)
                    for suffix in ("-wal", "-shm"):
                        side = Path(str(DB_PATH) + suffix)
                        if side.exists():
                            side.unlink()
                    init_db()
                    load_aliases()
                    rolled_back = True
                except (OSError, sqlite3.Error):
                    rolled_back = False
            return _restore_result(
                False,
                f"恢复失败：{exc}" + ("；已回滚到原库" if rolled_back
                                      else "；回滚失败，请用备份恢复"),
                replaced=replaced,
                rolled_back=rolled_back,
                backup=backup,
                stage="rolled_back" if rolled_back else
                      ("rollback_failed" if replaced else "not_replaced"),
            )
        finally:
            _RESTORING = False
            for path in (tmp, rollback_tmp):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def restore_db_from_path(src: str) -> int:
    path = Path(src).expanduser()
    if not path.exists():
        print(f"找不到文件：{path}")
        return 1
    try:
        info = inspect_db_file(path)
    except sqlite3.Error as exc:
        print(f"不是有效的 SQLite 文件：{exc}")
        return 1
    if not info["ok"]:
        print("这个文件不完整或已损坏（需要 character/stock/ledger 且完整性检查通过）")
        return 1
    result = _install_db(path, info)
    if not result.get("ok"):
        print(result.get("error") or "恢复失败")
        print(f"  replaced={result.get('replaced')} rolled_back={result.get('rolled_back')}"
              f" backup={result.get('backup') or '-'} stage={result.get('stage')}")
        return 1
    print("恢复完成：")
    for key, label in (("character", "角色"), ("stock", "库存"), ("ledger", "流水"), ("alias", "备注名")):
        print(f"  {label}：{result['counts'].get(key, 0)}")
    if result["backup"]:
        print(f"  原数据库已备份为 {result['backup']}")
    print("现在可以正常运行了。")
    return 0


def _move_to_pending(conn: sqlite3.Connection, row, ts: str) -> None:
    """把一行库存转进「未确认」，同角色同材料同品质的已存在则合并数量。

    注意：**保留原 updated_at**（那是物品真实的最后变动时间），
    迁入时间单独记在 pending_since，避免"放了 20 天"被重置成今天。
    """
    old = conn.execute(
        "SELECT id, qty FROM stock WHERE owner = ? AND character_id = ?"
        " AND location = ? AND item_name = ? AND hq = ?",
        (OWNER, row["character_id"], PENDING_LOC, row["item_name"], row["hq"]),
    ).fetchone()
    if old:
        conn.execute(
            "UPDATE stock SET qty = qty + ?, pending_since = ? WHERE id = ?",
            (int(row["qty"]), ts, old["id"]),
        )
        conn.execute("DELETE FROM stock WHERE id = ?", (row["id"],))
    else:
        conn.execute(
            "UPDATE stock SET location = ?, pending_from = ?, pending_since = ?,"
            " fc_id = 0 WHERE id = ?",
            (PENDING_LOC, row["location"], ts, row["id"]),
        )


def delete_location_stock(character_id: int, location: str) -> dict:
    """删掉一个雇员栏位：库存不删，转入「未确认」。先打快照。"""
    location = (location or "").strip()
    if not location:
        return {"ok": False, "error": "位置为空"}

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, item_id, item_name, hq, qty, character_id, location FROM stock"
            " WHERE owner = ? AND character_id = ? AND location = ?",
            (OWNER, character_id, location),
        ).fetchall()
        if not rows:
            return {"ok": False, "error": f"这个位置里没有库存（{location}）"}
        for r in rows:
            old = conn.execute(
                "SELECT qty FROM stock WHERE owner = ? AND character_id = ?"
                " AND location = ? AND item_name = ? AND hq = ?",
                (OWNER, character_id, PENDING_LOC, r["item_name"], r["hq"]),
            ).fetchone()
            if old and int(old["qty"]) + int(r["qty"]) > MAX_ITEM_QTY:
                return {"ok": False, "invalid": True,
                        "error": f"转入未确认后合并超过上限 {MAX_ITEM_QTY}"}
        total_qty = sum(int(r["qty"]) for r in rows)
    finally:
        conn.close()

    snap = create_snapshot("manual", f"删除位置 {location} 前的自动存档")

    ts = now_iso()
    conn = connect()
    try:
        for r in rows:
            _move_to_pending(conn, r, ts)
            conn.execute(
                "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
                " location, hq, delta, qty_after, note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (OWNER, ts, "pending", r["item_id"], r["item_name"], character_id,
                 location, r["hq"], -int(r["qty"]), 0,
                 f"删除位置 {location}，库存转入未确认"),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "location": location,
        "rows": len(rows),
        "qty": total_qty,
        "moved_to_pending": True,
        "snapshot": snap.get("name", "") if isinstance(snap, dict) else "",
    }


def pending_stock() -> dict:
    """未确认库存：按材料 + 品质聚合。"""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.item_id, s.item_name, s.hq, s.qty, s.updated_at,
                   s.pending_from, s.pending_since,
                   s.character_id, c.name AS character, c.world
            FROM stock s
            LEFT JOIN character c ON c.id = s.character_id
            WHERE s.owner = ? AND s.location = ?
            ORDER BY s.item_name, c.name
            """,
            (OWNER, PENDING_LOC),
        ).fetchall()
    finally:
        conn.close()

    items: dict = {}
    for r in rows:
        key = (r["item_name"], r["hq"])
        bucket = items.setdefault(
            key,
            {"item_name": r["item_name"], "item_id": r["item_id"], "hq": r["hq"],
             "total": 0, "rows": []},
        )
        bucket["total"] += int(r["qty"])
        age = days_since(r["updated_at"] or "")
        since = r["pending_since"] or ""
        bucket["rows"].append(
            {
                "stock_id": r["id"],
                "character_id": r["character_id"],
                "character": r["character"] or "",
                "world": r["world"] or "",
                "qty": r["qty"],
                "location": PENDING_LOC,
                "origin": r["pending_from"] or "",
                "updated_at": r["updated_at"],
                # age_text：这堆货最后一次真实变动是多久前（保留原值，不被迁移覆盖）
                "age_text": human_age(age),
                # moved_text：什么时候被转到未确认的
                "pending_since": since,
                "moved_text": human_age(days_since(since)) if since else "",
            }
        )

    return {
        "ok": True,
        "summary": {"rows": len(rows), "qty": sum(int(r["qty"]) for r in rows)},
        "items": list(items.values()),
    }


def _valid_target_location(
    conn: sqlite3.Connection, character_id: int, location: str, allowed: set
) -> bool:
    """目标位置白名单：固定位置，或 雇员1..10。

    雇员槽位上限 10（FF14 实际可拥有的最大数量）：
    用户可能刚买了一个新雇员（账本里还没记过），此时也要能把库存转进去。
    """
    if location in allowed:
        return True
    m = re.match(r"^雇员(\d+)$", location or "")
    if not m:
        return False
    return 1 <= int(m.group(1)) <= 10


def pending_move(stock_id: int, character_id: int, location: str) -> dict:
    """把一条未确认库存转给某个角色的某个位置；目标格已有则数量相加。"""
    location = (location or "").strip()
    if not location:
        return {"ok": False, "error": "请选择目标位置"}
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, item_id, item_name, hq, qty, character_id, pending_from"
            " FROM stock WHERE id = ? AND owner = ? AND location = ?",
            (stock_id, OWNER, PENDING_LOC),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "这条未确认库存已经不存在了（刷新看看）"}
        chr_row = conn.execute(
            "SELECT id, name, world, fc_id FROM character WHERE id = ? AND owner = ?",
            (character_id, OWNER),
        ).fetchone()
        if chr_row is None:
            return {"ok": False, "error": "目标角色不存在"}

        # 目标位置白名单：固定位置，或该角色已经用过的雇员槽（允许再往后一格）
        allowed = set(DEFAULT_LOCATIONS)
        if not _valid_target_location(conn, character_id, location, allowed):
            return {"ok": False, "error": f"目标位置不合法：{location}"}

        # 备注里要写「原属 谁 / 哪里」，不能写目的地
        origin_who = ""
        if row["character_id"]:
            origin_chr = conn.execute(
                "SELECT name, world FROM character WHERE id = ?", (row["character_id"],)
            ).fetchone()
            if origin_chr:
                origin_who = origin_chr["name"]
                if origin_chr["world"]:
                    origin_who += f"@{origin_chr['world']}"
        origin_where = row["pending_from"] or PENDING_LOC
        origin_text = f"{origin_who} / {origin_where}".strip(" /")

        ts = now_iso()
        fc_id = int(chr_row["fc_id"] or 0) if location == "部队柜" else 0
        target = conn.execute(
            "SELECT id, qty FROM stock WHERE owner = ? AND character_id = ?"
            " AND location = ? AND item_name = ? AND hq = ?",
            (OWNER, character_id, location, row["item_name"], row["hq"]),
        ).fetchone()
        if target:
            new_qty = int(target["qty"]) + int(row["qty"])
            if new_qty > MAX_ITEM_QTY:
                return {"ok": False, "invalid": True,
                        "error": f"目标格合并后超过上限 {MAX_ITEM_QTY}"}
            conn.execute(
                "UPDATE stock SET qty = ?, fc_id = ?, pending_from = '',"
                " pending_since = '', updated_at = ? WHERE id = ?",
                (new_qty, fc_id, ts, target["id"]),
            )
            conn.execute("DELETE FROM stock WHERE id = ?", (row["id"],))
            merged = True
        else:
            new_qty = int(row["qty"])
            conn.execute(
                "UPDATE stock SET character_id = ?, location = ?, fc_id = ?,"
                " pending_from = '', pending_since = '', updated_at = ? WHERE id = ?",
                (character_id, location, fc_id, ts, row["id"]),
            )
            merged = False

        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (OWNER, ts, "pending_move", row["item_id"], row["item_name"], character_id,
             location, row["hq"], int(row["qty"]), new_qty,
             f"未确认库存转入（原属 {origin_text}）"),
        )
        conn.commit()
        return {
            "ok": True,
            "item_name": row["item_name"],
            "qty": int(row["qty"]),
            "target_qty": new_qty,
            "merged": merged,
            "character": chr_row["name"],
            "location": location,
        }
    finally:
        conn.close()


def pending_drop(stock_id: int, note: str = "") -> dict:
    """丢弃一条未确认库存（真正删除），先打快照。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, item_id, item_name, hq, qty, character_id FROM stock"
            " WHERE id = ? AND owner = ? AND location = ?",
            (stock_id, OWNER, PENDING_LOC),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "这条未确认库存已经不存在了（刷新看看）"}

    snap = create_snapshot("auto", f"丢弃未确认库存 {row['item_name']}")
    conn = connect()
    try:
        conn.execute("DELETE FROM stock WHERE id = ?", (row["id"],))
        conn.execute(
            "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
            " location, hq, delta, qty_after, note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (OWNER, now_iso(), "pending_drop", row["item_id"], row["item_name"],
             row["character_id"], PENDING_LOC, row["hq"], -int(row["qty"]), 0,
             ("丢弃未确认库存：" + (note or "")).strip("：")),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "item_name": row["item_name"],
        "qty": int(row["qty"]),
        "snapshot": snap.get("name", "") if isinstance(snap, dict) else "",
    }


def fc_orphans() -> dict:
    """「部队柜」里、但没归到任何部队的库存。

    0.7.3 升级上来的用户必然有这类行：那时还没有部队概念，部队柜只是普通位置。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT s.qty, s.item_name, c.name AS character"
            " FROM stock s LEFT JOIN character c ON c.id = s.character_id"
            " WHERE s.owner = ? AND s.location = '部队柜'"
            " AND (s.fc_id = 0 OR s.fc_id NOT IN (SELECT id FROM fc WHERE owner = ?))",
            (OWNER, OWNER),
        ).fetchall()
    finally:
        conn.close()
    names = []
    for r in rows:
        n = r["character"] or ""
        if n and n not in names:
            names.append(n)
    return {
        "rows": len(rows),
        "qty": sum(int(r["qty"]) for r in rows),
        "characters": names,
    }


def fc_adopt_orphans() -> dict:
    """把未归属的部队柜库存归到一个指定部队（要求角色已在该部队）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE stock SET fc_id = ("
            "  SELECT fc_id FROM character c WHERE c.id = stock.character_id AND c.fc_id > 0"
            ")"
            " WHERE owner = ? AND location = '部队柜' AND fc_id = 0",
            (OWNER,),
        )
        conn.commit()
        return {"ok": True, "adopted": cur.rowcount}
    finally:
        conn.close()


def list_fc() -> list[dict]:
    conn = connect()
    try:
        fcs = conn.execute(
            "SELECT id, name, world, created_at FROM fc WHERE owner = ? ORDER BY id",
            (OWNER,),
        ).fetchall()
        chars = conn.execute(
            "SELECT id, name, world, account, fc_id FROM character WHERE owner = ? ORDER BY name",
            (OWNER,),
        ).fetchall()
        stats = conn.execute(
            "SELECT fc_id, COUNT(*) AS n, COALESCE(SUM(qty), 0) AS q"
            " FROM stock WHERE owner = ? AND fc_id > 0 GROUP BY fc_id",
            (OWNER,),
        ).fetchall()
    finally:
        conn.close()

    members: dict[int, list] = {}
    for c in chars:
        if c["fc_id"]:
            members.setdefault(int(c["fc_id"]), []).append(
                {"id": c["id"], "name": c["name"], "world": c["world"], "account": c["account"]}
            )
    stat_map = {int(r["fc_id"]): {"rows": r["n"], "qty": r["q"]} for r in stats}

    out = []
    for f in fcs:
        s = stat_map.get(int(f["id"]), {"rows": 0, "qty": 0})
        out.append(
            {
                "id": f["id"],
                "name": f["name"],
                "world": f["world"],
                "members": members.get(int(f["id"]), []),
                "stock_rows": s["rows"],
                "stock_qty": s["qty"],
            }
        )
    return out


def save_fc(name: str, world: str = "", fc_id: int = 0) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "部队名不能为空"}
    conn = connect()
    try:
        if fc_id:
            row = conn.execute(
                "SELECT id FROM fc WHERE id = ? AND owner = ?", (fc_id, OWNER)
            ).fetchone()
            if not row:
                return {"ok": False, "error": "部队不存在"}
            conn.execute(
                "UPDATE fc SET name = ?, world = ? WHERE id = ?",
                (name, (world or "").strip(), fc_id),
            )
            conn.commit()
            return {"ok": True, "id": fc_id, "name": name}
        try:
            cur = conn.execute(
                "INSERT INTO fc(owner, name, world, created_at) VALUES(?,?,?,?)",
                (OWNER, name, (world or "").strip(), now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return {"ok": False, "error": f"已经有叫「{name}」的部队了"}
        return {"ok": True, "id": cur.lastrowid, "name": name}
    finally:
        conn.close()


def set_character_fc(character_id: int, fc_id: int) -> dict:
    conn = connect()
    try:
        ch = conn.execute(
            "SELECT id FROM character WHERE id = ? AND owner = ?", (character_id, OWNER)
        ).fetchone()
        if not ch:
            return {"ok": False, "error": "角色不存在"}
        if fc_id:
            f = conn.execute(
                "SELECT id FROM fc WHERE id = ? AND owner = ?", (fc_id, OWNER)
            ).fetchone()
            if not f:
                return {"ok": False, "error": "部队不存在"}
        conn.execute(
            "UPDATE character SET fc_id = ? WHERE id = ?", (fc_id, character_id)
        )
        conn.commit()
        return {"ok": True, "character_id": character_id, "fc_id": fc_id}
    finally:
        conn.close()


def _fc_stock_clause() -> str:
    """部队柜库存的判定：显式打了 fc_id，或「位置=部队柜 且 角色属于该部队」。

    后半个条件是给老数据的：先录库存后加入部队的行，fc_id 还是 0。
    """
    return (
        "s.owner = ? AND (s.fc_id = ? OR (s.fc_id = 0 AND s.location = '部队柜'"
        " AND s.character_id IN (SELECT id FROM character WHERE owner = ? AND fc_id = ?)))"
    )


def fc_stock(fc_id: int) -> dict:
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT s.id, s.item_id, s.item_name, s.location, s.hq, s.qty, s.updated_at,
                   c.name AS character, c.world
            FROM stock s
            LEFT JOIN character c ON c.id = s.character_id
            WHERE {_fc_stock_clause()}
            ORDER BY s.item_name, c.name
            """,
            (OWNER, fc_id, OWNER, fc_id),
        ).fetchall()
    finally:
        conn.close()

    items: dict = {}
    for r in rows:
        key = (r["item_name"], r["hq"])
        bucket = items.setdefault(
            key,
            {"item_name": r["item_name"], "item_id": r["item_id"], "hq": r["hq"],
             "total": 0, "rows": []},
        )
        bucket["total"] += int(r["qty"])
        bucket["rows"].append(
            {
                "stock_id": r["id"],
                "character": r["character"] or "",
                "world": r["world"] or "",
                "location": r["location"],
                "qty": r["qty"],
                "updated_at": r["updated_at"],
            }
        )
    rows_total = len(rows)
    qty_total = sum(int(r["qty"]) for r in rows)
    for item in items.values():
        item["name"] = item["item_name"]
        item["qty"] = item["total"]
    return {
        "ok": True,
        "items": list(items.values()),
        "rows": rows_total,
        "qty": qty_total,
        "total_qty": qty_total,
    }


def fc_conflicts(fc_id: int) -> dict:
    """同一件材料被多个角色各记了一份（部队柜的经典问题）。"""
    data = fc_stock(fc_id)
    conflicts = []
    for item in data["items"]:
        if len(item["rows"]) > 1:
            latest = max(item["rows"], key=lambda r: r["updated_at"] or "")
            entries = []
            for r in item["rows"]:
                entries.append(
                    dict(r, age_text=human_age(days_since(r.get("updated_at") or "")))
                )
            conflicts.append(
                {
                    "item_name": item["item_name"],
                    "item_id": item["item_id"],
                    "hq": item["hq"],
                    # 两组别名：entries/conflicts 是我这边的命名，rows/groups 是界面读的
                    "entries": entries,
                    "rows": entries,
                    "suggested_qty": latest["qty"],
                    "suggested": latest["qty"],
                    "suggested_from": latest["character"],
                }
            )
    return {
        "ok": True,
        "conflicts": conflicts,
        "groups": conflicts,
        "count": len(conflicts),
    }


def fc_merge_apply(fc_id: int, items: list) -> dict:
    """把多行合成一行：数量由用户确认，其余行删除，写 fc_merged 流水。"""
    if not items:
        return {"ok": False, "error": "没有要归并的条目"}
    merge_snap = create_snapshot("auto", "部队归并前自动存档")
    merged = 0
    conn = connect()
    try:
        for it in items:
            name = str(it.get("item_name") or "").strip()
            hq = 1 if it.get("hq") else 0
            qty = int(it.get("qty") or 0)
            if not name or qty < 0:
                continue
            if qty > MAX_ITEM_QTY:
                continue
            rows = conn.execute(
                f"SELECT s.id, s.qty, s.character_id, s.location, s.item_id,"
                f" s.item_name, s.hq FROM stock s"
                f" WHERE {_fc_stock_clause()} AND s.item_name = ? AND s.hq = ?",
                (OWNER, fc_id, OWNER, fc_id, name, hq),
            ).fetchall()
            if not rows:
                continue
            keep_id = int(it.get("keep_stock_id") or rows[0]["id"])
            keep = next((r for r in rows if int(r["id"]) == keep_id), rows[0])
            ts = now_iso()
            for r in rows:
                if int(r["id"]) == int(keep["id"]):
                    continue
                conn.execute("DELETE FROM stock WHERE id = ?", (r["id"],))
                conn.execute(
                    "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
                    " location, hq, fc_id, delta, qty_after, note)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (OWNER, ts, "fc_merged", r["item_id"], r["item_name"],
                     r["character_id"], r["location"], r["hq"], fc_id,
                     -int(r["qty"]), 0,
                     f"部队归并：{len(rows)} 行 → 1 行（删除重复行）"),
                )
            keep_delta = qty - int(keep["qty"])
            conn.execute(
                "UPDATE stock SET qty = ?, fc_id = ?, updated_at = ? WHERE id = ?",
                (qty, fc_id, ts, keep["id"]),
            )
            if keep_delta:
                conn.execute(
                    "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
                    " location, hq, fc_id, delta, qty_after, note)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (OWNER, ts, "fc_merged", keep["item_id"], keep["item_name"],
                     keep["character_id"], keep["location"], keep["hq"], fc_id,
                     keep_delta, qty, f"部队归并：保留行数量调整为 {qty}"),
                )
            merged += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "merged": merged,
        "snapshot": merge_snap.get("name", "") if isinstance(merge_snap, dict) else "",
    }


def fc_delete(fc_id: int, mode: str) -> dict:
    """解散部队。mode=void 作废并留痕；mode=empty 表示已搬空。"""
    mode = "empty" if mode == "empty" else "void"
    conn = connect()
    try:
        f = conn.execute(
            "SELECT id, name FROM fc WHERE id = ? AND owner = ?", (fc_id, OWNER)
        ).fetchone()
        if not f:
            return {"ok": False, "error": "部队不存在"}
        rows = conn.execute(
            "SELECT s.id, s.item_id, s.item_name, s.character_id, s.location, s.hq, s.qty"
            f" FROM stock s WHERE {_fc_stock_clause()}",
            (OWNER, fc_id, OWNER, fc_id),
        ).fetchall()
    finally:
        conn.close()

    snap = create_snapshot("manual", f"解散部队「{f['name']}」前的自动存档")

    voided = 0
    if mode == "void" and rows:
        conn = connect()
        try:
            ts = now_iso()
            for r in rows:
                conn.execute("DELETE FROM stock WHERE id = ?", (r["id"],))
                conn.execute(
                    "INSERT INTO ledger(owner, ts, action, item_id, item_name, character_id,"
                    " location, hq, fc_id, delta, qty_after, note)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (OWNER, ts, "fc_disbanded", r["item_id"], r["item_name"],
                     r["character_id"], r["location"], r["hq"], fc_id,
                     -int(r["qty"]), 0, f"部队「{f['name']}」解散，库存作废"),
                )
            conn.commit()
        finally:
            conn.close()
        voided = len(rows)

    conn = connect()
    try:
        conn.execute(
            "UPDATE character SET fc_id = 0 WHERE owner = ? AND fc_id = ?",
            (OWNER, fc_id),
        )
        conn.execute("DELETE FROM fc WHERE id = ?", (fc_id,))
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "mode": mode,
        "voided_rows": voided,
        "voided": voided,
        "snapshot": snap.get("name", "") if isinstance(snap, dict) else "",
    }


def add_character(account: str, name: str, world: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "角色名不能为空"}
    conn = connect()
    try:
        try:
            conn.execute(
                "INSERT INTO character(owner, account, name, world, created_at)"
                " VALUES(?,?,?,?,?)",
                (OWNER, (account or "").strip(), name, (world or "").strip(), now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return {"ok": False, "error": f"{name}@{world or '未填服务器'} 已存在"}
        return {"ok": True}
    finally:
        conn.close()


def clear_demo() -> int:
    conn = connect()
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM character WHERE owner = ? AND account LIKE ?",
                (OWNER, DEMO_PREFIX + "%"),
            ).fetchall()
        ]
        for cid in ids:
            conn.execute("DELETE FROM stock WHERE character_id = ?", (cid,))
            conn.execute("DELETE FROM ledger WHERE character_id = ?", (cid,))
            conn.execute("DELETE FROM character WHERE id = ?", (cid,))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 示例数据（首次启动，界面里会明确标注）
# --------------------------------------------------------------------------

def seed_demo() -> None:
    conn = connect()
    try:
        exists = conn.execute("SELECT COUNT(*) AS n FROM character WHERE owner = ?", (OWNER,)).fetchone()["n"]
        if exists:
            return
    finally:
        conn.close()

    demo_chars = [
        ("示例账号A", "阿辉", "豆豆柴"),
        ("示例账号A", "老陈", "豆豆柴"),
        ("示例账号B", "阿吉", "猫小胖"),
    ]
    for account, name, world in demo_chars:
        add_character(account, name, world)

    chars = {c["name"]: c["id"] for c in list_characters()}
    demo_stock = [
        ("阿辉", "黑铁锭", "背包", 640, 0),
        ("阿辉", "黑铁锭", "雇员1", 500, 0),
        ("阿辉", "玄铁矿", "雇员2", 120, 0),
        ("老陈", "黑铁锭", "部队柜", 100, 0),
        ("老陈", "亚麻", "背包", 300, 0),
        ("阿吉", "黑铁锭", "背包", 60, 1),
        ("阿吉", "玄铁矿", "雇员1", 40, 0),
    ]
    for char_name, item, loc, qty, hq in demo_stock:
        cid = chars.get(char_name)
        if cid:
            record_stock(cid, item, loc, qty, hq)


# --------------------------------------------------------------------------
# 图标（国服 CSV 没有 Icon 列，按需从 XIVAPI 取单个物品，落盘缓存）
# --------------------------------------------------------------------------

ICON_CACHE_PATH = DATA_DIR / "icon_map.json"
STACK_CACHE_PATH = DATA_DIR / "stack_map.json"

_icon_cache: dict[int, str] = {}
_stack_cache: dict[int, int] = {}
_icon_fail_at: dict[int, float] = {}
_icon_lock = threading.Lock()
ICON_RETRY_SECONDS = 120.0   # 取失败后多久允许重试


def load_stack_cache() -> None:
    if not STACK_CACHE_PATH.exists():
        return
    try:
        raw = json.loads(STACK_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        try:
            _stack_cache[int(key)] = int(value)
        except (TypeError, ValueError):
            continue


def save_stack_cache() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        good = {str(k): v for k, v in _stack_cache.items() if v}
        STACK_CACHE_PATH.write_text(
            json.dumps(good, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def stack_of(item_id: int) -> int:
    """物品的堆叠上限：优先用物品表里的，其次用按需取回来的缓存，都没有返回 0。"""
    if not item_id:
        return 0
    info = INDEX.by_id(item_id) if INDEX.loaded else None
    if info and info.get("stack"):
        return int(info["stack"])
    with _icon_lock:
        return int(_stack_cache.get(item_id, 0))


def load_icon_cache() -> None:
    if not ICON_CACHE_PATH.exists():
        return
    try:
        raw = json.loads(ICON_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        try:
            _icon_cache[int(key)] = str(value)
        except ValueError:
            continue


def save_icon_cache() -> None:
    """只把成功拿到的 URL 落盘；失败不写，下次还能重试。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        good = {str(k): v for k, v in _icon_cache.items() if v}
        ICON_CACHE_PATH.write_text(
            json.dumps(good, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def tex_to_url(tex_path: str) -> str:
    path = (tex_path or "").strip().lstrip("/")
    if not path or not path.startswith("ui/"):
        return ""
    return f"https://beta.xivapi.com/api/1/asset?path={path}&format=png"


def fetch_icons(item_ids: list[int], timeout: float = 6.0) -> dict[int, str]:
    """用一个 keep-alive 连接批量取图标。

    每条都新建 HTTPS 连接的话，国服网络下光 TLS 握手就可能吃掉 1~2 秒，
    排在第一个的物品（常常是搜索结果里最主要的那个）必然超时 —— 这正是
    「铁锭没图标、后面的物品有图标」的原因。
    """
    out: dict[int, str] = {}
    if not item_ids:
        return out
    stack_got = False
    conn = None
    try:
        conn = http.client.HTTPSConnection("beta.xivapi.com", timeout=timeout)
        for item_id in item_ids:
            try:
                conn.request(
                    "GET",
                    f"/api/1/sheet/Item/{item_id}?fields=Icon,StackSize",
                    headers={
                        "User-Agent": f"ff14-stock/{VERSION}",
                        "Connection": "keep-alive",
                    },
                )
                resp = conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    continue
                payload = json.loads(body.decode("utf-8"))
                fields = payload.get("fields") or {}
                icon = fields.get("Icon") or {}
                url = tex_to_url(icon.get("path_hr1") or icon.get("path") or "")
                if url:
                    out[item_id] = url
                try:
                    stack_size = int(fields.get("StackSize") or 0)
                except (TypeError, ValueError):
                    stack_size = 0
                if stack_size > 0:
                    with _icon_lock:
                        _stack_cache[item_id] = stack_size
                    stack_got = True
            except Exception:  # noqa: BLE001 - 单条失败不影响其余
                break
    except Exception:  # noqa: BLE001
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if stack_got:
            save_stack_cache()
    return out


def icons_for(item_ids: list[int]) -> dict[str, str]:
    """批量解析图标地址：优先本地映射/缓存，缺的才联网，失败进冷却期可重试。"""
    out: dict[str, str] = {}
    pending: list[int] = []
    now = time.monotonic()

    for item_id in dict.fromkeys(item_ids):
        if not item_id:
            continue
        known = (INDEX.by_id(item_id) or {}).get("icon", 0) if INDEX.loaded else 0
        if known:
            out[str(item_id)] = icon_url(known)
            continue
        with _icon_lock:
            cached = _icon_cache.get(item_id, "")
            failed_at = _icon_fail_at.get(item_id, 0.0)
        if cached:
            out[str(item_id)] = cached
            continue
        if now - failed_at < ICON_RETRY_SECONDS:
            continue
        pending.append(item_id)

    if not pending:
        return out

    fetched = fetch_icons(pending[:24])
    with _icon_lock:
        stamp = time.monotonic()
        for item_id in pending[:24]:
            url = fetched.get(item_id, "")
            if url:
                _icon_cache[item_id] = url
            else:
                _icon_fail_at[item_id] = stamp
        if fetched:
            save_icon_cache()

    for item_id, url in fetched.items():
        out[str(item_id)] = url
    return out


def probe_icon(item_id: int = 5057, timeout: float = 6.0) -> dict:
    """真的发一次图标请求，把成功/失败原因带回来，用于页面上的「诊断」。"""
    started = time.monotonic()
    result: dict = {
        "item_id": item_id,
        "http_status": 0,
        "icon_path": "",
        "url": "",
        "error": "",
        "elapsed_ms": 0,
    }
    conn = None
    try:
        conn = http.client.HTTPSConnection("beta.xivapi.com", timeout=timeout)
        conn.request(
            "GET",
            f"/api/1/sheet/Item/{item_id}?fields=Icon",
            headers={"User-Agent": f"ff14-stock/{VERSION}", "Connection": "keep-alive"},
        )
        resp = conn.getresponse()
        body = resp.read()
        result["http_status"] = resp.status
        if resp.status == 200:
            payload = json.loads(body.decode("utf-8"))
            icon = (payload.get("fields") or {}).get("Icon") or {}
            result["icon_path"] = icon.get("path_hr1") or icon.get("path") or ""
            result["url"] = tex_to_url(result["icon_path"])
        else:
            result["error"] = f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


# --------------------------------------------------------------------------
# 自检接口（浏览器打开 /api/diag 就能看到物品表到底认不认得出名字）
# --------------------------------------------------------------------------

def diag() -> dict:
    sample = [{"id": i, "name": n} for i, n in (INDEX._names[:8] if INDEX.loaded else [])]
    probe_name = ""
    probe_id = 0
    if INDEX.loaded:
        probe_name = INDEX.canonical_name("铁锭")
        for i, n in INDEX._names:
            if n == probe_name:
                probe_id = i
                break
    report = ""
    report_path = DATA_DIR / "unmatched.txt"
    if report_path.exists():
        try:
            report = report_path.read_text(encoding="utf-8").strip()
        except OSError:
            report = ""
    with _icon_lock:
        icon_known = len([v for v in _icon_cache.values() if v])
    iron_hits = [n for _i, n in INDEX._names if "铁" in n][:40] if INDEX.loaded else []
    similar = suggest_items("铁锭", 10)
    nearby: list = []
    if INDEX.loaded:
        for probe_row in range(5050, 5071):
            info = INDEX.by_id(probe_row)
            if info:
                nearby.append([probe_row, info["name"]])
    load_aliases()
    return {
        "version": VERSION,
        "items_loaded": INDEX.count,
        "items_error": INDEX.error or "",
        "items_sample": sample,
        "probe": {"input": "铁锭", "resolved": probe_name, "item_id": probe_id},
        "nearby_ids_5050_5070": nearby,
        "alias_count": len(_alias_cache),
        "iron_candidates": iron_hits,
        "similar": [s["name"] for s in similar],
        "icon_probe": probe_icon(probe_id or 5057),
        "unmatched_report": report,
        "icon_known": icon_known,
        "aliases_list": [
            {"alias": a, "item_id": v[0], "item_name": v[1]}
            for a, v in sorted(_alias_cache.items())
        ],
        "db": state(),
    }


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

def _default_world() -> str:
    """取第一个有服务器信息的角色所在服务器，作为查价默认区域。"""
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT world FROM character WHERE owner = ? AND world <> '' LIMIT 1",
                (OWNER,),
            ).fetchone()
        finally:
            conn.close()
        return (row["world"] if row else "") or ""
    except sqlite3.Error:
        return ""


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = f"ff14-stock/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        return

    # ---- 响应工具 ----

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"ok": False, "error": "文件不存在"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ----

    def _client_allowed(self) -> bool:
        """手机访问没开时，只放行本机请求。"""
        if LAN_ENABLED:
            return True
        ip = (self.client_address or ("",))[0]
        return ip in ("127.0.0.1", "::1", "localhost")

    def _is_local_client(self) -> bool:
        ip = (self.client_address or ("",))[0]
        return ip in ("127.0.0.1", "::1", "localhost")

    def _host_name_ok(self, name: str) -> bool:
        name = (name or "").strip().lower()
        if name in ("localhost", "127.0.0.1", "::1"):
            return True
        if LAN_ENABLED:
            return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", name))
        return False

    def _request_host(self) -> str:
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return ""
        if host.startswith("["):          # [::1]:8765
            return host[1:host.find("]")] if "]" in host else host[1:]
        if ":" in host:
            return host.rsplit(":", 1)[0]
        return host

    def _request_trusted(self) -> bool:
        """Host/Origin 校验：挡 DNS rebinding 和跨站写请求。"""
        if not self._host_name_ok(self._request_host()):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https"):
                return False
            if not self._host_name_ok(parsed.hostname or ""):
                return False
        return True

    def _content_type_ok(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def _csrf_ok(self) -> bool:
        token = self.headers.get("X-CSRF-Token") or ""
        return bool(token) and secrets.compare_digest(token, CSRF_TOKEN)

    def do_GET(self) -> None:  # noqa: N802 - 标准库要求的名字
        try:
            self._do_get()
        except ValueError as exc:
            log_error("GET", exc)
            self._safe_json({"ok": False, "error": f"参数不合法：{exc}"}, 400)
        except Exception as exc:  # noqa: BLE001
            log_error("GET", exc)
            self._safe_json({"ok": False, "error": f"服务处理请求失败：{exc}"}, 500)

    def _safe_json(self, payload, status: int = 200) -> None:
        """异常兜底时再写一次响应；头已发出就只能断开，不掩盖原始异常。"""
        try:
            self._json(payload, status)
        except Exception:  # noqa: BLE001 - 客户端可能已断开
            pass

    def _do_get(self) -> None:
        note_request()
        if not self._request_trusted():
            self._json({"ok": False, "error": "请求来源不受信任，已拒绝"}, 403)
            return
        if not self._client_allowed():
            self._json({"ok": False, "error": "手机访问未开启"}, 403)
            return
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        if route in LOCAL_ONLY_GET and not self._is_local_client():
            self._json({"ok": False, "error": "该功能只允许在本机使用"}, 403)
            return
        if _RESTORING and route.startswith("/api/"):
            self._json({"ok": False, "error": "正在恢复数据，请稍后再试"}, 503)
            return

        def arg(key: str) -> str:
            return (qs.get(key) or [""])[0]

        if route == "/api/state":
            self._json({"ok": True, **state(), "local": self._is_local_client()})
            return
        if route == "/api/characters":
            self._json({"ok": True, "characters": list_characters()})
            return
        if route == "/api/stock/export":
            self._json(export_stock())
            return
        if route == "/api/stock/safety-lines":
            try:
                self._json(safety_lines())
            except Exception as exc:  # noqa: BLE001
                log_error("GET /api/stock/safety-lines", exc)
                self._json({"ok": False, "error": f"读取安全线失败：{exc}"}, 500)
            return
        if route == "/api/unmapped":
            self._json({"ok": True, "items": unmapped_items()})
            return
        if route == "/api/search":
            try:
                try:
                    lim = int(arg("limit_items") or 0)
                except ValueError:
                    lim = 0
                result = search(
                    query=arg("q"),
                    account=arg("account"),
                    character=arg("character"),
                    location=arg("location"),
                    hq=arg("hq"),
                    limit_items=lim,
                )
            except Exception as exc:  # noqa: BLE001
                log_error(f"GET {route}", exc)
                self._json({"ok": False, "error": f"搜索失败：{exc}"}, 500)
                return
            self._json({"ok": True, **result})
            return
        if route == "/api/item_search":
            self._json({"ok": True, "items": suggest_items(arg("q"), 8)})
            return
        if route == "/api/aliases":
            if not _alias_loaded:
                load_aliases()
            conn = connect()
            try:
                rows = conn.execute(
                    "SELECT alias, item_id, item_name, created_at FROM alias"
                    " WHERE owner = ? ORDER BY item_id, alias",
                    (OWNER,),
                ).fetchall()
            finally:
                conn.close()
            self._json({"ok": True, "aliases": [dict(r) for r in rows]})
            return
        if route == "/api/item_search_en":
            self._json({"ok": True, "items": search_items_en(arg("q"))})
            return
        if route == "/api/icons":
            ids = [int(x) for x in re.findall(r"\d+", arg("ids"))]
            self._json({"ok": True, "icons": icons_for(ids)})
            return
        if route == "/api/diag":
            self._json({"ok": True, **diag()})
            return
        if route == "/api/item_by_id":
            raw_ids = re.findall(r"\d+", arg("id"))
            if not raw_ids:
                self._json({"ok": False, "error": "缺少 id"}, 400)
                return
            self._json({"ok": True, **lookup_item_by_id(int(raw_ids[0]))})
            return
        if route == "/api/ledger":
            try:
                limit = int(arg("limit") or 20)
            except ValueError:
                limit = 20
            self._json({"ok": True, "rows": ledger_recent(limit)})
            return
        if route == "/api/audit":
            check_state = arg("check")
            if not check_state and arg("only_checked") == "1":
                check_state = "1"
            self._json(
                audit_summary(
                    date_from=arg("from"),
                    date_to=arg("to"),
                    check_state=check_state,
                    character=arg("character"),
                    account=arg("account"),
                )
            )
            return
        if route == "/api/audit/export":
            check_state = arg("check")
            if not check_state and arg("only_checked") == "1":
                check_state = "1"
            self._json(
                audit_export(
                    date_from=arg("from"),
                    date_to=arg("to"),
                    check_state=check_state,
                    character=arg("character"),
                    account=arg("account"),
                )
            )
            return
        if route == "/api/stock/lookup":
            try:
                hq = int(arg("hq") or 0)
            except ValueError:
                hq = 0
            self._json(
                stock_lookup(
                    int(arg("character_id") or 0),
                    arg("location") or "背包",
                    arg("item_name"),
                    hq,
                )
            )
            return
        if route == "/api/fc":
            self._json({"ok": True, "fcs": list_fc(), "orphan": fc_orphans()})
            return
        if route == "/api/fc/stock":
            self._json(fc_stock(int(arg("fc_id") or 0)))
            return
        if route == "/api/fc/conflicts":
            self._json(fc_conflicts(int(arg("fc_id") or 0)))
            return
        if route == "/api/lan":
            self._json(lan_state())
            return
        if route == "/api/npc/meta":
            self._json({"ok": True, **npc_shop.NPC_META})
            return
        if route == "/api/npc/shop":
            ids = []
            for part in (arg("ids") or arg("id") or "").split(","):
                part = part.strip()
                if part.isdigit():
                    ids.append(int(part))
            self._json(npc_shop.npc_shop_lookup(ids))
            return
        if route == "/api/reconcile":
            self._json(reconcile())
            return
        if route == "/api/recipe/meta":
            self._json(recipe_meta())
            return
        if route == "/api/recipe/lists":
            self._json(recipe_list_meta())
            return
        if route == "/api/recipe/list":
            self._json(recipe_list_get(arg("name")))
            return
        if route == "/api/market/regions":
            self._json({"ok": True, "regions": market.regions()})
            return
        if route == "/api/market/worlds":
            dc = arg("dc").strip() or "豆豆柴"
            self._json({"ok": True, "dc": dc, "worlds": market.worlds_of(dc)})
            return
        if route == "/api/market/price":
            raw_ids = arg("ids") or arg("id") or ""
            ids = []
            for part in raw_ids.split(","):
                part = part.strip()
                if part.isdigit():
                    ids.append(int(part))
            region = arg("region").strip()
            if not region:
                region = _default_world()
            self._json(market.fetch_prices(region, ids))
            return
        if route == "/api/pending":
            self._json(pending_stock())
            return
        if route == "/api/cost/overview":
            self._json(cost_overview())
            return
        if route == "/api/purchase":
            try:
                hq = int(arg("hq") or 0)
            except ValueError:
                hq = 0
            name = (arg("item_name") or "").strip()
            if not name:
                self._json({"ok": False, "error": "缺少 item_name"})
                return
            summary = purchase_summary(name, hq)
            self._json(
                {
                    "ok": True,
                    "item_name": name,
                    "hq": hq,
                    "count": summary["count"],
                    "avg_cost": summary["avg_cost"],
                    "total_qty": summary["total_qty"],
                    "total_cost": summary["total_cost"],
                    "purchases": summary["rows"],
                    "batches": summary["rows"],
                    "rows": summary["rows"],
                }
            )
            return
        if route == "/api/data-dir":
            exists = DB_PATH.exists()
            self._json(
                {
                    "ok": True,
                    "dir": str(DATA_DIR),
                    "db": str(DB_PATH),
                    "db_exists": exists,
                    "db_size": DB_PATH.stat().st_size if exists else 0,
                    "sidecars": [
                        p.name for p in (Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")) if p.exists()
                    ],
                }
            )
            return
        if route == "/api/snapshots":
            self._json({"ok": True, "snapshots": list_snapshots(), "keep": SNAP_KEEP})
            return
        if route == "/api/snapshot/preview":
            self._json(preview_snapshot(arg("name")))
            return
        if route == "/api/snapshot/download":
            name = Path(arg("name")).name
            path = SNAP_DIR / name
            if not name or not path.exists():
                self._json({"ok": False, "error": "找不到快照"}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{name}"'
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 静态文件
        if route in ("/", ""):
            self._file(STATIC_DIR / "ff14-stock-v2.html")
            return
        target = (STATIC_DIR / route.lstrip("/")).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json({"ok": False, "error": "非法路径"}, 403)
            return
        self._file(target)

    # ---- POST ----

    def _read_upload(self, limit: int):
        """按块把上传体写到临时文件；返回 (path, length, error)。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, 0, "请求体长度不合法"
        if length <= 0:
            return None, 0, "文件为空"
        if length > limit:
            return None, length, f"文件过大（上限 {limit // 1024 // 1024}MB）"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATA_DIR / f"_upload_{os.getpid()}_{time.time_ns()}.db"
        try:
            remaining = length
            with tmp.open("wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        raise OSError("上传中断")
                    fh.write(chunk)
                    remaining -= len(chunk)
            return tmp, length, ""
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None, length, f"上传失败：{exc}"

    def _discard_request_body(self) -> None:
        """早退 403 前读掉请求体，避免 Windows 在未读数据上关闭连接触发 RST。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if length <= 0 or length > JSON_BODY_LIMIT:
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def do_POST(self) -> None:  # noqa: N802
        note_request()
        if not self._client_allowed():
            self._discard_request_body()
            self._json({"ok": False, "error": "手机访问未开启"}, 403)
            return
        route = urlparse(self.path).path
        if not self._request_trusted():
            self._discard_request_body()
            self._json({"ok": False, "error": "请求来源不受信任，已拒绝"}, 403)
            return
        if route in LOCAL_ONLY_POST and not self._is_local_client():
            self._discard_request_body()
            self._json({"ok": False, "error": "该功能只允许在本机使用"}, 403)
            return
        if route not in ("/api/db/preview", "/api/db/restore") and not self._content_type_ok():
            self._json({"ok": False, "error": "请求格式不对（需要 application/json）"}, 415)
            return
        if not route.startswith("/api/page/") and not self._csrf_ok():
            self._json({"ok": False, "error": "CSRF token 失效，请刷新页面后重试"}, 403)
            return
        if _RESTORING and route not in ("/api/page/ping", "/api/page/closed"):
            self._json({"ok": False, "error": "正在恢复数据，请稍后再试"}, 503)
            return

        if route == "/api/db/preview":
            tmp, length, err = self._read_upload(DB_UPLOAD_LIMIT)
            if tmp is None:
                self._json({"ok": False, "error": err}, 413 if "过大" in err else 400)
                return
            try:
                result = preview_db_file(tmp, size=length)
            except Exception as exc:  # noqa: BLE001
                log_error("db preview", exc)
                result = {"ok": False, "error": str(exc)}
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            status = 500 if not result.get("ok") else 200
            self._json(result, status)
            return

        if route == "/api/db/restore":
            tmp, length, err = self._read_upload(DB_UPLOAD_LIMIT)
            if tmp is None:
                self._json({"ok": False, "error": err}, 413 if "过大" in err else 400)
                return
            try:
                info = inspect_db_file(tmp)
                if not info["ok"]:
                    result = {
                        "ok": False,
                        "error": "文件不完整或已损坏（需要 character/stock/ledger 且完整性检查通过）",
                    }
                else:
                    result = _install_db(tmp, info)
            except Exception as exc:  # noqa: BLE001
                log_error("db restore", exc)
                result = {"ok": False, "error": str(exc)}
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            self._json(result)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > JSON_BODY_LIMIT:
                self._json({"ok": False, "error": "请求体过大"}, 413)
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, OSError) as exc:
            self._json({"ok": False, "error": f"请求体解析失败：{exc}"}, 400)
            return

        try:
            if route == "/api/page/open":
                sid = str(payload.get("sid") or "")
                page_open(sid)
                page_ping(sid)
                result = {"ok": True}
            elif route == "/api/page/ping":
                result = {
                    "ok": True,
                    "live": page_ping(
                        str(payload.get("sid") or ""),
                        bool(payload.get("visible", True)),
                    ),
                }
            elif route == "/api/page/closed":
                page_closed(str(payload.get("sid") or ""))
                result = {"ok": True}
            elif route == "/api/exit":
                if not self._is_local_client():
                    result = {"ok": False, "error": "只能在本机退出"}
                elif _RESTORING:
                    result = {"ok": False, "error": "正在恢复数据，稍后再退出"}
                elif _HTTPD is None:
                    result = {"ok": False, "error": "服务尚未启动"}
                else:
                    result = {"ok": True}
                    timer = threading.Timer(0.4, _HTTPD.shutdown)
                    timer.daemon = True
                    timer.start()
            elif route == "/api/stock/safety-line":
                result = safety_line_set(payload.get("item_id"), payload.get("min_qty"))
                self._json(result, 400 if result.get("invalid") else 200)
                return
            elif route == "/api/stock/safety-line/delete":
                result = safety_line_delete(payload.get("item_id"))
                self._json(result, 400 if result.get("invalid") else 200)
                return
            elif route == "/api/stock":
                result = record_stock(
                    character_id=int(payload.get("character_id") or 0),
                    item_name=str(payload.get("item_name") or ""),
                    location=str(payload.get("location") or "背包"),
                    qty=payload.get("qty"),
                    hq=1 if payload.get("hq") else 0,
                    allow_unbound=bool(payload.get("allow_unbound")),
                    unit_price=int(payload.get("unit_price") or 0),
                    mode=str(payload.get("mode") or "set"),
                )
            elif route == "/api/purchase/delete":
                result = delete_purchase(int(payload.get("id") or 0))
            elif route == "/api/cost":
                result = set_cost(
                    str(payload.get("item_name") or ""),
                    int(payload.get("hq") or 0),
                    int(payload.get("unit_price") or 0),
                )
            elif route == "/api/stock/import/preview":
                prep = prepare_import(str(payload.get("text") or ""))
                if prep.get("ok"):
                    result = {
                        "ok": True,
                        "total": prep["total"],
                        "will_add": prep["will_add"],
                        "will_update": prep["will_update"],
                        "skipped": prep["skipped"],
                        "new_characters": prep["new_characters"],
                        "rows": prep["rows"],
                    }
                else:
                    result = prep
            elif route == "/api/stock/import/apply":
                result = import_apply(
                    str(payload.get("text") or ""),
                    str(payload.get("mode") or "overwrite"),
                )
            elif route == "/api/stock/batch":
                result = record_batch(
                    int(payload.get("character_id") or 0),
                    str(payload.get("text") or ""),
                    str(payload.get("location") or "背包"),
                    str(payload.get("mode") or "set"),
                )
            elif route == "/api/open-data-dir":
                try:
                    os.startfile(str(DATA_DIR))  # type: ignore[attr-defined]
                    result = {"ok": True, "dir": str(DATA_DIR)}
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": str(exc)}
            elif route == "/api/fc":
                result = save_fc(
                    str(payload.get("name") or ""),
                    str(payload.get("world") or ""),
                    int(payload.get("id") or 0),
                )
            elif route == "/api/fc/adopt":
                result = fc_adopt_orphans()
            elif route == "/api/character/fc":
                result = set_character_fc(
                    int(payload.get("character_id") or 0),
                    int(payload.get("fc_id") or 0),
                )
            elif route == "/api/fc/merge/apply":
                result = fc_merge_apply(
                    int(payload.get("fc_id") or 0),
                    payload.get("items") or [],
                )
            elif route == "/api/fc/delete":
                result = fc_delete(
                    int(payload.get("id") or 0),
                    str(payload.get("mode") or "void"),
                )
            elif route == "/api/pending/move":
                result = pending_move(
                    int(payload.get("stock_id") or 0),
                    int(payload.get("character_id") or 0),
                    str(payload.get("location") or "背包"),
                )
            elif route == "/api/pending/drop":
                result = pending_drop(
                    int(payload.get("stock_id") or 0),
                    str(payload.get("note") or ""),
                )
            elif route == "/api/location/delete":
                result = delete_location_stock(
                    int(payload.get("character_id") or 0),
                    str(payload.get("location") or ""),
                )
            elif route == "/api/undo":
                result = undo_last()
            elif route == "/api/recipe/expand":
                targets = payload.get("targets")
                if not isinstance(targets, list):
                    targets = [
                        {"item_id": payload.get("item_id"), "quantity": payload.get("quantity")}
                    ]
                result = expand_recipe(
                    targets,
                    use_stock=bool(payload.get("stock")),
                    overrides=payload.get("overrides") or {},
                    with_alternatives=bool(payload.get("with_alternatives")),
                    # 默认沿用方案 A（目标也抵扣库存）；传 false 表示「按数量新做」
                    deduct_target=bool(payload.get("target_stock", True)),
                )
            elif route == "/api/recipe/list/save":
                result = recipe_list_save(
                    payload.get("name"),
                    payload.get("base_revision"),
                    payload.get("payload"),
                    payload.get("updated_by"),
                )
            elif route == "/api/recipe/list/delete":
                result = recipe_list_delete(
                    payload.get("name"),
                    payload.get("base_revision"),
                )
            elif route == "/api/recipe/rebuild":
                result = build_recipe_index(progress=lambda m: print("[配方] " + m, flush=True))
            elif route == "/api/stock/update":
                result = update_stock(
                    int(payload.get("id") or 0),
                    payload.get("qty"),
                    int(payload.get("character_id") or 0),
                    str(payload.get("location") or "背包"),
                    str(payload.get("item_name") or ""),
                    1 if payload.get("hq") else 0,
                )
            elif route == "/api/stock/take":
                result = record_take(
                    int(payload.get("id") or 0),
                    payload.get("qty"),
                    str(payload.get("note") or ""),
                )
            elif route == "/api/ledger/note":
                result = set_ledger_note(
                    int(payload.get("id") or 0),
                    str(payload.get("note") or ""),
                )
            elif route == "/api/lan":
                result = set_lan(bool(payload.get("enabled")))
            elif route == "/api/data/backup":
                result = backup_now()
            elif route == "/api/npc/rebuild":
                result = build_npc_shop_index(
                    progress=lambda m: print("[NPC] " + m, flush=True)
                )
            elif route == "/api/market/buy-plan":
                region = str(payload.get("region") or "").strip()
                if not region:
                    region = _default_world()
                result = market.buy_plan(
                    region,
                    bool(payload.get("hq")),
                    payload.get("items") or [],
                )
                if result.get("ok"):
                    for row in result.get("items") or []:
                        row["name"] = name_of_item(int(row.get("item_id") or 0))
                    for row in result.get("unresolved") or []:
                        row["name"] = name_of_item(int(row.get("item_id") or 0))
            elif route == "/api/stock/move":
                result = move_stock(
                    int(payload.get("id") or 0),
                    payload.get("qty"),
                    int(payload.get("character_id") or 0),
                    str(payload.get("location") or "背包"),
                )
            elif route == "/api/stock/delete":
                result = delete_stock(int(payload.get("id") or 0))
            elif route == "/api/alias":
                result = set_alias(
                    str(payload.get("alias") or ""),
                    int(payload.get("item_id") or 0),
                )
            elif route == "/api/ledger/void":
                result = set_ledger_void(
                    int(payload.get("id") or 0),
                    bool(payload.get("void")),
                    str(payload.get("note") or ""),
                )
            elif route == "/api/ledger/check_batch":
                result = set_ledger_checked_batch(
                    list(payload.get("ids") or []),
                    bool(payload.get("checked")),
                )
            elif route == "/api/ledger/check":
                result = set_ledger_checked(
                    int(payload.get("id") or 0),
                    bool(payload.get("checked")),
                )
            elif route == "/api/snapshot/create":
                result = create_snapshot("manual", str(payload.get("note") or ""))
            elif route == "/api/snapshot/restore":
                result = restore_snapshot(str(payload.get("name") or ""))
            elif route == "/api/snapshot/cleanup":
                result = cleanup_legacy_snapshots()
            elif route == "/api/snapshot/delete":
                result = delete_snapshot(str(payload.get("name") or ""))
            elif route == "/api/alias/delete":
                result = delete_alias(str(payload.get("alias") or ""))
            elif route == "/api/character":
                result = add_character(
                    account=str(payload.get("account") or ""),
                    name=str(payload.get("name") or ""),
                    world=str(payload.get("world") or ""),
                )
            elif route == "/api/clear_demo":
                removed = clear_demo()
                result = {"ok": True, "removed": removed}
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
                return
        except Exception as exc:  # noqa: BLE001
            log_error(f"POST {route}", exc)
            self._json({"ok": False, "error": f"处理失败：{exc}"}, 500)
            return

        # 每次存入/取出/改动成功后存一份快照（保留最近 30 个）。
        # 这里整段包 try：快照是附加功能，任何问题都不该影响这次请求的响应。
        try:
            if (
                route in SNAP_AFTER
                and isinstance(result, dict)
                and result.get("ok")
                and not result.get("unchanged")
            ):
                auto_snapshot(snapshot_note(route, result))
        except Exception as exc:  # noqa: BLE001
            print(f"[快照] 钩子异常：{type(exc).__name__}: {exc}", flush=True)

        status = 400 if isinstance(result, dict) and result.get("invalid") else (
            409 if isinstance(result, dict) and result.get("conflict") else 200
        )
        self._json(result, status)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def bootstrap() -> None:
    """建目录、建表、载入或构建物品表。"""
    global DB_WAS_MISSING, DB_CREATED_NEW
    started = time.perf_counter()
    marks: list[tuple[str, float]] = []

    def mark(label: str) -> None:
        marks.append((label, (time.perf_counter() - started) * 1000))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DB_CREATED_NEW = not DB_PATH.exists()
    DB_WAS_MISSING = (
        DB_CREATED_NEW
        and SNAP_DIR.exists()
        and any(SNAP_DIR.glob("*.db"))
    )
    try:
        init_db()
    except sqlite3.DatabaseError as exc:
        print("=" * 60)
        print("  数据库文件损坏，无法打开")
        print(f"  原因：{exc}")
        print(f"  文件：{DB_PATH}")
        print("")
        print("  处理办法（任选其一）：")
        print("   1) 把你手上的 .db 文件拖到「恢复数据库.bat」上")
        print("   2) 命令行：python app.py --restore-db \"D:\\\\path\\\\stock.db\"")
        print("   3) 把 data\\snapshots 里最新的 .db 复制一份，改名成 stock.db")
        print("=" * 60)
        sys.exit(1)
    mark("数据库")
    load_icon_cache()
    load_stack_cache()
    mark("图标/堆叠缓存")
    load_recipes()
    mark("配方表")
    npc_shop.load_npc_shops(DATA_DIR, _ensure_data_file)
    mark("NPC 商店")
    load_lan_state()
    migrate_rename_iron()
    mark("其它")

    if not INDEX.load():
        print("[物品表] " + (INDEX.error or "未加载"))
        print("[物品表] 正在从国服数据源构建，仅首次需要（约 19MB）…")
        INDEX.build_from_csv(progress=lambda m: print("[物品表] " + m))
        if not INDEX.loaded:
            print("[物品表] 构建失败，搜索与录入仍可用，只是没有名称联想：")
            print("[物品表]   " + (INDEX.error or "未知原因"))
    mark("物品表")

    # 耗时分段打印：启动变慢时能直接看出卡在哪（这里换算成各段耗时，不是累计）
    parts: list[str] = []
    prev = 0.0
    for label, ms in marks:
        parts.append(f"{label} {ms - prev:.0f}ms")
        prev = ms
    total = (time.perf_counter() - started) * 1000
    print(f"[启动] 耗时 {total:.0f}ms（{' · '.join(parts)}）")

    # 国服转储没有 StackSize 列，不再为了它重新下载 19MB；单体识别改为按需从 XIVAPI 取
    if INDEX.loaded and not INDEX.has_stack:
        print("[物品表] 转储里没有「堆叠上限」列：单体识别按需从 XIVAPI 取（不影响记账）")

    if SEED_DEMO:
        seed_demo()
    else:
        print("[启动] 空库模式：不生成示例数据，可直接在页面里添加角色")

    if INDEX.loaded:
        probe = INDEX.id_of("黑铁锭")
        if probe:
            print(f"[自检] 物品名解析正常：黑铁锭 → item_id {probe}")
        else:
            print("[自检] 警告：物品表里查不到「黑铁锭」，名称列可能认错了，"
                  "请打开 /api/diag 看 items_sample")
    else:
        print("[自检] 物品表未载入，材料名不会做标准化处理")


def _edge_exe() -> Path | None:
    for root in (
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if not root:
            continue
        exe = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if exe.exists():
            return exe
    return None


def _edge_app_window(exe: Path, url: str) -> bool:
    """用系统自带的 Edge 以应用模式开一个无地址栏的窗口（比 pywebview 稳）。"""
    try:
        subprocess.Popen(
            [str(exe), f"--app={url}", "--window-size=1220,880",
             "--no-first-run", "--no-default-browser-check"],
            close_fds=True,
        )
        return True
    except OSError as exc:
        print(f"[窗口] Edge 启动失败（{exc}），改用默认浏览器")
        return False


def build_recipe_index(progress=None) -> dict:
    """下载 Recipe.csv（+ 等级表），裁剪成 data/recipe.json 并热加载。"""

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    say("下载配方表（约 2.4MB）…")
    try:
        header, rows = datasource.read_saint_csv(RECIPE_URL)
    except Exception as exc:  # noqa: BLE001 - 网络失败一律降级
        return {"ok": False, "error": f"配方表下载失败：{exc}"}
    if not header:
        return {"ok": False, "error": "配方表格式无法识别"}

    say("下载配方等级表（约 35KB）…")
    level_map: dict[int, int] = {}
    try:
        lv_header, lv_rows = datasource.read_saint_csv(RECIPE_LEVEL_URL, timeout=120)
        lv_key = lv_header.index("key") if "key" in lv_header else 0
        lv_col = next(
            (i for i, n in enumerate(lv_header) if n.lower() in ("classjoblevel", "classjoblevel")),
            None,
        )
        if lv_col is not None:
            for r in lv_rows:
                try:
                    level_map[int(r[lv_key])] = int(float(r[lv_col] or 0))
                except (IndexError, ValueError):
                    continue
    except Exception:  # noqa: BLE001 - 等级表拿不到就退化成按行号取最新
        level_map = {}

    def col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    idx_result = col("Item{Result}")
    idx_amount = col("Amount{Result}")
    idx_level = col("RecipeLevelTable")
    if idx_result < 0 or idx_amount < 0:
        return {"ok": False, "error": "配方表缺少结果列"}

    ing_items = [col(f"Item{{Ingredient}}[{i}]") for i in range(8)]
    ing_amounts = [col(f"Amount{{Ingredient}}[{i}]") for i in range(8)]

    recipes: dict[str, list[dict]] = {}
    for r in rows:
        try:
            result = int(r[idx_result])
            amount = int(r[idx_amount] or 1)
        except (IndexError, ValueError):
            continue
        if result <= 0 or amount <= 0:
            continue
        ings = []
        for ii, ia in zip(ing_items, ing_amounts):
            if ii < 0 or ia < 0:
                continue
            try:
                ing_id = int(r[ii] or 0)
                ing_n = int(r[ia] or 0)
            except (IndexError, ValueError):
                continue
            if ing_id > 0 and ing_n > 0:
                ings.append([ing_id, ing_n])
        if not ings:
            continue
        lv_key = 0
        if idx_level >= 0:
            try:
                lv_key = int(r[idx_level] or 0)
            except (IndexError, ValueError):
                lv_key = 0
        try:
            row_no = int(r[0])
        except (IndexError, ValueError):
            row_no = 0
        recipes.setdefault(str(result), []).append(
            {
                "row": row_no,
                "amount": amount,
                "level": level_map.get(lv_key, 0),
                "ings": ings,
            }
        )

    payload = {
        "version": datetime.now().strftime("%Y-%m-%d"),
        "source": f"{datasource.SAINT_GH}@{datasource.SAINT_REF}/Recipe.csv",
        "recipes": recipes,
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RECIPE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        tmp.replace(RECIPE_PATH)
    except OSError as exc:
        return {"ok": False, "error": f"写入 recipe.json 失败：{exc}"}

    say(f"配方 {sum(len(v) for v in recipes.values())} 条 / 成品 {len(recipes)} 种，已保存")
    load_recipes()
    return {"ok": True, "recipes": RECIPE_META["recipes"], "items": RECIPE_META["items"],
            "version": RECIPE_META["version"]}


def _ensure_data_file(name: str) -> None:
    """打包版：把 _internal\\data 里的只读资源复制到可写数据目录（首次运行时）。"""
    target = DATA_DIR / name
    if target.exists():
        return
    src = RES_DIR / "data" / name
    if not src.exists() or src == target:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    except OSError:
        pass


def load_recipes() -> None:
    """把 recipe.json 读进内存。失败不影响其它功能。"""
    global RECIPES, RECIPE_META
    RECIPES = {}
    RECIPE_META = {"version": "", "recipes": 0, "items": 0}
    _ensure_data_file("recipe.json")
    if not RECIPE_PATH.exists():
        return
    try:
        data = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    raw = data.get("recipes") or {}
    total = 0
    parsed: dict[int, list] = {}
    for key, items in raw.items():
        try:
            item_id = int(key)
        except (TypeError, ValueError):
            continue
        parsed[item_id] = items
        total += len(items)
    RECIPES = parsed
    RECIPE_META = {
        "version": data.get("version", ""),
        "recipes": total,
        "items": len(parsed),
    }


def recipe_meta() -> dict:
    return {"ok": True, **RECIPE_META}


def _pick_recipe(item_id: int) -> dict | None:
    """多配方时：等级高者优先，同等级取行号大的（较新）。"""
    items = RECIPES.get(item_id)
    if not items:
        return None
    return max(items, key=lambda r: (int(r.get("level") or 0), int(r.get("row") or 0)))


def _stock_by_item() -> dict[int, list[dict]]:
    """item_id -> [{character, world, location, qty, hq}]，排除「未确认」。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT s.item_name, s.location, s.qty, s.hq, c.name AS character, c.world"
            " FROM stock s LEFT JOIN character c ON c.id = s.character_id"
            " WHERE s.owner = ? AND s.location <> ?"
            " ORDER BY s.character_id, s.location",
            (OWNER, PENDING_LOC),
        ).fetchall()
    finally:
        conn.close()

    out: dict[int, list[dict]] = {}
    cache: dict[str, int] = {}
    for r in rows:
        name = r["item_name"] or ""
        if name not in cache:
            cache[name] = int(resolve_item(name)[0] or 0)
        item_id = cache[name]
        if not item_id:
            continue
        out.setdefault(item_id, []).append(
            {
                "character": r["character"] or "",
                "world": r["world"] or "",
                "location": r["location"],
                "qty": int(r["qty"]),
                "hq": 1 if int(r["hq"] or 0) else 0,
            }
        )
    return out


CRYSTAL_NAMES = {
    2: "火之碎晶", 3: "冰之碎晶", 4: "风之碎晶", 5: "土之碎晶", 6: "雷之碎晶", 7: "水之碎晶",
    8: "火之水晶", 9: "冰之水晶", 10: "风之水晶", 11: "土之水晶", 12: "雷之水晶", 13: "水之水晶",
    14: "火之晶簇", 15: "冰之晶簇", 16: "风之晶簇", 17: "土之晶簇", 18: "雷之晶簇", 19: "水之晶簇",
}


def name_of_item(item_id: int) -> str:
    info = INDEX.by_id(item_id)
    if info and info.get("name"):
        return info["name"]
    if item_id in CRYSTAL_NAMES:
        return CRYSTAL_NAMES[item_id]
    return f"#{item_id}"


def expand_recipe(
    targets: list,
    use_stock: bool = False,
    overrides: dict | None = None,
    with_alternatives: bool = False,
    deduct_target: bool = True,
) -> dict:
    """把多个目标的需求先聚合，再按配方拓扑顺序统一扣库存、统一取整。

    与逐目标 DFS 的关键差别：同一种中间品被多个目标需要时，需求先相加、
    库存只扣一次、批数只向上取整一次（2 个各需 1 份、每批产 3 份 → 1 批，不是 2 批）。

    只出计划：不写库、不扣真实库存、不打快照。
    """
    if not RECIPES:
        return {"ok": False, "error": "配方数据未就绪"}
    overrides = overrides or {}

    # ── 1. 合并目标（同 item_id 数量相加）──────────────────
    want: dict[int, int] = {}
    want_hq: dict[int, bool] = {}
    want_hq_requested: dict[int, bool] = {}
    need_hq: dict[int, int] = {}
    need_nq: dict[int, int] = {}
    for t in targets or []:
        try:
            item_id = int(t.get("item_id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "invalid": True, "error": "目标物品格式不对"}
        qty = _valid_item_qty(t.get("quantity"))
        if not item_id:
            return {"ok": False, "invalid": True, "error": "缺少目标物品"}
        if not qty:
            return {"ok": False, "invalid": True,
                    "error": f"数量必须是 1–{MAX_ITEM_QTY} 的整数"}
        hq_flag = bool(t.get("hq"))
        want[item_id] = want.get(item_id, 0) + qty
        if want[item_id] > MAX_ITEM_QTY:
            return {"ok": False, "invalid": True,
                    "error": f"同一目标合并后超过上限 {MAX_ITEM_QTY}"}
        want_hq_requested[item_id] = bool(want_hq_requested.get(item_id)) or hq_flag
        # 没有 HQ 版本的物品：请求了 HQ 也按 NQ 算（否则会去找根本不存在的 HQ 库存）
        if hq_flag and INDEX.can_be_hq(item_id) == 0:
            hq_flag = False
        want_hq[item_id] = bool(want_hq.get(item_id)) or hq_flag
        if hq_flag:
            need_hq[item_id] = need_hq.get(item_id, 0) + qty
        else:
            need_nq[item_id] = need_nq.get(item_id, 0) + qty
    if not want:
        return {"ok": False, "error": "至少需要一个目标"}

    def override_source(item_id: int) -> str:
        val = overrides.get(str(item_id)) or overrides.get(item_id)
        if isinstance(val, dict):
            val = val.get("source")
        return str(val or "")

    unresolvable_items: list[dict] = []

    def mark(item_id: int, reason: str) -> None:
        unresolvable_items.append(
            {"item_id": item_id, "name": name_of_item(item_id), "reason": reason}
        )

    # ── 2. 取可达子图，同时算入度与深度 ────────────────────
    children: dict[int, list] = {}
    indeg: dict[int, int] = {item_id: 0 for item_id in want}
    depth: dict[int, int] = {item_id: 0 for item_id in want}

    queue = list(want.keys())
    guard = 0
    while queue and guard < 100000:
        guard += 1
        cur = queue.pop(0)
        recipe = _pick_recipe(cur)
        if override_source(cur) == "buy" or recipe is None:
            children[cur] = []
            continue
        if depth.get(cur, 0) >= RECIPE_MAX_DEPTH:
            mark(cur, "超过最大递归深度")
            children[cur] = []
            continue
        ings: list = []
        for pair in recipe.get("ings") or []:
            try:
                ing_id, ing_qty = int(pair[0]), int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            if ing_id > 0 and ing_qty > 0:
                ings.append((ing_id, ing_qty))
        children[cur] = ings
        for ing_id, _ in ings:
            indeg[ing_id] = indeg.get(ing_id, 0) + 1
            if ing_id not in depth:
                depth[ing_id] = depth.get(cur, 0) + 1
                queue.append(ing_id)
            else:
                depth[ing_id] = min(depth[ing_id], depth.get(cur, 0) + 1)

    # ── 3. 拓扑排序（Kahn）：父节点全部算完才算子节点 ──────
    left = dict(indeg)
    ready = [item_id for item_id, deg in left.items() if deg == 0]
    order: list[int] = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for ing_id, _ in children.get(cur, []):
            left[ing_id] = left.get(ing_id, 0) - 1
            if left.get(ing_id) == 0:
                ready.append(ing_id)
    cyclic = [item_id for item_id, deg in left.items() if deg > 0]
    for item_id in cyclic:
        mark(item_id, "配方成环")

    # ── 4. 按拓扑顺序聚合需求、扣库存、取整 ────────────────
    stock = _stock_by_item() if use_stock else {}
    # 每行库存带剩余量：HQ 目标只吃 HQ，普通原料 NQ/HQ 都能用。
    # 同一物品同时被 HQ 目标和普通原料需要时，两条需求分开扣，互不污染。
    remain: dict[int, list] = {
        item_id: [dict(r, left=int(r["qty"])) for r in rows]
        for item_id, rows in stock.items()
    }
    parents: dict[int, dict] = {}     # 子材料 -> {父节点: 本计划用量}
    nodes: dict[int, dict] = {}
    max_depth = 0

    def take_from(item_id: int, need: int, hq_only: bool):
        """从剩余库存里扣 need 个，返回 (实际扣到, 明细行)。"""
        if not use_stock or need <= 0:
            return 0, []
        rows = []
        left = need
        for r in remain.get(item_id, []):
            if left <= 0:
                break
            if hq_only and not r.get("hq"):
                continue
            got = min(left, r["left"])
            if got <= 0:
                continue
            r["left"] -= got
            left -= got
            rows.append(
                {
                    "character": r["character"],
                    "world": r["world"],
                    "location": r["location"],
                    "qty": got,
                    "hq": 1 if r.get("hq") else 0,
                }
            )
        return need - left, rows

    def new_node(item_id: int, need: int) -> dict:
        return {
            "item_id": item_id,
            "name": name_of_item(item_id),
            "depth": depth.get(item_id, 0),
            "need": need,
            "from_stock": 0,
            "to_craft": 0,
            "to_buy": 0,
            "source": "craft",
            "craft_count": None,
            "per_batch": None,
            "produced": None,
            "surplus": None,
            "alternative": None,
            "where": [],
        }

    for item_id in list(order) + cyclic:
        need_hq_item = need_hq.get(item_id, 0)
        need_nq_item = need_nq.get(item_id, 0)
        need = need_hq_item + need_nq_item
        if need <= 0:
            continue
        max_depth = max(max_depth, depth.get(item_id, 0))
        recipe = _pick_recipe(item_id)
        ov = override_source(item_id)
        # 目标是「要做多少个」时不该抵扣成品库存：target_stock=false 就跳过目标这一层
        if item_id in want and not deduct_target:
            hq_take, nq_take = 0, 0
            used_rows: list = []
        else:
            # HQ 需求只吃 HQ 库存；普通需求先吃剩下的 HQ，再吃 NQ。
            hq_take, hq_rows = take_from(item_id, need_hq_item, True)
            nq_take, nq_rows = take_from(item_id, need_nq_item, False)
            used_rows = hq_rows + nq_rows
        from_stock = hq_take + nq_take
        rest = max(0, need - from_stock)

        node = new_node(item_id, need)
        node["from_stock"] = from_stock
        node["where"] = used_rows
        nodes[item_id] = node
        if item_id in want:
            node["quantity"] = want[item_id]
            node["hq"] = bool(want_hq.get(item_id))
            node["hq_requested"] = bool(want_hq_requested.get(item_id))
            node["can_be_hq"] = INDEX.can_be_hq(item_id)
            if need_hq_item:
                # 另外还有多少 NQ 可以凑（不参与抵扣，仅供前端提示）
                nq_rows = [r for r in remain.get(item_id, []) if not r.get("hq")]
                node["from_stock_nq"] = sum(int(r["left"]) for r in nq_rows)

        if ov == "buy":
            node["source"] = "buy"
            node["to_buy"] = rest
            continue
        if recipe is None:
            node["source"] = "gather"
            node["to_get"] = rest
            continue
        if rest <= 0:
            # 库存已经够了：不需要制作，但仍是可制作节点
            node["craft_count"] = 0
            node["per_batch"] = int(recipe.get("amount") or 1)
            node["produced"] = 0
            node["surplus"] = 0
            continue

        per_batch = int(recipe.get("amount") or 1)
        batches = -(-rest // per_batch)          # 向上取整，只在合并后取一次
        node["to_craft"] = rest
        node["craft_count"] = batches
        node["per_batch"] = per_batch
        node["produced"] = batches * per_batch
        node["surplus"] = node["produced"] - rest

        for ing_id, ing_qty in children.get(item_id, []):
            qty = ing_qty * batches
            # 原料永远是普通需求：HQ 成品也要用 NQ 材料做
            need_nq[ing_id] = need_nq.get(ing_id, 0) + qty
            slot = parents.setdefault(ing_id, {})
            slot[item_id] = slot.get(item_id, 0) + qty

    def used_by_for(item_id: int) -> list:
        rows = [
            {"item_id": pid, "name": name_of_item(pid), "qty": qty}
            for pid, qty in (parents.get(item_id) or {}).items()
            if pid != item_id and qty > 0
        ]
        rows.sort(key=lambda x: -x["qty"])
        return rows

    # ── 5. 输出 ─────────────────────────────────────────────
    target_out = []
    for item_id, qty in want.items():
        node = nodes.get(item_id) or new_node(item_id, qty)
        per_batch = node.get("per_batch")
        target_out.append(
            {
                "item_id": item_id,
                "name": node["name"],
                "quantity": qty,
                "hq": bool(node.get("hq")),
                "hq_requested": bool(node.get("hq_requested")),
                "can_be_hq": int(node.get("can_be_hq", -1)),
                "need": node["need"],
                "from_stock": node["from_stock"],
                "from_stock_nq": int(node.get("from_stock_nq") or 0),
                "to_craft": node["to_craft"],
                "craft_count": node["craft_count"] or 0,
                "per_batch": per_batch,
                "produced": node["produced"] or 0,
                "surplus": node["surplus"] or 0,
                "where": node["where"],
            }
        )

    base_out = []
    inter_out = []
    for item_id, node in nodes.items():
        node["used_by"] = used_by_for(item_id)
        if node["source"] == "gather":
            node["to_get"] = max(0, node["need"] - node["from_stock"])
            base_out.append(node)
        elif item_id not in want:      # 目标节点只在 targets 里出现一次
            inter_out.append(node)

    base_out.sort(key=lambda x: (-(x["to_get"]), x["name"]))
    inter_out.sort(key=lambda x: x["name"])

    if with_alternatives:
        for node in inter_out:
            pass  # v1 预留：buy 节点的「若自制」对比

    return {
        "ok": True,
        "data_version": RECIPE_META.get("version", ""),
        "targets": target_out,
        "intermediate": inter_out,
        "base": base_out,
        "summary": {
            "base_kinds": len(base_out),
            "intermediate_kinds": len(inter_out),
            "unknown_cost": 0,
            "unresolvable": len(unresolvable_items),
            "max_depth": max_depth,
        },
        "unresolvable_items": unresolvable_items,
    }


RECIPE_LIST_WORKING = "__working__"
RECIPE_LIST_NAME_MAX = 60
RECIPE_LIST_PAYLOAD_MAX = 256 * 1024


def _recipe_list_name(name) -> tuple[str, str]:
    value = str(name or "").strip()
    if not value:
        return "", "清单名不能为空"
    if len(value) > RECIPE_LIST_NAME_MAX:
        return "", f"清单名不能超过 {RECIPE_LIST_NAME_MAX} 个字符"
    return value, ""


def _recipe_list_revision(value) -> tuple[int | None, str]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, "base_revision 必须是非负整数"
    return value, ""


def _recipe_list_updated_by(value) -> tuple[str | None, str]:
    if value is None:
        return None, ""
    if not isinstance(value, str):
        return None, "updated_by 必须是字符串"
    text = value.strip()
    if len(text) > 64:
        return None, "updated_by 不能超过 64 个字符"
    return (text or None), ""


def _recipe_list_payload(payload) -> tuple[str, str]:
    """校验工作清单 payload，返回 (压缩 JSON, 错误)。"""
    if not isinstance(payload, dict):
        return "", "payload 必须是 JSON 对象"
    schema = payload.get("schema")
    if isinstance(schema, bool) or schema != 1:
        return "", "payload.schema 必须是 1"
    targets = payload.get("targets")
    if not isinstance(targets, list) or len(targets) > 200:
        return "", "targets 必须是数组且不超过 200 条"
    for t in targets:
        if not isinstance(t, dict):
            return "", "targets 每项必须是对象"
        item_id = t.get("item_id")
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
            return "", "targets.item_id 必须是正整数"
        qty = t.get("quantity")
        if not _valid_item_qty(qty):
            return "", "targets.quantity 必须是 1–9999999 的整数"
        if not isinstance(t.get("hq"), bool) or not isinstance(t.get("can_hq"), bool):
            return "", "targets.hq / can_hq 必须是布尔值"
    overrides = payload.get("overrides")
    if not isinstance(overrides, dict):
        return "", "overrides 必须是对象"
    for key, value in overrides.items():
        if not isinstance(key, str) or not key.isdigit():
            return "", "overrides 的键必须是物品 ID 数字串"
        if value not in ("buy", "craft"):
            return "", "overrides 的值只能是 buy 或 craft"
    for key in ("stockOn", "targetStock", "priceOn"):
        if not isinstance(payload.get(key), bool):
            return "", f"payload.{key} 必须是布尔值"
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > RECIPE_LIST_PAYLOAD_MAX:
        return "", "payload 超过 256KB"
    return text, ""


def recipe_list_meta() -> dict:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT name, revision, updated_at, updated_by FROM recipe_list"
            " WHERE owner = ? ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END, name",
            (OWNER, RECIPE_LIST_WORKING),
        ).fetchall()
    finally:
        conn.close()
    return {
        "ok": True,
        "lists": [
            {
                "name": r["name"],
                "revision": int(r["revision"]),
                "updated_at": r["updated_at"],
                "updated_by": r["updated_by"] or "",
            }
            for r in rows
        ],
    }


def recipe_list_get(name) -> dict:
    name, error = _recipe_list_name(name)
    if error:
        return {"ok": False, "error": error}
    conn = connect()
    try:
        row = conn.execute(
            "SELECT name, revision, payload FROM recipe_list"
            " WHERE owner = ? AND name = ?",
            (OWNER, name),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "没有这个清单"}
    try:
        payload = json.loads(row["payload"])
    except ValueError:
        return {"ok": False, "error": "清单数据损坏"}
    return {
        "ok": True,
        "name": row["name"],
        "revision": int(row["revision"]),
        "payload": payload,
    }


def _recipe_list_conflict(name, revision, updated_at="", payload=None,
                          error="服务器上已有更新版本") -> dict:
    """save/delete 共用的 409 响应；带 error 便于前端直接提示。"""
    return {
        "ok": False,
        "conflict": True,
        "error": error,
        "name": name,
        "revision": int(revision),
        "updated_at": updated_at or "",
        "payload": payload,
    }


def recipe_list_save(name, base_revision, payload, updated_by=None) -> dict:
    name, error = _recipe_list_name(name)
    if error:
        return {"ok": False, "invalid": True, "error": error}
    base_revision, error = _recipe_list_revision(base_revision)
    if error:
        return {"ok": False, "invalid": True, "error": error}
    updated_by, error = _recipe_list_updated_by(updated_by)
    if error:
        return {"ok": False, "invalid": True, "error": error}
    payload_text, error = _recipe_list_payload(payload)
    if error:
        return {"ok": False, "invalid": True, "error": error}

    ts = now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision, payload, updated_at FROM recipe_list"
            " WHERE owner = ? AND name = ?",
            (OWNER, name),
        ).fetchone()
        if row is None:
            if base_revision != 0:
                conn.rollback()
                return _recipe_list_conflict(
                    name, 0, payload=None, error="服务器上没有这个清单"
                )
            conn.execute(
                "INSERT INTO recipe_list(owner, name, payload, revision, updated_at,"
                " updated_by) VALUES(?,?,?,?,?,?)",
                (OWNER, name, payload_text, 1, ts, updated_by),
            )
            conn.commit()
            return {"ok": True, "name": name, "revision": 1}
        current_revision = int(row["revision"])
        if base_revision != current_revision:
            try:
                current_payload = json.loads(row["payload"])
            except ValueError:
                current_payload = None
            conn.rollback()
            return _recipe_list_conflict(
                name, current_revision, row["updated_at"], current_payload
            )
        conn.execute(
            "UPDATE recipe_list SET payload = ?, revision = revision + 1,"
            " updated_at = ?, updated_by = ? WHERE owner = ? AND name = ?",
            (payload_text, ts, updated_by, OWNER, name),
        )
        conn.commit()
        return {"ok": True, "name": name, "revision": current_revision + 1}
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def recipe_list_delete(name, base_revision=None) -> dict:
    name, error = _recipe_list_name(name)
    if error:
        return {"ok": False, "invalid": True, "error": error}
    if name == RECIPE_LIST_WORKING:
        return {"ok": False, "error": "工作清单不可删除"}
    if base_revision is not None:
        base_revision, error = _recipe_list_revision(base_revision)
        if error:
            return {"ok": False, "invalid": True, "error": error}

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT revision, payload, updated_at FROM recipe_list"
            " WHERE owner = ? AND name = ?",
            (OWNER, name),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": True, "name": name}
        if base_revision is not None and base_revision != int(row["revision"]):
            try:
                current_payload = json.loads(row["payload"])
            except ValueError:
                current_payload = None
            conn.rollback()
            return _recipe_list_conflict(
                name, int(row["revision"]), row["updated_at"], current_payload
            )
        conn.execute(
            "DELETE FROM recipe_list WHERE owner = ? AND name = ?", (OWNER, name)
        )
        conn.commit()
        return {"ok": True, "name": name}
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


HTTP_UA = f"FF14-Stock-Ledger/{VERSION} (local inventory ledger)"


def build_npc_shop_index(progress=None) -> dict:
    return npc_shop.build_npc_shop_index(
        DATA_DIR, _ensure_data_file, progress, user_agent=HTTP_UA
    )


class FastHTTPServer(ThreadingHTTPServer):
    """跳过 HTTPServer.server_bind 里的 socket.getfqdn。

    Windows 上绑定 0.0.0.0 时，反向解析可能卡十几秒；server_name 只用于
    Host 回显/日志，不需要真实主机名，直接跳过。
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host or "ff14-stock"
        self.server_port = int(port)


def _wait_port_ready(port: int, timeout: float = 2.0) -> bool:
    """等端口真正可连接，最多 timeout 秒。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _acquire_data_lock() -> str:
    """同一个数据目录只允许一个写实例；进程退出时由系统自动释放。"""
    global _DATA_LOCK
    if _DATA_LOCK is not None:
        return ""
    path = DATA_DIR / "stock.lock"
    fh = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(path, "r+b") if path.exists() else open(path, "w+b")
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"x")
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        return f"数据目录已被另一个实例占用：{DATA_DIR}"
    _DATA_LOCK = fh
    return ""


def _release_data_lock() -> None:
    global _DATA_LOCK
    fh = _DATA_LOCK
    _DATA_LOCK = None
    if fh is None:
        return
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


def serve(port: int = DEFAULT_PORT, force_browser: bool = False,
          open_browser: bool = True) -> None:
    httpd = None
    bind_host = LAN_HOST
    for offset in range(PORT_TRIES):
        candidate = port + offset
        try:
            httpd = FastHTTPServer((bind_host, candidate), Handler)
            port = candidate
            break
        except OSError:
            # 全网卡绑定失败时退回本机（例如被安全软件拦了）
            if bind_host != "127.0.0.1":
                try:
                    httpd = FastHTTPServer(("127.0.0.1", candidate), Handler)
                    bind_host = "127.0.0.1"
                    port = candidate
                    break
                except OSError:
                    pass
            continue
    if httpd is None:
        print(f"端口 {DEFAULT_PORT}-{DEFAULT_PORT + PORT_TRIES - 1} 全被占用，无法启动")
        sys.exit(1)
    boot_stage("端口绑定")

    url = f"http://127.0.0.1:{port}/"
    edge = None if force_browser else _edge_exe()

    global _HTTPD, _PORT
    _HTTPD = httpd
    _PORT = port

    print("=" * 56)
    print(f"  FF14 材料台账 v{VERSION}")
    print(f"  打开：{url}")
    print(f"  数据：{DB_PATH}")
    if FROZEN:
        print(f"  资源：{RES_DIR}")
    print("  窗口：" + ("应用窗口（Edge --app）" if edge else "默认浏览器"))
    if LAN_ENABLED:
        # 探测局域网地址在 Windows 上可能卡几秒，放到后台，别拖慢启动
        def _show_lan() -> None:
            for ip in lan_addresses():
                print(f"  手机：http://{ip}:{port}/", flush=True)

        threading.Thread(target=_show_lan, daemon=True).start()
    else:
        print("  手机：未开启（管理 → 手机访问 里打开）")
    print("  关闭本窗口即停止服务（数据已存盘）")
    print("=" * 56)

    def _open_window() -> None:
        try:
            if edge and _edge_app_window(edge, url):
                return
        except Exception as exc:  # noqa: BLE001
            print(f"[窗口] Edge 启动失败（{exc}），改用默认浏览器")
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            print(f"[窗口] 打不开浏览器（{exc}），请手动访问 {url}")

    # 服务放到后台线程先跑起来，然后立刻开窗口：
    # 既避免「窗口赶在监听前加载」的竞态，又不用干等延时
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    # 端口真正可连接才算就绪（绑定成功不等于能 accept）
    if _wait_port_ready(port):
        boot_stage("端口就绪")
    else:
        print("[启动] 端口 2 秒内仍不可连接（继续启动，请查看系统防火墙）", flush=True)

    # 心跳看门狗：所有页面的心跳都超时就自动退出
    threading.Thread(target=_reaper_loop, daemon=True).start()

    if open_browser:
        _open_window()
        boot_stage("窗口已开")
    else:
        boot_stage("窗口跳过")

    try:
        while server_thread.is_alive():
            server_thread.join(0.5)
    except KeyboardInterrupt:
        print("\n已停止")
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
    finally:
        httpd.server_close()


def _selftest_allowed() -> bool:
    """--selftest 会写演示数据，只允许在隔离目录运行。"""
    raw = os.environ.get("FF14_STOCK_DATA")
    if not raw:
        return False
    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        return False
    if path == (APP_DIR / "data").resolve():
        return False
    return "ff14材料台账" not in str(path)


def selftest() -> int:
    if not _selftest_allowed():
        print("[自检] 已拒绝：--selftest 会写演示库存和流水。")
        print("       请在隔离目录运行，例如：")
        print("       $env:FF14_STOCK_DATA='C:\\Temp\\ff14-selftest'; python app.py --selftest")
        return 1
    print(f"== 自检 v{VERSION} ==")
    init_db()
    seed_demo()
    print(f"[数据库] 角色 {len(list_characters())} 个")
    print(f"[物品表] 已加载 {INDEX.count} 条；错误：{INDEX.error or '无'}")

    for q in ("铁锭", "铁", "阿辉", "雇员1", "td"):
        result = search(query=q)
        print(
            f"[搜索] {q!r} → 材料 {len(result['items'])} 组 / 角色 {len(result['characters'])} 个"
            f" / hint={result['hint'] or '-'}"
        )

    if INDEX.loaded:
        print(f"[联想] 铁 → {[s['name'] for s in INDEX.suggest('铁', 5)]}")

    chars = list_characters()
    if chars:
        demo = record_stock(chars[0]["id"], "黄铜锭", "背包", 25, 0)
        print(f"[写入] {demo}")
        check = search(query="黄铜锭")
        print(f"[回读] 命中 {len(check['items'])} 组，合计 {sum(i['total'] for i in check['items'])}")

    try:
        snap = create_snapshot("manual", "自检")
        listing = list_snapshots()
        print(f"[快照] 创建成功 {snap.get('name')}，当前共 {len(listing)} 个")
        delete_snapshot(snap["name"])
        print("[快照] 已清理自检快照")
    except Exception as exc:  # noqa: BLE001
        print(f"[快照] 失败：{type(exc).__name__}: {exc}")

    print("== 自检结束 ==")
    return 0


def _ensure_stdio() -> None:
    """打包成无控制台窗口的 exe 时，sys.stdout 会是 None，print 会直接崩。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handle = open(DATA_DIR / "console.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = handle
        if sys.stderr is None:
            sys.stderr = handle
    except OSError:
        pass


def main() -> int:
    _ensure_stdio()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(description="FF14 材料台账")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--selftest", action="store_true", help="不启动服务，只跑一遍逻辑")
    parser.add_argument("--clear-demo", action="store_true", help="删除示例数据")
    parser.add_argument("--no-browser", action="store_true", help="什么都不自动打开（调试用）")
    parser.add_argument("--browser", action="store_true", help="强制用浏览器，不用内嵌窗口")
    parser.add_argument("--seed-demo", action="store_true", help="写入示例数据（开发用）")
    parser.add_argument("--restore-db", metavar="PATH", help="用指定的 .db 文件恢复数据后退出")
    parser.add_argument("--keep-alive", action="store_true",
                        help="页面关掉后不自动退出（默认已不自动退出，保留兼容）")
    parser.add_argument("--auto-exit", action="store_true",
                        help="关闭所有页面后自动退出（默认关闭）")
    parser.add_argument("--build-recipes", action="store_true",
                        help="下载并重建配方表（data/recipe.json）后退出")
    parser.add_argument("--build-npc", action="store_true",
                        help="下载并重建 NPC 金币商店表（data/npc_shop.json）后退出")
    parser.add_argument("--build-items", action="store_true",
                        help="重新下载并重建物品表（data/items.json，补 can_be_hq 字段）")
    args = parser.parse_args()

    lock_error = _acquire_data_lock()
    if lock_error:
        print(lock_error)
        print("请先关闭另一个窗口；或设置 FF14_STOCK_DATA 指向别的数据目录。")
        return 1

    if args.seed_demo:
        global SEED_DEMO
        SEED_DEMO = True

    if args.restore_db:
        return restore_db_from_path(args.restore_db)

    if args.build_recipes:
        bootstrap()
        result = build_recipe_index(progress=lambda m: print("[配方] " + m, flush=True))
        print(result)
        return 0 if result.get("ok") else 1

    if args.build_npc:
        bootstrap()
        result = build_npc_shop_index(progress=lambda m: print("[NPC] " + m, flush=True))
        print(result)
        return 0 if result.get("ok") else 1

    if args.build_items:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ok = INDEX.build_from_csv(progress=lambda m: print("[物品表] " + m, flush=True))
        print({"ok": ok, "items": INDEX.count, "with_hq": INDEX.has_hq,
               "error": INDEX.error})
        return 0 if ok else 1

    if args.selftest and not _selftest_allowed():
        print("[自检] 已拒绝：--selftest 会写演示库存和流水。")
        print("       请在隔离目录运行，例如：")
        print("       $env:FF14_STOCK_DATA='C:\\Temp\\ff14-selftest'; python app.py --selftest")
        return 1

    bootstrap()
    boot_stage("数据初始化")

    if args.clear_demo:
        removed = clear_demo()
        print(f"已删除 {removed} 个示例角色及其库存")
        return 0
    if args.selftest:
        return selftest()

    global AUTO_QUIT
    AUTO_QUIT = bool(args.auto_exit) and not args.keep_alive

    serve(
        args.port,
        force_browser=args.browser or args.no_browser,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
