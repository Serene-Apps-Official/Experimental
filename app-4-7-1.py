import streamlit as st
import datetime
import base64
import sqlite3
import secrets as pysecrets
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
    "Hindi":       ["Workbook", "Hindi Gram", "Digest"],
    "Marathi":     ["Workbook", "Marathi Gram", "Digest"],
    "History/Civics": ["Notebook", "Digest"],
    "Geography":   ["Notebook", "Digest"],
    "Maths 1":     ["Notebook", "Digest"],
    "Maths 2":     ["Notebook", "Digest"],
    "Science 1":   ["Notebook", "Digest"],
    "Science 2":   ["Notebook", "Digest"],
    "English":     ["Workbook", "English Gram", "Digest"],
}

STUDENTS = ["Maaz", "Ziyan", "Ismail", "Mutahhir", "Talha", "Shaikh Affan"]
NAME_OPTIONS = STUDENTS + [ADMIN_NAME]


def secret_key_for_name(name):
    """Turns a student name into its Secrets key, e.g. 'Shaikh Affan' ->
    'PASSCODE_SHAIKH_AFFAN'. Each student has their own passcode set in
    Streamlit Secrets, told to them individually and privately."""
    return "PASSCODE_" + name.strip().upper().replace(" ", "_")


def synthetic_email_for_name(name):
    """The accounts table requires a unique, non-null 'email' column from
    the old email-login system. Since login is now passcode-only (no real
    email collected), we derive a stable internal placeholder per name so
    the existing schema and account-lookup functions keep working as-is."""
    return name.strip().lower().replace(" ", ".") + "@safha.local"


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
        if "signature_data" not in existing_acct_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN signature_data TEXT")
        if "signature_type" not in existing_acct_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN signature_type TEXT")
        if "last_login_at" not in existing_acct_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_login_at TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS book_availability (
                book_id TEXT PRIMARY KEY,
                disabled INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migration safety for reservations: notice tracking so a student
        # is shown a one-time notice the next time they visit after their
        # reservation was cancelled/fulfilled/returned by admin.
        existing_res_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(reservations)").fetchall()]
        if "notice_seen" not in existing_res_cols2:
            conn.execute("ALTER TABLE reservations ADD COLUMN notice_seen INTEGER NOT NULL DEFAULT 1")

        # One-time migration: subject display names were simplified
        # (e.g. "Maths-1 (Algebra)" -> "Maths 1"). Remap any reservations
        # and book_availability rows still using the old book_id strings
        # so existing data isn't orphaned by the rename.
        BOOK_ID_RENAMES = {
            "Maths-1 (Algebra)": "Maths 1",
            "Maths-2 (Geometry)": "Maths 2",
            "Science-1 (Physics + Chemistry)": "Science 1",
            "Science-2 (Biology)": "Science 2",
        }
        for old_subject, new_subject in BOOK_ID_RENAMES.items():
            conn.execute(
                "UPDATE reservations SET book_id = REPLACE(book_id, ?, ?) WHERE book_id LIKE ?",
                (f"{old_subject}::", f"{new_subject}::", f"{old_subject}::%")
            )
            conn.execute(
                "UPDATE book_availability SET book_id = REPLACE(book_id, ?, ?) WHERE book_id LIKE ?",
                (f"{old_subject}::", f"{new_subject}::", f"{old_subject}::%")
            )
        # "Grammar Notebook" was renamed per-subject (e.g. "Hindi Gram").
        GRAMMAR_RENAMES = {
            "Hindi::Grammar Notebook": "Hindi::Hindi Gram",
            "Marathi::Grammar Notebook": "Marathi::Marathi Gram",
            "English::Grammar Notebook": "English::English Gram",
        }
        for old_id, new_id in GRAMMAR_RENAMES.items():
            conn.execute("UPDATE reservations SET book_id = ? WHERE book_id = ?", (new_id, old_id))
            conn.execute("UPDATE book_availability SET book_id = ? WHERE book_id = ?", (new_id, old_id))


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


IST_OFFSET = datetime.timedelta(hours=5, minutes=30)


def to_ist_display(iso_str):
    """Server time is stored as naive local server time, which on
    Streamlit Cloud is UTC. This converts a stored timestamp to IST for
    display, formatted like '13 August, 3:42 PM IST'."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
    except Exception:
        return iso_str
    dt_ist = dt + IST_OFFSET
    return f"{dt_ist.day} {dt_ist.strftime('%B')}, {dt_ist.strftime('%I:%M %p').lstrip('0')} IST"


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


def record_login(account_name):
    """Logs a login event with timestamp, and updates the account's
    last_login_at, so admin can see when everyone signs in."""
    ts = now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO login_history (account_name, timestamp) VALUES (?, ?)",
            (account_name, ts)
        )
        conn.execute(
            "UPDATE accounts SET last_login_at = ? WHERE name = ?",
            (ts, account_name)
        )


def get_login_history(limit=100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM login_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def save_account_signature(email, signature_data, signature_type):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET signature_data = ?, signature_type = ? WHERE email = ?",
            (signature_data, signature_type, email.strip().lower())
        )


# ---- Reservation operations -------------------------------------------------

def create_reservation(book_id, student_name, needed_by_date, signature_data=None, signature_type=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO reservations
               (book_id, student_name, needed_by_date, signature_data, signature_type, status, created_at, notice_seen)
               VALUES (?, ?, ?, ?, ?, 'waiting', ?, 1)""",
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
            "UPDATE reservations SET status = 'fulfilled', fulfilled_at = ?, notice_seen = 0 WHERE id = ?",
            (now_iso(), reservation_id)
        )


def mark_returned(reservation_id, returned_on_date):
    with get_conn() as conn:
        conn.execute(
            "UPDATE reservations SET returned = 1, returned_on = ?, notice_seen = 0 WHERE id = ?",
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


def cancel_reservation(reservation_id, notify=False):
    with get_conn() as conn:
        if notify:
            conn.execute(
                "UPDATE reservations SET status = 'cancelled', notice_seen = 0 WHERE id = ?",
                (reservation_id,)
            )
        else:
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


def get_disabled_book_ids():
    with get_conn() as conn:
        rows = conn.execute("SELECT book_id FROM book_availability WHERE disabled = 1").fetchall()
        return {r["book_id"] for r in rows}


def set_book_disabled(book_id, disabled):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO book_availability (book_id, disabled) VALUES (?, ?) "
            "ON CONFLICT(book_id) DO UPDATE SET disabled = excluded.disabled",
            (book_id, 1 if disabled else 0)
        )


def get_unseen_notice_for_student(student_name):
    """Returns the most recent cancelled/fulfilled/returned reservation
    the student hasn't been shown a notice for yet, or None."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM reservations
               WHERE student_name = ? AND notice_seen = 0
               ORDER BY id DESC LIMIT 1""",
            (student_name,)
        ).fetchone()
        return dict(row) if row else None


def mark_notice_seen(reservation_id):
    with get_conn() as conn:
        conn.execute("UPDATE reservations SET notice_seen = 1 WHERE id = ?", (reservation_id,))


init_db()


# =========================================================================
# EMAIL SENDING
# Uses Gmail SMTP with an App Password stored in Streamlit Secrets.
# Required secrets: SENDER_EMAIL, SENDER_APP_PASSWORD
# =========================================================================

def generate_device_token():
    return pysecrets.token_urlsafe(24)



# =========================================================================
# DESIGN SYSTEM — "Paper Bloom" theme
# Playful glassmorphism, warm mesh-gradient backdrop, Fraunces/Plus Jakarta
# Sans/Amiri headings. Inlined as one plain (non-f) triple quoted string —
# no Python interpolation happens inside it, so CSS braces and quotes are
# never parsed as Python syntax. Wrapped in try/except so a rendering
# issue here can never take down the rest of the app.
# =========================================================================

BOOK_DESK_CSS = """
/* =========================================================================
   SAFHA — "Paper Bloom" theme
   Playful glassmorphism: a living mesh-gradient backdrop in warm pink,
   sky blue, amber and violet, with frosted, blurred glass panels floating
   above it. Fraunces for display headings (kept from before — it already
   suits a book app), Plus Jakarta Sans for a warm, rounded, friendly body
   face, Amiri preserved for any Arabic text. Every class name below
   matches what the Python code already emits, so no HTML-generating code
   needed to change — only the visual system underneath it.
   ========================================================================= */

@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;0,9..144,700;1,9..144,500&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Amiri:wght@400;700&display=swap');

:root {
    --ink: #2d2a4a;
    --ink-dim: #6b6791;
    --ink-faint: #a09cc4;
    --paper: #fdf6ff;
    --paper-raised: rgba(255, 255, 255, 0.55);
    --paper-card: rgba(255, 255, 255, 0.62);
    --glass-border: rgba(255, 255, 255, 0.75);

    --pink: #ff6b9d;
    --pink-bright: #ff8fb8;
    --sky: #5b8def;
    --sky-bright: #7ea6f5;
    --amber: #ffb84d;
    --amber-bright: #ffca75;
    --violet: #7c5cfc;
    --violet-bright: #9c85fd;

    /* Kept so any leftover references to the old palette still resolve
       sensibly instead of breaking — mapped onto the new hues. */
    --gold: var(--violet);
    --gold-bright: var(--violet-bright);
    --gold-dim: #d8cfff;
    --line: rgba(124, 92, 252, 0.18);
    --danger: #ef4d6b;
    --good: #22b07d;
}

html, body, [data-testid="stAppViewContainer"] {
    background:
        radial-gradient(ellipse 60% 45% at 12% 8%, rgba(255, 107, 157, 0.35), transparent 60%),
        radial-gradient(ellipse 55% 45% at 88% 15%, rgba(91, 141, 239, 0.32), transparent 60%),
        radial-gradient(ellipse 60% 50% at 20% 92%, rgba(255, 184, 77, 0.30), transparent 60%),
        radial-gradient(ellipse 55% 50% at 85% 88%, rgba(124, 92, 252, 0.30), transparent 60%),
        var(--paper) !important;
    background-attachment: fixed !important;
    color: var(--ink) !important;
    font-family: 'Plus Jakarta Sans', -apple-system, sans-serif !important;
}

[data-testid="stHeader"] { background: transparent !important; }
#MainMenu, footer { visibility: hidden; }

/* ---- Hide Streamlit Cloud chrome: top-right toolbar (Deploy button,
   GitHub icon, three-dot menu incl. "Edit", "Share", "Fork/Star"), and
   the "Manage app" bar. This is cosmetic only — it declutters the UI for
   viewers, it does not restrict repo/dashboard access for anyone who
   already has edit rights on the underlying GitHub repo or Streamlit
   Cloud project. */
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
.stDeployButton,
[data-testid="stAppDeployButton"],
[class*="viewerBadge"],
[class*="stAppViewerBadge"] {
    display: none !important;
    visibility: hidden !important;
}

/* Slow ambient drift of the mesh gradient itself, so the backdrop feels
   alive without ever distracting from content. Respects reduced-motion
   via the global override further down. */
@keyframes safha-mesh-drift {
    0%, 100% { background-position: 0% 0%, 100% 0%, 0% 100%, 100% 100%; }
    50%      { background-position: 8% 4%, 92% 6%, 6% 94%, 94% 92%; }
}

/* ---- Floating paper / page-corner background ----
   Replaces the old floating spheres with gentle, colorful gradient blobs
   — soft, blurred orbs of color drifting slowly upward, echoing the
   living mesh gradient behind them. Pure CSS, no JS. Fixed behind all
   content, pointer-events disabled so it can NEVER intercept taps or
   clicks — purely decorative. Respects prefers-reduced-motion. */

.safha-particles {
    position: fixed;
    inset: 0;
    z-index: 0;
    overflow: hidden;
    pointer-events: none;
}

.safha-particles span {
    position: absolute;
    bottom: -20vh;
    display: block;
    border-radius: 50%;
    filter: blur(2px);
    opacity: 0;
    animation: safha-float-up linear infinite;
    will-change: transform, opacity;
}

.safha-particles span:nth-child(4n+1) {
    background: radial-gradient(circle at 35% 30%, rgba(255, 107, 157, 0.55), rgba(255, 107, 157, 0.05) 70%);
}
.safha-particles span:nth-child(4n+2) {
    background: radial-gradient(circle at 35% 30%, rgba(91, 141, 239, 0.55), rgba(91, 141, 239, 0.05) 70%);
}
.safha-particles span:nth-child(4n+3) {
    background: radial-gradient(circle at 35% 30%, rgba(255, 184, 77, 0.55), rgba(255, 184, 77, 0.05) 70%);
}
.safha-particles span:nth-child(4n+4) {
    background: radial-gradient(circle at 35% 30%, rgba(124, 92, 252, 0.55), rgba(124, 92, 252, 0.05) 70%);
}

.safha-particles span:nth-child(1)  { left: 4%;  width: 60px; height: 60px; animation-duration: 26s; animation-delay: 0s; }
.safha-particles span:nth-child(2)  { left: 14%; width: 34px; height: 34px; animation-duration: 19s; animation-delay: 2s; }
.safha-particles span:nth-child(3)  { left: 23%; width: 80px; height: 80px; animation-duration: 32s; animation-delay: 1s; }
.safha-particles span:nth-child(4)  { left: 33%; width: 26px; height: 26px; animation-duration: 17s; animation-delay: 5s; }
.safha-particles span:nth-child(5)  { left: 42%; width: 55px; height: 55px; animation-duration: 28s; animation-delay: 3s; }
.safha-particles span:nth-child(6)  { left: 52%; width: 40px; height: 40px; animation-duration: 22s; animation-delay: 7s; }
.safha-particles span:nth-child(7)  { left: 61%; width: 90px; height: 90px; animation-duration: 34s; animation-delay: 0.5s; }
.safha-particles span:nth-child(8)  { left: 70%; width: 30px; height: 30px; animation-duration: 20s; animation-delay: 4s; }
.safha-particles span:nth-child(9)  { left: 79%; width: 65px; height: 65px; animation-duration: 29s; animation-delay: 6s; }
.safha-particles span:nth-child(10) { left: 87%; width: 24px; height: 24px; animation-duration: 18s; animation-delay: 2.5s; }
.safha-particles span:nth-child(11) { left: 92%; width: 46px; height: 46px; animation-duration: 24s; animation-delay: 8s; }
.safha-particles span:nth-child(12) { left: 9%;  width: 36px; height: 36px; animation-duration: 23s; animation-delay: 10s; }
.safha-particles span:nth-child(13) { left: 47%; width: 20px; height: 20px; animation-duration: 15s; animation-delay: 9s; }
.safha-particles span:nth-child(14) { left: 66%; width: 52px; height: 52px; animation-duration: 30s; animation-delay: 11s; }
.safha-particles span:nth-child(15) { left: 76%; width: 30px; height: 30px; animation-duration: 21s; animation-delay: 6.5s; }
.safha-particles span:nth-child(16) { left: 18%; width: 42px; height: 42px; animation-duration: 27s; animation-delay: 13s; }
.safha-particles span:nth-child(17) { left: 38%; width: 70px; height: 70px; animation-duration: 31s; animation-delay: 4.5s; }
.safha-particles span:nth-child(18) { left: 58%; width: 28px; height: 28px; animation-duration: 19s; animation-delay: 12s; }

@keyframes safha-float-up {
    0%   { transform: translateY(0) translateX(0) scale(0.7);   opacity: 0; }
    10%  { opacity: 0.9; }
    50%  { transform: translateY(-58vh) translateX(3vw) scale(1.15); }
    90%  { opacity: 0.5; }
    100% { transform: translateY(-118vh) translateX(-3vw) scale(0.85); opacity: 0; }
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

/* ---- Typography ---- */

h1, h2, h3, .desk-title, .brand-mark {
    font-family: 'Fraunces', serif !important;
}

.desk-eyebrow {
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--pink);
    margin-bottom: 0.3rem;
}

.desk-title {
    font-size: 2.4rem;
    font-weight: 600;
    line-height: 1.08;
    background: linear-gradient(100deg, var(--violet) 0%, var(--pink) 50%, var(--amber) 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    margin: 0 0 0.4rem 0;
    animation: safha-fade-up 0.6s cubic-bezier(0.22, 1, 0.36, 1) both;
}

.desk-sub {
    font-size: 1rem;
    color: var(--ink-dim);
    font-weight: 500;
    margin-bottom: 1rem;
    animation: safha-fade-up 0.6s cubic-bezier(0.22, 1, 0.36, 1) 0.05s both;
}

.desk-header-rule {
    height: 3px;
    border-radius: 999px;
    background: linear-gradient(90deg, var(--pink), var(--sky), var(--amber), var(--violet));
    background-size: 300% 100%;
    animation: safha-shimmer 6s ease-in-out infinite;
    margin: 0.4rem 0 1.6rem 0;
    opacity: 0.85;
}

.brand-mark {
    font-size: 2.6rem;
    text-align: center;
    margin-bottom: 0.3rem;
    filter: drop-shadow(0 4px 14px rgba(124, 92, 252, 0.35));
    animation: safha-scale-in 0.5s cubic-bezier(0.22, 1, 0.36, 1) both;
}

.section-label {
    display: block;
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: 0.8rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--violet);
    margin: 1.1rem 0 0.4rem 0;
}

/* ---- Glass cards, rows, everything frosted ---- */

.book-row, .res-card, .stat-chip {
    background: var(--paper-card);
    backdrop-filter: blur(18px) saturate(160%);
    -webkit-backdrop-filter: blur(18px) saturate(160%);
    border: 1.5px solid var(--glass-border);
    border-radius: 20px;
    box-shadow: 0 8px 32px rgba(124, 92, 252, 0.12), 0 1.5px 4px rgba(255, 107, 157, 0.08);
    padding: 1rem 1.2rem;
    margin-bottom: 0.6rem;
    transition: transform 0.25s cubic-bezier(0.22, 1, 0.36, 1), box-shadow 0.25s ease;
    animation: safha-fade-up 0.5s cubic-bezier(0.22, 1, 0.36, 1) both;
}

.book-row:hover, .res-card:hover {
    transform: translateY(-3px) scale(1.005);
    box-shadow: 0 14px 40px rgba(124, 92, 252, 0.18), 0 2px 8px rgba(255, 107, 157, 0.12);
}

.book-row-main {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.8rem;
}

.book-row-title {
    font-weight: 700;
    font-size: 1rem;
    color: var(--ink);
}

.res-card-flex {
    display: flex;
    align-items: center;
    gap: 1rem;
}

.res-card-title {
    font-weight: 700;
    font-size: 1.05rem;
    color: var(--ink);
    margin-bottom: 0.2rem;
}

.res-card-meta {
    font-size: 0.86rem;
    color: var(--ink-dim);
    line-height: 1.6;
}

.row-spacer { height: 0.5rem; }

.empty-state {
    text-align: center;
    padding: 2rem 1rem;
    color: var(--ink-faint);
    font-style: italic;
    background: var(--paper-raised);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border: 1.5px dashed rgba(124, 92, 252, 0.35);
    border-radius: 20px;
}

/* ---- Queue badges — playful pill colors ---- */

.queue-badge {
    display: inline-block;
    padding: 0.3rem 0.8rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 700;
    white-space: nowrap;
}

.queue-badge.empty {
    background: rgba(34, 176, 125, 0.16);
    color: #16875e;
    border: 1.5px solid rgba(34, 176, 125, 0.35);
}

.queue-badge.busy {
    background: rgba(255, 107, 157, 0.18);
    color: #d13d70;
    border: 1.5px solid rgba(255, 107, 157, 0.4);
    animation: safha-pulse-ring 2.2s ease-out infinite;
}

.queue-badge:not(.empty):not(.busy) {
    background: rgba(255, 184, 77, 0.2);
    color: #b8790a;
    border: 1.5px solid rgba(255, 184, 77, 0.4);
}

.queue-badge.disabled {
    background: rgba(45, 42, 74, 0.1);
    color: var(--ink-faint);
    border: 1.5px solid rgba(45, 42, 74, 0.18);
}

/* ---- Suspension banner ---- */

.suspended-banner {
    background: linear-gradient(120deg, rgba(239, 77, 107, 0.14), rgba(255, 184, 77, 0.14));
    border: 1.5px solid rgba(239, 77, 107, 0.35);
    border-radius: 16px;
    padding: 0.9rem 1.1rem;
    color: #b3324f;
    font-weight: 600;
    font-size: 0.9rem;
    margin-bottom: 1rem;
}

.suspended-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    background: linear-gradient(120deg, #ef4d6b, #ffb84d);
    color: #fff;
    font-size: 0.78rem;
    font-weight: 700;
    padding: 0.3rem 0.85rem;
    border-radius: 999px;
    box-shadow: 0 4px 14px rgba(239, 77, 107, 0.3);
}

/* ---- Celebration animation on successful reservation ---- */

.celebrate-wrap {
    position: relative;
    margin-bottom: 1.2rem;
}

.celebrate-card {
    background: linear-gradient(120deg, rgba(124, 92, 252, 0.14), rgba(255, 107, 157, 0.14));
    border: 1.5px solid rgba(124, 92, 252, 0.3);
    border-radius: 22px;
    padding: 1.6rem 1.2rem;
    text-align: center;
    animation: safha-scale-in 0.5s cubic-bezier(0.22, 1, 0.36, 1) both;
    position: relative;
    z-index: 2;
}

.celebrate-emoji {
    font-size: 2.6rem;
    animation: safha-celebrate-bounce 0.7s ease 0.1s both;
}

.celebrate-title {
    font-family: 'Fraunces', serif;
    font-size: 1.4rem;
    font-weight: 600;
    color: var(--ink);
    margin: 0.3rem 0 0.2rem 0;
}

.celebrate-sub {
    font-size: 0.9rem;
    color: var(--ink-dim);
    line-height: 1.5;
}

.celebrate-confetti {
    position: absolute;
    inset: 0;
    overflow: visible;
    pointer-events: none;
    z-index: 1;
}

.confetti-piece {
    position: absolute;
    top: -10px;
    width: 8px;
    height: 14px;
    border-radius: 2px;
    opacity: 0;
    animation: safha-confetti-fall 1.8s ease-in forwards;
}

.confetti-piece.c0 { background: var(--pink); left: 4%;  animation-delay: 0.02s; }
.confetti-piece.c1 { background: var(--sky); left: 12%; animation-delay: 0.08s; }
.confetti-piece.c2 { background: var(--amber); left: 20%; animation-delay: 0.04s; }
.confetti-piece.c3 { background: var(--violet); left: 28%; animation-delay: 0.12s; }
.confetti-piece.c4 { background: var(--pink-bright); left: 36%; animation-delay: 0.06s; }
.confetti-piece.c5 { background: var(--sky-bright); left: 44%; animation-delay: 0.1s; }
.confetti-piece:nth-child(7)  { left: 52%; }
.confetti-piece:nth-child(8)  { left: 60%; }
.confetti-piece:nth-child(9)  { left: 68%; }
.confetti-piece:nth-child(10) { left: 76%; }
.confetti-piece:nth-child(11) { left: 84%; }
.confetti-piece:nth-child(12) { left: 92%; }
.confetti-piece:nth-child(n+13) { animation-duration: 2.1s; }

@keyframes safha-confetti-fall {
    0%   { transform: translateY(0) rotate(0deg); opacity: 1; }
    100% { transform: translateY(220px) rotate(360deg); opacity: 0; }
}

@keyframes safha-celebrate-bounce {
    0%   { transform: scale(0.4); opacity: 0; }
    60%  { transform: scale(1.15); opacity: 1; }
    100% { transform: scale(1); }
}

/* ---- Position ring on "My reservations" ---- */

.pos-ring {
    flex-shrink: 0;
    width: 48px;
    height: 48px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Fraunces', serif;
    font-weight: 600;
    font-size: 1.1rem;
    color: #fff;
    background: linear-gradient(135deg, var(--pink), var(--violet));
    box-shadow: 0 4px 14px rgba(124, 92, 252, 0.4);
    animation: safha-pulse-ring 2.6s ease-out infinite;
}

/* ---- Stat chips (Overview page) ---- */

.stat-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.7rem;
    margin-bottom: 0.7rem;
}

.stat-chip {
    text-align: center;
    padding: 1.1rem 0.6rem;
}

.stat-num {
    font-family: 'Fraunces', serif;
    font-size: 1.9rem;
    font-weight: 600;
    background: linear-gradient(120deg, var(--violet), var(--pink));
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
}

.stat-label {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-top: 0.15rem;
}

/* ---- Top account bar ---- */

.side-name {
    font-family: 'Fraunces', serif;
    font-size: 1.2rem;
    color: var(--ink);
    font-weight: 600;
    margin-top: 0.5rem;
}

.side-email {
    font-size: 0.78rem;
    color: var(--ink-faint);
    margin-bottom: 0.6rem;
}

/* ---- Footer ---- */

.desk-footer {
    text-align: center;
    font-size: 0.8rem;
    color: var(--ink-faint);
    padding: 2rem 0 1rem 0;
    animation: safha-fade-in 0.8s ease 0.3s both;
}

.desk-footer .name {
    color: var(--violet);
    font-weight: 700;
}

/* ---- Streamlit widget re-skinning: inputs, selects, buttons ---- */

[data-testid="stTextInput"] input,
[data-testid="stDateInput"] input,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div,
[data-testid="stFileUploaderDropzone"] {
    background: var(--paper-raised) !important;
    backdrop-filter: blur(14px) !important;
    -webkit-backdrop-filter: blur(14px) !important;
    border: 1.5px solid rgba(124, 92, 252, 0.25) !important;
    border-radius: 14px !important;
    color: var(--ink) !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
}

[data-testid="stTextInput"] input:focus,
[data-testid="stDateInput"] input:focus {
    border-color: var(--violet) !important;
    box-shadow: 0 0 0 3px rgba(124, 92, 252, 0.15) !important;
}

[data-testid="stFileUploaderDropzone"]:hover {
    border-color: var(--pink) !important;
}

::placeholder { color: var(--ink-faint) !important; opacity: 1; }

.stButton button, .stFormSubmitButton button, .stDownloadButton button {
    background: linear-gradient(120deg, var(--violet), var(--pink)) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 999px !important;
    font-weight: 700 !important;
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    padding: 0.6rem 1.4rem !important;
    box-shadow: 0 6px 20px rgba(124, 92, 252, 0.32) !important;
    transition: transform 0.2s cubic-bezier(0.22, 1, 0.36, 1), box-shadow 0.2s ease !important;
}

.stButton button:hover, .stFormSubmitButton button:hover, .stDownloadButton button:hover {
    transform: translateY(-2px) scale(1.02) !important;
    box-shadow: 0 10px 28px rgba(124, 92, 252, 0.42) !important;
}

.stButton button:active, .stFormSubmitButton button:active, .stDownloadButton button:active {
    transform: scale(0.97) translateY(0) !important;
}

/* Secondary buttons (e.g. "Back") get a softer glass look instead of the
   gradient, so primary actions still stand out. */
.stButton button[kind="secondary"] {
    background: var(--paper-raised) !important;
    color: var(--violet) !important;
    border: 1.5px solid rgba(124, 92, 252, 0.3) !important;
    box-shadow: none !important;
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
    background: var(--paper-raised);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border: 1.5px solid rgba(124, 92, 252, 0.22);
    border-radius: 999px;
    padding: 0.5rem 1rem !important;
    transition: all 0.2s cubic-bezier(0.22, 1, 0.36, 1);
    cursor: pointer;
    font-weight: 600;
    color: var(--ink-dim);
}

[data-testid="stRadio"] label:hover {
    background: rgba(255, 255, 255, 0.8);
    border-color: var(--pink);
    transform: translateY(-1px);
}

[data-testid="stRadio"] input:checked + div {
    color: #ffffff !important;
    font-weight: 700 !important;
}

/* Highlight the whole pill (not just the text) for the checked option by
   targeting the label that contains a checked input. */
[data-testid="stRadio"] label:has(input:checked) {
    background: linear-gradient(120deg, var(--violet), var(--pink)) !important;
    border-color: transparent !important;
    box-shadow: 0 6px 18px rgba(124, 92, 252, 0.35);
}

[data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {
    display: none !important;
}

/* ---- Expanders (admin: manage individual reservations) ---- */

[data-testid="stExpander"] {
    background: var(--paper-card) !important;
    backdrop-filter: blur(18px) !important;
    -webkit-backdrop-filter: blur(18px) !important;
    border: 1.5px solid var(--glass-border) !important;
    border-radius: 18px !important;
    margin-bottom: 0.6rem !important;
    overflow: hidden;
}

[data-testid="stExpander"] summary {
    font-weight: 600 !important;
    color: var(--ink) !important;
}

/* ---- Dataframes / tables ---- */

[data-testid="stDataFrame"] {
    border-radius: 16px !important;
    overflow: hidden;
    border: 1.5px solid var(--glass-border) !important;
}

/* ---- Tabs (kept for compatibility if used anywhere) ---- */

[data-baseweb="tab-list"] {
    background: transparent !important;
    gap: 0.3rem;
}

[data-baseweb="tab"] {
    background: var(--paper-raised) !important;
    border-radius: 12px 12px 0 0 !important;
    color: var(--ink-dim) !important;
}

/* ---- Success / error / info banners ---- */

[data-testid="stAlert"] {
    border-radius: 16px !important;
    backdrop-filter: blur(14px) !important;
    -webkit-backdrop-filter: blur(14px) !important;
    border: 1.5px solid transparent !important;
}

/* ---- Keyframes ---- */

@keyframes safha-fade-up {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: translateY(0); }
}

@keyframes safha-fade-in {
    from { opacity: 0; }
    to   { opacity: 1; }
}

@keyframes safha-scale-in {
    from { opacity: 0; transform: scale(0.85); }
    to   { opacity: 1; transform: scale(1); }
}

@keyframes safha-glow-breathe {
    0%, 100% { opacity: 0.7; }
    50%      { opacity: 1; }
}

@keyframes safha-shimmer {
    0%, 100% { background-position: 0% 50%; }
    50%      { background-position: 100% 50%; }
}

@keyframes safha-pulse-ring {
    0%   { box-shadow: 0 0 0 0 rgba(255, 107, 157, 0.45); }
    70%  { box-shadow: 0 0 0 10px rgba(255, 107, 157, 0); }
    100% { box-shadow: 0 0 0 0 rgba(255, 107, 157, 0); }
}

@keyframes safha-underline-grow {
    from { transform: scaleX(0); }
    to   { transform: scaleX(1); }
}

/* ---- Global reduced-motion override ---- */

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


def render_celebration(book_label):
    """A short, joyful confetti-style celebration shown right after a
    reservation is successfully placed. Pure CSS animation, no JS
    needed — a burst of colorful pieces that fall and fade, plus a
    success message. Auto-removes itself from view via animation only
    (no timers), so it never risks leaving stray interactive elements."""
    st.markdown(f"""
    <div class="celebrate-wrap">
        <div class="celebrate-confetti">
            {''.join(f'<span class="confetti-piece c{i % 6}"></span>' for i in range(24))}
        </div>
        <div class="celebrate-card">
            <div class="celebrate-emoji">🎉</div>
            <div class="celebrate-title">Reservation confirmed!</div>
            <div class="celebrate-sub">You've joined the queue for<br><b>{book_label}</b></div>
        </div>
    </div>
    """, unsafe_allow_html=True)


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


def render_otp_input(widget_key, num_boxes=4):
    """Renders num_boxes individual digit boxes styled like an OTP entry,
    with auto-advance on type, backspace-to-previous, and paste support.
    Pure HTML5 + JS, no package needed. When all boxes are filled, the
    combined code is placed in the URL as ?passcode_<widget_key>=XXXX so
    Python can read it back via st.query_params on the next rerun — same
    bridge pattern used for the login token and the signature canvas."""
    box_ids = [f"otpbox_{widget_key}_{i}" for i in range(num_boxes)]
    boxes_html = "".join(f"""
        <input type="tel" inputmode="numeric" maxlength="1" id="{box_ids[i]}"
            data-idx="{i}"
            style="width:56px; height:64px; font-size:1.8rem; text-align:center;
                   border-radius:14px; border:2px solid rgba(124,92,252,0.3);
                   background:rgba(255,255,255,0.7); color:#2d2a4a; font-weight:700;
                   font-family:'Plus Jakarta Sans', sans-serif; outline:none;
                   transition: border-color 0.2s ease, box-shadow 0.2s ease;">
    """ for i in range(num_boxes))

    js_ids = ", ".join(f"'{bid}'" for bid in box_ids)

    components.html(f"""
        <div style="display:flex; gap:10px; justify-content:center; padding:8px 0;">
            {boxes_html}
        </div>
        <script>
        (function() {{
            const ids = [{js_ids}];
            const inputs = ids.map(id => document.getElementById(id));

            function submitIfComplete() {{
                const code = inputs.map(inp => inp.value).join('');
                if (code.length === {num_boxes}) {{
                    const url = new URL(window.parent.location.href);
                    url.searchParams.set('passcode_{widget_key}', code);
                    window.parent.location.replace(url.toString());
                }}
            }}

            inputs.forEach((inp, idx) => {{
                inp.addEventListener('focus', function() {{
                    inp.style.borderColor = '#7c5cfc';
                    inp.style.boxShadow = '0 0 0 3px rgba(124,92,252,0.15)';
                }});
                inp.addEventListener('blur', function() {{
                    inp.style.borderColor = 'rgba(124,92,252,0.3)';
                    inp.style.boxShadow = 'none';
                }});
                inp.addEventListener('input', function(e) {{
                    inp.value = inp.value.replace(/[^0-9]/g, '').slice(0, 1);
                    if (inp.value && idx < inputs.length - 1) {{
                        inputs[idx + 1].focus();
                    }}
                    submitIfComplete();
                }});
                inp.addEventListener('keydown', function(e) {{
                    if (e.key === 'Backspace' && !inp.value && idx > 0) {{
                        inputs[idx - 1].focus();
                    }}
                }});
                inp.addEventListener('paste', function(e) {{
                    e.preventDefault();
                    const paste = (e.clipboardData || window.clipboardData).getData('text').replace(/[^0-9]/g, '');
                    for (let i = 0; i < inputs.length; i++) {{
                        inputs[i].value = paste[i] || '';
                    }}
                    const lastFilled = Math.min(paste.length, inputs.length) - 1;
                    if (lastFilled >= 0) inputs[lastFilled].focus();
                    submitIfComplete();
                }});
            }});
            if (inputs[0]) inputs[0].focus();
        }})();
        </script>
    """, height=90)


def read_otp_code(widget_key, num_boxes=4):
    """Reads back a code entered via render_otp_input(). Returns the
    code string or None if nothing is waiting. Clears the param after
    reading so it isn't reprocessed on future reruns."""
    param_key = f"passcode_{widget_key}"
    code = st.query_params.get(param_key)
    if not code or len(code) != num_boxes:
        return None
    del st.query_params[param_key]
    return code


def render_signature_canvas(widget_key):
    """Draws a large HTML5 <canvas> the student can sign on with a mouse
    or a finger (touch), with Pen and Eraser modes plus Clear. Pure
    browser HTML5 + JS -- no package, no requirements.txt change. On
    "Use this drawing", the canvas is exported as a PNG data URL and put
    directly into the page URL as ?sig_drawn_<key>=<data-url>, which
    Python reads back on the next rerun via st.query_params -- the same
    bridge pattern used for login tokens. The canvas is kept at a
    moderate resolution (640x260) so it is comfortably large to draw on
    while the resulting PNG still stays well under browser URL-length
    limits."""
    components.html(f"""
        <div style="font-family: 'Plus Jakarta Sans', sans-serif;">
            <canvas id="sigpad_{widget_key}" width="640" height="260"
                style="width:100%; max-width:640px; height:260px; display:block;
                       background:#ffffff; border:2px dashed rgba(124,92,252,0.4);
                       border-radius:16px; touch-action:none; cursor:crosshair;">
            </canvas>
            <div style="margin-top:12px; display:flex; gap:8px; flex-wrap:wrap;">
                <button id="penbtn_{widget_key}" type="button" style="
                    flex:1; min-width:90px; padding:11px; border-radius:999px;
                    border:1.5px solid transparent;
                    background:linear-gradient(120deg,#7c5cfc,#ff6b9d); color:#fff;
                    font-weight:700; font-family:inherit; cursor:pointer;">
                    ✏️ Pen
                </button>
                <button id="erasebtn_{widget_key}" type="button" style="
                    flex:1; min-width:90px; padding:11px; border-radius:999px; border:1.5px solid rgba(124,92,252,0.3);
                    background:#ffffff; color:#7c5cfc; font-weight:700; font-family:inherit; cursor:pointer;">
                    🧽 Eraser
                </button>
                <button id="clearbtn_{widget_key}" type="button" style="
                    flex:1; min-width:90px; padding:11px; border-radius:999px; border:1.5px solid rgba(239,77,107,0.35);
                    background:#ffffff; color:#ef4d6b; font-weight:700; font-family:inherit; cursor:pointer;">
                    Clear
                </button>
            </div>
            <button id="usebtn_{widget_key}" type="button" style="
                width:100%; margin-top:8px; padding:12px; border-radius:999px; border:none;
                background:linear-gradient(120deg,#5b8def,#22b07d); color:#fff;
                font-weight:700; font-family:inherit; cursor:pointer;">
                ✓ Use this drawing
            </button>
        </div>
        <script>
        (function() {{
            const canvas = document.getElementById('sigpad_{widget_key}');
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';

            let mode = 'pen';
            let drawing = false;
            let hasDrawn = false;

            const penBtn = document.getElementById('penbtn_{widget_key}');
            const eraseBtn = document.getElementById('erasebtn_{widget_key}');

            function setMode(newMode) {{
                mode = newMode;
                if (mode === 'pen') {{
                    penBtn.style.background = 'linear-gradient(120deg,#7c5cfc,#ff6b9d)';
                    penBtn.style.color = '#fff';
                    penBtn.style.border = '1.5px solid transparent';
                    eraseBtn.style.background = '#ffffff';
                    eraseBtn.style.color = '#7c5cfc';
                    eraseBtn.style.border = '1.5px solid rgba(124,92,252,0.3)';
                }} else {{
                    eraseBtn.style.background = 'linear-gradient(120deg,#7c5cfc,#ff6b9d)';
                    eraseBtn.style.color = '#fff';
                    eraseBtn.style.border = '1.5px solid transparent';
                    penBtn.style.background = '#ffffff';
                    penBtn.style.color = '#7c5cfc';
                    penBtn.style.border = '1.5px solid rgba(124,92,252,0.3)';
                }}
            }}

            function pos(e) {{
                const rect = canvas.getBoundingClientRect();
                const scaleX = canvas.width / rect.width;
                const scaleY = canvas.height / rect.height;
                const clientX = e.touches ? e.touches[0].clientX : e.clientX;
                const clientY = e.touches ? e.touches[0].clientY : e.clientY;
                return {{ x: (clientX - rect.left) * scaleX, y: (clientY - rect.top) * scaleY }};
            }}
            function start(e) {{
                e.preventDefault();
                drawing = true;
                const p = pos(e);
                ctx.beginPath();
                ctx.moveTo(p.x, p.y);
                if (mode === 'pen') {{
                    ctx.globalCompositeOperation = 'source-over';
                    ctx.strokeStyle = '#2d2a4a';
                    ctx.lineWidth = 3;
                    hasDrawn = true;
                }} else {{
                    ctx.globalCompositeOperation = 'source-over';
                    ctx.strokeStyle = '#ffffff';
                    ctx.lineWidth = 28;
                }}
            }}
            function move(e) {{
                if (!drawing) return;
                e.preventDefault();
                const p = pos(e);
                ctx.lineTo(p.x, p.y);
                ctx.stroke();
            }}
            function end(e) {{
                drawing = false;
            }}
            canvas.addEventListener('mousedown', start);
            canvas.addEventListener('mousemove', move);
            canvas.addEventListener('mouseup', end);
            canvas.addEventListener('mouseleave', end);
            canvas.addEventListener('touchstart', start, {{passive:false}});
            canvas.addEventListener('touchmove', move, {{passive:false}});
            canvas.addEventListener('touchend', end);

            penBtn.addEventListener('click', function() {{ setMode('pen'); }});
            eraseBtn.addEventListener('click', function() {{ setMode('eraser'); }});

            document.getElementById('clearbtn_{widget_key}').addEventListener('click', function() {{
                ctx.fillStyle = '#ffffff';
                ctx.fillRect(0, 0, canvas.width, canvas.height);
                hasDrawn = false;
            }});

            document.getElementById('usebtn_{widget_key}').addEventListener('click', function() {{
                if (!hasDrawn) {{
                    alert('Please draw your signature first.');
                    return;
                }}
                const dataUrl = canvas.toDataURL('image/png');
                if (dataUrl.length > 90000) {{
                    alert('This signature is too detailed to save. Please clear and try a simpler signature.');
                    return;
                }}
                const url = new URL(window.parent.location.href);
                url.searchParams.set('sig_drawn_{widget_key}', dataUrl);
                window.parent.location.replace(url.toString());
            }});
        }})();
        </script>
    """, height=380)



def read_drawn_signature(widget_key):
    """Reads back a signature drawn via render_signature_canvas(), if
    one is waiting in the URL query params. Returns (base64_str,
    mime_type) or (None, None) if nothing is waiting. Also clears the
    param afterwards so it isn't reprocessed on every future rerun."""
    param_key = f"sig_drawn_{widget_key}"
    data_url = st.query_params.get(param_key)
    if not data_url:
        return None, None
    try:
        header, b64_data = data_url.split(",", 1)
        mime_type = header.split(":")[1].split(";")[0]
    except Exception:
        del st.query_params[param_key]
        return None, None
    del st.query_params[param_key]
    return b64_data, mime_type


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
        if acct and acct["verified"]:
            # Suspended accounts can still log in and view their status —
            # reservation actions are blocked separately, later in the app.
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
        render_header("Shaikh Zulqarnain · 10th A", "Safha", "Select your name and enter your 4-digit passcode.")

        render_field_label("Select your name")
        chosen_name = st.selectbox("Name", NAME_OPTIONS, label_visibility="collapsed", key="login_name_select")

        render_field_label("Enter your 4-digit passcode")
        otp_code = read_otp_code("login")
        render_otp_input("login")

        if otp_code:
            passcode_clean = otp_code.strip()
            if chosen_name == ADMIN_NAME:
                admin_pw = st.secrets.get("ADMIN_PASSWORD", None)
                if not admin_pw:
                    st.error(
                        "No admin passcode is configured. Set ADMIN_PASSWORD in your app's Secrets "
                        "(Streamlit Cloud → Settings → Secrets)."
                    )
                elif passcode_clean != str(admin_pw):
                    st.error("Incorrect passcode.")
                else:
                    acct = create_or_get_account(ADMIN_NAME, ADMIN_EMAIL, is_admin=True)
                    force_admin_flag(ADMIN_EMAIL)
                    token = generate_device_token()
                    mark_verified_with_token(ADMIN_EMAIL, token)
                    record_login(ADMIN_NAME)
                    st.session_state.account = get_account_by_email(ADMIN_EMAIL)
                    st.query_params["t"] = token
                    save_token_to_local_storage(token)
                    log_admin_action("login")
                    st.rerun()
            else:
                expected_key = secret_key_for_name(chosen_name)
                expected_pw = st.secrets.get(expected_key, None)
                if not expected_pw:
                    st.error(
                        f"No passcode is configured for {chosen_name} yet. "
                        f"Set {expected_key} in your app's Secrets."
                    )
                elif passcode_clean != str(expected_pw):
                    st.error("Incorrect passcode.")
                else:
                    # Suspended accounts can still log in (so they can see
                    # their status and reservation history) — reservation
                    # actions are blocked separately, later in the app.
                    synth_email = synthetic_email_for_name(chosen_name)
                    create_or_get_account(chosen_name, synth_email)
                    token = generate_device_token()
                    mark_verified_with_token(synth_email, token)
                    record_login(chosen_name)
                    st.session_state.account = get_account_by_email(synth_email)
                    st.query_params["t"] = token
                    save_token_to_local_storage(token)
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
            ["Overview", "All reservations", "Book availability", "Students", "Activity", "Export & log"],
            label_visibility="collapsed",
            horizontal=True,
        )
    else:
        nav = st.radio(
            "Navigate", ["Reserve a book", "My reservations", "My account"],
            label_visibility="collapsed", horizontal=True,
        )
with logout_col:
    if st.button("Log out", use_container_width=True):
        do_logout()
        st.rerun()

st.markdown('<div class="desk-header-rule"></div>', unsafe_allow_html=True)

items = all_book_items()
counts = reservation_counts_by_book()
disabled_book_ids = get_disabled_book_ids()

# ---- One-time notice: let a student know if admin cancelled, fulfilled,
# or marked returned one of their reservations since they last checked.
if not is_admin:
    pending_notice = get_unseen_notice_for_student(account["name"])
    if pending_notice:
        subj_n, item_n = pending_notice["book_id"].split("::")
        if pending_notice["status"] == "cancelled":
            st.warning(f"Your reservation for **{subj_n} — {item_n}** was cancelled by {ADMIN_NAME}.")
        elif pending_notice["status"] == "fulfilled" and not pending_notice["returned"]:
            st.info(f"Your reservation for **{subj_n} — {item_n}** has been fulfilled — you can collect it.")
        elif pending_notice["returned"]:
            st.success(f"Your return of **{subj_n} — {item_n}** has been recorded. Thank you!")
        if st.button("Got it, dismiss", key=f"dismiss_notice_{pending_notice['id']}"):
            mark_notice_seen(pending_notice["id"])
            st.rerun()

# ---- Signature gate: every student needs one signature on file before
# they can reserve anything. Asked once at first login; editable anytime
# from "My account". Admin is exempt (doesn't reserve books). ----------
if not is_admin and not account.get("signature_data") and nav == "Reserve a book":
    render_header("Safha", "Add Your Signature", "One-time setup — this signature is reused for every reservation you make.")
    MAX_SIGNATURE_BYTES = int(0.5 * 1024 * 1024)

    render_field_label("Upload a photo of your signature (max 500KB)")
    first_sig = st.file_uploader("Signature", type=["png", "jpg", "jpeg"], label_visibility="collapsed")
    if st.button("Save signature", use_container_width=True):
        if first_sig is None:
            st.error("Please upload a photo of your signature to continue.")
        elif first_sig.size > MAX_SIGNATURE_BYTES:
            st.error("That photo is over 500KB. Please upload a smaller image.")
        else:
            sig_b64, sig_type = signature_to_base64(first_sig)
            save_account_signature(account["email"], sig_b64, sig_type)
            st.session_state.account = get_account_by_email(account["email"])
            st.success("Signature saved — you're all set.")
            st.rerun()
    render_footer()
    st.stop()

# ---- Reserve a book ---------------------------------------------------

if nav == "Reserve a book":
    render_header("Safha", "Reserve a Book", "Pick a subject and book type to join the queue.")

    if account["suspended"]:
        st.markdown("""
        <div class="suspended-banner">
            🚫 Your account is suspended. You can browse, but reservations are turned off.
            Please contact Shaikh Zulqarnain.
        </div>
        """, unsafe_allow_html=True)

    render_field_label("Name")
    st.markdown(f'<div class="book-row"><div class="book-row-title">{account["name"]}</div></div>', unsafe_allow_html=True)
    st.markdown('<div class="row-spacer"></div>', unsafe_allow_html=True)

    if not account["suspended"]:
        with st.form(key="reserve_form", clear_on_submit=True, border=False):
            render_field_label("Subject")
            subject = st.selectbox(
                "Subject", list(SUBJECTS.keys()), label_visibility="collapsed", key="reserve_subject"
            )

            render_field_label("Book type")
            # Item types depend on the chosen subject, so this selectbox is
            # rebuilt from SUBJECTS every run based on the subject picked above.
            available_types = [
                it for it in SUBJECTS[subject]
                if f"{subject}::{it}" not in disabled_book_ids
            ]
            if not available_types:
                st.warning(f"All book types for {subject} are currently unavailable for reservation.")
                item_type = None
            else:
                item_type = st.selectbox(
                    "Book type", available_types, label_visibility="collapsed", key="reserve_item_type"
                )

            render_field_label("Needed by")
            tomorrow = datetime.date.today() + datetime.timedelta(days=1)
            needed_by = st.date_input(
                "Needed by", value=tomorrow,
                min_value=datetime.date.today(), key="reserve_date",
                label_visibility="collapsed"
            )

            submitted = st.form_submit_button("Join queue", use_container_width=True, disabled=(item_type is None))

            if submitted and item_type:
                book_id = f"{subject}::{item_type}"
                if book_id in disabled_book_ids:
                    st.error("This book type isn't currently available for reservation.")
                elif student_already_in_queue(book_id, account["name"]):
                    st.error("You're already in the queue for this item.")
                else:
                    create_reservation(
                        book_id, account["name"], needed_by.isoformat(),
                        account.get("signature_data"), account.get("signature_type")
                    )
                    st.session_state.celebrate = f"{subject} — {item_type}"
                    st.rerun()

    # ---- Celebration animation after a successful reservation ---------
    if st.session_state.get("celebrate"):
        render_celebration(st.session_state.celebrate)
        del st.session_state.celebrate

    st.markdown('<div class="section-label">Subjects</div>', unsafe_allow_html=True)
    for subj in SUBJECTS:
        with st.expander(subj):
            for it in SUBJECTS[subj]:
                bid = f"{subj}::{it}"
                waiting = counts.get(bid, 0)
                is_disabled = bid in disabled_book_ids
                badge_html = '<span class="queue-badge disabled">unavailable</span>' if is_disabled else render_queue_badge_html(waiting)
                st.markdown(f"""
                <div class="book-row">
                    <div class="book-row-main">
                        <div class="book-row-title">{it}</div>
                        {badge_html}
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
            reserved_display = to_ist_display(r["created_at"])
            st.markdown(f"""
            <div class="res-card">
                <div class="res-card-flex">
                    <div class="pos-ring">#{pos}</div>
                    <div>
                        <div class="res-card-title">{subject} — {item_type}</div>
                        <div class="res-card-meta">
                            <b>Name:</b> {account['name']} &nbsp;·&nbsp;
                            <b>Book Reserved:</b> {subject} — {item_type} &nbsp;·&nbsp;
                            <b>Needed by:</b> {date_display}<br>
                            <b>Reserved on:</b> {reserved_display}
                        </div>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            if st.button("Cancel this reservation", key=f"cancel_{r['id']}"):
                cancel_reservation(r["id"])
                st.toast("Reservation cancelled.", icon="✅")
                st.rerun()

# ---- My account (students only): view/replace signature on file ------

elif nav == "My account" and not is_admin:
    render_header("Safha", "My Account", "Your details and standing signature.")

    render_field_label("Name")
    if account["suspended"]:
        st.markdown(
            f'<div class="book-row"><div class="book-row-main">'
            f'<div class="book-row-title">{account["name"]}</div>'
            f'<span class="suspended-pill">🚫 Suspended</span>'
            f'</div></div>', unsafe_allow_html=True
        )
        st.markdown("""
        <div class="suspended-banner">
            Your account is suspended. You can still view your account and past
            reservations, but new reservations are turned off. Please contact
            Shaikh Zulqarnain if you think this is a mistake.
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="book-row"><div class="book-row-title">{account["name"]}</div></div>', unsafe_allow_html=True)
    st.markdown('<div class="row-spacer"></div>', unsafe_allow_html=True)

    render_field_label("Signature on file")
    if account.get("signature_data"):
        try:
            sig_bytes = base64.b64decode(account["signature_data"])
            st.image(sig_bytes, caption="Current signature", width=200)
        except Exception:
            st.caption("Unable to display current signature.")
    else:
        st.caption("No signature on file yet.")

    st.markdown('<div class="row-spacer"></div>', unsafe_allow_html=True)
    render_field_label("Replace signature (max 500KB)")
    MAX_SIGNATURE_BYTES = int(0.5 * 1024 * 1024)

    new_sig = st.file_uploader("New signature", type=["png", "jpg", "jpeg"], label_visibility="collapsed", key="acct_sig_uploader")
    if st.button("Update signature", use_container_width=True):
        if new_sig is None:
            st.error("Please choose a photo first.")
        elif new_sig.size > MAX_SIGNATURE_BYTES:
            st.error("That photo is over 500KB. Please upload a smaller image.")
        else:
            sig_b64, sig_type = signature_to_base64(new_sig)
            save_account_signature(account["email"], sig_b64, sig_type)
            st.session_state.account = get_account_by_email(account["email"])
            st.success("Signature updated.")
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
    student_accounts_for_stats = [a for a in accounts_for_stats if a["email"].lower() != ADMIN_EMAIL.lower()]
    with_sig = sum(1 for a in student_accounts_for_stats if a.get("signature_data"))
    missing_sig_count = len(student_accounts_for_stats) - with_sig
    sig_rate = round((with_sig / len(student_accounts_for_stats)) * 100) if student_accounts_for_stats else 0
    active_students = len(student_accounts_for_stats)
    suspended_count = sum(1 for a in student_accounts_for_stats if a["suspended"])

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

elif nav == "All reservations" and is_admin:
    render_header("Safha", "All Reservations", "Full control — view, fulfil, cancel, edit, or delete any record.")

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
                "Needed by": format_reservation_date(r["needed_by_date"]),
                "Reserved on (IST)": to_ist_display(r["created_at"]),
                "Status": r["status"],
                "Returned": "Yes" if r["returned"] else "No",
            })
        st.dataframe(
            table_rows,
            use_container_width=True,
            hide_index=True,
            column_order=["Name", "Book Reserved", "Needed by", "Reserved on (IST)", "Status", "Returned"],
        )

        st.markdown('<div class="section-label">Manage individual reservations</div>', unsafe_allow_html=True)
        for r in all_res:
            subject, item_type = r["book_id"].split("::")
            date_display = format_reservation_date(r["needed_by_date"])
            reserved_display = to_ist_display(r["created_at"])
            with st.expander(f"{r['student_name']} — {subject} ({item_type}) · {date_display} · {r['status']}"):
                st.markdown(f"""
                <div class="res-card">
                    <div class="res-card-meta">
                        <b>Name:</b> {r['student_name']} &nbsp;·&nbsp;
                        <b>Book Reserved:</b> {subject} — {item_type} &nbsp;·&nbsp;
                        <b>Needed by:</b> {date_display} &nbsp;·&nbsp;
                        <b>Reserved on:</b> {reserved_display}<br>
                        <b>Status:</b> {r['status']} &nbsp;·&nbsp;
                        <b>Returned:</b> {'Yes' if r['returned'] else 'No'}
                    </div>
                </div>
                """, unsafe_allow_html=True)

                sig_source = r["signature_data"] or None
                sig_caption = f"{r['student_name']}'s signature at time of reservation"
                if not sig_source:
                    student_acct = get_account_by_email(synthetic_email_for_name(r["student_name"]))
                    if student_acct and student_acct.get("signature_data"):
                        sig_source = student_acct["signature_data"]
                        sig_caption = f"{r['student_name']}'s current signature on file"
                if sig_source:
                    try:
                        sig_bytes = base64.b64decode(sig_source)
                        st.image(sig_bytes, caption=sig_caption, width=180)
                    except Exception:
                        st.caption("Signature: (unable to display)")
                else:
                    st.caption("Signature: not on file")

                if r["status"] == "waiting":
                    render_field_label("Queue actions")
                    qc1, qc2 = st.columns(2)
                    with qc1:
                        if st.button("Mark fulfilled", key=f"fulfil_{r['id']}", use_container_width=True):
                            mark_fulfilled(r["id"])
                            log_admin_action("mark_fulfilled", f"{r['student_name']} — {r['book_id']}")
                            st.toast(f"Marked fulfilled for {r['student_name']}.", icon="📗")
                            st.rerun()
                    with qc2:
                        if st.button("Cancel reservation", key=f"admincancel_{r['id']}", use_container_width=True):
                            cancel_reservation(r["id"], notify=True)
                            log_admin_action("cancel_reservation", f"{r['student_name']} — {r['book_id']}")
                            st.toast(f"Cancelled {r['student_name']}'s reservation.", icon="🚫")
                            st.rerun()

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
                        st.toast("Date updated.", icon="🗓️")
                        st.rerun()

                st.markdown('<div class="row-spacer"></div>', unsafe_allow_html=True)
                c1, c2 = st.columns(2)
                with c1:
                    if not r["returned"]:
                        if st.button("Mark returned", key=f"ret_{r['id']}", use_container_width=True):
                            mark_returned(r["id"], datetime.date.today().isoformat())
                            log_admin_action("mark_returned", f"{r['student_name']} — {r['book_id']}")
                            st.toast(f"{r['student_name']}'s return recorded.", icon="✅")
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
                        st.toast("Record deleted.", icon="🗑️")
                        st.rerun()

# ---- Admin: Book availability -----------------------------------------

elif nav == "Book availability" and is_admin:
    render_header("Safha", "Book Availability", "Control which subjects and book types can currently be reserved.")

    for subj in SUBJECTS:
        with st.expander(subj):
            for it in SUBJECTS[subj]:
                bid = f"{subj}::{it}"
                is_disabled = bid in get_disabled_book_ids()
                c1, c2 = st.columns([3, 1])
                with c1:
                    status_label = "🚫 Unavailable" if is_disabled else "✅ Available"
                    st.markdown(f"**{it}** · {status_label}")
                with c2:
                    if is_disabled:
                        if st.button("Enable", key=f"enable_{bid}", use_container_width=True):
                            set_book_disabled(bid, False)
                            log_admin_action("enable_book", bid)
                            st.toast(f"{subj} — {it} is now available.", icon="✅")
                            st.rerun()
                    else:
                        if st.button("Disable", key=f"disable_{bid}", use_container_width=True):
                            set_book_disabled(bid, True)
                            log_admin_action("disable_book", bid)
                            st.toast(f"{subj} — {it} is now unavailable.", icon="🚫")
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
            last_login = to_ist_display(a.get("last_login_at")) if a.get("last_login_at") else "never logged in"
            sig_status = "signature on file" if a.get("signature_data") else "no signature yet"
            status_pill = '<span class="suspended-pill">🚫 Suspended</span>' if a["suspended"] else ""
            st.markdown(
                f"**{a['name']}** {status_pill} · {sig_status}  \nLast login: {last_login}",
                unsafe_allow_html=True
            )
        with c2:
            if a["suspended"]:
                if st.button("Unsuspend", key=f"unsusp_{a['id']}", use_container_width=True):
                    set_suspended(a["email"], False)
                    log_admin_action("unsuspend", a["email"])
                    st.toast(f"{a['name']} unsuspended.", icon="✅")
                    st.rerun()
            else:
                if st.button("Suspend", key=f"susp_{a['id']}", use_container_width=True):
                    set_suspended(a["email"], True)
                    log_admin_action("suspend", a["email"])
                    st.toast(f"{a['name']} suspended.", icon="🚫")
                    st.rerun()

# ---- Admin: Activity (login + reservation timeline) -------------------

elif nav == "Activity" and is_admin:
    render_header("Safha", "Activity", "Every login and reservation, timestamped in IST.")

    st.markdown('<div class="section-label">Recent logins</div>', unsafe_allow_html=True)
    logins = get_login_history(limit=100)
    if not logins:
        st.markdown('<div class="empty-state">No logins recorded yet.</div>', unsafe_allow_html=True)
    else:
        login_rows = [
            {"Name": l["account_name"], "Logged in at (IST)": to_ist_display(l["timestamp"])}
            for l in logins
        ]
        st.dataframe(login_rows, use_container_width=True, hide_index=True)

    st.markdown('<div class="section-label">Recent reservations</div>', unsafe_allow_html=True)
    all_res_activity = get_all_reservations()
    if not all_res_activity:
        st.markdown('<div class="empty-state">No reservations recorded yet.</div>', unsafe_allow_html=True)
    else:
        res_rows = []
        for r in all_res_activity:
            subject, item_type = r["book_id"].split("::")
            res_rows.append({
                "Name": r["student_name"],
                "Book Reserved": f"{subject} — {item_type}",
                "Reserved at (IST)": to_ist_display(r["created_at"]),
                "Status": r["status"],
            })
        st.dataframe(res_rows, use_container_width=True, hide_index=True)

# ---- Admin: Export & log ---------------------------------------------

elif nav == "Export & log" and is_admin:
    render_header("Safha", "Export & Log", "Download reservation data and review recent admin activity.")

    all_res = get_all_reservations()
    if all_res:
        import csv
        import io
        buf = io.StringIO()
        fieldnames = ["Name", "Book Reserved", "Needed by", "Status", "Returned", "Returned On", "Reserved On (IST)"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_res:
            subject, item_type = r["book_id"].split("::")
            writer.writerow({
                "Name": r["student_name"],
                "Book Reserved": f"{subject} — {item_type}",
                "Needed by": r["needed_by_date"],
                "Status": r["status"],
                "Returned": "Yes" if r["returned"] else "No",
                "Returned On": r["returned_on"] or "",
                "Reserved On (IST)": to_ist_display(r["created_at"]),
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
        log_rows = [
            {"Time (IST)": to_ist_display(l["timestamp"]), "Action": l["action"], "Detail": l["detail"]}
            for l in logs
        ]
        st.dataframe(
            log_rows,
            use_container_width=True,
            hide_index=True,
            column_order=["Time (IST)", "Action", "Detail"],
        )

render_footer()
