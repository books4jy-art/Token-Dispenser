"""SQLite storage for the point shop bot.

Every function that changes points or stock runs inside one transaction
(`BEGIN IMMEDIATE`), so two purchases at the same moment can never spend the
same points or hand out the same stock item twice.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    balance    INTEGER NOT NULL DEFAULT 0,
    total_charged INTEGER NOT NULL DEFAULT 0,
    total_spent   INTEGER NOT NULL DEFAULT 0,
    last_daily INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    price       INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,            -- 'stock' | 'role' | 'manual'
    role_id     INTEGER,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stock (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    content    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    price      INTEGER NOT NULL,
    delivered  TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,             -- 'done' | 'pending' | 'refunded'
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS charges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    depositor  TEXT NOT NULL,             -- 입금자명 the user said they will send from
    status     TEXT NOT NULL,             -- 'pending' | 'approved' | 'rejected' | 'expired'
    handled_by INTEGER,                   -- admin id, or 0 when matched automatically
    points     INTEGER NOT NULL DEFAULT 0,
    log_channel_id INTEGER,
    log_message_id INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deposits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    depositor  TEXT NOT NULL,
    raw        TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,      -- stops a re-sent notification paying twice
    charge_id  INTEGER,                   -- NULL until matched to a charge request
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL,
    amount     INTEGER NOT NULL,          -- won taken out of the bank account
    note       TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


MEMBERSHIP_DAYS = 30  # how long one monthly membership lasts

# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add them to a
# database that already exists, so they are added here one by one if missing.
MIGRATIONS = {
    "products": {
        "category": "TEXT NOT NULL DEFAULT '기타'",
        "unit": "TEXT NOT NULL DEFAULT ''",          # '' = fixed item, else e.g. '200개' (price is per unit)
        "max_qty": "INTEGER NOT NULL DEFAULT 1",
        "sale_start": "INTEGER",                      # KST hour the item goes on sale (NULL = always)
        "sale_end": "INTEGER",                        # KST hour it stops (exclusive)
        "form": "TEXT",                               # NULL = no order form, '' = game codes only,
                                                      # other text = codes + a required field with that label
        "auto": "TEXT",                               # fulfil.AUTO key: the bot edits the save itself
        "risk": "TEXT NOT NULL DEFAULT ''",          # '' | some | high: ban-risk warning (catalog.RISK_TEXT)
        "position": "INTEGER",                        # order in the shop (catalog items); NULL = by id
        "membership": "TEXT NOT NULL DEFAULT ''",    # '' | '<tier>:month' | '<tier>:forever' (role given)
    },
    "orders": {
        "quantity": "INTEGER NOT NULL DEFAULT 1",
        "request": "TEXT NOT NULL DEFAULT ''",        # what the buyer filled in (game codes etc.)
        "job": "TEXT NOT NULL DEFAULT ''",            # JSON for an automatic edit (holds the game codes)
        "job_state": "TEXT NOT NULL DEFAULT ''",      # '' | queued | running | done | failed | interrupted
        "log_channel_id": "INTEGER",
        "log_message_id": "INTEGER",
    },
}


class ShopError(Exception):
    """A purchase/charge problem whose message is shown to the user as-is."""


@dataclass
class Product:
    id: int
    guild_id: int
    name: str
    price: int
    description: str
    kind: str
    role_id: int | None
    active: bool
    stock: int  # remaining stock items (only meaningful for kind == 'stock')
    category: str = "기타"
    unit: str = ""
    max_qty: int = 1
    sale_start: int | None = None
    sale_end: int | None = None
    form: str | None = None
    auto: str | None = None
    risk: str = ""
    membership: str = ""

    def on_sale(self, hour: int) -> bool:
        """Whether the item can be bought at this KST hour (0–23)."""
        if self.sale_start is None or self.sale_end is None or self.sale_start == self.sale_end:
            return True
        if self.sale_start < self.sale_end:
            return self.sale_start <= hour < self.sale_end
        return hour >= self.sale_start or hour < self.sale_end  # window past midnight, e.g. 22~4

    def sale_hours(self) -> str:
        if self.sale_start is None or self.sale_end is None or self.sale_start == self.sale_end:
            return ""
        return f"{self.sale_start:02d}:00~{self.sale_end:02d}:00"


@dataclass
class Purchase:
    order_id: int
    product: Product
    delivered: str
    balance: int
    price: int  # what was actually paid in total
    quantity: int = 1


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self._lock = threading.Lock()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------ settings --
    def get_setting(self, guild_id: int, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, guild_id: int, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, value),
            )

    # -------------------------------------------------------------- points --
    @staticmethod
    def _ensure_user(c: sqlite3.Connection, guild_id: int, user_id: int) -> sqlite3.Row:
        c.execute(
            "INSERT OR IGNORE INTO users (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id)
        )
        return c.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ).fetchone()

    @staticmethod
    def _add(c: sqlite3.Connection, guild_id: int, user_id: int, delta: int, reason: str,
             allow_negative: bool = False) -> int:
        row = Database._ensure_user(c, guild_id, user_id)
        new_balance = row["balance"] + delta
        if new_balance < 0 and not allow_negative:
            raise ShopError(f"잔액이 부족해요. (보유: {row['balance']:,}원)")
        c.execute(
            "UPDATE users SET balance=? WHERE guild_id=? AND user_id=?",
            (new_balance, guild_id, user_id),
        )
        c.execute(
            "INSERT INTO ledger (guild_id, user_id, delta, reason, created_at) VALUES (?,?,?,?,?)",
            (guild_id, user_id, delta, reason, int(time.time())),
        )
        return new_balance

    def get_user(self, guild_id: int, user_id: int) -> sqlite3.Row:
        with self._tx() as c:
            return self._ensure_user(c, guild_id, user_id)

    def add_points(self, guild_id: int, user_id: int, delta: int, reason: str) -> int:
        with self._tx() as c:
            return self._add(c, guild_id, user_id, delta, reason)

    def claim_daily(self, guild_id: int, user_id: int, amount: int, day_start: int) -> int | None:
        """Give the daily reward. Returns the new balance, or None if already claimed today."""
        with self._tx() as c:
            row = self._ensure_user(c, guild_id, user_id)
            if row["last_daily"] >= day_start:
                return None
            c.execute(
                "UPDATE users SET last_daily=? WHERE guild_id=? AND user_id=?",
                (int(time.time()), guild_id, user_id),
            )
            return self._add(c, guild_id, user_id, amount, "출석 체크")

    def leaderboard(self, guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT user_id, balance FROM users WHERE guild_id=? AND balance>0 "
            "ORDER BY balance DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()

    # ------------------------------------------------------------ products --
    def _product(self, c: sqlite3.Connection, product_id: int, guild_id: int) -> Product | None:
        row = c.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id) AS stock_count "
            "FROM products p WHERE p.id=? AND p.guild_id=?",
            (product_id, guild_id),
        ).fetchone()
        if row is None:
            return None
        return Product(
            id=row["id"], guild_id=row["guild_id"], name=row["name"], price=row["price"],
            description=row["description"], kind=row["kind"], role_id=row["role_id"],
            active=bool(row["active"]), stock=row["stock_count"], category=row["category"],
            unit=row["unit"], max_qty=row["max_qty"], sale_start=row["sale_start"],
            sale_end=row["sale_end"], form=row["form"], auto=row["auto"], risk=row["risk"] or "",
            membership=row["membership"] or "",
        )

    def get_product(self, guild_id: int, product_id: int) -> Product | None:
        return self._product(self._conn, product_id, guild_id)

    def list_products(self, guild_id: int, include_inactive: bool = False) -> list[Product]:
        sql = "SELECT id FROM products WHERE guild_id=? AND kind != 'lifetime'"
        if not include_inactive:
            sql += " AND active=1"
        ids = [r["id"] for r in self._conn.execute(sql + " ORDER BY position IS NULL, position, id", (guild_id,))]
        return [p for i in ids if (p := self._product(self._conn, i, guild_id))]

    def add_product(
        self, guild_id: int, name: str, price: int, description: str, kind: str,
        role_id: int | None = None, category: str = "기타", unit: str = "", max_qty: int = 1,
        sale_start: int | None = None, sale_end: int | None = None, form: str | None = None,
    ) -> int:
        with self._tx() as c:
            return self._insert_product(
                c, guild_id, name, price, description, kind, role_id, category, unit,
                max_qty, sale_start, sale_end, form,
            )

    @staticmethod
    def _insert_product(c: sqlite3.Connection, guild_id: int, name: str, price: int,
                        description: str, kind: str, role_id: int | None, category: str,
                        unit: str, max_qty: int, sale_start: int | None, sale_end: int | None,
                        form: str | None, auto: str | None = None) -> int:
        cur = c.execute(
            "INSERT INTO products (guild_id, name, price, description, kind, role_id, category, "
            "unit, max_qty, sale_start, sale_end, form, auto) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (guild_id, name, price, description, kind, role_id, category, unit,
             max(1, max_qty), sale_start, sale_end, form, auto),
        )
        return cur.lastrowid

    def add_catalog(self, guild_id: int, items: list[dict]) -> tuple[int, int, int]:
        """Make the shop this list. Items with the same name are updated (price included), new
        ones added, and every other 수동/자동 처리 item taken off sale (orders keep their history).
        Returns (added, updated, removed)."""
        added = updated = 0
        with self._tx() as c:
            existing = {
                r["name"]: r["id"] for r in c.execute(
                    "SELECT id, name FROM products WHERE guild_id=? AND active=1 AND kind='manual'", (guild_id,)
                )
            }
            keep: set[int] = set()
            for position, item in enumerate(items):
                values = (item["price"], item.get("description", ""), item.get("category", "기타"),
                          item.get("auto"), item.get("unit", ""), max(1, item.get("max_qty", 1)),
                          item.get("form"), item.get("sale_start"), item.get("sale_end"),
                          item.get("risk", ""), item.get("membership", ""), position)
                if item["name"] in existing:
                    pid = existing[item["name"]]
                    c.execute(
                        "UPDATE products SET price=?, description=?, category=?, auto=?, unit=?, max_qty=?, "
                        "form=?, sale_start=?, sale_end=?, risk=?, membership=?, position=? WHERE id=?",
                        (*values, pid),
                    )
                    updated += 1
                else:
                    pid = self._insert_product(
                        c, guild_id, item["name"], item["price"], item.get("description", ""),
                        item.get("kind", "manual"), None, item.get("category", "기타"),
                        item.get("unit", ""), item.get("max_qty", 1), item.get("sale_start"),
                        item.get("sale_end"), item.get("form"), item.get("auto"),
                    )
                    c.execute("UPDATE products SET risk=?, membership=?, position=? WHERE id=?",
                              (item.get("risk", ""), item.get("membership", ""), position, pid))
                    added += 1
                keep.add(pid)
            gone = [pid for pid in existing.values() if pid not in keep]
            c.executemany("UPDATE products SET active=0 WHERE id=?", [(pid,) for pid in gone])
        return added, updated, len(gone)

    def update_product(self, guild_id: int, product_id: int, **fields: object) -> bool:
        """Change product fields. None = leave as is; -1 for sale_start/sale_end clears them."""
        allowed = {"name", "price", "description", "active", "category", "unit", "max_qty",
                   "sale_start", "sale_end"}
        fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
        for k in ("sale_start", "sale_end"):
            if fields.get(k) == -1:
                fields[k] = None
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as c:
            cur = c.execute(
                f"UPDATE products SET {sets} WHERE id=? AND guild_id=?",
                (*fields.values(), product_id, guild_id),
            )
            return cur.rowcount > 0

    def add_stock(self, guild_id: int, product_id: int, items: list[str]) -> int:
        with self._tx() as c:
            product = self._product(c, product_id, guild_id)
            if product is None or product.kind != "stock":
                raise ShopError("재고형 상품이 아니거나 존재하지 않는 상품이에요.")
            c.executemany(
                "INSERT INTO stock (product_id, content) VALUES (?, ?)",
                [(product_id, item) for item in items],
            )
            return product.stock + len(items)

    def clear_stock(self, guild_id: int, product_id: int) -> int:
        with self._tx() as c:
            if self._product(c, product_id, guild_id) is None:
                raise ShopError("존재하지 않는 상품이에요.")
            return c.execute("DELETE FROM stock WHERE product_id=?", (product_id,)).rowcount

    # ------------------------------------------------------------ purchase --
    def purchase(
        self, guild_id: int, user_id: int, product_id: int, quantity: int = 1,
        request: str = "", hour: int | None = None, job: str = "",
    ) -> Purchase:
        with self._tx() as c:
            product = self._product(c, product_id, guild_id)
            if product is None or not product.active or product.kind not in ("stock", "role", "manual"):
                raise ShopError("판매 중인 상품이 아니에요.")
            if hour is not None and not product.on_sale(hour):
                raise ShopError(f"지금은 판매 시간이 아니에요. (판매 시간: {product.sale_hours()})")
            max_qty = product.max_qty if product.unit and product.kind != "stock" else 1
            if not 1 <= quantity <= max_qty:
                raise ShopError(f"수량은 1~{max_qty} 사이로 입력해 주세요.")
            delivered = ""
            status = "done"
            if product.kind == "stock":
                item = c.execute(
                    "SELECT id, content FROM stock WHERE product_id=? ORDER BY id LIMIT 1",
                    (product_id,),
                ).fetchone()
                if item is None:
                    raise ShopError("품절된 상품이에요. 재고가 채워지면 다시 시도해 주세요.")
                c.execute("DELETE FROM stock WHERE id=?", (item["id"],))
                delivered = item["content"]
            elif product.kind == "manual":
                status = "pending"
            price = product.price * quantity
            balance = self._add(c, guild_id, user_id, -price, f"구매: {product.name} x{quantity}")
            c.execute(
                "UPDATE users SET total_spent=total_spent+? WHERE guild_id=? AND user_id=?",
                (price, guild_id, user_id),
            )
            cur = c.execute(
                "INSERT INTO orders (guild_id, user_id, product_id, product_name, price, "
                "delivered, status, created_at, quantity, request, job, job_state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (guild_id, user_id, product.id, product.name, price, delivered,
                 status, int(time.time()), quantity, request, job, "queued" if job else ""),
            )
            return Purchase(cur.lastrowid, product, delivered, balance, price, quantity)

    def refund_order(self, guild_id: int, order_id: int) -> sqlite3.Row:
        with self._tx() as c:
            order = c.execute(
                "SELECT * FROM orders WHERE id=? AND guild_id=?", (order_id, guild_id)
            ).fetchone()
            if order is None:
                raise ShopError("존재하지 않는 주문이에요.")
            if order["status"] == "refunded":
                raise ShopError("이미 환불된 주문이에요.")
            if order["price"]:
                self._add(c, guild_id, order["user_id"], order["price"], f"환불: 주문 #{order_id}")
            c.execute(
                "UPDATE users SET total_spent=MAX(total_spent-?, 0) WHERE guild_id=? AND user_id=?",
                (order["price"], guild_id, order["user_id"]),
            )
            c.execute("UPDATE orders SET status='refunded', request='', job='' WHERE id=?", (order_id,))
            return order

    def complete_order(self, guild_id: int, order_id: int) -> sqlite3.Row:
        with self._tx() as c:
            order = c.execute(
                "SELECT * FROM orders WHERE id=? AND guild_id=?", (order_id, guild_id)
            ).fetchone()
            if order is None or order["status"] != "pending":
                raise ShopError("처리 대기 중인 주문이 아니에요.")
            # The buyer's game codes aren't needed once the order is done.
            c.execute("UPDATE orders SET status='done', request='', job='' WHERE id=?", (order_id,))
            return order

    def memberships(self, guild_id: int, user_id: int | None = None) -> dict[tuple[int, str], int | None]:
        """Every (user, tier) that ever bought a membership -> when it ends (None = permanent).

        Built from the orders, so past purchases count too; refunded ones don't add any time. A monthly
        purchase lasts MEMBERSHIP_DAYS per unit; buying again before it ends adds on to it."""
        sql = ("SELECT o.user_id, o.created_at, o.quantity, o.status, p.membership FROM orders o "
               "JOIN products p ON p.id=o.product_id WHERE o.guild_id=? AND p.membership!=''")
        args: list[object] = [guild_id]
        if user_id is not None:
            sql += " AND o.user_id=?"
            args.append(user_id)
        ends: dict[tuple[int, str], int | None] = {}
        for row in self._conn.execute(sql + " ORDER BY o.created_at, o.id", args):
            tier, _, period = row["membership"].partition(":")
            key = (row["user_id"], tier)
            if row["status"] == "refunded":
                ends.setdefault(key, 0)  # ended (unless another purchase keeps it going)
            elif period == "forever":
                ends[key] = None
            elif key not in ends or ends[key] is not None:
                start = max(ends.get(key) or 0, row["created_at"])
                ends[key] = start + MEMBERSHIP_DAYS * 86400 * max(1, row["quantity"])
        return ends

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def set_order_log(self, order_id: int, channel_id: int, message_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE orders SET log_channel_id=?, log_message_id=? WHERE id=?",
                      (channel_id, message_id, order_id))

    def set_job_state(self, order_id: int, state: str, wipe: bool = False) -> None:
        """Track an automatic edit. wipe=True also drops the game codes once they're used."""
        with self._tx() as c:
            if wipe:
                c.execute("UPDATE orders SET job_state=?, job='', request='' WHERE id=?", (state, order_id))
            else:
                c.execute("UPDATE orders SET job_state=? WHERE id=?", (state, order_id))

    def jobs_in_state(self, *states: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(states))
        return self._conn.execute(
            f"SELECT * FROM orders WHERE status='pending' AND job_state IN ({marks}) ORDER BY id", states
        ).fetchall()

    def recent_orders(self, guild_id: int, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM orders WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, user_id, limit),
        ).fetchall()

    # ------------------------------------------------------------- charges --
    @staticmethod
    def _approve(
        c: sqlite3.Connection, charge: sqlite3.Row, points: int, admin_id: int
    ) -> int:
        c.execute(
            "UPDATE charges SET status='approved', handled_by=?, points=? WHERE id=?",
            (admin_id, points, charge["id"]),
        )
        balance = Database._add(
            c, charge["guild_id"], charge["user_id"], points, f"충전 #{charge['id']}"
        )
        c.execute(
            "UPDATE users SET total_charged=total_charged+? WHERE guild_id=? AND user_id=?",
            (points, charge["guild_id"], charge["user_id"]),
        )
        return balance

    def create_charge(
        self, guild_id: int, user_id: int, amount: int, depositor: str,
        points: int, match_window: int,
    ) -> tuple[int, int | None]:
        """File a charge request. If a matching deposit already arrived (the user sent
        the money before asking), it is approved at once.
        Returns (charge id, new balance if approved right away else None)."""
        depositor = normalize_name(depositor)
        now = int(time.time())
        with self._tx() as c:
            pending = c.execute(
                "SELECT COUNT(*) AS n FROM charges WHERE guild_id=? AND user_id=? AND status='pending'",
                (guild_id, user_id),
            ).fetchone()["n"]
            if pending >= 3:
                raise ShopError("처리 대기 중인 충전 신청이 너무 많아요. 입금 확인을 기다려 주세요.")
            cur = c.execute(
                "INSERT INTO charges (guild_id, user_id, amount, depositor, status, created_at) "
                "VALUES (?,?,?,?, 'pending', ?)",
                (guild_id, user_id, amount, depositor, now),
            )
            charge_id = cur.lastrowid
            for dep in c.execute(
                "SELECT * FROM deposits WHERE guild_id=? AND charge_id IS NULL AND amount=? "
                "AND created_at>=? ORDER BY id",
                (guild_id, amount, now - match_window),
            ).fetchall():
                if names_match(depositor, dep["depositor"]):
                    c.execute("UPDATE deposits SET charge_id=? WHERE id=?", (charge_id, dep["id"]))
                    charge = c.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone()
                    return charge_id, self._approve(c, charge, points, 0)
            return charge_id, None

    def pending_charges(self, guild_id: int, limit: int = 25) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM charges WHERE guild_id=? AND status='pending' ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()

    def unique_amount(self, guild_id: int, amount: int, match_window: int) -> int:
        """An amount no other waiting request uses (adds 1–99원 if needed), so a deposit
        without the sender's name can still be matched to exactly one request."""
        since = int(time.time()) - match_window
        taken = {r["amount"] for r in self._conn.execute(
            "SELECT amount FROM charges WHERE guild_id=? AND status='pending' AND created_at>=?",
            (guild_id, since),
        )}
        if amount not in taken:
            return amount
        for extra in secrets.SystemRandom().sample(range(1, 100), 99):
            if amount + extra not in taken:
                return amount + extra
        raise ShopError("지금은 같은 금액의 충전 신청이 너무 많아요. 잠시 뒤 다시 시도해 주세요.")

    def set_charge_log(self, charge_id: int, channel_id: int, message_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE charges SET log_channel_id=?, log_message_id=? WHERE id=?",
                (channel_id, message_id, charge_id),
            )

    def get_charge(self, charge_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone()

    def resolve_charge(
        self, guild_id: int, charge_id: int, approve: bool, admin_id: int, points: int
    ) -> tuple[sqlite3.Row, int | None]:
        """Approve or reject a pending charge by hand. Returns (charge, new balance or None)."""
        with self._tx() as c:
            charge = c.execute(
                "SELECT * FROM charges WHERE id=? AND guild_id=?", (charge_id, guild_id)
            ).fetchone()
            if charge is None:
                raise ShopError("존재하지 않는 충전 신청이에요.")
            if charge["status"] != "pending":
                raise ShopError("이미 처리된 충전 신청이에요.")
            if not approve:
                c.execute(
                    "UPDATE charges SET status='rejected', handled_by=? WHERE id=?",
                    (admin_id, charge_id),
                )
                return charge, None
            return charge, self._approve(c, charge, points, admin_id)

    def cancel_charge(self, guild_id: int, charge_id: int, admin_id: int) -> tuple[sqlite3.Row, int]:
        """Undo an approved top-up whose money never arrived: take the balance back (it may go
        below zero if it was already spent) and drop it from the vault."""
        with self._tx() as c:
            charge = c.execute(
                "SELECT * FROM charges WHERE id=? AND guild_id=?", (charge_id, guild_id)
            ).fetchone()
            if charge is None:
                raise ShopError("존재하지 않는 충전 신청이에요.")
            if charge["status"] != "approved":
                raise ShopError("승인된 충전만 취소할 수 있어요.")
            balance = self._add(c, guild_id, charge["user_id"], -charge["points"],
                                f"충전 취소 #{charge_id} (by {admin_id})", allow_negative=True)
            c.execute(
                "UPDATE users SET total_charged=MAX(total_charged-?, 0) WHERE guild_id=? AND user_id=?",
                (charge["points"], guild_id, charge["user_id"]),
            )
            c.execute("UPDATE charges SET status='cancelled', handled_by=? WHERE id=?", (admin_id, charge_id))
            c.execute("UPDATE deposits SET charge_id=NULL WHERE charge_id=?", (charge_id,))
            return charge, balance

    def expire_charges(self, older_than: int) -> list[sqlite3.Row]:
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM charges WHERE status='pending' AND created_at<?", (older_than,)
            ).fetchall()
            c.execute(
                "UPDATE charges SET status='expired' WHERE status='pending' AND created_at<?",
                (older_than,),
            )
            return rows

    # ------------------------------------------------------------ deposits --
    def record_deposit(
        self, guild_id: int, amount: int, depositor: str, raw: str, dedupe_key: str,
        points_for: Callable[[int], int], match_window: int,
    ) -> tuple[int, sqlite3.Row | None, int | None] | None:
        """Store a bank deposit and match it to the oldest pending charge with the same
        amount and depositor name. Returns None for a duplicate notification, otherwise
        (deposit id, matched charge or None, user's new balance or None)."""
        depositor = normalize_name(depositor)
        now = int(time.time())
        with self._tx() as c:
            try:
                cur = c.execute(
                    "INSERT INTO deposits (guild_id, amount, depositor, raw, dedupe_key, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (guild_id, amount, depositor, raw, dedupe_key, now),
                )
            except sqlite3.IntegrityError:
                return None
            deposit_id = cur.lastrowid
            waiting = c.execute(
                "SELECT * FROM charges WHERE guild_id=? AND status='pending' AND amount=? "
                "AND created_at>=? ORDER BY id",
                (guild_id, amount, now - match_window),
            ).fetchall()
            if depositor:
                matches = [ch for ch in waiting if names_match(ch["depositor"], depositor)]
            else:
                # No sender name in the notification: only safe when exactly one request
                # is waiting for this exact amount.
                matches = waiting if len(waiting) == 1 else []
            if matches:
                charge = matches[0]
                c.execute("UPDATE deposits SET charge_id=? WHERE id=?", (charge["id"], deposit_id))
                return deposit_id, charge, self._approve(c, charge, points_for(amount), 0)
            return deposit_id, None, None

    def unmatched_deposits(self, guild_id: int, limit: int = 15) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM deposits WHERE guild_id=? AND charge_id IS NULL ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()

    def link_deposit(
        self, guild_id: int, deposit_id: int, user_id: int, points: int, admin_id: int
    ) -> tuple[sqlite3.Row, int]:
        """Credit an unmatched deposit to a user by hand (an admin matched it)."""
        with self._tx() as c:
            dep = c.execute(
                "SELECT * FROM deposits WHERE id=? AND guild_id=?", (deposit_id, guild_id)
            ).fetchone()
            if dep is None:
                raise ShopError("존재하지 않는 입금 내역이에요.")
            if dep["charge_id"] is not None:
                raise ShopError("이미 처리된 입금이에요.")
            cur = c.execute(
                "INSERT INTO charges (guild_id, user_id, amount, depositor, status, created_at) "
                "VALUES (?,?,?,?, 'pending', ?)",
                (guild_id, user_id, dep["amount"], dep["depositor"], int(time.time())),
            )
            charge = c.execute("SELECT * FROM charges WHERE id=?", (cur.lastrowid,)).fetchone()
            c.execute("UPDATE deposits SET charge_id=? WHERE id=?", (charge["id"], deposit_id))
            return dep, self._approve(c, charge, points, admin_id)

    def price_for(self, guild_id: int, user_id: int, product: Product) -> int:
        """What this user pays for one unit."""
        return product.price

    # --------------------------------------------------------------- vault --
    # The vault is the real money (won) users paid for points: every approved
    # charge adds its amount, every admin withdrawal takes it out.
    @staticmethod
    def _vault(c: sqlite3.Connection, guild_id: int) -> dict[str, int]:
        received = c.execute(
            "SELECT COALESCE(SUM(amount), 0) AS n, COUNT(*) AS k FROM charges "
            "WHERE guild_id=? AND status='approved'",
            (guild_id,),
        ).fetchone()
        withdrawn = c.execute(
            "SELECT COALESCE(SUM(amount), 0) AS n FROM withdrawals WHERE guild_id=?", (guild_id,)
        ).fetchone()["n"]
        unmatched = c.execute(
            "SELECT COALESCE(SUM(amount), 0) AS n, COUNT(*) AS k FROM deposits "
            "WHERE guild_id=? AND charge_id IS NULL",
            (guild_id,),
        ).fetchone()
        return {
            "received": received["n"],
            "charges": received["k"],
            "withdrawn": withdrawn,
            "available": received["n"] - withdrawn,
            "unmatched": unmatched["n"],
            "unmatched_count": unmatched["k"],
        }

    def vault(self, guild_id: int) -> dict[str, int]:
        return self._vault(self._conn, guild_id)

    def withdraw(self, guild_id: int, admin_id: int, amount: int, note: str) -> dict[str, int]:
        with self._tx() as c:
            available = self._vault(c, guild_id)["available"]
            if amount > available:
                raise ShopError(f"금고 잔액이 부족해요. (출금 가능: {available:,}원)")
            c.execute(
                "INSERT INTO withdrawals (guild_id, admin_id, amount, note, created_at) "
                "VALUES (?,?,?,?,?)",
                (guild_id, admin_id, amount, note, int(time.time())),
            )
            return self._vault(c, guild_id)

    def recent_withdrawals(self, guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM withdrawals WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()


def normalize_name(name: str) -> str:
    name = re.sub(r"\([^)]*\)|（[^）]*）", "", name)  # drop a bracketed memo: "송이(찐막)" -> "송이"
    return "".join(name.split())[:20]


def names_match(requested: str, deposited: str) -> bool:
    """Same name, ignoring spaces and case. Banks cut long names short in their
    notifications, so a deposited name of 3+ characters that starts the requested
    name also counts."""
    a, b = normalize_name(requested).lower(), normalize_name(deposited).lower()
    if not a or not b:
        return False
    return a == b or (len(b) >= 3 and a.startswith(b))
