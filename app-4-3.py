import streamlit as st
import datetime
import base64
import sqlite3
import smtplib
import ssl
import random
import string
import secrets as pysecrets
from email.mime.text import MIMEText
from contextlib import contextmanager

st.set_page_config(
    page_title="Safha — Shaikh Zulqarnain",
    page_icon="📚",
    layout="centered",
    initial_sidebar_state="expanded",
)

# =========================================================================
# CONFIG
# =========================================================================

DB_PATH = "reservations.db"

# The one email address allowed to log in as admin. Selecting the admin
# display name (below) with any other email will be rejected.
ADMIN_EMAIL = "zohebpass1231@gmail.com"
ADMIN_NAME = "Shaikh Zulqarnain"

SUBJECTS = {
    "Hindi":            ["Workbook", "Grammar Notebook", "Digest"],
    "Marathi":          ["Workbook", "Grammar Notebook", "Digest"],
    "History/Civics":   ["Notebook", "Digest"],
    "Geography":        ["Notebook", "Digest"],
    "Maths-1 (Algebra)":  ["Notebook", "Digest"],
    "Maths-2 (Geometry)": ["Notebook", "Digest"],
    "Science-1 (Physics + Chemistry)": ["Notebook", "Digest"],
    "Science-2 (Biology)":            ["Notebook", "Digest"],
    "English":          ["Workbook", "Grammar Notebook", "Digest"],
}

STUDENTS = ["Maaz", "Ziyan", "Ismail", "Mutahhir", "Talha", "Shaikh Affan"]
NAME_OPTIONS = STUDENTS + [ADMIN_NAME]


def all_book_items():
    items = []
    for subject, item_types in SUBJECTS.items():
        for item_type in item_types:
            book_id = f"{subject}::{item_type}"
            items.append({
                "book_id": book_id,
                "subject": subject,
                "item_type": item_type,
                "label": f"{subject} — {item_type}",
            })
    return items


# =========================================================================
# DATABASE LAYER
# =========================================================================

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                is_admin INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                device_token TEXT,
                suspended INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_codes (
                email TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL,
                student_name TEXT NOT NULL,
                needed_by_date TEXT NOT NULL,
                signature_data TEXT,
                signature_type TEXT,
                status TEXT NOT NULL DEFAULT 'waiting',
                created_at TEXT NOT NULL,
                fulfilled_at TEXT,
                returned INTEGER NOT NULL DEFAULT 0,
                returned_on TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                detail TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        # Migration safety: add columns if an older DB file is reused
        existing_cols = [r["name"] for r in conn.execute("PRAGMA table_info(reservations)").fetchall()]
        if "returned" not in existing_cols:
            conn.execute("ALTER TABLE reservations ADD COLUMN returned INTEGER NOT NULL DEFAULT 0")
        if "returned_on" not in existing_cols:
            conn.execute("ALTER TABLE reservations ADD COLUMN returned_on TEXT")
        existing_acct_cols = [r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()]
        if "suspended" not in existing_acct_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN suspended INTEGER NOT NULL DEFAULT 0")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---- Accounts / auth --------------------------------------------------------

def get_account_by_email(email):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
        return dict(row) if row else None


def get_account_by_device_token(token):
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE device_token = ?", (token,)).fetchone()
        return dict(row) if row else None


def create_or_get_account(name, email, is_admin=False):
    email = email.strip().lower()
    existing = get_account_by_email(email)
    if existing:
        return existing
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO accounts (name, email, is_admin, verified, created_at) VALUES (?, ?, ?, 0, ?)",
            (name, email, 1 if is_admin else 0, now_iso())
        )
    return get_account_by_email(email)


def force_admin_flag(email):
    """Guarantees the admin account always has is_admin=1, even if it was
    previously created as a regular student account before the admin
    password flow was ever used for it."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET is_admin = 1 WHERE email = ?",
            (email.strip().lower(),)
        )


def mark_verified_with_token(email, device_token):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET verified = 1, device_token = ? WHERE email = ?",
            (device_token, email.strip().lower())
        )


def set_login_code(email, name, code):
    expires = (datetime.datetime.now() + datetime.timedelta(minutes=10)).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO login_codes (email, code, name, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET code=excluded.code, name=excluded.name, expires_at=excluded.expires_at",
            (email.strip().lower(), code, name, expires)
        )


def check_login_code(email, code):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM login_codes WHERE email = ?", (email.strip().lower(),)).fetchone()
        if not row:
            return False
        if row["code"] != code:
            return False
        if datetime.datetime.now() > datetime.datetime.fromisoformat(row["expires_at"]):
            return False
        return True


def clear_login_code(email):
    with get_conn() as conn:
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email.strip().lower(),))


# ---- Reservation operations -------------------------------------------------

def create_reservation(book_id, student_name, needed_by_date, signature_data, signature_type):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO reservations
               (book_id, student_name, needed_by_date, signature_data, signature_type, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'waiting', ?)""",
            (book_id, student_name, needed_by_date, signature_data, signature_type, now_iso())
        )


def get_queue_for_book(book_id, include_fulfilled=False):
    with get_conn() as conn:
        if include_fulfilled:
            rows = conn.execute(
                "SELECT * FROM reservations WHERE book_id = ? ORDER BY id ASC", (book_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reservations WHERE book_id = ? AND status = 'waiting' ORDER BY id ASC",
                (book_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_all_reservations():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM reservations ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_reservations_for_student(student_name):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE student_name = ? AND status = 'waiting' ORDER BY id ASC",
            (student_name,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_queue_position(reservation_id):
    with get_conn() as conn:
        row = conn.execute("SELECT book_id, id FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
        if not row:
            return None
        count = conn.execute(
            "SELECT COUNT(*) as c FROM reservations WHERE book_id = ? AND status = 'waiting' AND id <= ?",
            (row["book_id"], row["id"])
        ).fetchone()
        return count["c"]


def mark_fulfilled(reservation_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE reservations SET status = 'fulfilled', fulfilled_at = ? WHERE id = ?",
            (now_iso(), reservation_id)
        )


def mark_returned(reservation_id, returned_on_date):
    with get_conn() as conn:
        conn.execute(
            "UPDATE reservations SET returned = 1, returned_on = ? WHERE id = ?",
            (returned_on_date, reservation_id)
        )


def unmark_returned(reservation_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE reservations SET returned = 0, returned_on = NULL WHERE id = ?",
            (reservation_id,)
        )


def update_reservation_date(reservation_id, new_date_iso):
    with get_conn() as conn:
        conn.execute(
            "UPDATE reservations SET needed_by_date = ? WHERE id = ?",
            (new_date_iso, reservation_id)
        )


def cancel_reservation(reservation_id):
    with get_conn() as conn:
        conn.execute("UPDATE reservations SET status = 'cancelled' WHERE id = ?", (reservation_id,))


def delete_reservation(reservation_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))


def log_admin_action(action, detail=""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_log (action, detail, timestamp) VALUES (?, ?, ?)",
            (action, detail, now_iso())
        )


def reservation_counts_by_book():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT book_id, COUNT(*) as waiting_count FROM reservations WHERE status = 'waiting' GROUP BY book_id"
        ).fetchall()
        return {r["book_id"]: r["waiting_count"] for r in rows}


def student_already_in_queue(book_id, student_name):
    """True if this student already has a 'waiting' reservation for this book."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM reservations WHERE book_id = ? AND student_name = ? AND status = 'waiting' LIMIT 1",
            (book_id, student_name)
        ).fetchone()
        return row is not None


def set_suspended(email, suspended):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET suspended = ? WHERE email = ?",
            (1 if suspended else 0, email.strip().lower())
        )


def get_all_accounts():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]


init_db()


# =========================================================================
# EMAIL SENDING
# Uses Gmail SMTP with an App Password stored in Streamlit Secrets.
# Required secrets: SENDER_EMAIL, SENDER_APP_PASSWORD
# =========================================================================

def send_verification_email(to_email, code):
    sender_email = st.secrets.get("SENDER_EMAIL", None)
    sender_password = st.secrets.get("SENDER_APP_PASSWORD", None)

    if not sender_email or not sender_password:
        return False, (
            "Email sending isn't configured yet. Set SENDER_EMAIL and SENDER_APP_PASSWORD "
            "in your app's Secrets (Streamlit Cloud → Settings → Secrets)."
        )

    subject = "Your Book Desk verification code"
    body = (
        f"Your verification code is: {code}\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"— Safha (Shaikh Zulqarnain's book sharing log)"
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = to_email

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls(context=context)
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, to_email, msg.as_string())
        return True, None
    except Exception as e:
        return False, f"Couldn't send the email: {e}"


def generate_code():
    return "".join(random.choices(string.digits, k=6))


def generate_device_token():
    return pysecrets.token_urlsafe(24)



# =========================================================================
# DESIGN SYSTEM — "Serene Falah" theme
# Deep ink background, cyan glow accent, Fraunces/Amiri headings. Inlined
# as one plain (non-f) triple quoted string — no Python interpolation
# happens inside it, so CSS braces and quotes are never parsed as Python
# syntax. Wrapped in try/except so a rendering issue here can never take
# down the rest of the app.
# =========================================================================

BOOK_DESK_CSS = """
/* =========================================================================
   THE BOOK DESK — "Serene Falah" theme
   Deep ink background, cyan glow accent, Fraunces serif headings, Amiri
   for any Arabic text, hairline borders instead of solid card outlines.
   ========================================================================= */

@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;1,9..144,500&family=Inter:wght@300;400;500;600;700&family=Amiri:wght@400;700&display=swap');

:root {
    --ink: #eaf6f8;
    --ink-dim: #9db4bc;
    --ink-faint: #62777e;
    --paper: #070f14;
    --paper-raised: #0d1a21;
    --paper-card: #10202a;
    --gold: #2fb8c9;
    --gold-bright: #7fe3f0;
    --gold-dim: #1c5b64;
    --line: rgba(78, 200, 217, 0.22);
    --danger: #e0796a;
    --good: #6fc79a;
}

html, body, [data-testid="stAppViewContainer"] {
    background:
        radial-gradient(circle at 50% 0%, rgba(47, 184, 201, 0.10), transparent 60%),
        var(--paper) !important;
    color: var(--ink) !important;
    font-family: 'Inter', -apple-system, sans-serif !important;
}

[data-testid="stHeader"] { background: transparent !important; }
#MainMenu, footer { visibility: hidden; }

/* ---- Floating bubble/particle background ----
   Pure CSS, no JS. Fixed behind all content, pointer-events disabled so
   it can NEVER intercept taps or clicks — purely decorative. Respects
   prefers-reduced-motion for accessibility and low-end-device comfort. */

.safha-particles {
    position: fixed;
    inset: 0;
    z-index: 0;
    overflow: hidden;
    pointer-events: none;
}

.safha-particles span {
    position: absolute;
    bottom: -12vh;
    display: block;
    border-radius: 50%;
    background: radial-gradient(circle at 32% 28%,
        rgba(127, 227, 240, 0.55),
        rgba(47, 184, 201, 0.18) 55%,
        rgba(47, 184, 201, 0) 75%);
    box-shadow: 0 0 18px rgba(47, 184, 201, 0.15);
    animation: safha-float-up linear infinite;
    will-change: transform, opacity;
}

.safha-particles span:nth-child(1)  { left: 4%;  width: 18px; height: 18px; animation-duration: 22s; animation-delay: 0s; }
.safha-particles span:nth-child(2)  { left: 14%; width: 10px; height: 10px; animation-duration: 16s; animation-delay: 2s; }
.safha-particles span:nth-child(3)  { left: 23%; width: 26px; height: 26px; animation-duration: 28s; animation-delay: 1s; }
.safha-particles span:nth-child(4)  { left: 33%; width: 8px;  height: 8px;  animation-duration: 14s; animation-delay: 5s; }
.safha-particles span:nth-child(5)  { left: 42%; width: 20px; height: 20px; animation-duration: 24s; animation-delay: 3s; }
.safha-particles span:nth-child(6)  { left: 52%; width: 14px; height: 14px; animation-duration: 19s; animation-delay: 7s; }
.safha-particles span:nth-child(7)  { left: 61%; width: 30px; height: 30px; animation-duration: 30s; animation-delay: 0.5s; }
.safha-particles span:nth-child(8)  { left: 70%; width: 12px; height: 12px; animation-duration: 17s; animation-delay: 4s; }
.safha-particles span:nth-child(9)  { left: 79%; width: 22px; height: 22px; animation-duration: 25s; animation-delay: 6s; }
.safha-particles span:nth-child(10) { left: 87%; width: 9px;  height: 9px;  animation-duration: 15s; animation-delay: 2.5s; }
.safha-particles span:nth-child(11) { left: 92%; width: 16px; height: 16px; animation-duration: 21s; animation-delay: 8s; }
.safha-particles span:nth-child(12) { left: 9%;  width: 13px; height: 13px; animation-duration: 20s; animation-delay: 10s; }
.safha-particles span:nth-child(13) { left: 47%; width: 7px;  height: 7px;  animation-duration: 13s; animation-delay: 9s; }
.safha-particles span:nth-child(14) { left: 66%; width: 19px; height: 19px; animation-duration: 26s; animation-delay: 11s; }
.safha-particles span:nth-child(15) { left: 76%; width: 11px; height: 11px; animation-duration: 18s; animation-delay: 6.5s; }
.safha-particles span:nth-child(16) { left: 18%; width: 15px; height: 15px; animation-duration: 23s; animation-delay: 13s; }
.safha-particles span:nth-child(17) { left: 38%; width: 24px; height: 24px; animation-duration: 27s; animation-delay: 4.5s; }
.safha-particles span:nth-child(18) { left: 58%; width: 10px; height: 10px; animation-duration: 16s; animation-delay: 12s; }

@keyframes safha-float-up {
    0%   { transform: translateY(0) translateX(0) scale(1);   opacity: 0; }
    8%   { opacity: 0.85; }
    50%  { transform: translateY(-55vh) translateX(2vw) scale(1.08); }
    92%  { opacity: 0.6; }
    100% { transform: translateY(-112vh) translateX(-2vw) scale(0.9); opacity: 0; }
}

/* Make sure real content always sits above the particle layer.
   Target both current and older Streamlit container test-ids so this
   keeps working across Streamlit version updates. */
[data-testid="stAppViewContainer"] > .main,
[data-testid="stAppViewContainer"] [data-testid="stMain"],
[data-testid="stAppViewContainer"] [data-testid="block-container"] {
    position: relative;
    z-index: 1;
}

@media (prefers-reduced-motion: reduce) {
    .safha-particles { display: none !important; }
}

.block-container {
    max-width: 780px !important;
    padding-top: 2.5rem !important;
    padding-bottom: 4rem !important;
}

/* ---- Header ---- */

.desk-header { margin-bottom: 1.75rem; }

.desk-eyebrow {
    font-family: 'Inter', sans-serif;
    font-size: 0.72rem;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--gold-bright);
    font-weight: 600;
    margin-bottom: 0.4rem;
    display: flex;
    align-items: center;
    gap: 8px;
}

.desk-eyebrow::before {
    content: '';
    width: 6px;
    height: 6px;
    transform: rotate(45deg);
    background: var(--gold);
    display: inline-block;
}

.desk-title {
    font-family: 'Fraunces', serif;
    font-size: 2.4rem;
    font-weight: 500;
    letter-spacing: -0.01em;
    color: var(--ink);
    line-height: 1.1;
    margin-bottom: 0.4rem;
}

.desk-sub {
    font-size: 0.95rem;
    color: var(--ink-dim);
    font-weight: 300;
    margin-bottom: 1rem;
}

.desk-header-rule {
    height: 1px;
    background: linear-gradient(90deg, var(--gold-dim), transparent 70%);
    margin-top: 0.5rem;
}

/* ---- Section labels ---- */

.section-label {
    display: block;
    font-size: 0.72rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--gold-bright);
    font-weight: 600;
    margin: 1.1rem 0 0.4rem 0;
}

/* ---- Inputs ---- */

[data-testid="stTextInput"] input,
[data-testid="stDateInput"] input,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
    background: var(--paper-raised) !important;
    border: 1px solid var(--line) !important;
    border-radius: 8px !important;
    color: var(--ink) !important;
}

[data-testid="stTextInput"] input:focus,
[data-testid="stDateInput"] input:focus {
    border-color: var(--gold) !important;
    box-shadow: 0 0 0 1px var(--gold) !important;
}

[data-testid="stSelectbox"] label,
[data-testid="stTextInput"] label,
[data-testid="stDateInput"] label,
[data-testid="stRadio"] label {
    color: var(--ink-dim) !important;
}

[data-baseweb="select"] svg { fill: var(--ink-dim) !important; }

/* ---- Buttons — pill-shaped, cyan fill, shimmer on hover ---- */

.stButton button, .stFormSubmitButton button, .stDownloadButton button {
    background: transparent !important;
    color: var(--ink) !important;
    border: 1px solid var(--line) !important;
    border-radius: 999px !important;
    font-weight: 500 !important;
    letter-spacing: 0.02em;
    transition: transform 0.25s ease, box-shadow 0.25s ease, border-color 0.25s ease, background 0.25s ease, color 0.25s ease !important;
}

.stButton button:hover, .stFormSubmitButton button:hover, .stDownloadButton button:hover {
    background: var(--gold) !important;
    color: var(--paper) !important;
    border-color: var(--gold) !important;
    transform: translateY(-1px);
    box-shadow: 0 10px 24px -12px rgba(47, 184, 201, 0.55);
}

.stButton button[kind="primary"] {
    background: var(--gold) !important;
    color: var(--paper) !important;
    border-color: var(--gold) !important;
}

/* ---- Book rows ---- */

.book-row {
    background: var(--paper-card);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 0.85rem 1rem;
    margin-top: 0.6rem;
}

.book-row-main {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.book-row-title {
    font-family: 'Fraunces', serif;
    font-size: 1.05rem;
    font-weight: 500;
    color: var(--ink);
}

.row-spacer { height: 0.6rem; }

/* ---- Queue badges ---- */

.queue-badge {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    padding: 0.25rem 0.6rem;
    border-radius: 999px;
    background: rgba(47, 184, 201, 0.12);
    color: var(--gold-bright);
    border: 1px solid var(--gold-dim);
}

.queue-badge.empty {
    background: rgba(111, 199, 154, 0.12);
    color: var(--good);
    border-color: rgba(111, 199, 154, 0.4);
}

.queue-badge.busy {
    background: rgba(224, 121, 106, 0.12);
    color: var(--danger);
    border-color: rgba(224, 121, 106, 0.4);
}

/* ---- Reservation / result cards ---- */

.res-card {
    background: var(--paper-card);
    border: 1px solid var(--line);
    border-left: 3px solid var(--gold);
    border-radius: 12px;
    padding: 0.8rem 1rem;
    margin-top: 0.7rem;
    margin-bottom: 0.3rem;
}

.res-card-title {
    font-family: 'Fraunces', serif;
    font-size: 1rem;
    font-weight: 500;
    color: var(--ink);
    margin-bottom: 0.15rem;
}

.res-card-meta {
    font-size: 0.8rem;
    color: var(--ink-dim);
}

.empty-state {
    color: var(--ink-faint);
    font-style: italic;
    padding: 1.5rem 0;
    text-align: center;
    border: 1px dashed var(--line);
    border-radius: 14px;
    margin-top: 0.5rem;
}

/* ---- Top account bar (formerly sidebar; kept as top-of-page nav so it
   can never be hidden behind a collapse arrow) ---- */

.side-name {
    font-family: 'Fraunces', serif;
    font-size: 1.15rem;
    color: var(--gold-bright);
    font-weight: 500;
    margin-top: 0.5rem;
}

.side-email {
    font-size: 0.78rem;
    color: var(--ink-faint);
    margin-bottom: 0.5rem;
}

/* ---- Tabs ---- */

[data-testid="stTabs"] [data-baseweb="tab-list"] {
    gap: 0.3rem;
    border-bottom: 1px solid var(--line);
}

[data-testid="stTabs"] button[role="tab"] {
    color: var(--ink-faint) !important;
    font-weight: 500;
    font-size: 0.85rem;
}

[data-testid="stTabs"] button[aria-selected="true"] {
    color: var(--gold-bright) !important;
    border-bottom-color: var(--gold) !important;
}

/* ---- Alerts ---- */

[data-testid="stAlert"] {
    border-radius: 12px !important;
    background: var(--paper-card) !important;
    border: 1px solid var(--line) !important;
}

/* ---- Dataframe (admin log) ---- */

[data-testid="stDataFrame"] {
    border: 1px solid var(--line) !important;
    border-radius: 12px !important;
    overflow: hidden;
}

/* ---- Footer ---- */

.desk-footer {
    margin-top: 3rem;
    padding-top: 1.6rem;
    border-top: 1px solid var(--line);
    text-align: center;
    font-size: 0.72rem;
    color: var(--ink-faint);
    letter-spacing: 0.08em;
    text-transform: uppercase;
}

.desk-footer .name {
    display: block;
    margin-top: 0.35rem;
    font-family: 'Fraunces', serif;
    font-style: italic;
    font-weight: 500;
    font-size: 1.35rem;
    letter-spacing: 0.01em;
    text-transform: none;
    background: linear-gradient(90deg, var(--gold-dim), var(--gold-bright) 45%, var(--gold-dim));
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    text-shadow: 0 0 22px rgba(127, 227, 240, 0.25);
}
/* =========================================================================
   CUSTOM WIDGET SKIN
   Streamlit's native controls (radio, tabs, file_uploader, expander,
   dataframe) are kept for their functionality — they're the only way data
   reaches Python — but every default visual trace is stripped and rebuilt
   to look like a hand-built component.
   ========================================================================= */

/* Hide Streamlit chrome globally */
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
div[data-testid="stToolbar"],
.stDeployButton,
[data-testid="stMainMenu"] {
    display: none !important;
}

/* ---- Top nav pills (built on the main-page st.radio; circles hidden,
   styled as horizontal pill buttons) ---- */

[data-testid="stRadio"] > div {
    display: flex;
    flex-direction: row;
    flex-wrap: wrap;
    gap: 0.4rem;
}

[data-testid="stRadio"] label {
    background: transparent;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 0.45rem 0.9rem !important;
    transition: all 0.2s ease;
    cursor: pointer;
}

[data-testid="stRadio"] label:hover {
    background: rgba(47, 184, 201, 0.08);
    border-color: var(--gold);
}

[data-testid="stRadio"] input:checked + div {
    color: var(--gold-bright) !important;
    font-weight: 600 !important;
}

[data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {
    display: none !important;
}

/* ---- Custom tab strip enhancements (targets ALL st.tabs on the page,
   since a wrapping <div> from st.markdown does not nest around a
   separately-rendered st.tabs() call in Streamlit's DOM) ---- */

[data-testid="stTabs"] [data-baseweb="tab-list"] {
    overflow-x: auto;
    scrollbar-width: thin;
}

[data-testid="stTabs"] [data-baseweb="tab-highlight"],
[data-testid="stTabs"] [data-baseweb="tab-border"] {
    display: none !important;
}

/* ---- File uploader restyled as a signature dropzone ---- */

[data-testid="stFileUploader"] {
    background: var(--paper-raised);
    border: 1.5px dashed var(--line);
    border-radius: 14px;
    padding: 0.4rem;
}

[data-testid="stFileUploader"] section {
    background: transparent !important;
    border: none !important;
}

[data-testid="stFileUploader"] small {
    color: var(--ink-faint) !important;
}

[data-testid="stFileUploaderDropzoneInstructions"] span,
[data-testid="stFileUploaderDropzoneInstructions"] div {
    color: var(--ink-dim) !important;
}

[data-testid="stFileUploader"] button {
    background: transparent !important;
    color: var(--gold-bright) !important;
    border: 1px solid var(--gold-dim) !important;
    border-radius: 999px !important;
}

/* ---- Expander restyled as a clean accordion row ---- */

[data-testid="stExpander"] {
    background: var(--paper-card) !important;
    border: 1px solid var(--line) !important;
    border-radius: 14px !important;
    margin-top: 0.6rem;
    overflow: hidden;
}

[data-testid="stExpander"] summary {
    padding: 0.85rem 1rem !important;
    font-family: 'Fraunces', serif;
    font-size: 0.95rem;
    color: var(--ink) !important;
}

[data-testid="stExpander"] summary:hover {
    background: rgba(47, 184, 201, 0.06);
}

[data-testid="stExpander"] svg {
    fill: var(--gold) !important;
}

/* ---- Dataframe restyled as a clean elegant table ---- */

[data-testid="stDataFrame"] * {
    font-family: 'Inter', sans-serif !important;
}

[data-testid="stDataFrame"] [role="columnheader"] {
    background: var(--paper-raised) !important;
    color: var(--gold-bright) !important;
    font-weight: 600 !important;
    font-size: 0.78rem !important;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

[data-testid="stDataFrame"] [role="gridcell"] {
    background: var(--paper-card) !important;
    color: var(--ink) !important;
    font-size: 0.85rem !important;
}

/* ---- Hide default field labels/help text we've replaced with our own ---- */

[data-testid="stWidgetLabel"] {
    display: none;
}

.show-label [data-testid="stWidgetLabel"] {
    display: block;
}

.show-label [data-testid="stWidgetLabel"] p {
    color: var(--ink-dim) !important;
    font-size: 0.82rem !important;
}

/* ---- Custom brand mark for the login screen ---- */

.brand-mark {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 56px;
    height: 56px;
    border-radius: 16px;
    background: linear-gradient(135deg, rgba(47,184,201,0.18), rgba(47,184,201,0.02));
    border: 1px solid var(--line);
    margin: 0 auto 1.1rem auto;
    font-family: 'Fraunces', serif;
    font-size: 1.5rem;
    color: var(--gold-bright);
    text-align: center;
}

/* ---- Custom stat chips for admin overview ---- */

.stat-row {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin: 0.6rem 0 1.2rem 0;
}

.stat-chip {
    flex: 1 1 120px;
    background: var(--paper-card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 0.7rem 0.9rem;
}

.stat-chip .stat-num {
    font-family: 'Fraunces', serif;
    font-size: 1.5rem;
    color: var(--gold-bright);
    line-height: 1;
}

.stat-chip .stat-label {
    font-size: 0.7rem;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-top: 0.2rem;
}

/* ---- Custom queue-position ring ---- */

.pos-ring {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    border-radius: 50%;
    border: 1.5px solid var(--gold);
    color: var(--gold-bright);
    font-family: 'Fraunces', serif;
    font-weight: 600;
    font-size: 0.95rem;
    flex-shrink: 0;
}

.res-card-flex {
    display: flex;
    align-items: center;
    gap: 0.75rem;
}

/* =========================================================================
   ANIMATIONS
   Entrance keyframes on load, subtle ambient motion on key accents, and
   richer hover/press feedback everywhere the person actually interacts.
   Respects prefers-reduced-motion for anyone who's asked their system to
   minimize motion.
   ========================================================================= */

@keyframes safha-fade-up {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: translateY(0); }
}

@keyframes safha-fade-in {
    from { opacity: 0; }
    to   { opacity: 1; }
}

@keyframes safha-scale-in {
    from { opacity: 0; transform: scale(0.92); }
    to   { opacity: 1; transform: scale(1); }
}

@keyframes safha-glow-breathe {
    0%, 100% { box-shadow: 0 0 0 0 rgba(47, 184, 201, 0.0); }
    50%      { box-shadow: 0 0 26px 4px rgba(47, 184, 201, 0.22); }
}

@keyframes safha-shimmer {
    0%   { background-position: -200% 0; }
    100% { background-position: 200% 0; }
}

@keyframes safha-pulse-ring {
    0%   { box-shadow: 0 0 0 0 rgba(47, 184, 201, 0.35); }
    70%  { box-shadow: 0 0 0 8px rgba(47, 184, 201, 0); }
    100% { box-shadow: 0 0 0 0 rgba(47, 184, 201, 0); }
}

@keyframes safha-underline-grow {
    from { width: 0%; }
    to   { width: 100%; }
}

/* ---- Page entrance: header + brand mark fade/rise in on load ---- */

.desk-header {
    animation: safha-fade-up 0.6s cubic-bezier(0.22, 1, 0.36, 1) both;
}

.brand-mark {
    animation: safha-scale-in 0.5s cubic-bezier(0.22, 1, 0.36, 1) both,
               safha-glow-breathe 3.2s ease-in-out 0.6s infinite;
}

/* ---- Cards: staggered rise-in. nth-of-type gives each row in a list a
   slightly later start so they cascade rather than pop in together ---- */

.book-row, .res-card, .stat-chip, [data-testid="stExpander"] {
    animation: safha-fade-up 0.5s cubic-bezier(0.22, 1, 0.36, 1) both;
}

.book-row:nth-of-type(1),  .res-card:nth-of-type(1)  { animation-delay: 0.02s; }
.book-row:nth-of-type(2),  .res-card:nth-of-type(2)  { animation-delay: 0.08s; }
.book-row:nth-of-type(3),  .res-card:nth-of-type(3)  { animation-delay: 0.14s; }
.book-row:nth-of-type(4),  .res-card:nth-of-type(4)  { animation-delay: 0.20s; }
.book-row:nth-of-type(5),  .res-card:nth-of-type(5)  { animation-delay: 0.26s; }
.book-row:nth-of-type(n+6),.res-card:nth-of-type(n+6){ animation-delay: 0.30s; }

.stat-chip:nth-of-type(1) { animation-delay: 0.02s; }
.stat-chip:nth-of-type(2) { animation-delay: 0.08s; }
.stat-chip:nth-of-type(3) { animation-delay: 0.14s; }
.stat-chip:nth-of-type(4) { animation-delay: 0.20s; }

/* Card hover lift, layered on top of the entrance animation */
.book-row, .res-card, .stat-chip {
    transition: transform 0.25s ease, border-color 0.25s ease, box-shadow 0.25s ease;
}

.book-row:hover, .res-card:hover, .stat-chip:hover {
    transform: translateY(-3px);
    border-color: var(--gold-dim);
    box-shadow: 0 14px 30px -18px rgba(47, 184, 201, 0.45);
}

/* ---- Buttons: shimmer sweep + press feedback ---- */

.stButton button, .stFormSubmitButton button, .stDownloadButton button {
    position: relative;
    overflow: hidden;
}

.stButton button::before, .stFormSubmitButton button::before, .stDownloadButton button::before {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(110deg, transparent 30%, rgba(255,255,255,0.16) 50%, transparent 70%);
    background-size: 200% 100%;
    background-position: 200% 0;
    opacity: 0;
    transition: opacity 0.2s ease;
    pointer-events: none;
}

.stButton button:hover::before, .stFormSubmitButton button:hover::before, .stDownloadButton button:hover::before {
    opacity: 1;
    animation: safha-shimmer 1.1s ease-in-out infinite;
}

.stButton button:active, .stFormSubmitButton button:active, .stDownloadButton button:active {
    transform: scale(0.97) translateY(0) !important;
}

/* ---- Queue badge: gentle pulse when students are waiting ---- */

.queue-badge.busy {
    animation: safha-pulse-ring 2.2s ease-out infinite;
}

/* ---- Top nav: active pill glows softly, links nudge on hover ---- */

[data-testid="stRadio"] label {
    transition: all 0.2s ease, transform 0.2s ease;
}

[data-testid="stRadio"] label:hover {
    transform: translateY(-1px);
}

/* ---- Position ring: soft pulse to draw the eye to queue placement ---- */

.pos-ring {
    animation: safha-pulse-ring 2.6s ease-out infinite;
}

/* ---- Footer name: fades in last, after everything else has settled ---- */

.desk-footer {
    animation: safha-fade-in 0.8s ease 0.3s both;
}

/* ---- Input focus: soft glow grows in rather than snapping on ---- */

[data-testid="stTextInput"] input,
[data-testid="stDateInput"] input {
    transition: border-color 0.25s ease, box-shadow 0.25s ease;
}

/* ---- Tab underline slides in on selection ---- */

[data-testid="stTabs"] button[aria-selected="true"] {
    position: relative;
}

/* ---- Alerts (success/error/info) rise in when they appear ---- */

[data-testid="stAlert"] {
    animation: safha-fade-up 0.35s cubic-bezier(0.22, 1, 0.36, 1) both;
}

/* ---- File uploader dropzone glows gently to invite interaction ---- */

[data-testid="stFileUploader"] {
    transition: border-color 0.25s ease, background 0.25s ease;
}

[data-testid="stFileUploader"]:hover {
    border-color: var(--gold-dim);
    background: rgba(47, 184, 201, 0.03);
}

/* ---- Respect reduced-motion preference: disable non-essential motion ---- */

@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.001ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.001ms !important;
    }
}
"""

try:
    st.markdown("<style>" + BOOK_DESK_CSS + "</style>", unsafe_allow_html=True)
except Exception:
    pass

# Floating decorative bubbles — purely visual, pointer-events disabled via
# CSS above, so this can never intercept a tap or block a button.
st.markdown(
    '<div class="safha-particles">' + "".join(f"<span></span>" for _ in range(18)) + "</div>",
    unsafe_allow_html=True,
)


# =========================================================================
# Helpers
# =========================================================================

def render_header(eyebrow, title, sub):
    st.markdown(f"""
    <div class="desk-header">
        <div class="desk-eyebrow">{eyebrow}</div>
        <div class="desk-title">{title}</div>
        <div class="desk-sub">{sub}</div>
        <div class="desk-header-rule"></div>
    </div>
    """, unsafe_allow_html=True)


def days_until(date_str):
    try:
        target = datetime.date.fromisoformat(date_str)
        return (target - datetime.date.today()).days
    except Exception:
        return None


def signature_to_base64(uploaded_file):
    """Reads an uploaded signature image and returns (base64_str, mime_type)."""
    if uploaded_file is None:
        return None, None
    file_bytes = uploaded_file.getvalue()
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    return b64, uploaded_file.type


def format_reservation_date(date_str):
    """Formats a date like '5 August' and adds '(tomorrow)' / '(today)' when relevant."""
    try:
        d = datetime.date.fromisoformat(date_str)
    except Exception:
        return date_str
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    try:
        pretty = f"{d.day} {d.strftime('%B')}"
    except Exception:
        pretty = date_str
    if d == today:
        return f"{pretty} (today)"
    elif d == tomorrow:
        return f"{pretty} (tomorrow)"
    return pretty


def render_queue_badge_html(count):
    if count == 0:
        return '<span class="queue-badge empty">available</span>'
    elif count >= 2:
        return f'<span class="queue-badge busy">{count} waiting</span>'
    else:
        return f'<span class="queue-badge">{count} waiting</span>'


def render_footer():
    st.markdown("""
    <div class="desk-footer">
        Developed by <span class="name">Shaikh Zulqarnain</span>
    </div>
    """, unsafe_allow_html=True)


def render_field_label(text):
    """Custom label replacing Streamlit's default widget label, styled to
    match the rest of the hand-built UI."""
    st.markdown(f'<span class="section-label">{text}</span>', unsafe_allow_html=True)


# ---- localStorage <-> URL token bridge -------------------------------
# Streamlit has no direct localStorage API, so these inject tiny JS
# snippets via components.html to read/write it and reload with the
# right ?t= query param. This makes login persist in the same browser
# even after the URL itself loses the token (closed tab, fresh bookmark,
# home-screen shortcut saved without it, etc).

import streamlit.components.v1 as components


def save_token_to_local_storage(token):
    components.html(f"""
        <script>
        try {{
            window.localStorage.setItem('safha_token', '{token}');
        }} catch (e) {{}}
        </script>
    """, height=0, width=0)


def clear_token_from_local_storage():
    components.html("""
        <script>
        try {
            window.localStorage.removeItem('safha_token');
        } catch (e) {}
        </script>
    """, height=0, width=0)


def try_restore_token_from_local_storage():
    """If the URL has no ?t= token, check the browser's localStorage for
    one and, if found, reload the page with it appended to the URL so
    Streamlit's normal query-param login path picks it up."""
    components.html("""
        <script>
        try {
            const stored = window.localStorage.getItem('safha_token');
            if (stored) {
                const url = new URL(window.parent.location.href);
                if (!url.searchParams.get('t')) {
                    url.searchParams.set('t', stored);
                    window.parent.location.replace(url.toString());
                }
            }
        } catch (e) {}
        </script>
    """, height=0, width=0)


# =========================================================================
# SESSION / LOGIN
# Device stays logged in via a token stored in the URL query params.
# =========================================================================

if "account" not in st.session_state:
    st.session_state.account = None
if "pending_login" not in st.session_state:
    st.session_state.pending_login = None

if st.session_state.account is None:
    token_from_url = st.query_params.get("t", None)
    if token_from_url:
        acct = get_account_by_device_token(token_from_url)
        if acct and acct["verified"] and not acct["suspended"]:
            st.session_state.account = acct
    else:
        # No token in the URL at all — check localStorage before giving up
        # and showing the login screen. If one turns up, this triggers a
        # one-time reload with ?t=... attached, which then gets picked up
        # by the block above on the next run.
        try_restore_token_from_local_storage()


def do_logout():
    st.session_state.account = None
    st.session_state.pending_login = None
    st.query_params.clear()
    clear_token_from_local_storage()


# =========================================================================
# LOGIN SCREEN — custom card shell around functional Streamlit inputs
# =========================================================================

if st.session_state.account is None:
    login_col_l, login_col_mid, login_col_r = st.columns([1, 3, 1])
    with login_col_mid:
        st.markdown('<div class="brand-mark">📚</div>', unsafe_allow_html=True)
        render_header("Shaikh Zulqarnain · 10th A", "Safha", "Log in to reserve books or manage the desk.")

        if st.session_state.pending_login is None:
            render_field_label("Select your name")
            chosen_name = st.selectbox("Name", NAME_OPTIONS, label_visibility="collapsed")

            render_field_label("Your email address")
            email_input = st.text_input("Email", label_visibility="collapsed", placeholder="you@example.com")

            if st.button("Continue", use_container_width=True):
                email_clean = email_input.strip().lower()
                if not email_clean or "@" not in email_clean:
                    st.error("Please enter a valid email address.")
                elif chosen_name == ADMIN_NAME:
                    if email_clean != ADMIN_EMAIL.lower():
                        st.error("This email isn't authorized for the admin account.")
                    else:
                        st.session_state.pending_login = {"name": ADMIN_NAME, "email": email_clean, "mode": "admin_password"}
                        st.rerun()
                else:
                    existing = get_account_by_email(email_clean)
                    if existing and existing["name"] != chosen_name:
                        st.error("This email is already registered under a different name.")
                    else:
                        code = generate_code()
                        ok, err = send_verification_email(email_clean, code)
                        if not ok:
                            st.error(err)
                        else:
                            set_login_code(email_clean, chosen_name, code)
                            st.session_state.pending_login = {"name": chosen_name, "email": email_clean, "mode": "email_code"}
                            st.success(f"Code sent to {email_clean}. Check your inbox.")
                            st.rerun()

        elif st.session_state.pending_login["mode"] == "admin_password":
            render_field_label(f"{ADMIN_NAME} login — enter the admin password")
            pw = st.text_input("Password", type="password", label_visibility="collapsed")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Unlock admin", use_container_width=True):
                    admin_pw = st.secrets.get("ADMIN_PASSWORD", None)
                    if not admin_pw:
                        st.error(
                            "No admin password is configured. Set ADMIN_PASSWORD in your app's Secrets "
                            "(Streamlit Cloud → Settings → Secrets)."
                        )
                    elif pw == admin_pw:
                        acct = create_or_get_account(ADMIN_NAME, ADMIN_EMAIL, is_admin=True)
                        force_admin_flag(ADMIN_EMAIL)
                        token = generate_device_token()
                        mark_verified_with_token(ADMIN_EMAIL, token)
                        st.session_state.account = get_account_by_email(ADMIN_EMAIL)
                        st.session_state.pending_login = None
                        st.query_params["t"] = token
                        save_token_to_local_storage(token)
                        log_admin_action("login")
                        st.rerun()
                    else:
                        st.error("Incorrect password.")
            with c2:
                if st.button("Back", use_container_width=True, type="secondary"):
                    st.session_state.pending_login = None
                    st.rerun()

        elif st.session_state.pending_login["mode"] == "email_code":
            pending = st.session_state.pending_login
            render_field_label(f"Enter the 6-digit code sent to {pending['email']}")
            code_input = st.text_input("Code", label_visibility="collapsed", placeholder="123456", max_chars=6)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Verify", use_container_width=True):
                    if check_login_code(pending["email"], code_input.strip()):
                        existing_acct = get_account_by_email(pending["email"])
                        if existing_acct and existing_acct["suspended"]:
                            st.error("This account has been suspended. Please contact Shaikh Zulqarnain.")
                        else:
                            create_or_get_account(pending["name"], pending["email"])
                            token = generate_device_token()
                            mark_verified_with_token(pending["email"], token)
                            clear_login_code(pending["email"])
                            st.session_state.account = get_account_by_email(pending["email"])
                            st.session_state.pending_login = None
                            st.query_params["t"] = token
                            save_token_to_local_storage(token)
                            st.rerun()
                    else:
                        st.error("Incorrect or expired code.")
            with c2:
                if st.button("Resend code", use_container_width=True, type="secondary"):
                    code = generate_code()
                    ok, err = send_verification_email(pending["email"], code)
                    if ok:
                        set_login_code(pending["email"], pending["name"], code)
                        st.success("New code sent.")
                    else:
                        st.error(err)
            if st.button("Use a different name or email", type="secondary"):
                st.session_state.pending_login = None
                st.rerun()

    render_footer()
    st.stop()


# =========================================================================
# LOGGED-IN APP
# =========================================================================

account = st.session_state.account
is_admin = bool(account["is_admin"])

# Navigation is rendered directly in the main page (not in st.sidebar) so
# it can never be hidden behind a collapse arrow that's unreliable on some
# mobile browsers — the nav is always visible, right under the account info.
st.markdown(f'<div class="side-name">{account["name"]}</div>', unsafe_allow_html=True)
st.markdown(f'<div class="side-email">{account["email"]}</div>', unsafe_allow_html=True)

nav_col, logout_col = st.columns([3, 1])
with nav_col:
    if is_admin:
        nav = st.radio(
            "Navigate",
            ["Overview", "Queues", "All reservations", "Students", "Export & log"],
            label_visibility="collapsed",
            horizontal=True,
        )
    else:
        nav = st.radio(
            "Navigate", ["Reserve a book", "My reservations"],
            label_visibility="collapsed", horizontal=True,
        )
with logout_col:
    if st.button("Log out", use_container_width=True):
        do_logout()
        st.rerun()

st.markdown('<div class="desk-header-rule"></div>', unsafe_allow_html=True)

items = all_book_items()
counts = reservation_counts_by_book()

# ---- Reserve a book ---------------------------------------------------

if nav == "Reserve a book":
    render_header("Safha", "Reserve a Book", "Fill out every field to join the queue.")

    MAX_SIGNATURE_BYTES = int(1.5 * 1024 * 1024)  # 1.5 MB

    render_field_label("Name")
    st.markdown(f'<div class="book-row"><div class="book-row-title">{account["name"]}</div></div>', unsafe_allow_html=True)
    st.markdown('<div class="row-spacer"></div>', unsafe_allow_html=True)

    with st.form(key="reserve_form", clear_on_submit=True, border=False):
        render_field_label("Subject")
        subject = st.selectbox(
            "Subject", list(SUBJECTS.keys()), label_visibility="collapsed", key="reserve_subject"
        )

        render_field_label("Book type")
        # Item types depend on the chosen subject, so this selectbox is
        # rebuilt from SUBJECTS every run based on the subject picked above.
        item_type = st.selectbox(
            "Book type", SUBJECTS[subject], label_visibility="collapsed", key="reserve_item_type"
        )

        render_field_label("Needed by")
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        needed_by = st.date_input(
            "Needed by", value=tomorrow,
            min_value=datetime.date.today(), key="reserve_date",
            label_visibility="collapsed"
        )

        render_field_label("Signature (photo of handwritten signature, max 1.5MB)")
        sig_file = st.file_uploader(
            "Signature",
            type=["png", "jpg", "jpeg"],
            key="reserve_signature",
            label_visibility="collapsed",
        )

        submitted = st.form_submit_button("Join queue", use_container_width=True)

        if submitted:
            book_id = f"{subject}::{item_type}"
            if student_already_in_queue(book_id, account["name"]):
                st.error("You're already in the queue for this item.")
            elif sig_file is None:
                st.error("Please upload a photo of your signature to complete the reservation.")
            elif sig_file.size > MAX_SIGNATURE_BYTES:
                st.error("That signature photo is over 1.5MB. Please upload a smaller image.")
            else:
                sig_b64, sig_type = signature_to_base64(sig_file)
                create_reservation(book_id, account["name"], needed_by.isoformat(), sig_b64, sig_type)
                st.success(f"Joined the queue for {subject} — {item_type}.")
                st.rerun()

    st.markdown('<div class="section-label">Current queue sizes</div>', unsafe_allow_html=True)
    for subj in SUBJECTS:
        for it in SUBJECTS[subj]:
            bid = f"{subj}::{it}"
            waiting = counts.get(bid, 0)
            st.markdown(f"""
            <div class="book-row">
                <div class="book-row-main">
                    <div class="book-row-title">{subj} — {it}</div>
                    {render_queue_badge_html(waiting)}
                </div>
            </div>
            """, unsafe_allow_html=True)

# ---- My reservations ---------------------------------------------------

elif nav == "My reservations":
    render_header("Safha", "My Reservations", "Your active spots in the queue.")

    my_res = get_reservations_for_student(account["name"])
    if not my_res:
        st.markdown('<div class="empty-state">You have no active reservations.</div>', unsafe_allow_html=True)
    else:
        for r in my_res:
            subject, item_type = r["book_id"].split("::")
            pos = get_queue_position(r["id"])
            date_display = format_reservation_date(r["needed_by_date"])
            st.markdown(f"""
            <div class="res-card">
                <div class="res-card-flex">
                    <div class="pos-ring">#{pos}</div>
                    <div>
                        <div class="res-card-title">{subject} — {item_type}</div>
                        <div class="res-card-meta">
                            <b>Name:</b> {account['name']} &nbsp;·&nbsp;
                            <b>Book Reserved:</b> {subject} — {item_type} &nbsp;·&nbsp;
                            <b>Date:</b> {date_display}
                        </div>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            if r["signature_data"]:
                try:
                    sig_bytes = base64.b64decode(r["signature_data"])
                    st.image(sig_bytes, caption="Signature", width=180)
                except Exception:
                    st.caption("Signature: (unable to display)")
            else:
                st.caption("Signature: not on file")
            if st.button("Cancel this reservation", key=f"cancel_{r['id']}"):
                cancel_reservation(r["id"])
                st.rerun()

# ---- Admin: Overview ---------------------------------------------------

elif nav == "Overview" and is_admin:
    render_header("Safha", "Overview", "A snapshot of activity across the whole class.")

    all_res_for_stats = get_all_reservations()
    accounts_for_stats = get_all_accounts()

    waiting_count = sum(1 for r in all_res_for_stats if r["status"] == "waiting")
    fulfilled_count = sum(1 for r in all_res_for_stats if r["status"] == "fulfilled")
    cancelled_count = sum(1 for r in all_res_for_stats if r["status"] == "cancelled")
    returned_count = sum(1 for r in all_res_for_stats if r["returned"])
    with_sig = sum(1 for r in all_res_for_stats if r["signature_data"])
    missing_sig_count = len(all_res_for_stats) - with_sig
    sig_rate = round((with_sig / len(all_res_for_stats)) * 100) if all_res_for_stats else 0
    active_students = len([a for a in accounts_for_stats if a["email"].lower() != ADMIN_EMAIL.lower()])
    suspended_count = sum(1 for a in accounts_for_stats if a["suspended"] and a["email"].lower() != ADMIN_EMAIL.lower())

    st.markdown(f"""
    <div class="stat-row">
        <div class="stat-chip"><div class="stat-num">{len(all_res_for_stats)}</div><div class="stat-label">Total reservations</div></div>
        <div class="stat-chip"><div class="stat-num">{waiting_count}</div><div class="stat-label">Waiting</div></div>
        <div class="stat-chip"><div class="stat-num">{fulfilled_count}</div><div class="stat-label">Fulfilled</div></div>
        <div class="stat-chip"><div class="stat-num">{returned_count}</div><div class="stat-label">Returned</div></div>
    </div>
    <div class="stat-row">
        <div class="stat-chip"><div class="stat-num">{active_students}</div><div class="stat-label">Registered students</div></div>
        <div class="stat-chip"><div class="stat-num">{suspended_count}</div><div class="stat-label">Suspended</div></div>
        <div class="stat-chip"><div class="stat-num">{sig_rate}%</div><div class="stat-label">Signed on file</div></div>
        <div class="stat-chip"><div class="stat-num">{missing_sig_count}</div><div class="stat-label">Missing signature</div></div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="section-label">Waiting queue by subject</div>', unsafe_allow_html=True)
    counts_by_book = reservation_counts_by_book()
    any_waiting = False
    for subject in SUBJECTS:
        subject_total = sum(
            counts_by_book.get(f"{subject}::{item_type}", 0) for item_type in SUBJECTS[subject]
        )
        if subject_total > 0:
            any_waiting = True
            st.markdown(f"""
            <div class="book-row">
                <div class="book-row-main">
                    <div class="book-row-title">{subject}</div>
                    {render_queue_badge_html(subject_total)}
                </div>
            </div>
            """, unsafe_allow_html=True)
    if not any_waiting:
        st.markdown('<div class="empty-state">No one is currently waiting on any subject.</div>', unsafe_allow_html=True)

    st.markdown('<div class="section-label">Reservations by student</div>', unsafe_allow_html=True)
    per_student = {}
    for r in all_res_for_stats:
        per_student.setdefault(r["student_name"], 0)
        per_student[r["student_name"]] += 1
    if not per_student:
        st.markdown('<div class="empty-state">No reservations recorded yet.</div>', unsafe_allow_html=True)
    else:
        for name, count in sorted(per_student.items(), key=lambda x: -x[1]):
            st.markdown(f"""
            <div class="res-card">
                <div class="res-card-meta"><b>{name}</b> &nbsp;·&nbsp; {count} reservation{'s' if count != 1 else ''} total</div>
            </div>
            """, unsafe_allow_html=True)

# ---- Admin: Queues -------------------------------------------------

elif nav == "Queues" and is_admin:
    render_header("Safha", "Queues", "See who's waiting for each subject item.")

    subjects = list(SUBJECTS.keys())
    render_field_label("Subject")
    pick_subject = st.selectbox("Subject", subjects, label_visibility="collapsed")
    for item_type in SUBJECTS[pick_subject]:
        book_id = f"{pick_subject}::{item_type}"
        queue = get_queue_for_book(book_id)
        st.markdown(f'<div class="section-label">{item_type} — {len(queue)} waiting</div>', unsafe_allow_html=True)
        if not queue:
            st.caption("No one waiting.")
        for r in queue:
            c1, c2, c3 = st.columns([3, 1, 1])
            with c1:
                st.markdown(f"**{r['student_name']}** · needed by {r['needed_by_date']}")
            with c2:
                if st.button("Fulfilled", key=f"fulfil_{r['id']}"):
                    mark_fulfilled(r["id"])
                    log_admin_action("mark_fulfilled", f"{r['student_name']} — {book_id}")
                    st.rerun()
            with c3:
                if st.button("Cancel", key=f"admincancel_{r['id']}"):
                    cancel_reservation(r["id"])
                    log_admin_action("cancel_reservation", f"{r['student_name']} — {book_id}")
                    st.rerun()
        st.markdown('<div class="row-spacer"></div>', unsafe_allow_html=True)

# ---- Admin: All reservations ---------------------------------------

elif nav == "All reservations" and is_admin:
    render_header("Safha", "All Reservations", "Full control — view, edit, or delete any record.")

    all_res = get_all_reservations()
    if not all_res:
        st.markdown('<div class="empty-state">No reservations yet.</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="section-label">Overview table</div>', unsafe_allow_html=True)
        table_rows = []
        for r in all_res:
            subject, item_type = r["book_id"].split("::")
            table_rows.append({
                "Name": r["student_name"],
                "Book Reserved": f"{subject} — {item_type}",
                "Date": format_reservation_date(r["needed_by_date"]),
                "Status": r["status"],
                "Returned": "Yes" if r["returned"] else "No",
                "Signature": "On file" if r["signature_data"] else "Missing",
            })
        st.dataframe(
            table_rows,
            use_container_width=True,
            hide_index=True,
            column_order=["Name", "Book Reserved", "Date", "Status", "Returned", "Signature"],
        )

        st.markdown('<div class="section-label">Manage individual reservations</div>', unsafe_allow_html=True)
        for r in all_res:
            subject, item_type = r["book_id"].split("::")
            date_display = format_reservation_date(r["needed_by_date"])
            with st.expander(f"{r['student_name']} — {subject} ({item_type}) · {date_display}"):
                st.markdown(f"""
                <div class="res-card">
                    <div class="res-card-meta">
                        <b>Name:</b> {r['student_name']} &nbsp;·&nbsp;
                        <b>Book Reserved:</b> {subject} — {item_type} &nbsp;·&nbsp;
                        <b>Date:</b> {date_display} &nbsp;·&nbsp;
                        <b>Status:</b> {r['status']} &nbsp;·&nbsp;
                        <b>Returned:</b> {'Yes' if r['returned'] else 'No'}
                    </div>
                </div>
                """, unsafe_allow_html=True)

                if r["signature_data"]:
                    try:
                        sig_bytes = base64.b64decode(r["signature_data"])
                        st.image(sig_bytes, caption="Signature on file", width=180)
                    except Exception:
                        st.caption("Signature: (unable to display)")
                else:
                    st.caption("Signature: not on file")

                render_field_label("Edit reservation date")
                ec1, ec2 = st.columns([2, 1])
                with ec1:
                    try:
                        current_date = datetime.date.fromisoformat(r["needed_by_date"])
                    except Exception:
                        current_date = datetime.date.today()
                    new_date = st.date_input(
                        "New date", value=current_date, key=f"editdate_{r['id']}",
                        label_visibility="collapsed"
                    )
                with ec2:
                    if st.button("Save date", key=f"savedate_{r['id']}", use_container_width=True):
                        update_reservation_date(r["id"], new_date.isoformat())
                        log_admin_action("edit_date", f"{r['student_name']} — {r['book_id']} → {new_date.isoformat()}")
                        st.success("Date updated.")
                        st.rerun()

                st.markdown('<div class="row-spacer"></div>', unsafe_allow_html=True)
                c1, c2 = st.columns(2)
                with c1:
                    if not r["returned"]:
                        if st.button("Mark returned", key=f"ret_{r['id']}", use_container_width=True):
                            mark_returned(r["id"], datetime.date.today().isoformat())
                            log_admin_action("mark_returned", f"{r['student_name']} — {r['book_id']}")
                            st.rerun()
                    else:
                        if st.button("Undo returned", key=f"unret_{r['id']}", use_container_width=True):
                            unmark_returned(r["id"])
                            log_admin_action("unmark_returned", f"{r['student_name']} — {r['book_id']}")
                            st.rerun()
                with c2:
                    if st.button("Delete record", key=f"del_{r['id']}", use_container_width=True):
                        delete_reservation(r["id"])
                        log_admin_action("delete_reservation", f"{r['student_name']} — {r['book_id']}")
                        st.rerun()

# ---- Admin: Students -------------------------------------------------

elif nav == "Students" and is_admin:
    render_header("Safha", "Students", "Manage student access to Safha.")

    accounts_list = get_all_accounts()
    for a in accounts_list:
        if a["email"].lower() == ADMIN_EMAIL.lower():
            continue
        c1, c2 = st.columns([3, 1])
        with c1:
            status = "suspended" if a["suspended"] else "active"
            st.markdown(f"**{a['name']}** · {a['email']} · {status}")
        with c2:
            if a["suspended"]:
                if st.button("Unsuspend", key=f"unsusp_{a['id']}"):
                    set_suspended(a["email"], False)
                    log_admin_action("unsuspend", a["email"])
                    st.rerun()
            else:
                if st.button("Suspend", key=f"susp_{a['id']}"):
                    set_suspended(a["email"], True)
                    log_admin_action("suspend", a["email"])
                    st.rerun()

# ---- Admin: Export & log ---------------------------------------------

elif nav == "Export & log" and is_admin:
    render_header("Safha", "Export & Log", "Download reservation data and review recent admin activity.")

    all_res = get_all_reservations()
    if all_res:
        import csv
        import io
        buf = io.StringIO()
        fieldnames = ["Name", "Book Reserved", "Date", "Status", "Returned", "Returned On", "Signature", "Reserved On"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_res:
            subject, item_type = r["book_id"].split("::")
            writer.writerow({
                "Name": r["student_name"],
                "Book Reserved": f"{subject} — {item_type}",
                "Date": r["needed_by_date"],
                "Status": r["status"],
                "Returned": "Yes" if r["returned"] else "No",
                "Returned On": r["returned_on"] or "",
                "Signature": "On file" if r["signature_data"] else "Missing",
                "Reserved On": r["created_at"],
            })
        st.download_button(
            "Download all reservations (CSV)",
            data=buf.getvalue(),
            file_name=f"reservations_{datetime.date.today().isoformat()}.csv",
            mime="text/csv",
            use_container_width=True
        )
    else:
        st.caption("No reservation data to export yet.")

    st.markdown('<div class="section-label">Admin action log</div>', unsafe_allow_html=True)
    with get_conn() as conn:
        logs = [dict(r) for r in conn.execute(
            "SELECT * FROM admin_log ORDER BY id DESC LIMIT 50"
        ).fetchall()]
    if not logs:
        st.caption("No actions logged yet.")
    else:
        st.dataframe(
            logs,
            use_container_width=True,
            hide_index=True,
            column_order=["timestamp", "action", "detail"],
        )

render_footer()
