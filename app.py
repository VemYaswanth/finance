from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import hmac
from io import BytesIO
import json
import os
import re
import shutil
import sqlite3
import ssl
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - exercised only when dependency is missing
    PdfReader = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USERS_DIR = DATA_DIR / "users"
TEMPLATES_DIR = BASE_DIR / "templates"
MAX_UPLOAD_BYTES = 35 * 1024 * 1024
ADMIN_USER_ID = "admin"
PASSWORD_ITERATIONS = 260_000
PLAID_ENV_URLS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}
PLAID_SOURCE = "Plaid Sync"


@dataclass
class Transaction:
    id: str
    statement: str
    account_type: str
    account_name: str
    date: str
    description: str
    amount: float
    category: str
    source_line: str


CATEGORY_KEYWORDS = {
    "Credit Card Payment": [
        "credit card payment",
        "payment to credit card",
        "payment thank you",
        "payment received",
        "automatic payment - thank you",
        "statement credit",
        "autopay payment",
        "online payment",
        "american express ach pmt",
        "chase credit crd autopay",
        "chase crd autopay",
        "credit crd autopay",
        "ach pmt",
        "pymt",
        "mobile pymt",
        "auto pymt",
        "chase card",
        "amex epayment",
        "american express payment",
        "discover payment",
        "capital one payment",
        "citi card payment",
    ],
    "Housing": ["rent", "mortgage", "apartment", "hoa", "property tax", "applegate"],
    "Utilities": ["electric", "water", "gas", "internet", "utility", "comcast", "xfinity", "at&t", "verizon"],
    "Groceries": ["grocery", "market", "kroger", "walmart", "costco", "sams club", "aldi", "trader joe", "whole foods"],
    "Dining": [
        "restaurant",
        "cafe",
        "coffee",
        "starbucks",
        "mcdonald",
        "chipotle",
        "doordash",
        "uber eats",
        "culvers",
        "taco bell",
        "domino",
        "fresh india",
        "desi brothers",
        "butter chicken",
        "sankranti",
    ],
    "Transportation": [
        "gas station",
        "fuel",
        "uber",
        "lyft",
        "parking",
        "toll",
        "shell",
        "chevron",
        "exxon",
        "kwik trip",
        "holiday stations",
        "regal auto wash",
    ],
    "Travel": ["frontier", "southwest airlines", "delta air lines", "expedia", "airbnb", "hertz", "hotel", "lodging", "lugless"],
    "Entertainment": ["amc", "regal cinemas", "xscape", "steam purchase", "psn"],
    "Health": ["pharmacy", "clinic", "doctor", "dental", "cvs", "walgreens", "hospital"],
    "Subscriptions": ["netflix", "spotify", "hulu", "apple.com", "google", "microsoft", "subscription"],
    "Shopping": ["amazon", "target", "best buy", "store", "retail", "shop", "wal-mart", "wm supercenter", "walmart.com", "usps"],
    "Debt": ["loan", "interest charge", "student loan"],
    "Transfers": ["transfer", "zelle", "venmo", "paypal", "cash app"],
    "Income": [
        "payroll",
        "salary",
        "deposit",
        "direct dep",
        "interest paid",
        "interest payment",
        "dividend",
        "tax ref",
        "taxrfd",
        "casttaxrfd",
        "franchise tax",
        "irs treas",
    ],
    "Fees": ["fee", "overdraft", "maintenance charge", "atm withdrawal fee", "official checks charge"],
}

DATE_PATTERNS = [
    r"(?P<date>\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)",
    r"(?P<date>\d{4}-\d{1,2}-\d{1,2})",
]
MONEY_RE = re.compile(r"(?P<amount>[-+]?\(?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})\)?|[-+]?\$?\d+\.\d{2})")
SKIP_LINE_RE = re.compile(
    r"\b("
    r"statement period|opening balance|closing balance|beginning balance|ending balance|"
    r"previous balance|new balance|available balance|account summary|daily balance|"
    r"total deposits|total withdrawals|total credits|total debits|subtotal|page \d+"
    r")\b",
    re.I,
)
POSITIVE_HINT_RE = re.compile(
    r"\b(payroll|salary|deposit|direct dep|interest paid|interest payment|dividend|refund|tax ref|taxrfd|casttaxrfd|franchise tax|irs treas|reversal|credit|cashback|cash back)\b",
    re.I,
)
NEGATIVE_HINT_RE = re.compile(
    r"\b(purchase|withdrawal|debit|fee|payment to|autopay|pos|checkcard|card purchase|ach debit)\b",
    re.I,
)
BALANCE_HINT_RE = re.compile(r"\b(balance|available|ledger)\b", re.I)
INTERNAL_FLOW_CATEGORIES = {"Transfers", "Credit Card Payment"}
CATEGORY_PRIORITY = [
    "Credit Card Payment",
    "Travel",
    "Housing",
    "Utilities",
    "Groceries",
    "Dining",
    "Transportation",
    "Entertainment",
    "Health",
    "Subscriptions",
    "Shopping",
    "Debt",
    "Transfers",
    "Income",
    "Fees",
]


def ensure_dirs() -> None:
    USERS_DIR.mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return slug[:60] or f"user-{uuid.uuid4().hex[:8]}"


def user_dir(user_id: str) -> Path:
    canonical_id = slugify(user_id)
    canonical = USERS_DIR / canonical_id
    if canonical.exists() or not USERS_DIR.exists():
        return canonical
    for path in USERS_DIR.iterdir():
        if not path.is_dir():
            continue
        profile = path / "profile.json"
        if not profile.exists():
            continue
        try:
            meta = read_json(profile, {})
        except Exception:
            continue
        if slugify(str(meta.get("id", path.name))) == canonical_id:
            return path
    return canonical


def user_meta_path(user_id: str) -> Path:
    return user_dir(user_id) / "profile.json"


def transactions_path(user_id: str) -> Path:
    return user_dir(user_id) / "transactions.json"


def statement_cache_path(user_id: str) -> Path:
    return user_dir(user_id) / "statement_cache.json"


def raw_text_dir(user_id: str) -> Path:
    return user_dir(user_id) / "raw_text"


def statements_dir(user_id: str) -> Path:
    return user_dir(user_id) / "statements"


def category_overrides_path(user_id: str) -> Path:
    return user_dir(user_id) / "category_overrides.json"


def merchant_rules_path(user_id: str) -> Path:
    return user_dir(user_id) / "merchant_rules.json"


def manual_transactions_path(user_id: str) -> Path:
    return user_dir(user_id) / "manual_transactions.json"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    tmp.replace(path)


def password_hash(password: str, salt: str | None = None) -> dict[str, Any]:
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_ITERATIONS)
    return {"password_salt": salt, "password_hash": digest.hex(), "password_iterations": PASSWORD_ITERATIONS}


def verify_password(meta: dict[str, Any], password: str) -> bool:
    salt = meta.get("password_salt")
    stored = meta.get("password_hash")
    iterations = int(meta.get("password_iterations", PASSWORD_ITERATIONS))
    if not salt or not stored:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return hmac.compare_digest(digest.hex(), str(stored))


def public_user_meta(meta: dict[str, Any]) -> dict[str, Any]:
    public = {key: value for key, value in meta.items() if not key.startswith("password_")}
    public["is_admin"] = public.get("id") == ADMIN_USER_ID
    public["has_password"] = bool(meta.get("password_hash"))
    return public


def database_path() -> Path:
    return USERS_DIR / "financial_review.sqlite3"


def db_connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(database_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS statements (
                user_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                transaction_count INTEGER NOT NULL DEFAULT 0,
                parsed_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'parsed',
                error TEXT,
                PRIMARY KEY (user_id, filename)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                statement TEXT NOT NULL,
                account_type TEXT NOT NULL,
                account_name TEXT NOT NULL,
                date TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                source_line TEXT NOT NULL,
                signature TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_user_date
                ON transactions(user_id, date);
            CREATE INDEX IF NOT EXISTS idx_transactions_user_statement
                ON transactions(user_id, statement);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_user_signature
                ON transactions(user_id, signature);

            CREATE TABLE IF NOT EXISTS category_overrides (
                user_id TEXT NOT NULL,
                signature TEXT NOT NULL,
                category TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, signature)
            );

            CREATE TABLE IF NOT EXISTS merchant_rules (
                user_id TEXT NOT NULL,
                pattern TEXT NOT NULL,
                category TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, pattern)
            );

            CREATE TABLE IF NOT EXISTS plaid_items (
                user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                access_token TEXT NOT NULL,
                institution_name TEXT,
                cursor TEXT,
                accounts_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_synced_at TEXT,
                PRIMARY KEY (user_id, item_id)
            );
            """
        )


def upsert_user_record(meta: dict[str, Any]) -> None:
    init_db()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users (id, name, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name
            """,
            (meta["id"], meta.get("name", meta["id"]), meta.get("created_at", datetime.now().isoformat(timespec="seconds"))),
        )


def row_to_transaction(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "statement": row["statement"],
        "account_type": row["account_type"],
        "account_name": row["account_name"],
        "date": row["date"],
        "description": row["description"],
        "amount": row["amount"],
        "category": row["category"],
        "source_line": row["source_line"],
    }


def db_has_user_data(user_id: str) -> bool:
    init_db()
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM transactions WHERE user_id = ?) +
                (SELECT COUNT(*) FROM statements WHERE user_id = ?) +
                (SELECT COUNT(*) FROM category_overrides WHERE user_id = ?) +
                (SELECT COUNT(*) FROM merchant_rules WHERE user_id = ?) +
                (SELECT COUNT(*) FROM plaid_items WHERE user_id = ?) AS count
            """,
            (user_id, user_id, user_id, user_id, user_id),
        ).fetchone()
    return bool(row and row["count"])


def migrate_user_json_to_db(user_id: str) -> None:
    if db_has_user_data(user_id):
        return

    transactions = read_json(transactions_path(user_id), [])
    overrides = read_json(category_overrides_path(user_id), {})
    rules = read_json(merchant_rules_path(user_id), [])
    cache = load_statement_cache(user_id)
    now = datetime.now().isoformat(timespec="seconds")

    if transactions:
        replace_all_transactions(user_id, transactions)

    with db_connect() as conn:
        for filename, entry in cache.get("statements", {}).items():
            fingerprint = entry.get("fingerprint", {})
            if not isinstance(fingerprint, dict):
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO statements
                    (user_id, filename, size, mtime_ns, transaction_count, parsed_at, status, error)
                VALUES (?, ?, ?, ?, ?, ?, 'parsed', NULL)
                """,
                (
                    user_id,
                    filename,
                    int(fingerprint.get("size", 0)),
                    int(fingerprint.get("mtime_ns", 0)),
                    int(entry.get("transaction_count", 0)),
                    entry.get("updated_at", now),
                ),
            )
        for signature, category in overrides.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO category_overrides (user_id, signature, category, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, signature, category, now),
            )
        for rule in rules:
            pattern = rule.get("pattern")
            category = rule.get("category")
            if pattern and category:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO merchant_rules (user_id, pattern, category, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, pattern, category, rule.get("created_at", now)),
                )


def list_users() -> list[dict[str, Any]]:
    ensure_dirs()
    init_db()
    users: list[dict[str, Any]] = []
    for path in sorted(USERS_DIR.iterdir()):
        if not path.is_dir():
            continue
        meta = read_json(path / "profile.json", {})
        user_id = slugify(str(meta.get("id", path.name)))
        migrate_user_json_to_db(user_id)
        users.append(
            {
                "id": user_id,
                "name": meta.get("name", user_id),
                "created_at": meta.get("created_at"),
                "is_admin": user_id == ADMIN_USER_ID,
                "has_password": bool(meta.get("password_hash")),
                "transaction_count": count_user_transactions(user_id),
                "statement_count": len(list((path / "statements").glob("*.pdf"))) if (path / "statements").exists() else 0,
            }
        )
    return users


def authenticate_user(name: str, password: str) -> dict[str, Any]:
    ensure_dirs()
    user_id = slugify(name)
    if not password:
        raise ValueError("PASSWORD_REQUIRED")
    path = user_dir(user_id)
    is_new = not path.exists()
    statements_dir(user_id).mkdir(parents=True, exist_ok=True)
    raw_text_dir(user_id).mkdir(parents=True, exist_ok=True)

    meta = read_json(user_meta_path(user_id), {})
    if not meta:
        meta = {"id": user_id, "name": name.strip() or user_id, "created_at": datetime.now().isoformat(timespec="seconds")}
        meta.update(password_hash(password))
        write_json(user_meta_path(user_id), meta)
    elif not meta.get("password_hash"):
        meta.update(password_hash(password))
        write_json(user_meta_path(user_id), meta)
    elif not verify_password(meta, password):
        raise PermissionError("PASSWORD_MISMATCH")
    upsert_user_record(meta)
    migrate_user_json_to_db(user_id)
    public = public_user_meta(meta)
    public["is_new"] = is_new
    return public




def issue_session_token(user_id: str) -> str:
    """Create a browser remember token without storing the raw token on disk."""
    token = uuid.uuid4().hex + uuid.uuid4().hex
    meta_path = user_meta_path(user_id)
    meta = read_json(meta_path, {})
    tokens = meta.get("session_tokens", [])
    if not isinstance(tokens, list):
        tokens = []
    tokens = tokens[-9:]
    tokens.append({
        "hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    meta["session_tokens"] = tokens
    write_json(meta_path, meta)
    return token


def authenticate_session_token(user_id: str, token: str) -> dict[str, Any]:
    user_id = slugify(user_id)
    if not user_id or not token:
        raise PermissionError("SESSION_REQUIRED")
    meta = read_json(user_meta_path(user_id), None)
    if meta is None:
        raise FileNotFoundError("UNKNOWN_USER")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    tokens = meta.get("session_tokens", [])
    if not isinstance(tokens, list) or not any(item.get("hash") == digest for item in tokens if isinstance(item, dict)):
        raise PermissionError("SESSION_EXPIRED")
    upsert_user_record(meta)
    migrate_user_json_to_db(user_id)
    return public_user_meta(meta)

def create_or_login_user(name: str, password: str = "password") -> dict[str, Any]:
    return authenticate_user(name, password)


def create_user(name: str, password: str = "password") -> dict[str, Any]:
    return authenticate_user(name, password)


def require_admin(user_id: str) -> None:
    if slugify(user_id) != ADMIN_USER_ID or not user_dir(ADMIN_USER_ID).exists():
        raise PermissionError("ADMIN_REQUIRED")


def admin_create_user(name: str, password: str) -> dict[str, Any]:
    if not name.strip():
        raise ValueError("USER_NAME_REQUIRED")
    if not password:
        raise ValueError("PASSWORD_REQUIRED")
    user_id = slugify(name)
    if user_dir(user_id).exists():
        raise FileExistsError("USER_EXISTS")
    return authenticate_user(name, password)


def admin_change_password(user_id: str, password: str) -> dict[str, Any]:
    user_id = slugify(user_id)
    if not password:
        raise ValueError("PASSWORD_REQUIRED")
    meta = read_json(user_meta_path(user_id), None)
    if meta is None:
        raise FileNotFoundError("UNKNOWN_USER")
    meta.update(password_hash(password))
    write_json(user_meta_path(user_id), meta)
    return public_user_meta(meta)


def remove_user(user_id: str) -> None:
    user_id = slugify(user_id)
    if user_id == ADMIN_USER_ID:
        raise ValueError("CANNOT_REMOVE_ADMIN")
    path = user_dir(user_id)
    if not path.exists():
        raise FileNotFoundError("UNKNOWN_USER")
    with db_connect() as conn:
        for table in ("transactions", "statements", "category_overrides", "merchant_rules", "plaid_items", "users"):
            conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'users' else 'user_id'} = ?", (user_id,))
    shutil.rmtree(path)


def extract_pdf_text(pdf_path: Path) -> str:
    if PdfReader is None:
        raise RuntimeError("The pypdf package is required. Run: python -m pip install pypdf")
    reader = PdfReader(str(pdf_path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def parse_numeric_date(value: str) -> date | None:
    value = value.strip()
    for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%y"]:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_textual_date(month_name: str, day: str, year: str) -> date | None:
    month = MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def infer_statement_end_date(text: str, statement_name: str) -> date | None:
    patterns = [
        r"Opening/Closing Date\s+\d{1,2}/\d{1,2}/\d{2,4}\s*-\s*(?P<date>\d{1,2}/\d{1,2}/\d{2,4})",
        r"Statement Date:\s*(?P<date>\d{1,2}/\d{1,2}/\d{2,4})",
        r"(?<!/)Closing Date\s+(?P<date>\d{1,2}/\d{1,2}/\d{2,4})",
        r"STATEMENT PERIOD\s+(?P<start_month>[A-Za-z]+)\s+\d{1,2}\s*-\s*(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})",
        r"through\s+(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        if "date" in match.groupdict():
            parsed = parse_numeric_date(match.group("date"))
        else:
            parsed = parse_textual_date(match.group("month"), match.group("day"), match.group("year"))
        if parsed:
            return parsed

    filename_match = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", statement_name)
    if filename_match:
        try:
            return date(int(filename_match.group(1)), int(filename_match.group(2)), int(filename_match.group(3)))
        except ValueError:
            return None
    return None


def resolve_yearless_date(value: str, statement_year: int | None, statement_end: date | None) -> date | None:
    match = re.fullmatch(r"(?P<month>\d{1,2})[/-](?P<day>\d{1,2})", value.strip())
    if not match:
        return None
    month = int(match.group("month"))
    day = int(match.group("day"))
    year = statement_end.year if statement_end else (statement_year or date.today().year)
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if statement_end and candidate > statement_end + timedelta(days=45):
        candidate = date(year - 1, month, day)
    return candidate


def normalize_date(value: str, statement_year: int | None = None, statement_end: date | None = None) -> str:
    value = value.strip()
    yearless = resolve_yearless_date(value, statement_year, statement_end)
    if yearless:
        return yearless.isoformat()
    parsed = parse_numeric_date(value)
    if parsed:
        return parsed.isoformat()
    return value


def normalize_textual_date(month_name: str, day: str, statement_year: int | None, statement_end: date | None) -> str:
    year = statement_end.year if statement_end else (statement_year or date.today().year)
    parsed = parse_textual_date(month_name, day, str(year))
    if not parsed:
        return f"{month_name} {day}"
    if statement_end and parsed > statement_end + timedelta(days=45):
        parsed = date(parsed.year - 1, parsed.month, parsed.day)
    return parsed.isoformat()


def parse_amount(value: str) -> Decimal | None:
    cleaned = value.replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def amount_has_explicit_sign(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(("-", "+", "(")) or stripped.endswith("-")


def signed_amount_from_context(amount: Decimal, token: str, context: str) -> Decimal:
    text = context.lower()
    if amount_has_explicit_sign(token):
        return amount
    if "online transfer from" in text or "transfer from" in text:
        return abs(amount)
    if "online transfer to" in text or "transfer to" in text:
        return -abs(amount)
    if POSITIVE_HINT_RE.search(text):
        return abs(amount)
    if NEGATIVE_HINT_RE.search(text):
        return -abs(amount)
    return -abs(amount)


def infer_statement_profile(text: str, statement_name: str) -> dict[str, str]:
    probe = f"{statement_name}\n{text[:2500]}".lower()
    account_type = "unknown"
    if re.search(r"\b(blue cash everyday|credit card|cardmember|minimum payment|payment due|credit limit|account activity|mastercard ending|visa signature)\b", probe):
        account_type = "credit_card"
    elif "capitalone.com" in probe or "capital one 360" in probe or "360 checking" in probe:
        account_type = "checking"
    elif re.search(r"\b(checking & savings|chase college checking|deposit account|primary account)\b", probe):
        account_type = "checking"
    elif re.search(r"\b(savings|money market)\b", probe):
        account_type = "savings"
    elif re.search(r"\b(checking|debit card|atm withdrawal)\b", probe):
        account_type = "checking"

    if "blue cash everyday" in probe or probe.lstrip().startswith("american express"):
        institution = "American Express"
    elif "discover it card" in probe or "discover.com" in probe:
        institution = "Discover"
    elif "capitalone.com" in probe or "capital one 360" in probe or "360 checking" in probe:
        institution = "Capital One"
    elif "bankofamerica.com" in probe or "bank of america" in probe[:1200]:
        institution = "Bank of America"
    else:
        institution = "Unknown"
    institutions = {
        "American Express": ["american express", "amex"],
        "Chase": ["chase", "jpmorgan"],
        "Bank of America": ["bank of america", "bofa"],
        "Capital One": ["capital one", "capitalone.com", "360 checking"],
        "Citi": ["citibank", "citi"],
        "Discover": ["discover"],
        "Wells Fargo": ["wells fargo"],
    }
    if institution == "Unknown":
        for name, needles in institutions.items():
            if name == "American Express" and account_type != "credit_card":
                continue
            if any(needle in probe for needle in needles):
                institution = name
                break

    suffix = ""
    suffix_match = re.search(r"(?:account|card)\s*(?:number|ending|ending in|#)?\s*[:#]?\s*([x*\-\s\d]{4,24})", probe)
    if suffix_match:
        digits = re.sub(r"\D", "", suffix_match.group(1))
        if len(digits) >= 4:
            suffix = f" {digits[-4:]}"

    product = ""
    if "blue cash everyday" in probe:
        product = " Blue Cash"
    if institution == "Capital One" and "360 checking" in probe and account_type != "credit_card":
        return {"account_type": "checking", "account_name": "Capital One 360 Checking 2748"}
    return {
        "account_type": account_type,
        "account_name": f"{institution}{product} {account_type.replace('_', ' ').title()}{suffix}".strip(),
    }


def profile_for_section(base_profile: dict[str, str], section: str) -> dict[str, str]:
    if section == "checking":
        return {"account_type": "checking", "account_name": "Chase College Checking 8912"}
    if section == "savings":
        return {"account_type": "savings", "account_name": "Chase Savings 7225"}
    return base_profile


def update_deposit_section(line: str, current: str) -> str:
    upper = line.upper()
    if "CHASE SAVINGS" in upper or "SAVINGS SUMMARY" in upper:
        return "savings"
    if "CHASE COLLEGE CHECKING" in upper or "CHECKING SUMMARY" in upper:
        return "checking"
    if "CHASE CHECKING" in upper or "CHECKING ACCOUNT" in upper:
        return "checking"
    if "CHASE SAVINGS ACCOUNT" in upper:
        return "savings"
    return current


def normalize_credit_card_amount(description: str, amount: Decimal) -> Decimal:
    text = description.lower()
    if "payment" in text or "pymt" in text or "statement credit" in text or "reward/refund" in text or "cash rebate" in text or "bonus" in text:
        return abs(amount)
    return -abs(amount)


def is_likely_balance_only_deposit_line(description: str, amount_token: str, amount_matches: list[re.Match[str]]) -> bool:
    text = description.lower()
    if amount_has_explicit_sign(amount_token):
        return False
    if len(amount_matches) > 1:
        return False
    if "online transfer from" in text or text == "interest payment":
        return True
    return False


def parse_multiline_amex_transactions(
    lines: list[str],
    statement_name: str,
    profile: dict[str, str],
    year: int | None,
    statement_end: date | None,
) -> list[Transaction]:
    transactions: list[Transaction] = []
    current: dict[str, str] | None = None
    in_detail = False

    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if line in {"Payments Amount", "Credits Amount", "Amount"} or line.startswith("Detail"):
            in_detail = True
            continue
        if not in_detail:
            continue

        date_match = re.match(r"(?P<date>\d{1,2}/\d{1,2}/\d{2})\*?\s+(?P<desc>.+)$", line)
        if date_match:
            current = {"date": normalize_date(date_match.group("date"), year, statement_end), "description": date_match.group("desc").strip()}
            continue

        if current:
            amount_match = re.fullmatch(r"(?P<amount>-?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})|-?\$?\d+\.\d{2})", line)
            if amount_match:
                parsed = parse_amount(amount_match.group("amount"))
                if parsed is None:
                    current = None
                    continue
                description = current["description"]
                amount = normalize_credit_card_amount(description, parsed)
                transactions.append(
                    Transaction(
                        id=uuid.uuid4().hex,
                        statement=statement_name,
                        account_type=profile["account_type"],
                        account_name=profile["account_name"],
                        date=current["date"],
                        description=description[:180],
                        amount=float(amount),
                        category=categorize(description, float(amount)),
                        source_line=f"{current['date']} {description} {line}",
                    )
                )
                current = None
            elif not line.startswith("p. ") and not line.startswith("Continued"):
                current["description"] = f"{current['description']} {line}".strip()

    return transactions


def parse_chase_credit_card_line(
    line: str,
    statement_name: str,
    profile: dict[str, str],
    year: int | None,
    statement_end: date | None,
) -> Transaction | None:
    match = re.match(r"(?P<date>\d{1,2}/\d{1,2})\s+(?P<desc>.+?)\s+(?P<amount>-?\d+\.\d{2})$", line)
    if not match:
        return None
    description = match.group("desc").strip()
    if re.fullmatch(r"\d{6}.*", description) or SKIP_LINE_RE.search(description):
        return None
    parsed = parse_amount(match.group("amount"))
    if parsed is None:
        return None
    amount = normalize_credit_card_amount(description, parsed)
    return Transaction(
        id=uuid.uuid4().hex,
        statement=statement_name,
        account_type=profile["account_type"],
        account_name=profile["account_name"],
        date=normalize_date(match.group("date"), year, statement_end),
        description=description[:180],
        amount=float(amount),
        category=categorize(description, float(amount)),
        source_line=line,
    )


def parse_capital_one_360_line(
    line: str,
    statement_name: str,
    profile: dict[str, str],
    year: int | None,
    statement_end: date | None,
) -> Transaction | None:
    match = re.match(
        r"(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s+"
        r"(?P<desc>.+?)\s+(?P<kind>Debit|Credit)\s+(?P<sign>[+-])\s+"
        r"(?P<amount>\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})|\$?\d+\.\d{2})\s+"
        r"(?P<balance>-?\s*\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})|-?\s*\$?\d+\.\d{2})$",
        line,
        flags=re.I,
    )
    if not match:
        return None
    parsed = parse_amount(match.group("amount"))
    if parsed is None or parsed == 0:
        return None
    amount = abs(parsed) if match.group("sign") == "+" else -abs(parsed)
    description = re.sub(r"\s+", " ", match.group("desc")).strip(" -:")
    account_name = profile["account_name"]
    lowered = line.lower()
    if "360 performance savings" in lowered:
        row_profile = {"account_type": "savings", "account_name": "Capital One 360 Performance Savings 2841"}
    elif profile["account_name"].startswith("Capital One"):
        row_profile = {"account_type": "checking", "account_name": "Capital One 360 Checking 2748"}
    elif profile["account_type"] == "unknown":
        row_profile = {"account_type": "checking", "account_name": "Capital One 360 Checking 2748"}
    else:
        row_profile = profile
    return Transaction(
        id=uuid.uuid4().hex,
        statement=statement_name,
        account_type=row_profile["account_type"],
        account_name=row_profile.get("account_name", account_name),
        date=normalize_textual_date(match.group("month"), match.group("day"), year, statement_end),
        description=description[:180],
        amount=float(amount),
        category=categorize(description, float(amount)),
        source_line=line,
    )


def build_credit_card_transaction(
    statement_name: str,
    profile: dict[str, str],
    tx_date: str,
    description: str,
    amount: Decimal,
    source_line: str,
) -> Transaction:
    normalized_amount = normalize_credit_card_amount(description, amount)
    return Transaction(
        id=uuid.uuid4().hex,
        statement=statement_name,
        account_type=profile["account_type"],
        account_name=profile["account_name"],
        date=tx_date,
        description=re.sub(r"\s+", " ", description).strip()[:180],
        amount=float(normalized_amount),
        category=categorize(description, float(normalized_amount)),
        source_line=source_line,
    )


def parse_compact_numeric_credit_card_tables(
    text: str,
    statement_name: str,
    profile: dict[str, str],
    year: int | None,
    statement_end: date | None,
) -> list[Transaction]:
    rows: list[Transaction] = []
    compact = re.sub(r"\s+", " ", text)
    row_re = re.compile(
        r"(?P<date>\d{2}/\d{2})\s+\d{2}/\d{2}\s+"
        r"(?P<desc>.+?)\s+\d{4}\s+\d{4}\s+(?P<amount>-?\d+\.\d{2})"
        r"(?=\s*(?:\d{2}/\d{2}\s+\d{2}/\d{2}|Payments and Other Credits|Purchases and Adjustments|TOTAL|Fees|Interest|$))",
        re.I,
    )
    for match in row_re.finditer(compact):
        parsed = parse_amount(match.group("amount"))
        if parsed is None or parsed == 0:
            continue
        description = match.group("desc")
        if SKIP_LINE_RE.search(description):
            continue
        rows.append(
            build_credit_card_transaction(
                statement_name,
                profile,
                normalize_date(match.group("date"), year, statement_end),
                description,
                parsed,
                match.group(0),
            )
        )
    return rows


def parse_compact_textual_credit_card_tables(
    text: str,
    statement_name: str,
    profile: dict[str, str],
    year: int | None,
    statement_end: date | None,
) -> list[Transaction]:
    rows: list[Transaction] = []
    compact = re.sub(r"\s+", " ", text)
    month = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    row_re = re.compile(
        rf"(?P<tmonth>{month})\s+(?P<tday>\d{{1,2}})\s+{month}\s+\d{{1,2}}\s+"
        rf"(?P<desc>.+?)\s+(?P<sign>-)?\s*\$?(?P<amount>\d{{1,3}}(?:,\d{{3}})*(?:\.\d{{2}})|\d+\.\d{{2}})"
        rf"(?=\s+{month}\s+\d{{1,2}}\s+{month}\s+\d{{1,2}}\s+| YASWANTH| Total| Fees| Interest|\s*$)",
        re.I,
    )
    for match in row_re.finditer(compact):
        parsed = parse_amount(match.group("amount"))
        if parsed is None or parsed == 0:
            continue
        if match.group("sign"):
            parsed = -abs(parsed)
        description = match.group("desc")
        if SKIP_LINE_RE.search(description):
            continue
        rows.append(
            build_credit_card_transaction(
                statement_name,
                profile,
                normalize_textual_date(match.group("tmonth"), match.group("tday"), year, statement_end),
                description,
                parsed,
                match.group(0),
            )
        )
    return rows


def parse_discover_transaction_lines(
    lines: list[str],
    statement_name: str,
    profile: dict[str, str],
    year: int | None,
    statement_end: date | None,
) -> list[Transaction]:
    rows: list[Transaction] = []
    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        match = re.match(
            r"(?P<date>\d{2}/\d{2})\s+(?P<desc>.+?)\s+(?P<category>[A-Za-z/ ]+)\s+\$?(?P<amount>\d+\.\d{2})$",
            line,
        )
        if not match:
            continue
        parsed = parse_amount(match.group("amount"))
        if parsed is None or parsed == 0:
            continue
        description = match.group("desc")
        rows.append(
            build_credit_card_transaction(
                statement_name,
                profile,
                normalize_date(match.group("date"), year, statement_end),
                description,
                parsed,
                line,
            )
        )
    return rows


def parse_chase_compact_activity(
    text: str,
    statement_name: str,
    profile: dict[str, str],
    year: int | None,
    statement_end: date | None,
) -> list[Transaction]:
    rows: list[Transaction] = []
    compact = re.sub(r"\s+", " ", text)
    if "Date of Transaction Merchant Name or Transaction Description" not in compact:
        return rows
    activity = compact.split("Date of Transaction Merchant Name or Transaction Description", 1)[1]
    activity = re.split(r"\b(?:Total fees charged|Year-to-date totals|Interest Charges)\b", activity, maxsplit=1, flags=re.I)[0]
    row_re = re.compile(
        r"(?P<date>\d{2}/\d{2})\s+(?P<desc>.+?)\s+(?P<amount>-?\d+\.\d{2})"
        r"(?=\s+\d{2}/\d{2}\s+|\s*$)",
        re.I,
    )
    for match in row_re.finditer(activity):
        parsed = parse_amount(match.group("amount"))
        if parsed is None or parsed == 0:
            continue
        rows.append(
            build_credit_card_transaction(
                statement_name,
                profile,
                normalize_date(match.group("date"), year, statement_end),
                match.group("desc"),
                parsed,
                match.group(0),
            )
        )
    return rows


def statement_lines_with_continuations(lines: list[str]) -> list[str]:
    combined: list[str] = []
    pending = ""
    starts_textual_transaction = re.compile(r"^[A-Za-z]{3,9}\s+\d{1,2}\b")
    capital_one_amount = re.compile(r"\b(?:Debit|Credit)\s+[+-]\s+\$?\d", re.I)

    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        is_textual_header = bool(re.search(r"\b(through|statement period)\b", line, re.I) or re.match(r"^[A-Za-z]{3,9}\s+\d{1,2}\s*[-,]", line))
        if pending:
            if starts_textual_transaction.search(line):
                combined.append(pending)
                pending = line
            else:
                pending = f"{pending} {line}".strip()
            if capital_one_amount.search(pending):
                combined.append(pending)
                pending = ""
            continue
        if starts_textual_transaction.search(line) and not is_textual_header and not capital_one_amount.search(line):
            pending = line
            continue
        combined.append(line)

    if pending:
        combined.append(pending)
    return combined


def choose_transaction_amount(line: str, date_end: int) -> tuple[re.Match[str], Decimal] | None:
    matches = [match for match in MONEY_RE.finditer(line) if match.start() >= date_end]
    if not matches:
        return None

    if len(matches) == 1:
        match = matches[0]
    else:
        trailing_text = line[matches[-1].end() :]
        before_last = line[max(0, matches[-1].start() - 24) : matches[-1].start()]
        last_looks_like_balance = BALANCE_HINT_RE.search(before_last + " " + trailing_text) or not amount_has_explicit_sign(matches[-1].group("amount"))
        match = matches[-2] if last_looks_like_balance else matches[-1]

    raw_amount = parse_amount(match.group("amount"))
    if raw_amount is None or raw_amount == 0:
        return None
    context = line[date_end : match.start()]
    return match, signed_amount_from_context(raw_amount, match.group("amount"), context)


def categorize(description: str, amount: float) -> str:
    text = description.lower()
    ordered_categories = CATEGORY_PRIORITY + [category for category in CATEGORY_KEYWORDS if category not in CATEGORY_PRIORITY]
    for category in ordered_categories:
        keywords = CATEGORY_KEYWORDS[category]
        if any(keyword in text for keyword in keywords):
            return category
    if amount > 0:
        return "Income"
    return "Uncategorized"


def infer_statement_year(text: str) -> int | None:
    matches = re.findall(r"\b(20\d{2}|19\d{2})\b", text)
    if not matches:
        return None
    years = [int(match) for match in matches]
    return max(set(years), key=years.count)


def parse_transactions(text: str, statement_name: str) -> list[Transaction]:
    year = infer_statement_year(text)
    statement_end = infer_statement_end_date(text, statement_name)
    profile = infer_statement_profile(text, statement_name)
    lines = text.splitlines()
    if profile["account_name"].startswith("American Express"):
        return parse_multiline_amex_transactions(lines, statement_name, profile, year, statement_end)

    transactions: list[Transaction] = []
    seen: set[tuple[str, str, float]] = set()
    deposit_section = "checking" if "Checking & Savings" in text or "CHASE COLLEGE CHECKING" in text else profile["account_type"]

    compact_rows: list[Transaction] = []
    if profile["account_type"] == "credit_card":
        compact_rows.extend(parse_compact_numeric_credit_card_tables(text, statement_name, profile, year, statement_end))
        compact_rows.extend(parse_compact_textual_credit_card_tables(text, statement_name, profile, year, statement_end))
        if profile["account_name"].startswith("Discover"):
            compact_rows.extend(parse_discover_transaction_lines(lines, statement_name, profile, year, statement_end))
        if profile["account_name"].startswith("Chase Credit Card"):
            compact_rows.extend(parse_chase_compact_activity(text, statement_name, profile, year, statement_end))
    for tx in compact_rows:
        key = (tx.date, tx.description.lower(), tx.amount)
        if key in seen:
            continue
        seen.add(key)
        transactions.append(tx)
    if compact_rows and not profile["account_name"].startswith("Chase Credit Card"):
        return transactions
    if profile["account_type"] == "credit_card" and not profile["account_name"].startswith("Chase Credit Card"):
        return transactions

    for line in statement_lines_with_continuations(lines):
        if len(line) < 8:
            continue
        deposit_section = update_deposit_section(line, deposit_section)
        if SKIP_LINE_RE.search(line):
            continue

        if profile["account_name"].startswith("Capital One") and re.match(r"[A-Za-z]{3,9}\s+\d{1,2}\b", line):
            tx = parse_capital_one_360_line(line, statement_name, profile, year, statement_end)
            if tx is not None:
                key = (tx.date, tx.description.lower(), tx.amount)
                if key in seen:
                    continue
                seen.add(key)
                transactions.append(tx)
                continue

        if profile["account_name"].startswith("Chase Credit Card"):
            tx = parse_chase_credit_card_line(line, statement_name, profile, year, statement_end)
            if tx is None:
                continue
            key = (tx.date, tx.description.lower(), tx.amount)
            if key in seen:
                continue
            seen.add(key)
            transactions.append(tx)
            continue

        date_match = None
        for pattern in DATE_PATTERNS:
            date_match = re.search(pattern, line)
            if date_match:
                break
        if not date_match:
            continue

        amount_matches = [match for match in MONEY_RE.finditer(line) if match.start() >= date_match.end()]
        chosen = choose_transaction_amount(line, date_match.end())
        if chosen is None:
            continue
        amount_match, amount = chosen

        tx_date = normalize_date(date_match.group("date"), year, statement_end)
        between = line[date_match.end() : amount_match.start()].strip(" -:\t")
        before = line[: date_match.start()].strip()
        description = between or before or "Unknown transaction"
        description = re.sub(r"\b(?:debit|credit|withdrawal|purchase|transaction)\b", "", description, flags=re.I)
        description = re.sub(r"\s+", " ", description).strip(" -:") or "Unknown transaction"
        if profile["account_type"] in {"checking", "savings"} and is_likely_balance_only_deposit_line(
            description,
            amount_match.group("amount"),
            amount_matches,
        ):
            continue

        key = (tx_date, description.lower(), float(amount))
        if key in seen:
            continue
        seen.add(key)
        row_profile = profile_for_section(profile, deposit_section)
        transactions.append(
            Transaction(
                id=uuid.uuid4().hex,
                statement=statement_name,
                account_type=row_profile["account_type"],
                account_name=row_profile["account_name"],
                date=tx_date,
                description=description[:180],
                amount=float(amount),
                category=categorize(description, float(amount)),
                source_line=line,
            )
        )
    return transactions


def count_user_transactions(user_id: str) -> int:
    init_db()
    with db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM transactions WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["count"] if row else 0)


def load_transactions(user_id: str) -> list[dict[str, Any]]:
    init_db()
    if user_dir(user_id).exists():
        migrate_user_json_to_db(user_id)
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, statement, account_type, account_name, date, description, amount, category, source_line
            FROM transactions
            WHERE user_id = ?
            ORDER BY date, statement, description, amount
            """,
            (user_id,),
        ).fetchall()
    return [row_to_transaction(row) for row in rows]


def replace_all_transactions(user_id: str, transactions: list[dict[str, Any]]) -> None:
    init_db()
    with db_connect() as conn:
        conn.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
        insert_transactions(conn, user_id, transactions)


def save_transactions(user_id: str, transactions: list[dict[str, Any]]) -> None:
    replace_all_transactions(user_id, transactions)


def insert_transactions(conn: sqlite3.Connection, user_id: str, transactions: list[dict[str, Any]]) -> None:
    for tx in transactions:
        payload = dict(tx)
        payload.setdefault("id", f"tx-{uuid.uuid4().hex}")
        payload.setdefault("statement", "")
        payload.setdefault("account_type", "unknown")
        payload.setdefault("account_name", payload.get("account_type", "Unknown"))
        payload.setdefault("date", "")
        payload.setdefault("description", "")
        payload.setdefault("amount", 0.0)
        payload.setdefault("category", "Uncategorized")
        payload.setdefault("source_line", "")
        signature = transaction_signature(payload)
        conn.execute(
            """
            INSERT INTO transactions
                (id, user_id, statement, account_type, account_name, date, description, amount, category, source_line, signature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, signature) DO UPDATE SET
                id = excluded.id,
                statement = excluded.statement,
                account_type = excluded.account_type,
                account_name = excluded.account_name,
                date = excluded.date,
                description = excluded.description,
                amount = excluded.amount,
                category = excluded.category,
                source_line = excluded.source_line
            """,
            (
                payload["id"],
                user_id,
                payload["statement"],
                payload["account_type"],
                payload["account_name"],
                payload["date"],
                payload["description"],
                float(payload["amount"]),
                payload["category"],
                payload["source_line"],
                signature,
            ),
        )


def replace_statement_transactions(
    user_id: str,
    filename: str,
    fingerprint: dict[str, int],
    transactions: list[dict[str, Any]],
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    init_db()
    with db_connect() as conn:
        conn.execute("DELETE FROM transactions WHERE user_id = ? AND statement = ?", (user_id, filename))
        insert_transactions(conn, user_id, transactions)
        conn.execute(
            """
            INSERT INTO statements
                (user_id, filename, size, mtime_ns, transaction_count, parsed_at, status, error)
            VALUES (?, ?, ?, ?, ?, ?, 'parsed', NULL)
            ON CONFLICT(user_id, filename) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                transaction_count = excluded.transaction_count,
                parsed_at = excluded.parsed_at,
                status = 'parsed',
                error = NULL
            """,
            (user_id, filename, fingerprint["size"], fingerprint["mtime_ns"], len(transactions), now),
        )


def mark_statement_error(user_id: str, filename: str, fingerprint: dict[str, int], error: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    init_db()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO statements
                (user_id, filename, size, mtime_ns, transaction_count, parsed_at, status, error)
            VALUES (?, ?, ?, ?, 0, ?, 'error', ?)
            ON CONFLICT(user_id, filename) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                parsed_at = excluded.parsed_at,
                status = 'error',
                error = excluded.error
            """,
            (user_id, filename, fingerprint["size"], fingerprint["mtime_ns"], now, error),
        )


def load_statement_record(user_id: str, filename: str) -> dict[str, Any] | None:
    init_db()
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT filename, size, mtime_ns, transaction_count, parsed_at, status, error
            FROM statements
            WHERE user_id = ? AND filename = ?
            """,
            (user_id, filename),
        ).fetchone()
    return dict(row) if row else None


def statement_record_is_current(entry: dict[str, Any] | None, fingerprint: dict[str, int]) -> bool:
    return bool(
        entry
        and entry.get("status") == "parsed"
        and entry.get("size") == fingerprint["size"]
        and entry.get("mtime_ns") == fingerprint["mtime_ns"]
    )


def prune_missing_statement_records(user_id: str, filenames: set[str]) -> None:
    init_db()
    with db_connect() as conn:
        rows = conn.execute("SELECT filename FROM statements WHERE user_id = ?", (user_id,)).fetchall()
        missing = [row["filename"] for row in rows if row["filename"] not in filenames]
        for filename in missing:
            conn.execute("DELETE FROM transactions WHERE user_id = ? AND statement = ?", (user_id, filename))
            conn.execute("DELETE FROM statements WHERE user_id = ? AND filename = ?", (user_id, filename))


def append_manual_transaction_document(user_id: str, transaction: dict[str, Any]) -> None:
    document = read_json(manual_transactions_path(user_id), [])
    if not isinstance(document, list):
        document = []
    document.append(transaction)
    write_json(manual_transactions_path(user_id), document)


def load_manual_transactions(user_id: str) -> list[dict[str, Any]]:
    return [tx for tx in load_transactions(user_id) if tx.get("statement") == "Manual Entry"]


def remove_manual_transaction(user_id: str, transaction_id: str) -> bool:
    transaction_id = transaction_id.strip()
    if not transaction_id:
        return False
    init_db()
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM transactions
            WHERE user_id = ? AND id = ? AND statement = 'Manual Entry'
            """,
            (user_id, transaction_id),
        ).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM transactions WHERE user_id = ? AND id = ?", (user_id, transaction_id))

    document = read_json(manual_transactions_path(user_id), [])
    if isinstance(document, list):
        write_json(manual_transactions_path(user_id), [tx for tx in document if tx.get("id") != transaction_id])
    return True


def add_manual_transaction(user_id: str, fields: dict[str, list[str]]) -> dict[str, Any] | None:
    tx_date = fields.get("date", [""])[0].strip()
    account_name = fields.get("account", [""])[0].strip()
    description = fields.get("description", [""])[0].strip()
    category = fields.get("category", [""])[0].strip()
    raw_amount = fields.get("amount", [""])[0].strip()
    parsed_amount = parse_amount(raw_amount)

    if not tx_date or not account_name or not description or not category or parsed_amount is None:
        return None
    try:
        datetime.strptime(tx_date, "%Y-%m-%d")
    except ValueError:
        return None

    now = datetime.now().isoformat(timespec="seconds")
    transaction = {
        "id": f"manual-{uuid.uuid4().hex[:12]}",
        "statement": "Manual Entry",
        "account_type": "cash",
        "account_name": account_name,
        "date": tx_date,
        "description": description,
        "amount": float(parsed_amount),
        "category": category,
        "source_line": f"manual entry created {now}",
        "created_at": now,
    }
    init_db()
    with db_connect() as conn:
        insert_transactions(conn, user_id, [transaction])
    append_manual_transaction_document(user_id, transaction)
    return transaction


def add_savings_adjustment(user_id: str, fields: dict[str, list[str]]) -> dict[str, Any] | None:
    target_date = fields.get("date", [""])[0].strip()
    account_name = fields.get("account", [""])[0].strip() or "Savings adjustment"
    raw_amount = fields.get("current_amount", [""])[0].strip()
    target_amount = parse_amount(raw_amount)
    if not target_date or target_amount is None:
        return None
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return None

    current_savings = Decimal(str(savings_total(load_transactions(user_id))))
    adjustment = target_amount - current_savings
    now = datetime.now().isoformat(timespec="seconds")
    transaction = {
        "id": f"adjustment-{uuid.uuid4().hex[:12]}",
        "statement": "Manual Entry",
        "account_type": "cash",
        "account_name": account_name,
        "date": target_date,
        "description": f"Savings balance adjustment to ${float(target_amount):,.2f}",
        "amount": float(adjustment),
        "category": "Savings Adjustment",
        "source_line": f"manual savings adjustment created {now}",
        "created_at": now,
        "target_savings": float(target_amount),
        "previous_savings": float(current_savings),
    }
    init_db()
    with db_connect() as conn:
        insert_transactions(conn, user_id, [transaction])
    append_manual_transaction_document(user_id, transaction)
    return transaction


def load_statement_cache(user_id: str) -> dict[str, Any]:
    cache = read_json(statement_cache_path(user_id), {"version": 1, "statements": {}})
    if not isinstance(cache, dict):
        return {"version": 1, "statements": {}}
    statements = cache.get("statements")
    if not isinstance(statements, dict):
        cache["statements"] = {}
    cache["version"] = 1
    return cache


def save_statement_cache(user_id: str, cache: dict[str, Any]) -> None:
    write_json(statement_cache_path(user_id), cache)


def statement_fingerprint(pdf_path: Path) -> dict[str, int]:
    stat = pdf_path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def cached_statement_is_current(entry: dict[str, Any] | None, fingerprint: dict[str, int]) -> bool:
    return bool(entry and entry.get("fingerprint") == fingerprint and isinstance(entry.get("transactions"), list))


def transaction_signature(tx: dict[str, Any]) -> str:
    if tx.get("statement") == "Manual Entry":
        return "|".join(
            [
                "manual",
                str(tx.get("id", "")),
                str(tx.get("account_name", "")),
                str(tx.get("date", "")),
                str(tx.get("description", "")).strip().lower(),
                f"{float(tx.get('amount', 0.0)):.2f}",
            ]
        )
    if tx.get("statement") == PLAID_SOURCE:
        return "|".join(["plaid", str(tx.get("id", ""))])
    return "|".join(
        [
            str(tx.get("statement", "")),
            str(tx.get("account_name", "")),
            str(tx.get("date", "")),
            str(tx.get("description", "")).strip().lower(),
            f"{float(tx.get('amount', 0.0)):.2f}",
        ]
    )


def transaction_dedupe_key(tx: dict[str, Any]) -> tuple[Any, ...]:
    return (tx.get("date"), str(tx.get("description", "")).lower(), tx.get("amount"), tx.get("statement"))


def load_category_overrides(user_id: str) -> dict[str, str]:
    init_db()
    if user_dir(user_id).exists():
        migrate_user_json_to_db(user_id)
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT signature, category FROM category_overrides WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {row["signature"]: row["category"] for row in rows}


def save_category_overrides(user_id: str, overrides: dict[str, str]) -> None:
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as conn:
        conn.execute("DELETE FROM category_overrides WHERE user_id = ?", (user_id,))
        for signature, category in overrides.items():
            conn.execute(
                """
                INSERT INTO category_overrides (user_id, signature, category, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, signature, category, now),
            )


def apply_category_overrides(user_id: str, transactions: list[dict[str, Any]]) -> None:
    overrides = load_category_overrides(user_id)
    for tx in transactions:
        category = overrides.get(transaction_signature(tx))
        if category:
            tx["category"] = category


def load_merchant_rules(user_id: str) -> list[dict[str, str]]:
    init_db()
    if user_dir(user_id).exists():
        migrate_user_json_to_db(user_id)
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT pattern, category, created_at
            FROM merchant_rules
            WHERE user_id = ?
            ORDER BY created_at, pattern
            """,
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows if row["pattern"] and row["category"]]


def save_merchant_rules(user_id: str, rules: list[dict[str, str]]) -> None:
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as conn:
        conn.execute("DELETE FROM merchant_rules WHERE user_id = ?", (user_id,))
        for rule in rules:
            pattern = rule.get("pattern")
            category = rule.get("category")
            if pattern and category:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO merchant_rules (user_id, pattern, category, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, pattern, category, rule.get("created_at", now)),
                )


def apply_merchant_rules_to_transactions(rules: list[dict[str, str]], transactions: list[dict[str, Any]]) -> int:
    updated = 0
    for tx in transactions:
        description = tx.get("description", "").lower()
        original = tx.get("category")
        for rule in rules:
            pattern = rule.get("pattern", "").lower().strip()
            if pattern and pattern in description:
                tx["category"] = rule["category"]
        if tx.get("category") != original:
            updated += 1
    return updated


def apply_merchant_rules(user_id: str, transactions: list[dict[str, Any]]) -> int:
    return apply_merchant_rules_to_transactions(load_merchant_rules(user_id), transactions)


def add_merchant_rule(user_id: str, pattern: str, category: str) -> dict[str, Any] | None:
    pattern = re.sub(r"\s+", " ", pattern.strip())
    category = category.strip()
    if not pattern or not category:
        return None
    rules = load_merchant_rules(user_id)
    rule = {"pattern": pattern, "category": category, "created_at": datetime.now().isoformat(timespec="seconds")}
    replaced = False
    for index, existing in enumerate(rules):
        if existing.get("pattern", "").lower() == pattern.lower():
            rules[index] = rule
            replaced = True
            break
    if not replaced:
        rules.append(rule)
    save_merchant_rules(user_id, rules)

    transactions = load_transactions(user_id)
    updated_count = apply_merchant_rules_to_transactions([rule], transactions)
    apply_category_overrides(user_id, transactions)
    save_transactions(user_id, transactions)
    return {"rule": rule, "updated_count": updated_count}


def set_transaction_category(user_id: str, transaction_id: str, category: str) -> dict[str, Any] | None:
    category = category.strip()
    if not category:
        return None
    transactions = load_transactions(user_id)
    overrides = load_category_overrides(user_id)
    updated: dict[str, Any] | None = None
    for tx in transactions:
        if tx.get("id") == transaction_id:
            tx["category"] = category
            overrides[transaction_signature(tx)] = category
            updated = tx
            break
    if updated is None:
        return None
    save_transactions(user_id, transactions)
    save_category_overrides(user_id, overrides)
    return updated


def rename_category(user_id: str, old_category: str, new_category: str) -> int:
    old_category = old_category.strip()
    new_category = new_category.strip()
    if not old_category or not new_category:
        return 0
    transactions = load_transactions(user_id)
    overrides = load_category_overrides(user_id)
    count = 0
    for tx in transactions:
        if tx.get("category") == old_category:
            tx["category"] = new_category
            overrides[transaction_signature(tx)] = new_category
            count += 1
    overrides = {signature: (new_category if category == old_category else category) for signature, category in overrides.items()}
    save_transactions(user_id, transactions)
    save_category_overrides(user_id, overrides)
    return count


def merge_transaction_payloads(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {transaction_dedupe_key(tx) for tx in existing}
    merged = list(existing)
    for tx in new:
        payload = dict(tx)
        key = transaction_dedupe_key(payload)
        if key not in seen:
            merged.append(payload)
            seen.add(key)
    return sorted(merged, key=lambda item: item.get("date", ""))


def merge_transactions(existing: list[dict[str, Any]], new: list[Transaction]) -> list[dict[str, Any]]:
    return merge_transaction_payloads(existing, [asdict(tx) for tx in new])


def plaid_base_url() -> str:
    return PLAID_ENV_URLS.get(os.getenv("PLAID_ENV", "sandbox").lower(), PLAID_ENV_URLS["sandbox"])


def plaid_configured() -> bool:
    return bool(os.getenv("PLAID_CLIENT_ID") and os.getenv("PLAID_SECRET"))


def plaid_request(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not plaid_configured():
        raise RuntimeError("PLAID_NOT_CONFIGURED")
    body = {
        "client_id": os.getenv("PLAID_CLIENT_ID"),
        "secret": os.getenv("PLAID_SECRET"),
        **payload,
    }
    req = request.Request(
        f"{plaid_base_url()}{endpoint}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30, context=ssl.create_default_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(detail)
            message = payload.get("error_message") or payload.get("error_code") or detail
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise RuntimeError(message) from exc


def create_plaid_link_token(user_id: str) -> dict[str, Any]:
    products = [part.strip() for part in os.getenv("PLAID_PRODUCTS", "transactions").split(",") if part.strip()]
    country_codes = [part.strip().upper() for part in os.getenv("PLAID_COUNTRY_CODES", "US").split(",") if part.strip()]
    payload: dict[str, Any] = {
        "client_name": os.getenv("PLAID_CLIENT_NAME", "Financial Review"),
        "language": "en",
        "country_codes": country_codes,
        "user": {"client_user_id": user_id},
        "products": products,
    }
    redirect_uri = os.getenv("PLAID_REDIRECT_URI")
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri
    return plaid_request("/link/token/create", payload)


def list_plaid_items(user_id: str) -> list[dict[str, Any]]:
    init_db()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT item_id, institution_name, cursor, accounts_json, created_at, updated_at, last_synced_at
            FROM plaid_items
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["accounts"] = json.loads(item.pop("accounts_json") or "[]")
        except json.JSONDecodeError:
            item["accounts"] = []
        items.append(item)
    return items


def save_plaid_item(
    user_id: str,
    item_id: str,
    access_token: str,
    institution_name: str = "",
    cursor: str | None = None,
    accounts: list[dict[str, Any]] | None = None,
    last_synced_at: str | None = None,
) -> None:
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT created_at, cursor, accounts_json, last_synced_at FROM plaid_items WHERE user_id = ? AND item_id = ?",
            (user_id, item_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO plaid_items
                (user_id, item_id, access_token, institution_name, cursor, accounts_json, created_at, updated_at, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, item_id) DO UPDATE SET
                access_token = excluded.access_token,
                institution_name = excluded.institution_name,
                cursor = excluded.cursor,
                accounts_json = excluded.accounts_json,
                updated_at = excluded.updated_at,
                last_synced_at = excluded.last_synced_at
            """,
            (
                user_id,
                item_id,
                access_token,
                institution_name,
                cursor if cursor is not None else (existing["cursor"] if existing else None),
                json.dumps(accounts if accounts is not None else (json.loads(existing["accounts_json"]) if existing else [])),
                existing["created_at"] if existing else now,
                now,
                last_synced_at if last_synced_at is not None else (existing["last_synced_at"] if existing else None),
            ),
        )


def exchange_plaid_public_token(user_id: str, public_token: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = metadata or {}
    exchange = plaid_request("/item/public_token/exchange", {"public_token": public_token})
    institution = metadata.get("institution") or {}
    save_plaid_item(
        user_id,
        exchange["item_id"],
        exchange["access_token"],
        str(institution.get("name") or ""),
    )
    return {"item_id": exchange["item_id"], "institution_name": institution.get("name") or "", "request_id": exchange.get("request_id")}


def plaid_account_type(account: dict[str, Any] | None) -> str:
    if not account:
        return "unknown"
    account_type = str(account.get("type") or "").lower()
    subtype = str(account.get("subtype") or "").lower()
    if account_type == "credit":
        return "credit_card"
    if subtype in {"checking", "savings"}:
        return subtype
    if account_type in {"depository", "investment", "loan"}:
        return account_type
    return "unknown"


def plaid_category(description: str, amount: float, transaction: dict[str, Any]) -> str:
    pfc = transaction.get("personal_finance_category") or {}
    primary = str(pfc.get("primary") or "").replace("_", " ").title()
    detailed = str(pfc.get("detailed") or "").lower()
    if "transfer" in detailed or primary == "Transfer":
        return "Transfers"
    if primary in {"Food And Drink"}:
        return "Dining"
    if primary in {"Transportation", "Travel", "Income"}:
        return primary
    if primary in {"Shops", "General Merchandise"}:
        return "Shopping"
    return categorize(description, amount)


def plaid_transaction_payload(transaction: dict[str, Any], account: dict[str, Any] | None, institution_name: str) -> dict[str, Any] | None:
    if transaction.get("pending"):
        return None
    plaid_id = transaction.get("transaction_id")
    if not plaid_id:
        return None
    raw_amount = Decimal(str(transaction.get("amount", "0")))
    amount = -raw_amount
    description = str(transaction.get("merchant_name") or transaction.get("name") or "Plaid transaction").strip()
    account_name = str((account or {}).get("name") or (account or {}).get("official_name") or institution_name or "Plaid Account")
    if institution_name and not account_name.lower().startswith(institution_name.lower()):
        account_name = f"{institution_name} {account_name}".strip()
    amount_float = float(amount)
    return {
        "id": f"plaid-{plaid_id}",
        "statement": PLAID_SOURCE,
        "account_type": plaid_account_type(account),
        "account_name": account_name,
        "date": transaction.get("date") or transaction.get("authorized_date") or "",
        "description": description[:180],
        "amount": amount_float,
        "category": plaid_category(description, amount_float, transaction),
        "source_line": f"plaid transaction_id={plaid_id}",
    }


def sync_plaid_item(user_id: str, item: dict[str, Any]) -> dict[str, Any]:
    added: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    accounts: list[dict[str, Any]] = []
    cursor = item.get("cursor")
    has_more = True

    while has_more:
        payload: dict[str, Any] = {"access_token": item["access_token"], "count": 500}
        if cursor:
            payload["cursor"] = cursor
        response = plaid_request("/transactions/sync", payload)
        added.extend(response.get("added", []))
        modified.extend(response.get("modified", []))
        removed.extend(response.get("removed", []))
        if response.get("accounts"):
            accounts = response["accounts"]
        cursor = response.get("next_cursor", cursor)
        has_more = bool(response.get("has_more"))

    account_map = {account.get("account_id"): account for account in accounts}
    institution_name = item.get("institution_name") or ""
    payloads = [
        payload
        for tx in added + modified
        if (payload := plaid_transaction_payload(tx, account_map.get(tx.get("account_id")), institution_name)) is not None
    ]
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as conn:
        for tx in removed:
            tx_id = tx.get("transaction_id")
            if tx_id:
                conn.execute("DELETE FROM transactions WHERE user_id = ? AND id = ?", (user_id, f"plaid-{tx_id}"))
        insert_transactions(conn, user_id, payloads)

    transactions = load_transactions(user_id)
    merchant_rule_count = apply_merchant_rules(user_id, transactions)
    apply_category_overrides(user_id, transactions)
    save_transactions(user_id, transactions)
    save_plaid_item(
        user_id,
        item["item_id"],
        item["access_token"],
        institution_name,
        cursor,
        accounts or item.get("accounts") or [],
        now,
    )
    return {
        "item_id": item["item_id"],
        "added": len(added),
        "modified": len(modified),
        "removed": len(removed),
        "stored": len(payloads),
        "merchant_rule_count": merchant_rule_count,
    }


def sync_plaid_items(user_id: str) -> dict[str, Any]:
    init_db()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT item_id, access_token, institution_name, cursor, accounts_json
            FROM plaid_items
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        try:
            item["accounts"] = json.loads(item.pop("accounts_json") or "[]")
        except json.JSONDecodeError:
            item["accounts"] = []
        results.append(sync_plaid_item(user_id, item))
    return {
        "ok": True,
        "item_count": len(results),
        "results": results,
        "transaction_count": count_user_transactions(user_id),
        "items": list_plaid_items(user_id),
    }


def scan_user_statements(user_id: str) -> dict[str, Any]:
    folder = statements_dir(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    raw_text_dir(user_id).mkdir(parents=True, exist_ok=True)
    init_db()
    migrate_user_json_to_db(user_id)

    parsed_by_statement: dict[str, int] = {}
    errors: list[dict[str, str]] = []
    cache_hit_count = 0
    parsed_statement_count = 0
    pdf_paths = sorted(folder.glob("*.pdf"))
    current_filenames = {pdf_path.name for pdf_path in pdf_paths}

    prune_missing_statement_records(user_id, current_filenames)

    for pdf_path in pdf_paths:
        try:
            fingerprint = statement_fingerprint(pdf_path)
            statement_record = load_statement_record(user_id, pdf_path.name)
            if statement_record_is_current(statement_record, fingerprint):
                parsed_by_statement[pdf_path.name] = int(statement_record.get("transaction_count", 0))
                cache_hit_count += 1
                continue
            else:
                text = extract_pdf_text(pdf_path)
                (raw_text_dir(user_id) / f"{pdf_path.stem}.txt").write_text(text, encoding="utf-8")
                parsed = parse_transactions(text, pdf_path.name)
                statement_transactions = [asdict(tx) for tx in parsed]
                replace_statement_transactions(user_id, pdf_path.name, fingerprint, statement_transactions)
                parsed_by_statement[pdf_path.name] = len(statement_transactions)
                parsed_statement_count += 1
        except Exception as exc:
            error = str(exc)
            try:
                mark_statement_error(user_id, pdf_path.name, statement_fingerprint(pdf_path), error)
            except Exception:
                pass
            errors.append({"statement": pdf_path.name, "error": error})

    transactions = load_transactions(user_id)
    ai_category_count = apply_ai_categories(transactions) if os.getenv("OPENAI_API_KEY") and os.getenv("FIN_REVIEW_AI_CATEGORIZE") == "1" else 0
    merchant_rule_count = apply_merchant_rules(user_id, transactions)
    apply_category_overrides(user_id, transactions)
    save_transactions(user_id, transactions)
    return {
        "statement_count": len(pdf_paths),
        "transaction_count": len(transactions),
        "parsed_by_statement": parsed_by_statement,
        "parsed_statement_count": parsed_statement_count,
        "cached_statement_count": cache_hit_count,
        "ai_category_count": ai_category_count,
        "merchant_rule_count": merchant_rule_count,
        "errors": errors,
        "folder": str(folder),
    }


def filtered_transactions(transactions: list[dict[str, Any]], params: dict[str, list[str]]) -> list[dict[str, Any]]:
    category = params.get("category", [""])[0]
    account_type = params.get("account_type", [""])[0]
    start = params.get("start", [""])[0]
    end = params.get("end", [""])[0]
    query = params.get("q", [""])[0].lower().strip()
    result = []
    for tx in transactions:
        if category and tx.get("category") != category:
            continue
        if account_type and tx.get("account_type", "unknown") != account_type:
            continue
        if start and tx.get("date", "") < start:
            continue
        if end and tx.get("date", "") > end:
            continue
        if query and query not in tx.get("description", "").lower():
            continue
        result.append(tx)
    return result


def merchant_name(description: str) -> str:
    value = re.sub(r"\b(?:web id|ppd id|transaction#|inv_|ticket number|agreement number)\b[:\w\- ]*", "", description, flags=re.I)
    value = re.sub(r"\b\d{3}[- ]?\d{3}[- ]?\d{4}\b", "", value)
    value = re.sub(r"\b\d{6,}\b", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -*_,")
    return value[:48] or "Unknown merchant"


def savings_total(transactions: list[dict[str, Any]]) -> float:
    return round(sum(tx["amount"] for tx in transactions if tx.get("category") not in INTERNAL_FLOW_CATEGORIES), 2)


def bank_names_from_transactions(transactions: list[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    known = [
        ("american express", "American Express"),
        ("amex", "American Express"),
        ("chase", "Chase"),
        ("jpmorgan", "Chase"),
        ("discover", "Discover"),
        ("capital one", "Capital One"),
        ("citi", "Citi"),
        ("bank of america", "Bank of America"),
        ("wells fargo", "Wells Fargo"),
    ]
    for tx in transactions:
        account = str(tx.get("account_name") or tx.get("statement") or "").lower()
        for needle, label in known:
            if needle in account:
                names.add(label)
                break
    return sorted(names)


def summarize(transactions: list[dict[str, Any]], user_name: str) -> dict[str, Any]:
    internal_out = -sum(tx["amount"] for tx in transactions if tx["amount"] < 0 and tx.get("category") in INTERNAL_FLOW_CATEGORIES)
    internal_in = sum(tx["amount"] for tx in transactions if tx["amount"] > 0 and tx.get("category") in INTERNAL_FLOW_CATEGORIES)
    spending_transactions = [tx for tx in transactions if tx.get("category") not in INTERNAL_FLOW_CATEGORIES]
    income = sum(tx["amount"] for tx in spending_transactions if tx["amount"] > 0)
    expenses = -sum(tx["amount"] for tx in spending_transactions if tx["amount"] < 0)
    net = income - expenses
    by_category: dict[str, float] = {}
    by_month: dict[str, dict[str, float]] = {}
    by_account: dict[str, dict[str, float]] = {}
    by_merchant: dict[str, float] = {}
    for tx in transactions:
        category = tx.get("category", "Uncategorized")
        by_category[category] = by_category.get(category, 0.0) + tx["amount"]
        month = tx.get("date", "")[:7] or "Unknown"
        row = by_month.setdefault(month, {"income": 0.0, "expenses": 0.0, "net": 0.0})
        account = tx.get("account_name", tx.get("account_type", "Unknown"))
        account_row = by_account.setdefault(account, {"income": 0.0, "expenses": 0.0, "net": 0.0, "transactions": 0.0})
        account_row["transactions"] += 1
        if tx["amount"] >= 0 and tx.get("category") not in INTERNAL_FLOW_CATEGORIES:
            row["income"] += tx["amount"]
            account_row["income"] += tx["amount"]
        elif tx.get("category") not in INTERNAL_FLOW_CATEGORIES:
            row["expenses"] += -tx["amount"]
            account_row["expenses"] += -tx["amount"]
            merchant = merchant_name(tx.get("description", ""))
            by_merchant[merchant] = by_merchant.get(merchant, 0.0) + -tx["amount"]
        row["net"] = row["income"] - row["expenses"]
        account_row["net"] = account_row["income"] - account_row["expenses"]

    largest_expenses = sorted([tx for tx in spending_transactions if tx["amount"] < 0], key=lambda tx: tx["amount"])[:8]
    deterministic = build_deterministic_narrative(user_name, income, expenses, net, by_category, by_month, internal_out)
    ai_summary = call_openai_summary(transactions, deterministic) if os.getenv("OPENAI_API_KEY") else None

    return {
        "income": round(income, 2),
        "expenses": round(expenses, 2),
        "net": round(net, 2),
        "savings": savings_total(transactions),
        "internal_out": round(internal_out, 2),
        "internal_in": round(internal_in, 2),
        "transaction_count": len(transactions),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items())},
        "by_month": {k: {inner: round(value, 2) for inner, value in v.items()} for k, v in sorted(by_month.items())},
        "by_account": {k: {inner: round(value, 2) for inner, value in v.items()} for k, v in sorted(by_account.items())},
        "top_merchants": {k: round(v, 2) for k, v in sorted(by_merchant.items(), key=lambda item: item[1], reverse=True)[:12]},
        "largest_expenses": largest_expenses,
        "narrative": ai_summary or deterministic,
        "ai_enabled": bool(ai_summary),
    }


def build_deterministic_narrative(
    user_name: str,
    income: float,
    expenses: float,
    net: float,
    by_category: dict[str, float],
    by_month: dict[str, dict[str, float]],
    internal_out: float = 0.0,
) -> str:
    expense_categories = sorted(
        [(cat, -amount) for cat, amount in by_category.items() if amount < 0 and cat not in INTERNAL_FLOW_CATEGORIES],
        key=lambda item: item[1],
        reverse=True,
    )
    top = ", ".join(f"{cat} ${amount:,.2f}" for cat, amount in expense_categories[:3]) or "no expenses detected"
    months = sorted(by_month)
    period = f"{months[0]} through {months[-1]}" if months else "the uploaded period"
    savings_rate = (net / income * 100) if income else 0
    return (
        f"{user_name}'s uploaded statements cover {period}. Total detected income is ${income:,.2f}, "
        f"detected expenses are ${expenses:,.2f}, and net cash flow is ${net:,.2f}. "
        f"Internal transfers and credit-card payments total ${internal_out:,.2f} and are excluded from expense totals to avoid double counting. "
        f"The implied savings rate is {savings_rate:.1f}%. Largest spending areas: {top}. "
        "Review Uncategorized transactions to improve accuracy."
    )


def call_openai_summary(transactions: list[dict[str, Any]], fallback: str) -> str | None:
    sample = transactions[-120:]
    prompt = {
        "role": "user",
        "content": (
            "Summarize this person's financial history from bank statement transactions. "
            "Be concrete, mention spending patterns, income consistency, risks, and category cleanup needs. "
            "Do not invent facts. Transactions JSON:\n"
            + json.dumps(sample, ensure_ascii=True)
        ),
    }
    payload = {
        "model": os.getenv("FIN_REVIEW_AI_MODEL", "gpt-4.1-mini"),
        "input": [prompt],
        "temperature": 0.2,
        "max_output_tokens": 550,
    }
    try:
        req = request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        context = ssl.create_default_context()
        with request.urlopen(req, timeout=30, context=context) as response:
            data = json.loads(response.read().decode("utf-8"))
        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    chunks.append(content.get("text", ""))
        return "\n".join(chunks).strip() or fallback
    except Exception:
        return None


def apply_ai_categories(transactions: list[dict[str, Any]]) -> int:
    candidates = [
        {
            "id": tx["id"],
            "description": tx.get("description", ""),
            "amount": tx.get("amount", 0),
            "current_category": tx.get("category", "Uncategorized"),
        }
        for tx in transactions
        if tx.get("category") == "Uncategorized" or tx.get("category") == "Shopping"
    ][:180]
    if not candidates:
        return 0

    allowed = sorted(CATEGORY_KEYWORDS.keys() | {"Uncategorized"})
    payload = {
        "model": os.getenv("FIN_REVIEW_AI_MODEL", "gpt-4.1-mini"),
        "input": [
            {
                "role": "user",
                "content": (
                    "Categorize financial transactions. Return strict JSON only: "
                    '{"categories":[{"id":"transaction id","category":"one allowed category"}]}. '
                    "Use only these categories: "
                    + ", ".join(allowed)
                    + ". Do not change amounts or descriptions. If unsure, use Uncategorized. "
                    "Transactions:\n"
                    + json.dumps(candidates, ensure_ascii=True)
                ),
            }
        ],
        "temperature": 0,
        "max_output_tokens": 1200,
    }
    try:
        req = request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=45, context=ssl.create_default_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = ""
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text += content.get("text", "")
        result = json.loads(text)
    except Exception:
        return 0

    by_id = {tx["id"]: tx for tx in transactions}
    updated = 0
    for item in result.get("categories", []):
        tx_id = item.get("id")
        category = item.get("category")
        if tx_id in by_id and category in allowed and by_id[tx_id].get("category") != category:
            by_id[tx_id]["category"] = category
            updated += 1
    return updated


def app_shell() -> str:
    template_path = TEMPLATES_DIR / "index.html"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Financial Review</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <main class="app">
    <section id="login-screen" class="login-screen">
      <div class="login-copy">
        <div class="brand-pill">Financial Review</div>
        <h1>Understand every statement in one private workspace.</h1>
        <p>Enter a username to create or reopen a local profile. Each profile maps to its own statement folder and financial history.</p>
      </div>
      <form id="login-form" class="login-panel">
        <label for="username">Username</label>
        <div class="login-row">
          <input id="username" name="name" placeholder="for example, maria" autocomplete="username" required>
          <button type="submit">Enter</button>
        </div>
        <small id="login-status">No password. This is a local folder-based login.</small>
      </form>
    </section>

    <section id="workspace" class="workspace hidden">
      <header class="topbar">
        <div>
          <div class="eyebrow">Current user</div>
          <h1 id="title">Financial workspace</h1>
          <p id="subtitle">Statements are read from the mapped local folder for this user.</p>
        </div>
        <div class="actions">
          <button id="open-manual-entry" type="button">Manual Entry</button>
          <button id="open-graph-settings" class="ghost" type="button">Graphs</button>
          <button id="refresh-statements" class="secondary" type="button">Refresh PDFs</button>
        </div>
      </header>

      <section class="folder-strip">
        <div>
          <span>Mapped statement folder</span>
          <strong id="folder-path">data/users/.../statements</strong>
        </div>
      </section>

      <section id="filters" class="filters">
        <input id="query" placeholder="Search description">
        <select id="category"></select>
        <select id="account_type"></select>
        <input id="start" type="date">
        <input id="end" type="date">
        <button id="clear-filters" class="secondary" type="button">Clear</button>
      </section>

      <section class="category-tools">
        <div>
          <h2>Category Controls</h2>
          <small id="category-status">Change one transaction, rename a category, or create merchant rules for future matches.</small>
        </div>
        <form id="rename-category-form">
          <select id="rename-from" name="old_category"></select>
          <input id="rename-to" name="new_category" placeholder="New category name">
          <button type="submit" class="secondary">Rename</button>
        </form>
        <form id="merchant-rule-form">
          <input id="merchant-pattern" name="pattern" placeholder="Merchant text, e.g. DUNKIN">
          <input id="merchant-category" name="category" placeholder="Category">
          <button type="submit" class="secondary">Apply Merchant Rule</button>
        </form>
      </section>

      <section id="dashboard">
        <div class="metrics">
          <div><span>Income</span><strong id="income">$0</strong></div>
          <div><span>Expenses</span><strong id="expenses">$0</strong></div>
          <div><span>Net</span><strong id="net">$0</strong></div>
          <div><span>Transactions</span><strong id="count">0</strong></div>
        </div>
        <section class="summary">
          <div>
            <h2>AI Financial Summary</h2>
            <small id="ai-state"></small>
          </div>
          <p id="summary-text"></p>
        </section>
        <section id="visual-grid" class="visual-grid graph-count-0">
          <div id="monthly-panel" data-chart-panel="monthly">
            <div class="chart-heading">
              <h2>Monthly Cash Flow</h2>
            </div>
            <div id="monthly-chart" class="chart"></div>
          </div>
          <div id="category-panel" data-chart-panel="category">
            <div class="chart-heading">
              <h2>Category Mix</h2>
            </div>
            <div id="category-chart" class="chart"></div>
          </div>
          <div id="account-panel" data-chart-panel="account">
            <div class="chart-heading">
              <h2>Account Detail</h2>
            </div>
            <div id="account-chart" class="chart"></div>
          </div>
          <div id="merchant-panel" data-chart-panel="merchant">
            <div class="chart-heading">
              <h2>Top Merchants</h2>
            </div>
            <div id="merchant-chart" class="chart"></div>
          </div>
        </section>
        <section class="transactions-panel">
          <h2>Transactions</h2>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Date</th><th>Account</th><th>Description</th><th>Category</th><th>Amount</th><th>Statement</th><th>Rule</th></tr></thead>
              <tbody id="transactions"></tbody>
            </table>
          </div>
        </section>
      </section>
    </section>
  </main>
  <section id="graph-modal" class="modal hidden" aria-hidden="true">
    <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="graph-modal-title">
      <div class="modal-header">
        <div>
          <h2 id="graph-modal-title">Graph Visibility</h2>
          <small>Choose which graph positions appear and how each one is drawn.</small>
        </div>
        <button id="close-graph-settings" class="ghost icon-button" type="button" aria-label="Close graph settings">X</button>
      </div>
      <div class="mock-layout" aria-hidden="true">
        <div data-mock-chart="monthly">Variable 1</div>
        <div data-mock-chart="category">Variable 2</div>
        <div data-mock-chart="account">Variable 3</div>
        <div data-mock-chart="merchant">Variable 4</div>
      </div>
      <form id="graph-settings-form" class="graph-settings-form">
        <label class="graph-option">
          <input id="monthly-visible" type="checkbox" data-chart-visible="monthly">
          <span>Variable 1</span>
          <strong>Monthly Cash Flow</strong>
          <select id="monthly-chart-type" class="chart-type">
            <option value="bar">Bar</option>
            <option value="line">Line</option>
          </select>
        </label>
        <label class="graph-option">
          <input id="category-visible" type="checkbox" data-chart-visible="category">
          <span>Variable 2</span>
          <strong>Category Mix</strong>
          <select id="category-chart-type" class="chart-type">
            <option value="pie">Pie</option>
            <option value="bar">Bar</option>
          </select>
        </label>
        <label class="graph-option">
          <input id="account-visible" type="checkbox" data-chart-visible="account">
          <span>Variable 3</span>
          <strong>Account Detail</strong>
          <select id="account-chart-type" class="chart-type">
            <option value="bar">Bar</option>
            <option value="pie">Pie</option>
          </select>
        </label>
        <label class="graph-option">
          <input id="merchant-visible" type="checkbox" data-chart-visible="merchant">
          <span>Variable 4</span>
          <strong>Top Merchants</strong>
          <select id="merchant-chart-type" class="chart-type">
            <option value="bar">Bar</option>
            <option value="pie">Pie</option>
          </select>
        </label>
        <button type="submit">Select</button>
      </form>
    </div>
  </section>
  <section id="manual-modal" class="modal hidden" aria-hidden="true">
    <div class="modal-panel manual-panel" role="dialog" aria-modal="true" aria-labelledby="manual-modal-title">
      <div class="modal-header">
        <div>
          <h2 id="manual-modal-title">Manual Transaction</h2>
          <small>Add cash or off-statement activity directly to the local database.</small>
        </div>
        <button id="close-manual-entry" class="ghost icon-button" type="button" aria-label="Close manual entry">X</button>
      </div>
      <form id="manual-entry-form" class="manual-entry-form">
        <label>
          <span>Date</span>
          <input id="manual-date" name="date" type="date" required>
        </label>
        <label>
          <span>Account</span>
          <input id="manual-account" name="account" placeholder="Cash wallet" required>
        </label>
        <label>
          <span>Description</span>
          <input id="manual-description" name="description" placeholder="Farmers market" required>
        </label>
        <label>
          <span>Category</span>
          <input id="manual-category" name="category" list="manual-category-options" placeholder="Groceries" required>
          <datalist id="manual-category-options"></datalist>
        </label>
        <label>
          <span>Amount</span>
          <input id="manual-amount" name="amount" inputmode="decimal" placeholder="-25.00" required>
        </label>
        <small id="manual-entry-status">Use negative amounts for spending and positive amounts for income.</small>
        <button type="submit">Save Transaction</button>
      </form>
    </div>
  </section>
  <script src="/static/app.js"></script>
</body>
</html>"""


CSS = """
:root {
  color-scheme: light;
  --bg: #edf6ff;
  --panel: rgba(255, 255, 255, 0.78);
  --panel-solid: #ffffff;
  --ink: #112033;
  --muted: #66758d;
  --line: rgba(112, 132, 170, 0.24);
  --accent: #00a7a7;
  --accent-dark: #047b86;
  --accent-2: #f2577a;
  --gold: #f3a712;
  --violet: #7657ff;
  --sky: #32b7ff;
  --mint: #2fd49c;
  --shadow: 0 24px 70px rgba(52, 73, 117, 0.16);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background:
    linear-gradient(115deg, rgba(0, 167, 167, 0.18), transparent 36%),
    linear-gradient(245deg, rgba(242, 87, 122, 0.16), transparent 38%),
    linear-gradient(0deg, rgba(118, 87, 255, 0.12), transparent 44%),
    repeating-linear-gradient(90deg, rgba(17, 32, 51, 0.035) 0 1px, transparent 1px 72px),
    repeating-linear-gradient(0deg, rgba(17, 32, 51, 0.03) 0 1px, transparent 1px 72px),
    linear-gradient(135deg, #fbfdff 0%, var(--bg) 54%, #f4efff 100%);
  color: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.app {
  width: min(1240px, calc(100% - 40px));
  margin: 0 auto;
  padding: 28px 0 44px;
}
h1, h2, p { margin-top: 0; }
h1 { margin-bottom: 10px; font-size: clamp(32px, 5vw, 58px); line-height: 1.02; letter-spacing: 0; }
h2 { font-size: 16px; margin-bottom: 14px; }
p { color: var(--muted); line-height: 1.55; }
small, .metrics span, .folder-strip span, .eyebrow { color: var(--muted); }
form { display: flex; gap: 10px; }
input, select, button {
  min-height: 44px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0 13px;
  font: inherit;
}
input, select {
  background: rgba(255, 255, 255, 0.9);
  color: var(--ink);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.85);
}
button {
  border-color: transparent;
  background: linear-gradient(135deg, var(--accent), var(--violet));
  color: white;
  font-weight: 760;
  cursor: pointer;
  box-shadow: 0 10px 24px rgba(0, 167, 167, 0.22);
  transition: transform 140ms ease, filter 140ms ease, border-color 140ms ease;
}
button:hover { transform: translateY(-1px); filter: brightness(1.05) saturate(1.08); }
.secondary { background: linear-gradient(135deg, #e8fff8, #eef1ff); color: #075f6b; border-color: rgba(0, 167, 167, 0.22); box-shadow: none; }
.secondary:hover { background: linear-gradient(135deg, #d9fff2, #e4e8ff); }
.ghost { background: transparent; color: var(--ink); border-color: var(--line); }
.ghost:hover { background: rgba(255, 255, 255, 0.78); }
.hidden { display: none !important; }
.login-screen {
  min-height: calc(100vh - 72px);
  display: grid;
  grid-template-columns: minmax(0, 1fr) 430px;
  gap: 32px;
  align-items: center;
}
.brand-pill {
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  padding: 0 12px;
  border: 1px solid rgba(0, 167, 167, 0.25);
  border-radius: 999px;
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.86), rgba(232, 255, 248, 0.72));
  color: var(--accent-dark);
  font-weight: 800;
  margin-bottom: 22px;
}
.login-copy p { max-width: 640px; font-size: 18px; }
.login-panel {
  display: grid;
  gap: 13px;
  padding: 24px;
  border: 1px solid rgba(255, 255, 255, 0.86);
  border-radius: 18px;
  background: var(--panel);
  box-shadow: var(--shadow);
  backdrop-filter: blur(18px);
}
.login-panel label { font-size: 13px; color: var(--muted); font-weight: 800; text-transform: uppercase; letter-spacing: 0.08em; }
.login-row { display: grid; grid-template-columns: 1fr auto; gap: 10px; }
.login-row input { min-height: 54px; font-size: 18px; }
.login-row button { min-width: 104px; min-height: 54px; }
.workspace { display: grid; gap: 16px; }
.topbar, .folder-strip, .filters, .category-tools, .metrics div, .summary, .visual-grid > div, .transactions-panel {
  background: var(--panel);
  border: 1px solid rgba(255, 255, 255, 0.82);
  box-shadow: 0 14px 45px rgba(52, 73, 117, 0.1);
  backdrop-filter: blur(14px);
}
.topbar {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  align-items: center;
  border-radius: 18px;
  padding: 22px;
}
.topbar h1 { font-size: clamp(28px, 4vw, 42px); }
.eyebrow { font-size: 12px; text-transform: uppercase; letter-spacing: 0.11em; font-weight: 850; margin-bottom: 7px; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
.folder-strip {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  align-items: center;
  border-radius: 14px;
  padding: 16px;
}
.folder-strip div { min-width: 0; display: grid; gap: 5px; }
.folder-strip, .filters, .category-tools {
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.82), rgba(244, 250, 255, 0.7)),
    linear-gradient(90deg, rgba(0, 167, 167, 0.08), rgba(118, 87, 255, 0.08));
}
.folder-strip strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  font-size: 13px;
}
.upload { align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.filters {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 170px 160px 150px 150px auto;
  gap: 10px;
  border-radius: 14px;
  padding: 12px;
}
.category-tools {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(360px, .95fr);
  gap: 16px;
  align-items: center;
  border-radius: 14px;
  padding: 14px;
}
.category-tools h2 { margin-bottom: 4px; }
.category-tools form { display: grid; grid-template-columns: minmax(130px, 1fr) minmax(160px, 1fr) auto; }
#merchant-rule-form { grid-column: 1 / -1; grid-template-columns: minmax(240px, 1fr) minmax(170px, .5fr) auto; }
.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(140px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.metrics div { border-radius: 14px; padding: 17px; display: grid; gap: 10px; }
.metrics strong { font-size: 28px; letter-spacing: 0; }
.metrics div:nth-child(1) { border-color: rgba(47, 212, 156, 0.32); background: linear-gradient(135deg, rgba(255,255,255,.82), rgba(226,255,246,.76)); }
.metrics div:nth-child(2) { border-color: rgba(242, 87, 122, 0.3); background: linear-gradient(135deg, rgba(255,255,255,.82), rgba(255,238,243,.78)); }
.metrics div:nth-child(3) { border-color: rgba(118, 87, 255, 0.28); background: linear-gradient(135deg, rgba(255,255,255,.82), rgba(241,238,255,.78)); }
.metrics div:nth-child(4) { border-color: rgba(50, 183, 255, 0.32); background: linear-gradient(135deg, rgba(255,255,255,.82), rgba(232,247,255,.78)); }
.summary {
  display: grid;
  grid-template-columns: 250px 1fr;
  gap: 20px;
  border-radius: 14px;
  padding: 18px;
  margin-bottom: 14px;
}
.summary p { margin-bottom: 0; }
.visual-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-bottom: 14px; }
.visual-grid.graph-count-0 { display: none; }
.visual-grid.graph-count-1 { grid-template-columns: 1fr; }
.visual-grid.graph-count-1 > div { min-height: 380px; }
.visual-grid.graph-count-3 > div:first-child { grid-column: 1 / -1; }
.visual-grid > div, .transactions-panel { border-radius: 14px; padding: 18px; }
.chart-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }
.chart-heading h2 { margin-bottom: 0; }
.chart-type {
  width: 108px;
  min-height: 36px;
  padding: 0 9px;
  font-size: 13px;
  font-weight: 760;
}
.chart { min-height: 270px; display: grid; align-content: end; gap: 12px; }
.chart-legend { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; color: var(--muted); font-size: 12px; font-weight: 760; }
.legend-item { display: inline-flex; align-items: center; gap: 7px; }
.dot { width: 10px; height: 10px; border-radius: 999px; display: inline-block; }
.dot.income { background: var(--accent); }
.dot.expense { background: var(--accent-2); }
.dot.category { background: var(--gold); }
.dot.pie-0 { background: #0f766e; }
.dot.pie-1 { background: #be4b49; }
.dot.pie-2 { background: #c58b22; }
.dot.pie-3 { background: #4f46e5; }
.dot.pie-4 { background: #7c3aed; }
.dot.pie-5 { background: #0891b2; }
.dot.pie-6 { background: #db2777; }
.dot.pie-7 { background: #4d7c0f; }
.chart-row { display: grid; gap: 7px; }
.chart-meta { display: grid; grid-template-columns: minmax(82px, 1fr) auto; gap: 10px; align-items: baseline; font-size: 13px; }
.chart-meta strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chart-meta span { color: var(--muted); font-variant-numeric: tabular-nums; }
.track { height: 34px; background: linear-gradient(180deg, #f2f7ff 0%, #e9f0fb 100%); border-radius: 10px; overflow: hidden; display: flex; box-shadow: inset 0 0 0 1px rgba(16, 20, 23, 0.04); }
.bar-income { background: linear-gradient(90deg, var(--mint), var(--sky)); }
.bar-expense { background: linear-gradient(90deg, var(--accent-2), #ff9a70); }
.bar-category { background: linear-gradient(90deg, var(--gold), var(--violet)); height: 100%; border-radius: 10px; }
.pie-chart {
  display: grid;
  grid-template-columns: minmax(190px, 240px) minmax(0, 1fr);
  gap: 18px;
  align-items: center;
}
.pie-ring {
  width: min(100%, 238px);
  aspect-ratio: 1;
  border-radius: 50%;
  background: var(--pie-stops, #d9e0e5);
  box-shadow: inset 0 0 0 1px rgba(16, 20, 23, 0.06);
  position: relative;
}
.pie-ring::after {
  content: "";
  position: absolute;
  inset: 28%;
  border-radius: 50%;
  background: var(--panel-solid);
  box-shadow: 0 0 0 1px rgba(16, 20, 23, 0.04);
}
.pie-list { display: grid; gap: 9px; min-width: 0; }
.pie-item { display: grid; grid-template-columns: 14px minmax(0, 1fr) auto; gap: 9px; align-items: baseline; font-size: 13px; }
.pie-item strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pie-item span { color: var(--muted); font-variant-numeric: tabular-nums; }
.line-chart { min-height: 260px; align-content: stretch; }
.line-svg { width: 100%; min-height: 238px; overflow: visible; }
.line-grid { stroke: #d9e0e5; stroke-width: 1; }
.line-income { fill: none; stroke: var(--accent); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.line-expense { fill: none; stroke: var(--accent-2); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.line-dot { stroke: var(--panel-solid); stroke-width: 2; }
.line-label { fill: var(--muted); font-size: 11px; font-weight: 760; }
.chart-empty { min-height: 180px; display: grid; place-items: center; color: var(--muted); text-align: center; }
.table-wrap { overflow: auto; max-height: 520px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: 11px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: var(--panel-solid); color: #4e5a62; font-size: 12px; text-transform: uppercase; letter-spacing: 0.07em; }
td.amount { text-align: right; font-variant-numeric: tabular-nums; font-weight: 760; }
.category-picker {
  width: min(190px, 100%);
  min-height: 34px;
  padding: 0 8px;
  font-size: 13px;
}
.rule-button {
  min-height: 34px;
  padding: 0 10px;
  font-size: 12px;
}
.negative { color: var(--accent-2); }
.positive { color: var(--accent); }
.icon-button { width: 42px; min-height: 42px; padding: 0; }
.modal {
  position: fixed;
  inset: 0;
  z-index: 20;
  display: grid;
  place-items: center;
  padding: 22px;
  background: rgba(16, 20, 23, 0.36);
}
.modal-panel {
  width: min(760px, 100%);
  max-height: min(820px, calc(100vh - 44px));
  overflow: auto;
  border-radius: 14px;
  border: 1px solid rgba(255, 255, 255, 0.78);
  background: var(--panel-solid);
  box-shadow: var(--shadow);
  padding: 18px;
}
.manual-panel { width: min(560px, 100%); }
.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 14px;
  margin-bottom: 14px;
}
.modal-header h2 { margin-bottom: 4px; }
.mock-layout {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 16px;
  padding: 12px;
  border: 1px dashed var(--line);
  border-radius: 10px;
  background: #f7faf9;
}
.mock-layout div {
  min-height: 70px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  background: linear-gradient(180deg, #eef4f3, #e6ecef);
  color: var(--muted);
  font-size: 12px;
  font-weight: 850;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border: 1px solid var(--line);
}
.mock-layout div.active {
  border-color: rgba(15, 118, 110, 0.55);
  background: linear-gradient(180deg, #e2f4ef, #d4ebe6);
  color: var(--accent-dark);
}
.graph-settings-form {
  display: grid;
  gap: 10px;
}
.graph-option {
  display: grid;
  grid-template-columns: 22px minmax(86px, .4fr) minmax(150px, 1fr) 120px;
  gap: 10px;
  align-items: center;
  padding: 11px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fbfcfc;
}
.graph-option input { min-height: auto; accent-color: var(--accent); }
.graph-option span { color: var(--muted); font-size: 12px; font-weight: 850; text-transform: uppercase; letter-spacing: 0.05em; }
.graph-option strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.graph-settings-form > button { justify-self: end; min-width: 120px; }
.manual-entry-form {
  display: grid;
  gap: 12px;
}
.manual-entry-form label {
  display: grid;
  gap: 6px;
}
.manual-entry-form label span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 850;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.manual-entry-form button { justify-self: end; min-width: 170px; }
@media (max-width: 920px) {
  .login-screen, .summary, .visual-grid, .folder-strip, .category-tools { grid-template-columns: 1fr; }
  .visual-grid.graph-count-3 > div:first-child { grid-column: auto; }
  .topbar { align-items: flex-start; flex-direction: column; }
  .actions, .upload { justify-content: flex-start; }
  .filters, .metrics { grid-template-columns: 1fr 1fr; }
  .category-tools form, #merchant-rule-form { grid-template-columns: 1fr; }
}
@media (max-width: 580px) {
  .app { width: min(100% - 24px, 1240px); padding-top: 14px; }
  .login-row, .filters, .metrics { grid-template-columns: 1fr; }
  form, .upload { display: grid; }
  .chart-heading { align-items: stretch; flex-direction: column; }
  .chart-type { width: 100%; }
  .mock-layout { grid-template-columns: 1fr; }
  .graph-option { grid-template-columns: 22px 1fr; }
  .graph-option strong, .graph-option select { grid-column: 2; }
  .graph-settings-form > button { justify-self: stretch; }
  .pie-chart { grid-template-columns: 1fr; }
  .pie-ring { justify-self: center; }
  .chart-meta { grid-template-columns: 1fr; gap: 2px; }
}
"""


JS = """
let currentUser = null;
let currentSummary = null;
let currentCategories = [];
const pieColors = ["#0f766e", "#be4b49", "#c58b22", "#4f46e5", "#7c3aed", "#0891b2", "#db2777", "#4d7c0f"];
const graphSettingsKey = "financialReviewGraphSettingsV2";
const chartDefinitions = [
  { key: "monthly", panel: "monthly-panel", visible: "monthly-visible", type: "monthly-chart-type", defaultType: "bar" },
  { key: "category", panel: "category-panel", visible: "category-visible", type: "category-chart-type", defaultType: "pie" },
  { key: "account", panel: "account-panel", visible: "account-visible", type: "account-chart-type", defaultType: "bar" },
  { key: "merchant", panel: "merchant-panel", visible: "merchant-visible", type: "merchant-chart-type", defaultType: "bar" },
];

function defaultGraphSettings() {
  return Object.fromEntries(chartDefinitions.map((chart) => [
    chart.key,
    { visible: false, type: chart.defaultType },
  ]));
}

let graphSettings = defaultGraphSettings();

const $ = (id) => document.getElementById(id);
const money = (value) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value || 0);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || response.statusText);
  }
  return response.json();
}

function setBusy(message) {
  $("subtitle").textContent = message;
}

function filterQuery() {
  const params = new URLSearchParams();
  ["query", "category", "account_type", "start", "end"].forEach((id) => {
    const key = id === "query" ? "q" : id;
    if ($(id).value) params.set(key, $(id).value);
  });
  return params.toString();
}

async function loginWithUsername(form) {
  const name = new FormData(form).get("name");
  if (!name || !String(name).trim()) return;
  $("login-status").textContent = "Opening stored transaction database...";
  const session = await api("/api/session", { method: "POST", body: new FormData(form) });
  currentUser = session.user.id;
  localStorage.setItem("financialReviewUser", currentUser);
  $("login-screen").classList.add("hidden");
  $("workspace").classList.remove("hidden");
  $("folder-path").textContent = session.folder;
  await refreshDashboard();
}

async function refreshDashboard() {
  if (!currentUser) return;
  const payload = await api(`/api/users/${currentUser}/summary?${filterQuery()}`);
  currentSummary = payload;
  $("title").textContent = payload.user.name;
  $("folder-path").textContent = payload.folder;
  $("subtitle").textContent = `${payload.statement_count} PDF statement(s), ${payload.summary.transaction_count} matching transaction(s)`;
  $("income").textContent = money(payload.summary.income);
  $("expenses").textContent = money(payload.summary.expenses);
  $("net").textContent = money(payload.summary.net);
  $("count").textContent = payload.summary.transaction_count;
  $("summary-text").textContent = payload.summary.narrative;
  $("ai-state").textContent = payload.summary.ai_enabled ? "OpenAI summary enabled" : "Local rules summary. Set OPENAI_API_KEY for richer AI narration.";
  updateCategoryOptions(payload.categories);
  updateAccountOptions(payload.account_types);
  renderCharts();
  drawTransactions(payload.transactions);
}

function updateCategoryOptions(categories) {
  currentCategories = categories.slice();
  const selected = $("category").value;
  $("category").innerHTML = `<option value="">All categories</option>` + categories.map((cat) => `<option value="${escapeHtml(cat)}">${escapeHtml(cat)}</option>`).join("");
  $("category").value = selected;
  const renameSelected = $("rename-from").value;
  $("rename-from").innerHTML = categories.map((cat) => `<option value="${escapeHtml(cat)}">${escapeHtml(cat)}</option>`).join("");
  $("rename-from").value = categories.includes(renameSelected) ? renameSelected : (categories[0] || "");
  $("manual-category-options").innerHTML = categories.map((cat) => `<option value="${escapeHtml(cat)}"></option>`).join("");
}

function updateAccountOptions(accountTypes) {
  const selected = $("account_type").value;
  const labels = { checking: "Checking", savings: "Savings", credit_card: "Credit card", unknown: "Unknown" };
  $("account_type").innerHTML = `<option value="">All accounts</option>` + accountTypes.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(labels[type] || type)}</option>`).join("");
  $("account_type").value = selected;
}

function loadGraphSettings() {
  try {
    const raw = localStorage.getItem(graphSettingsKey);
    if (!raw) {
      graphSettings = defaultGraphSettings();
      return;
    }
    const saved = JSON.parse(raw);
    chartDefinitions.forEach((chart) => {
      if (saved[chart.key]) {
        const allowedTypes = Array.from($(chart.type).options).map((option) => option.value);
        graphSettings[chart.key] = {
          visible: Boolean(saved[chart.key].visible),
          type: allowedTypes.includes(saved[chart.key].type) ? saved[chart.key].type : chart.defaultType,
        };
      }
    });
  } catch {
    graphSettings = defaultGraphSettings();
  }
}

function saveGraphSettings() {
  localStorage.setItem(graphSettingsKey, JSON.stringify(graphSettings));
}

function syncGraphSettingsForm() {
  chartDefinitions.forEach((chart) => {
    $(chart.visible).checked = graphSettings[chart.key].visible;
    $(chart.type).value = graphSettings[chart.key].type;
  });
  updateMockLayout();
}

function updateMockLayout() {
  chartDefinitions.forEach((chart) => {
    const mock = document.querySelector(`[data-mock-chart="${chart.key}"]`);
    if (mock) mock.classList.toggle("active", $(chart.visible).checked);
  });
}

function applyGraphVisibility() {
  const selected = chartDefinitions.filter((chart) => graphSettings[chart.key].visible);
  const grid = $("visual-grid");
  grid.classList.remove("graph-count-0", "graph-count-1", "graph-count-2", "graph-count-3", "graph-count-4");
  grid.classList.add(`graph-count-${selected.length}`);
  chartDefinitions.forEach((chart) => {
    $(chart.panel).classList.toggle("hidden", !graphSettings[chart.key].visible);
  });
}

function renderCharts() {
  if (!currentSummary) return;
  drawMonthly(currentSummary.summary.by_month);
  drawCategories(currentSummary.summary.by_category);
  drawAccounts(currentSummary.summary.by_account);
  drawMerchants(currentSummary.summary.top_merchants);
  applyGraphVisibility();
}

function openGraphModal() {
  syncGraphSettingsForm();
  $("graph-modal").classList.remove("hidden");
  $("graph-modal").setAttribute("aria-hidden", "false");
}

function closeGraphModal() {
  $("graph-modal").classList.add("hidden");
  $("graph-modal").setAttribute("aria-hidden", "true");
}

function openManualModal() {
  const today = new Date().toISOString().slice(0, 10);
  if (!$("manual-date").value) $("manual-date").value = today;
  if (!$("manual-account").value) $("manual-account").value = "Cash";
  $("manual-entry-status").textContent = "Use negative amounts for spending and positive amounts for income.";
  $("manual-modal").classList.remove("hidden");
  $("manual-modal").setAttribute("aria-hidden", "false");
  $("manual-description").focus();
}

function closeManualModal() {
  $("manual-modal").classList.add("hidden");
  $("manual-modal").setAttribute("aria-hidden", "true");
}

function chartType(id, fallback) {
  const chart = chartDefinitions.find((item) => item.type === id);
  return chart ? graphSettings[chart.key].type : fallback;
}

function percent(value, total) {
  return total ? `${(value / total * 100).toFixed(1)}%` : "0.0%";
}

function pieStops(entries, total) {
  let start = 0;
  return entries.map(([, amount], index) => {
    const end = start + (amount / total * 100);
    const segment = `${pieColors[index % pieColors.length]} ${start.toFixed(2)}% ${end.toFixed(2)}%`;
    start = end;
    return segment;
  }).join(", ");
}

function drawPie(chart, entries, emptyMessage) {
  const total = entries.reduce((sum, [, amount]) => sum + amount, 0);
  if (!entries.length || total <= 0) {
    chart.innerHTML = `<div class="chart-empty">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  chart.innerHTML = `<div class="pie-chart">
    <div class="pie-ring" style="--pie-stops: conic-gradient(${pieStops(entries, total)})"></div>
    <div class="pie-list">
      ${entries.map(([label, amount], index) => `<div class="pie-item">
        <i class="dot pie-${index % pieColors.length}"></i>
        <strong>${escapeHtml(label)}</strong>
        <span>${percent(amount, total)} ${money(amount)}</span>
      </div>`).join("")}
    </div>
  </div>`;
}

function categoryExpenseEntries(rows, limit = 10) {
  const internalCategories = new Set(["Transfers", "Credit Card Payment"]);
  return Object.entries(rows || {})
    .filter(([cat, amount]) => amount < 0 && !internalCategories.has(cat))
    .map(([cat, amount]) => [cat, Math.abs(amount)])
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit);
}

function drawMonthly(rows) {
  const chart = $("monthly-chart");
  const entries = Object.entries(rows);
  if (!entries.length) {
    chart.innerHTML = `<div class="chart-empty">No transactions yet. Add PDFs to the mapped folder, then log in again.</div>`;
    return;
  }
  if (chartType("monthly-chart-type", "bar") === "line") {
    drawMonthlyLine(chart, entries);
    return;
  }
  const max = Math.max(...entries.map(([, row]) => Math.max(row.income, row.expenses)), 1);
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot income"></i>Income</span><span class="legend-item"><i class="dot expense"></i>Spending</span></div>` + entries.map(([month, row]) => {
    const incomeWidth = Math.max(2, row.income / max * 100);
    const expenseWidth = Math.max(2, row.expenses / max * 100);
    return `<div class="chart-row">
      <div class="chart-meta"><strong>${escapeHtml(month)}</strong><span>Net ${money(row.net)}</span></div>
      <div class="track"><div class="bar-income" style="width:${incomeWidth}%"></div><div class="bar-expense" style="width:${expenseWidth}%"></div></div>
    </div>`;
  }).join("");
}

function drawMonthlyLine(chart, entries) {
  const width = 620;
  const height = 250;
  const padX = 42;
  const padY = 28;
  const max = Math.max(...entries.map(([, row]) => Math.max(row.income, row.expenses)), 1);
  const xFor = (index) => entries.length === 1 ? width / 2 : padX + index * ((width - padX * 2) / (entries.length - 1));
  const yFor = (value) => height - padY - (value / max * (height - padY * 2));
  const pathFor = (key) => entries.map(([, row], index) => `${index ? "L" : "M"} ${xFor(index).toFixed(1)} ${yFor(row[key] || 0).toFixed(1)}`).join(" ");
  const labels = entries.map(([month], index) => {
    if (entries.length > 8 && index % Math.ceil(entries.length / 6) !== 0 && index !== entries.length - 1) return "";
    return `<text class="line-label" x="${xFor(index).toFixed(1)}" y="${height - 4}" text-anchor="middle">${escapeHtml(month.slice(2))}</text>`;
  }).join("");
  const dots = (key, fill) => entries.map(([, row], index) => `<circle class="line-dot" cx="${xFor(index).toFixed(1)}" cy="${yFor(row[key] || 0).toFixed(1)}" r="4" fill="${fill}"></circle>`).join("");
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot income"></i>Income</span><span class="legend-item"><i class="dot expense"></i>Spending</span></div>
    <div class="line-chart">
      <svg class="line-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Monthly income and spending line chart">
        <line class="line-grid" x1="${padX}" y1="${height - padY}" x2="${width - padX}" y2="${height - padY}"></line>
        <line class="line-grid" x1="${padX}" y1="${padY}" x2="${width - padX}" y2="${padY}"></line>
        <path class="line-income" d="${pathFor("income")}"></path>
        <path class="line-expense" d="${pathFor("expenses")}"></path>
        ${dots("income", "#0f766e")}
        ${dots("expenses", "#be4b49")}
        ${labels}
      </svg>
    </div>`;
}

function drawCategories(rows) {
  const chart = $("category-chart");
  const entries = categoryExpenseEntries(rows);
  if (!entries.length) {
    chart.innerHTML = `<div class="chart-empty">No expense categories yet.</div>`;
    return;
  }
  if (chartType("category-chart-type", "pie") === "pie") {
    drawPie(chart, entries, "No expense categories yet.");
    return;
  }
  const max = Math.max(...entries.map(([, amount]) => amount), 1);
  const total = entries.reduce((sum, [, amount]) => sum + amount, 0);
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot category"></i>Expense category</span></div>` + entries.map(([cat, amount]) => {
    const width = Math.max(3, amount / max * 100);
    return `<div class="chart-row">
      <div class="chart-meta"><strong>${escapeHtml(cat)}</strong><span>${percent(amount, total)} ${money(amount)}</span></div>
      <div class="track"><div class="bar-category" style="width:${width}%"></div></div>
    </div>`;
  }).join("");
}

function drawAccounts(rows) {
  const chart = $("account-chart");
  const entries = Object.entries(rows || {})
    .map(([account, row]) => [account, row.expenses || 0, row.net || 0])
    .sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    chart.innerHTML = `<div class="chart-empty">No account activity yet.</div>`;
    return;
  }
  if (chartType("account-chart-type", "bar") === "pie") {
    drawPie(chart, entries.map(([account, expenses]) => [account, expenses]).filter(([, expenses]) => expenses > 0).slice(0, 8), "No account spending yet.");
    return;
  }
  const max = Math.max(...entries.map(([, expenses]) => expenses), 1);
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot expense"></i>Spending</span></div>` + entries.map(([account, expenses, net]) => {
    const width = Math.max(3, expenses / max * 100);
    return `<div class="chart-row">
      <div class="chart-meta"><strong>${escapeHtml(account)}</strong><span>${money(expenses)} spent - ${money(net)} net</span></div>
      <div class="track"><div class="bar-expense" style="width:${width}%"></div></div>
    </div>`;
  }).join("");
}

function drawMerchants(rows) {
  const chart = $("merchant-chart");
  const entries = Object.entries(rows || {});
  if (!entries.length) {
    chart.innerHTML = `<div class="chart-empty">No merchant spending yet.</div>`;
    return;
  }
  if (chartType("merchant-chart-type", "bar") === "pie") {
    drawPie(chart, entries.slice(0, 8), "No merchant spending yet.");
    return;
  }
  const max = Math.max(...entries.map(([, amount]) => amount), 1);
  chart.innerHTML = `<div class="chart-legend"><span class="legend-item"><i class="dot category"></i>Merchant spend</span></div>` + entries.map(([merchant, amount]) => {
    const width = Math.max(3, amount / max * 100);
    return `<div class="chart-row">
      <div class="chart-meta"><strong>${escapeHtml(merchant)}</strong><span>${money(amount)}</span></div>
      <div class="track"><div class="bar-category" style="width:${width}%"></div></div>
    </div>`;
  }).join("");
}

function categoryOptions(selected) {
  const categories = currentCategories.includes(selected) ? currentCategories : currentCategories.concat([selected]);
  return categories.map((cat) => `<option value="${escapeHtml(cat)}" ${cat === selected ? "selected" : ""}>${escapeHtml(cat)}</option>`).join("");
}

function merchantPatternFromDescription(description) {
  return String(description || "")
    .replace(/\\b(web id|ppd id|transaction#|inv_|ticket number|agreement number)[:\\w\\- ]*/ig, "")
    .replace(/\\b\\d{3}[- ]?\\d{3}[- ]?\\d{4}\\b/g, "")
    .replace(/\\b\\d{6,}\\b/g, "")
    .replace(/\\s+/g, " ")
    .trim()
    .slice(0, 64);
}

function drawTransactions(transactions) {
  if (!transactions.length) {
    $("transactions").innerHTML = `<tr><td colspan="7">No transactions found for the current filters.</td></tr>`;
    return;
  }
  $("transactions").innerHTML = transactions.slice().reverse().map((tx) => {
    const cls = tx.amount < 0 ? "negative" : "positive";
    const pattern = merchantPatternFromDescription(tx.description);
    return `<tr>
      <td>${escapeHtml(tx.date)}</td>
      <td>${escapeHtml(tx.account_name || tx.account_type || "Unknown")}</td>
      <td>${escapeHtml(tx.description)}</td>
      <td><select class="category-picker" data-transaction-id="${escapeHtml(tx.id)}">${categoryOptions(tx.category)}</select></td>
      <td class="amount ${cls}">${money(tx.amount)}</td>
      <td>${escapeHtml(tx.statement)}</td>
      <td><button type="button" class="secondary rule-button" data-merchant-pattern="${escapeHtml(pattern)}" data-category="${escapeHtml(tx.category)}">Use Merchant</button></td>
    </tr>`;
  }).join("");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await loginWithUsername(event.currentTarget);
  } catch (error) {
    $("login-status").textContent = error.message;
  }
});

$("transactions").addEventListener("change", async (event) => {
  const picker = event.target.closest(".category-picker");
  if (!picker || !currentUser) return;
  const form = new FormData();
  form.set("transaction_id", picker.dataset.transactionId);
  form.set("category", picker.value);
  $("category-status").textContent = "Saving category override...";
  await api(`/api/users/${currentUser}/transactions/category`, { method: "POST", body: form });
  $("category-status").textContent = "Category override saved.";
  await refreshDashboard();
});

$("rename-category-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentUser) return;
  const form = new FormData(event.currentTarget);
  if (!form.get("old_category") || !form.get("new_category")) return;
  $("category-status").textContent = "Renaming category...";
  const result = await api(`/api/users/${currentUser}/categories/rename`, { method: "POST", body: form });
  $("category-status").textContent = `Renamed ${result.updated_count} transaction(s).`;
  $("rename-to").value = "";
  await refreshDashboard();
});

$("merchant-rule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentUser) return;
  const form = new FormData(event.currentTarget);
  if (!form.get("pattern") || !form.get("category")) return;
  $("category-status").textContent = "Applying merchant rule...";
  const result = await api(`/api/users/${currentUser}/merchant-rules`, { method: "POST", body: form });
  $("category-status").textContent = `Merchant rule applied to ${result.updated_count} transaction(s).`;
  await refreshDashboard();
});

$("manual-entry-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentUser) return;
  const form = new FormData(event.currentTarget);
  $("manual-entry-status").textContent = "Saving manual transaction...";
  try {
    const result = await api(`/api/users/${currentUser}/transactions/manual`, { method: "POST", body: form });
    $("manual-entry-status").textContent = `Saved to ${result.document}`;
    event.currentTarget.reset();
    closeManualModal();
    $("category-status").textContent = "Manual transaction saved.";
    await refreshDashboard();
  } catch (error) {
    $("manual-entry-status").textContent = error.message;
  }
});

$("transactions").addEventListener("click", (event) => {
  const button = event.target.closest(".rule-button");
  if (!button) return;
  $("merchant-pattern").value = button.dataset.merchantPattern || "";
  $("merchant-category").value = button.dataset.category || "";
  $("merchant-pattern").focus();
  $("category-status").textContent = "Merchant rule form filled from that transaction. Edit the text if needed, then apply.";
});

async function refreshStatementFolder() {
  if (!currentUser) return;
  const button = $("refresh-statements");
  button.disabled = true;
  setBusy("Refreshing statement folder and checking for new PDFs...");
  $("category-status").textContent = "Scanning mapped folder for new PDFs...";
  try {
    const result = await api(`/api/users/${currentUser}/scan`, { method: "POST" });
    await refreshDashboard();
    const errorText = result.errors && result.errors.length ? ` ${result.errors.length} statement(s) had parsing errors.` : "";
    $("category-status").textContent = `Refresh complete: ${result.statement_count} PDF statement(s), ${result.transaction_count} transaction(s).${errorText}`;
  } catch (error) {
    $("category-status").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

$("refresh-statements").addEventListener("click", refreshStatementFolder);

$("open-manual-entry").addEventListener("click", openManualModal);
$("close-manual-entry").addEventListener("click", closeManualModal);
$("manual-modal").addEventListener("click", (event) => {
  if (event.target === $("manual-modal")) closeManualModal();
});

$("open-graph-settings").addEventListener("click", openGraphModal);
$("close-graph-settings").addEventListener("click", closeGraphModal);
$("graph-modal").addEventListener("click", (event) => {
  if (event.target === $("graph-modal")) closeGraphModal();
});
$("graph-settings-form").addEventListener("input", updateMockLayout);
$("graph-settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  chartDefinitions.forEach((chart) => {
    graphSettings[chart.key] = {
      visible: $(chart.visible).checked,
      type: $(chart.type).value || chart.defaultType,
    };
  });
  saveGraphSettings();
  syncGraphSettingsForm();
  renderCharts();
  closeGraphModal();
});

["query", "category", "account_type", "start", "end"].forEach((id) => $(id).addEventListener("input", () => refreshDashboard()));
$("clear-filters").addEventListener("click", () => {
  ["query", "category", "account_type", "start", "end"].forEach((id) => $(id).value = "");
  refreshDashboard();
});

const remembered = localStorage.getItem("financialReviewUser");
if (remembered) {
  $("username").value = remembered;
}
loadGraphSettings();
syncGraphSettingsForm();
applyGraphVisibility();
"""


def parse_multipart_body(headers: Any, body: bytes) -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    content_type = headers.get("Content-Type", "")
    message_bytes = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    message = BytesParser(policy=email_policy).parsebytes(message_bytes)
    fields: dict[str, list[str]] = {}
    files: dict[str, list[dict[str, Any]]] = {}
    if not message.is_multipart():
        return fields, files

    for part in message.iter_parts():
        disposition = part.get_content_disposition()
        if disposition != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.setdefault(name, []).append({"filename": filename, "file": BytesIO(payload)})
        else:
            charset = part.get_content_charset() or "utf-8"
            fields.setdefault(name, []).append(payload.decode(charset, errors="replace"))
    return fields, files


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_text(app_shell(), "text/html; charset=utf-8")
        elif parsed.path == "/static/styles.css":
            static_path = BASE_DIR / "static" / "styles.css"
            body = static_path.read_text(encoding="utf-8") if static_path.exists() else CSS
            self.send_text(body, "text/css; charset=utf-8")
        elif parsed.path == "/static/app.js":
            static_path = BASE_DIR / "static" / "app.js"
            body = static_path.read_text(encoding="utf-8") if static_path.exists() else JS
            self.send_text(body, "application/javascript; charset=utf-8")
        elif parsed.path == "/api/session":
            params = parse.parse_qs(parsed.query)
            user_id = params.get("user", [""])[0]
            token = params.get("token", [""])[0]
            try:
                user = authenticate_session_token(user_id, token)
            except FileNotFoundError as error:
                self.send_api_error(HTTPStatus.NOT_FOUND, str(error), "Saved login user was not found")
                return
            except PermissionError as error:
                self.send_api_error(HTTPStatus.UNAUTHORIZED, str(error), "Saved login expired. Please enter the password again.")
                return
            self.send_json({"user": user, "folder": str(statements_dir(user["id"]))})
        elif parsed.path == "/api/users":
            self.send_json(list_users())
        elif re.fullmatch(r"/api/admin/[^/]+/users", parsed.path):
            admin_id = parsed.path.split("/")[3]
            try:
                require_admin(admin_id)
            except PermissionError as error:
                self.send_api_error(HTTPStatus.FORBIDDEN, str(error), "Admin access is required")
                return
            self.send_json({"users": list_users()})
        elif re.fullmatch(r"/api/users/[^/]+/summary", parsed.path):
            user_id = parsed.path.split("/")[3]
            self.handle_summary(user_id, parse.parse_qs(parsed.query))
        elif re.fullmatch(r"/api/users/[^/]+/plaid/status", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            self.send_json({"configured": plaid_configured(), "environment": os.getenv("PLAID_ENV", "sandbox"), "items": list_plaid_items(user_id)})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = parse.urlparse(self.path)
        if parsed.path in {"/api/users", "/api/session"}:
            fields = self.read_form()
            name = fields.get("name", [""])[0]
            password = fields.get("password", [""])[0]
            if not name.strip():
                self.send_api_error(HTTPStatus.BAD_REQUEST, "USER_NAME_REQUIRED", "User name is required")
                return
            try:
                user = authenticate_user(name, password)
            except ValueError as error:
                self.send_api_error(HTTPStatus.BAD_REQUEST, str(error), "Password is required")
                return
            except PermissionError as error:
                self.send_api_error(HTTPStatus.UNAUTHORIZED, str(error), "Username exists, but the password does not match")
                return
            payload = {"user": user, "folder": str(statements_dir(user["id"])), "session_token": issue_session_token(user["id"])}
            self.send_json(payload if parsed.path == "/api/session" else user)
        elif re.fullmatch(r"/api/admin/[^/]+/users", parsed.path):
            admin_id = parsed.path.split("/")[3]
            try:
                require_admin(admin_id)
                fields = self.read_form()
                user = admin_create_user(fields.get("name", [""])[0], fields.get("password", [""])[0])
            except PermissionError as error:
                self.send_api_error(HTTPStatus.FORBIDDEN, str(error), "Admin access is required")
                return
            except FileExistsError as error:
                self.send_api_error(HTTPStatus.CONFLICT, str(error), "That user already exists")
                return
            except ValueError as error:
                self.send_api_error(HTTPStatus.BAD_REQUEST, str(error), "User name and password are required")
                return
            self.send_json({"ok": True, "user": user})
        elif re.fullmatch(r"/api/admin/[^/]+/users/[^/]+/password", parsed.path):
            parts = parsed.path.split("/")
            admin_id, user_id = parts[3], parts[5]
            try:
                require_admin(admin_id)
                user = admin_change_password(user_id, self.read_form().get("password", [""])[0])
            except PermissionError as error:
                self.send_api_error(HTTPStatus.FORBIDDEN, str(error), "Admin access is required")
                return
            except FileNotFoundError as error:
                self.send_api_error(HTTPStatus.NOT_FOUND, str(error), "Unknown user")
                return
            except ValueError as error:
                self.send_api_error(HTTPStatus.BAD_REQUEST, str(error), "Password is required")
                return
            self.send_json({"ok": True, "user": user})
        elif re.fullmatch(r"/api/admin/[^/]+/users/[^/]+/delete", parsed.path):
            parts = parsed.path.split("/")
            admin_id, user_id = parts[3], parts[5]
            try:
                require_admin(admin_id)
                remove_user(user_id)
            except PermissionError as error:
                self.send_api_error(HTTPStatus.FORBIDDEN, str(error), "Admin access is required")
                return
            except FileNotFoundError as error:
                self.send_api_error(HTTPStatus.NOT_FOUND, str(error), "Unknown user")
                return
            except ValueError as error:
                self.send_api_error(HTTPStatus.BAD_REQUEST, str(error), "Cannot remove the admin user")
                return
            self.send_json({"ok": True, "removed_id": slugify(user_id)})
        elif re.fullmatch(r"/api/users/[^/]+/scan", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            self.send_json(scan_user_statements(user_id))
        elif re.fullmatch(r"/api/users/[^/]+/transactions/category", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            fields = self.read_form()
            transaction_id = fields.get("transaction_id", [""])[0]
            category = fields.get("category", [""])[0]
            updated = set_transaction_category(user_id, transaction_id, category)
            if updated is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Transaction not found or category is blank")
                return
            self.send_json({"ok": True, "transaction": updated})
        elif re.fullmatch(r"/api/users/[^/]+/transactions/manual", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            transaction = add_manual_transaction(user_id, self.read_form())
            if transaction is None:
                self.send_error(HTTPStatus.BAD_REQUEST, "Date, account, description, category, and amount are required")
                return
            self.send_json({"ok": True, "transaction": transaction, "document": str(manual_transactions_path(user_id))})
        elif re.fullmatch(r"/api/users/[^/]+/transactions/adjustment", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            transaction = add_savings_adjustment(user_id, self.read_form())
            if transaction is None:
                self.send_error(HTTPStatus.BAD_REQUEST, "Date and current amount are required")
                return
            self.send_json({"ok": True, "transaction": transaction, "document": str(manual_transactions_path(user_id))})
        elif re.fullmatch(r"/api/users/[^/]+/transactions/manual/delete", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            fields = self.read_form()
            transaction_id = fields.get("transaction_id", [""])[0]
            if not remove_manual_transaction(user_id, transaction_id):
                self.send_error(HTTPStatus.NOT_FOUND, "Manual transaction not found")
                return
            self.send_json({"ok": True, "removed_id": transaction_id, "document": str(manual_transactions_path(user_id))})
        elif re.fullmatch(r"/api/users/[^/]+/categories/rename", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            fields = self.read_form()
            updated_count = rename_category(user_id, fields.get("old_category", [""])[0], fields.get("new_category", [""])[0])
            self.send_json({"ok": True, "updated_count": updated_count})
        elif re.fullmatch(r"/api/users/[^/]+/merchant-rules", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            fields = self.read_form()
            result = add_merchant_rule(user_id, fields.get("pattern", [""])[0], fields.get("category", [""])[0])
            if result is None:
                self.send_error(HTTPStatus.BAD_REQUEST, "Merchant pattern and category are required")
                return
            self.send_json({"ok": True, **result})
        elif re.fullmatch(r"/api/users/[^/]+/plaid/link-token", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            try:
                self.send_json(create_plaid_link_token(user_id))
            except RuntimeError as exc:
                self.send_api_error(HTTPStatus.BAD_REQUEST, str(exc), "Plaid is not configured or Plaid returned an error")
        elif re.fullmatch(r"/api/users/[^/]+/plaid/exchange", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            fields = self.read_form()
            public_token = fields.get("public_token", [""])[0]
            metadata_raw = fields.get("metadata", ["{}"])[0]
            try:
                metadata = json.loads(metadata_raw) if metadata_raw else {}
                result = exchange_plaid_public_token(user_id, public_token, metadata)
                sync_result = sync_plaid_items(user_id)
                self.send_json({"ok": True, **result, "sync": sync_result})
            except (RuntimeError, json.JSONDecodeError) as exc:
                self.send_api_error(HTTPStatus.BAD_REQUEST, str(exc), "Plaid token exchange failed")
        elif re.fullmatch(r"/api/users/[^/]+/plaid/sync", parsed.path):
            user_id = parsed.path.split("/")[3]
            if not user_dir(user_id).exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
                return
            try:
                self.send_json(sync_plaid_items(user_id))
            except RuntimeError as exc:
                self.send_api_error(HTTPStatus.BAD_REQUEST, str(exc), "Plaid sync failed")
        elif re.fullmatch(r"/api/users/[^/]+/upload", parsed.path):
            user_id = parsed.path.split("/")[3]
            self.handle_upload(user_id)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def handle_summary(self, user_id: str, params: dict[str, list[str]]) -> None:
        meta = read_json(user_meta_path(user_id), None)
        if meta is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
            return
        all_transactions = load_transactions(user_id)
        txs = filtered_transactions(all_transactions, params)
        categories = sorted({tx.get("category", "Uncategorized") for tx in all_transactions})
        account_types = sorted({tx.get("account_type", "unknown") for tx in all_transactions})
        payload = {
            "user": public_user_meta(meta),
            "statement_count": len(list(statements_dir(user_id).glob("*.pdf"))) if statements_dir(user_id).exists() else 0,
            "folder": str(statements_dir(user_id)),
            "database": str(database_path()),
            "bank_names": bank_names_from_transactions(all_transactions),
            "categories": categories,
            "account_types": account_types,
            "merchant_rules": load_merchant_rules(user_id),
            "plaid": {"configured": plaid_configured(), "environment": os.getenv("PLAID_ENV", "sandbox"), "items": list_plaid_items(user_id)},
            "manual_transactions": [tx for tx in all_transactions if tx.get("statement") == "Manual Entry"],
            "transactions": txs,
            "summary": summarize(txs, meta.get("name", user_id)),
        }
        self.send_json(payload)

    def handle_upload(self, user_id: str) -> None:
        if not user_dir(user_id).exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown user")
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload is too large")
            return

        body = self.rfile.read(length)
        _fields, parsed_files = parse_multipart_body(self.headers, body)
        files = parsed_files.get("statement", [])

        uploaded_count = 0
        for item in files:
            if not item.get("filename"):
                continue
            safe_name = Path(item["filename"]).name
            if not safe_name.lower().endswith(".pdf"):
                continue
            destination = statements_dir(user_id) / f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{safe_name}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as handle:
                shutil.copyfileobj(item["file"], handle)
            uploaded_count += 1

        scan = scan_user_statements(user_id)
        scan["uploaded_count"] = uploaded_count
        self.send_json(scan)

    def read_form(self) -> dict[str, list[str]]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0"))
        if content_type.startswith("multipart/form-data"):
            body = self.rfile.read(length)
            fields, _files = parse_multipart_body(self.headers, body)
            return fields
        body = self.rfile.read(length).decode("utf-8")
        if content_type.startswith("application/x-www-form-urlencoded"):
            return parse.parse_qs(body)
        return {key: [value] for key, value in parse.parse_qsl(body)}

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_api_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self.send_json({"ok": False, "code": code, "message": message}, int(status))

    def send_text(self, body: str, content_type: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    ensure_dirs()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Financial Review is running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
