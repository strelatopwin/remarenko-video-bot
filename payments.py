import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiohttp

PLANS = {
    "day": ("RP День", 1, 46),
    "five": ("RP П’ять днів", 5, 230),
    "week": ("RP Тиждень", 7, 391),
    "month": ("RP Місяць", 30, 414),
}
TRIALS = 1
DAY = 86400
API = "https://api.wayforpay.com/api"
SIGNED_STATUS = (
    "merchantAccount", "orderReference", "amount", "currency",
    "authCode", "cardPan", "transactionStatus", "reasonCode",
)


CLIENT_STAGES = {
    "new": "Нові · ще без пробного кружечка",
    "trial": "Використали пробний кружечок",
    "pending": "Створили рахунок · оплата не підтверджена",
    "active": "Мають активний доступ",
    "expired": "Платний доступ закінчився",
}
CLIENTS = """
    WITH ids AS (
        SELECT user_id FROM clients
        UNION SELECT user_id FROM trial_deliveries
        UNION SELECT user_id FROM orders
        UNION SELECT user_id FROM access
    ), classified AS (
        SELECT ids.user_id, c.first_name, c.username, c.last_seen,
               paid.plan AS paid_plan, pending.plan AS pending_plan,
               CASE
                   WHEN a.expires_at > :now THEN 'active'
                   WHEN a.expires_at > 0 OR paid.reference IS NOT NULL THEN 'expired'
                   WHEN pending.reference IS NOT NULL THEN 'pending'
                   WHEN EXISTS (SELECT 1 FROM trial_deliveries t WHERE t.user_id = ids.user_id) THEN 'trial'
                   ELSE 'new'
               END AS stage
        FROM ids
        LEFT JOIN clients c USING (user_id)
        LEFT JOIN access a USING (user_id)
        LEFT JOIN orders paid ON paid.reference = (
            SELECT reference FROM orders WHERE user_id = ids.user_id AND activated_at IS NOT NULL
            ORDER BY activated_at DESC, created_at DESC, reference DESC LIMIT 1
        )
        LEFT JOIN orders pending ON pending.reference = (
            SELECT reference FROM orders WHERE user_id = ids.user_id
                AND activated_at IS NULL AND created_at > :invoice_since
            ORDER BY created_at DESC, reference DESC LIMIT 1
        )
        WHERE ids.user_id != :owner
    )
"""


class Payments:
    def __init__(self, session):
        self.session = session
        self.owner = int(os.getenv("OWNER_ID", "788167958"))
        self.account = os.getenv("WFP_MERCHANT_ACCOUNT", "").strip()
        self.secret = os.getenv("WFP_SECRET_KEY", "").strip()
        self.domain = os.getenv("WFP_MERCHANT_DOMAIN", "").strip()
        settings = (self.account, self.secret, self.domain)
        self.enabled = any(settings)
        if self.enabled and not all(settings):
            raise SystemExit("Set WFP_MERCHANT_ACCOUNT, WFP_SECRET_KEY and WFP_MERCHANT_DOMAIN together.")
        if not self.enabled:
            return
        path = Path(os.getenv("ACCESS_DB_PATH", "/data/video-access.sqlite3"))
        if os.getenv("RAILWAY_ENVIRONMENT_ID"):
            mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
            if not mount or not path.resolve().is_relative_to(Path(mount).resolve()):
                raise SystemExit("Mount a Railway Volume at /data before enabling payments.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS clients (
                    user_id INTEGER PRIMARY KEY, first_name TEXT NOT NULL,
                    username TEXT NOT NULL, last_seen INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS access (
                    user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS trial_deliveries (
                    user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    reference TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    plan TEXT NOT NULL, days INTEGER NOT NULL, amount INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, url TEXT NOT NULL,
                    checked_at INTEGER NOT NULL DEFAULT 0, activated_at INTEGER,
                    notified_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS orders_by_client ON orders(user_id, activated_at, created_at);
            """)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def remember_client(self, user):
        if not self.enabled or user["id"] == self.owner:
            return
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO clients VALUES (?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name,
                   username=excluded.username, last_seen=excluded.last_seen""",
                (user["id"], user.get("first_name") or "", user.get("username") or "", int(time.time())),
            )

    def client_report(self, stage=None, offset=0):
        now = int(time.time())
        params = {"now": now, "invoice_since": now - DAY, "owner": self.owner,
                  "stage": stage, "offset": offset}
        with closing(self.connect()) as db:
            if stage is not None:
                return db.execute(
                    CLIENTS + """SELECT * FROM classified WHERE stage = :stage
                                 ORDER BY last_seen DESC, user_id DESC LIMIT 11 OFFSET :offset""",
                    params,
                ).fetchall()

            stages = dict(db.execute(CLIENTS + "SELECT stage, COUNT(*) FROM classified GROUP BY stage", params))
            plans = db.execute(
                """SELECT plan, COUNT(*) AS purchases, SUM(amount) AS revenue
                   FROM orders WHERE activated_at IS NOT NULL AND user_id != ?
                   GROUP BY plan""",
                (self.owner,),
            ).fetchall()
            return stages, plans

    def state(self, user_id):
        with closing(self.connect()) as db:
            expiry = db.execute(
                "SELECT expires_at FROM access WHERE user_id=?", (user_id,)
            ).fetchone()
            used = db.execute(
                "SELECT COUNT(*) FROM trial_deliveries WHERE user_id=?", (user_id,)
            ).fetchone()[0]
        return expiry[0] if expiry else 0, max(0, TRIALS - used)

    def status(self, user_id):
        if user_id == self.owner:
            return "👑 Доступ власника без обмежень."
        if not self.enabled:
            return "🎥 Доступ відкритий."
        expiry, remaining = self.state(user_id)
        now = int(time.time())
        if expiry > now:
            hours = (expiry - now + 3599) // 3600
            end = datetime.fromtimestamp(expiry, ZoneInfo("Europe/Kyiv"))
            return f"✅ Доступ до {end:%d.%m.%Y о %H:%M} за Києвом. Залишилось приблизно {hours // 24} дн. {hours % 24} год."
        return f"🎁 Безкоштовних кружечків залишилось: {remaining} із {TRIALS}."

    def use_trial(self, user_id):
        if not self.enabled or user_id == self.owner:
            return False
        expiry, remaining = self.state(user_id)
        return expiry <= time.time() and remaining > 0

    def blocked(self, user_id):
        if not self.enabled or user_id == self.owner:
            return False
        expiry, remaining = self.state(user_id)
        return expiry <= time.time() and not remaining

    def record_trial(self, user_id, message_id):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO trial_deliveries VALUES (?,?)", (user_id, message_id))

    def sign(self, fields):
        data = ";".join("" if value is None else str(value) for value in fields)
        return hmac.new(self.secret.encode(), data.encode(), hashlib.md5).hexdigest()

    async def request(self, payload):
        async with self.session.post(API, json=payload, timeout=aiohttp.ClientTimeout(total=12),
                                     allow_redirects=False) as response:
            response.raise_for_status()
            result = await response.json(content_type=None)
        if not isinstance(result, dict):
            raise ValueError("Invalid WayForPay response")
        return result

    async def invoice(self, user_id, plan):
        name, days, amount = PLANS[plan]
        now = int(time.time())
        with closing(self.connect()) as db:
            recent = db.execute(
                """SELECT url FROM orders WHERE user_id=? AND plan=? AND activated_at IS NULL
                   AND created_at>? ORDER BY created_at DESC LIMIT 1""",
                (user_id, plan, now - DAY + 60),
            ).fetchone()
        if recent:
            return recent["url"]

        reference = "RPVIDEO-" + uuid4().hex
        price = f"{amount}.00"
        title = f"Remarenko Кружечки · {name}"
        result = await self.request({
            "transactionType": "CREATE_INVOICE", "apiVersion": 1, "language": "UA",
            "merchantAccount": self.account, "merchantAuthType": "SimpleSignature",
            "merchantDomainName": self.domain, "orderReference": reference,
            "orderDate": now, "orderTimeout": DAY, "amount": price, "currency": "UAH",
            "productName": [title], "productCount": [1], "productPrice": [price],
            "merchantSignature": self.sign([
                self.account, self.domain, reference, now, price, "UAH", title, 1, price,
            ]),
        })
        url = result.get("invoiceUrl") or ""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "secure.wayforpay.com":
            raise ValueError("Invalid invoice URL")
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO orders(reference,user_id,plan,days,amount,created_at,url)
                   VALUES (?,?,?,?,?,?,?)""",
                (reference, user_id, plan, days, amount, now, url),
            )
        return url

    async def approved(self, order):
        reference = order["reference"]
        result = await self.request({
            "transactionType": "CHECK_STATUS", "apiVersion": 1,
            "merchantAccount": self.account, "orderReference": reference,
            "merchantSignature": self.sign([self.account, reference]),
        })
        if result.get("transactionStatus") != "Approved":
            return False
        signature = result.get("merchantSignature")
        if (
            not isinstance(signature, str)
            or result["merchantAccount"] != self.account
            or result["orderReference"] != reference
            or result["currency"] != "UAH"
            or Decimal(str(result["amount"])) != order["amount"]
            or not hmac.compare_digest(signature.lower(), self.sign(result[k] for k in SIGNED_STATUS))
        ):
            raise ValueError("WayForPay payment verification failed")
        return True

    def activate(self, order):
        now = int(time.time())
        with closing(self.connect()) as db, db:
            updated = db.execute(
                "UPDATE orders SET activated_at=? WHERE reference=? AND activated_at IS NULL",
                (now, order["reference"]),
            ).rowcount
            if updated:
                db.execute(
                    """INSERT INTO access VALUES (?,?)
                       ON CONFLICT(user_id) DO UPDATE SET expires_at=max(access.expires_at,?) + ?""",
                    (order["user_id"], now + order["days"] * DAY, now, order["days"] * DAY),
                )

    async def run(self, notify):
        while True:
            now = int(time.time())
            with closing(self.connect()) as db:
                pending = db.execute(
                    """SELECT * FROM orders WHERE activated_at IS NULL
                       AND checked_at < CASE WHEN created_at>? THEN ? ELSE ? END
                       ORDER BY (created_at>?) DESC, checked_at, created_at LIMIT 10""",
                    (now - 3 * DAY, now - 60, now - DAY, now - 3 * DAY),
                ).fetchall()
            for order in pending:
                with closing(self.connect()) as db, db:
                    db.execute("UPDATE orders SET checked_at=? WHERE reference=?", (now, order["reference"]))
                try:
                    if await self.approved(order):
                        self.activate(order)
                except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError, InvalidOperation) as error:
                    logging.warning("WayForPay status check failed (%s)", type(error).__name__)

            with closing(self.connect()) as db:
                paid = db.execute(
                    """SELECT reference,user_id FROM orders WHERE activated_at IS NOT NULL
                       AND notified_at IS NULL ORDER BY activated_at LIMIT 10"""
                ).fetchall()
            for order in paid:
                with closing(self.connect()) as db, db:
                    db.execute("UPDATE orders SET notified_at=? WHERE reference=?", (now, order["reference"]))
                try:
                    await notify(order["user_id"], "💳 Оплату підтверджено.\n" + self.status(order["user_id"]))
                except Exception as error:
                    logging.warning("Payment notification failed (%s)", type(error).__name__)
            await asyncio.sleep(10)
