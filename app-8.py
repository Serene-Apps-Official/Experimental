import os
import sqlite3
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import streamlit as st

# ============================================================
# Scheduled Gmail Sender - Streamlit
#
# IMPORTANT:
# - Use a Gmail App Password, NOT your normal Gmail password.
# - This app stores schedules in SQLite.
# - For reliable cloud delivery when nobody is using the page,
#   run this app's "worker" mode from an external scheduler.
#
# Environment / Streamlit secrets:
#   GMAIL_ADDRESS = "yourgmail@gmail.com"
#   GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
#
# Optional:
#   DB_PATH = "scheduled_emails.db"
# ============================================================

st.set_page_config(
    page_title="Cloud Mail Scheduler",
    page_icon="📨",
    layout="centered",
)

IST = ZoneInfo("Asia/Kolkata")

# Use an absolute path next to this script so the database resolves to the
# same file regardless of the process's current working directory (this
# avoids ambiguity between the interactive app and any automated/worker
# request hitting the same deployment).
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(_APP_DIR, "scheduled_emails.db"))


def get_secret(name: str, default: str = "") -> str:
    """Read a secret from environment variables or Streamlit secrets."""
    value = os.getenv(name)
    if value:
        return value

    try:
        value = st.secrets.get(name, "")
        if value:
            return str(value)
    except Exception:
        pass

    return default


GMAIL_ADDRESS = get_secret("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = get_secret("GMAIL_APP_PASSWORD")


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode allows readers and writers to work concurrently without
    # blocking each other, which matters here since the interactive UI
    # and the automated worker ping can both hit the database close in time.
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            scheduled_at_utc TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            sent_at_utc TEXT,
            error TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def save_email(recipient, subject, body, scheduled_at_utc):
    conn = get_connection()
    cur = conn.execute(
        """
        INSERT INTO scheduled_emails
        (recipient, subject, body, scheduled_at_utc, created_at_utc, status)
        VALUES (?, ?, ?, ?, ?, 'scheduled')
        """,
        (
            recipient,
            subject,
            body,
            scheduled_at_utc.isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    email_id = cur.lastrowid
    conn.close()
    return email_id


def get_scheduled_emails():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT *
        FROM scheduled_emails
        WHERE status = 'scheduled'
        ORDER BY scheduled_at_utc ASC
        """
    ).fetchall()
    conn.close()
    return rows


def get_recent_emails(limit=50):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT *
        FROM scheduled_emails
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def cancel_email(email_id):
    conn = get_connection()
    conn.execute(
        """
        UPDATE scheduled_emails
        SET status = 'cancelled'
        WHERE id = ? AND status = 'scheduled'
        """,
        (email_id,),
    )
    conn.commit()
    conn.close()


def claim_due_email(email_id):
    """
    Atomically changes a due email from scheduled -> sending.
    This prevents two worker instances from sending the same email.
    """
    conn = get_connection()
    cur = conn.execute(
        """
        UPDATE scheduled_emails
        SET status = 'sending'
        WHERE id = ?
          AND status = 'scheduled'
          AND scheduled_at_utc <= ?
        """,
        (email_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    changed = cur.rowcount == 1
    conn.close()
    return changed


def mark_sent(email_id):
    conn = get_connection()
    conn.execute(
        """
        UPDATE scheduled_emails
        SET status = 'sent',
            sent_at_utc = ?,
            error = NULL
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), email_id),
    )
    conn.commit()
    conn.close()


def mark_failed(email_id, error):
    conn = get_connection()
    conn.execute(
        """
        UPDATE scheduled_emails
        SET status = 'failed',
            error = ?
        WHERE id = ?
        """,
        (str(error)[:2000], email_id),
    )
    conn.commit()
    conn.close()


def send_gmail(recipient, subject, body):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "GMAIL_ADDRESS or GMAIL_APP_PASSWORD is not configured."
        )

    msg = EmailMessage()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def process_due_emails():
    """
    Process all emails whose scheduled time has arrived.

    Run this function from a cron/GitHub Actions/other scheduler.
    """
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    rows = conn.execute(
        """
        SELECT *
        FROM scheduled_emails
        WHERE status = 'scheduled'
          AND scheduled_at_utc <= ?
        ORDER BY scheduled_at_utc ASC
        """,
        (now,),
    ).fetchall()
    conn.close()

    results = []

    for row in rows:
        email_id = row["id"]

        if not claim_due_email(email_id):
            continue

        try:
            send_gmail(
                row["recipient"],
                row["subject"],
                row["body"],
            )
            mark_sent(email_id)
            results.append((email_id, "sent", None))
        except Exception as exc:
            mark_failed(email_id, exc)
            results.append((email_id, "failed", str(exc)))

    return results


def worker_mode():
    st.title("⚙️ Email Worker")
    st.caption("Processes emails whose scheduled time has arrived.")

    # Require a secret key so random visitors can't trigger sends or see
    # the Gmail account. Set WORKER_SECRET in Streamlit secrets and pass
    # it as ?worker=1&key=... when calling this URL.
    worker_secret = get_secret("WORKER_SECRET")
    provided_key = query_params.get("key", "")

    if worker_secret and provided_key != worker_secret:
        st.error("Invalid or missing key.")
        return

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        st.error("Gmail credentials are not configured.")
        st.code(
            'GMAIL_ADDRESS = "yourgmail@gmail.com"\n'
            'GMAIL_APP_PASSWORD = "your-16-character-app-password"',
            language="toml",
        )
        return

    # Auto-run on page load so an external pinger (e.g. GitHub Actions
    # hitting this URL on a schedule) doesn't need to click anything.
    with st.spinner("Processing..."):
        results = process_due_emails()

    if not results:
        st.info("There are no emails due right now.")
    else:
        for email_id, status, error in results:
            if status == "sent":
                st.success(f"Email #{email_id} sent successfully.")
            else:
                st.error(f"Email #{email_id} failed: {error}")

    if st.button("🔁 Check again"):
        st.rerun()

    st.caption(f"Gmail account: {GMAIL_ADDRESS}")
    st.caption(f"Checked at: {datetime.now(timezone.utc).isoformat()}")


def main_app():
    st.title("📨 Cloud Mail Scheduler")
    st.caption("Schedule an email to be sent through Gmail.")

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        st.warning("Gmail is not configured yet.")
        st.markdown(
            """
### Configure Gmail

Create a Streamlit secret named:

```toml
GMAIL_ADDRESS = "yourgmail@gmail.com"
GMAIL_APP_PASSWORD = "your-16-character-app-password"
```

Use a **Google App Password**. Your normal Gmail password will not work
with this SMTP setup.
"""
        )

    with st.form("schedule_form", clear_on_submit=True):
        st.subheader("Schedule an email")

        recipient = st.text_input(
            "Recipient email",
            placeholder="recipient@example.com",
        )

        subject = st.text_input(
            "Subject",
            placeholder="Your scheduled email",
        )

        body = st.text_area(
            "Message",
            placeholder="Write your message here...",
            height=180,
        )

        col1, col2 = st.columns(2)

        with col1:
            selected_date = st.date_input(
                "Date",
                value=datetime.now(IST).date(),
                min_value=datetime.now(IST).date(),
            )

        with col2:
            selected_time = st.time_input(
                "Time (IST)",
                value=datetime.now(IST).replace(
                    second=0,
                    microsecond=0,
                ).time(),
            )

        submitted = st.form_submit_button(
            "📅 Schedule Email",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
            st.error("Configure Gmail credentials before scheduling.")
            return

        if not recipient.strip():
            st.error("Please enter a recipient email.")
            return

        if "@" not in recipient or "." not in recipient.split("@")[-1]:
            st.error("Please enter a valid recipient email.")
            return

        if not subject.strip():
            st.error("Please enter a subject.")
            return

        if not body.strip():
            st.error("Please enter a message.")
            return

        local_dt = datetime.combine(
            selected_date,
            selected_time,
        ).replace(tzinfo=IST)

        scheduled_utc = local_dt.astimezone(timezone.utc)

        if scheduled_utc <= datetime.now(timezone.utc):
            st.error("Please select a future date and time.")
            return

        email_id = save_email(
            recipient.strip(),
            subject.strip(),
            body,
            scheduled_utc,
        )

        st.success(
            f"Email #{email_id} scheduled for "
            f"{local_dt.strftime('%d %B %Y, %I:%M %p')} IST."
        )

    st.divider()

    st.subheader("🗓️ Scheduled emails")

    rows = get_scheduled_emails()

    if not rows:
        st.info("No scheduled emails.")
    else:
        for row in rows:
            utc_dt = datetime.fromisoformat(row["scheduled_at_utc"])
            local_dt = utc_dt.astimezone(IST)

            with st.container(border=True):
                col_a, col_b = st.columns([4, 1])

                with col_a:
                    st.markdown(f"**{row['subject']}**")
                    st.caption(
                        f"To: {row['recipient']}  •  "
                        f"{local_dt.strftime('%d %b %Y, %I:%M %p')} IST"
                    )

                with col_b:
                    if st.button(
                        "Cancel",
                        key=f"cancel_{row['id']}",
                    ):
                        cancel_email(row["id"])
                        st.rerun()

    st.divider()

    with st.expander("📜 Recent email history"):
        history = get_recent_emails()

        if not history:
            st.info("No email history yet.")
        else:
            for row in history:
                status = row["status"].upper()
                st.markdown(
                    f"**#{row['id']} — {status}**  \n"
                    f"{row['subject']} → {row['recipient']}"
                )

                if row["error"]:
                    st.error(row["error"])

                st.divider()


init_db()

# Open the worker by adding ?worker=1 to the URL.
query_params = st.query_params

if query_params.get("worker") == "1":
    worker_mode()
else:
    main_app()
