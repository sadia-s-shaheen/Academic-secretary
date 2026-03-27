import sqlite3
import threading
from contextlib import contextmanager
from config import DB_PATH, DEFAULT_DIGEST_TIME, DEFAULT_TIMEZONE
_lock = threading.Lock()


@contextmanager
def get_db():
    """Thread-safe DB connection context manager."""
    with _lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # allows concurrent reads
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# =============================================
# SETUP
# =============================================

def setup_database():
    print("[DB] Setting up database...")
    with get_db() as conn:
        # Users table — source of truth for phone numbers
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                phone_number TEXT PRIMARY KEY,
                name         TEXT,
                timezone     TEXT    DEFAULT 'Asia/Kolkata',
                digest_time  TEXT    DEFAULT '08:00',
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Tasks linked to users via FK
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_phone   TEXT    NOT NULL REFERENCES users(phone_number) ON DELETE CASCADE,
                title        TEXT,
                subject      TEXT,
                task_type    TEXT,
                due_date     TEXT,
                original_msg TEXT,
                status       TEXT    DEFAULT 'Pending',
                priority     TEXT    DEFAULT 'medium',
                last_reminded DATETIME DEFAULT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Chat history
        conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_phone TEXT REFERENCES users(phone_number) ON DELETE CASCADE,
                role       TEXT,
                content    TEXT,
                timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    print("[DB] Setup complete.")


# =============================================
# USER HELPERS
# =============================================

def ensure_user_exists(phone_number: str):
    """Upsert a user row so FK constraints are satisfied."""
    with get_db() as conn:
        conn.execute('''
            INSERT OR IGNORE INTO users (phone_number, timezone, digest_time)
            VALUES (?, ?, ?)
        ''', (phone_number, DEFAULT_TIMEZONE, DEFAULT_DIGEST_TIME))


def get_user(phone_number: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE phone_number = ?", (phone_number,)
        ).fetchone()
    return dict(row) if row else None


def update_user_prefs(phone_number: str, digest_time: str = None, timezone: str = None):
    ensure_user_exists(phone_number)
    with get_db() as conn:
        if digest_time:
            conn.execute("UPDATE users SET digest_time = ? WHERE phone_number = ?",
                         (digest_time, phone_number))
        if timezone:
            conn.execute("UPDATE users SET timezone = ? WHERE phone_number = ?",
                         (timezone, phone_number))


# =============================================
# TASK HELPERS
# =============================================

def clean_old_completed_tasks():
    with get_db() as conn:
        conn.execute('''
            DELETE FROM tasks
            WHERE status = 'Done'
            AND due_date IS NOT NULL
            AND date(due_date) <= date('now', '-30 days')
        ''')


def get_formatted_records(phone_number: str):
    from ai_engine import _relative_date_label
    """
    Returns active tasks as a clean WhatsApp-friendly Markdown list.
    Excludes Done tasks older than 24 hours so the AI stays focused.
    """
    with get_db() as conn:
        rows = conn.execute('''
            SELECT * FROM tasks
            WHERE user_phone = ?
              AND NOT (
                    status = 'Done'
                    AND datetime(created_at) <= datetime('now', '-24 hours')
                  )
            ORDER BY
              CASE status WHEN 'Draft' THEN 0 WHEN 'Pending' THEN 1 ELSE 2 END,
              due_date ASC
        ''', (phone_number,)).fetchall()

    if not rows:
        return "No existing records."

    lines = []
    for r in rows:
        emoji = {"exam": "📝", "assignment": "📄", "lab_report": "🔬",
                 "project": "🗂️", "event": "📅", "task": "📌"}.get(r['task_type'], "📌")
        status_tag = " ✅" if r['status'] == 'Done' else (" 🔸Draft" if r['status'] == 'Draft' else "")
        due_str = f" (Due {r['due_date']})" if r['due_date'] and r['due_date'] != 'No due date' else ""
        subj = f" *{r['subject']}*" if r['subject'] else ""
        title = r['title'] or "Untitled"
                # Inside the for r in rows: loop
        rel_date = _relative_date_label(r['due_date']) if r['due_date'] else "No date"
        # format: [Natural Label] (Actual ISO Date)
        due_context = f" [Due: {rel_date} ({r['due_date']})]" if r['due_date'] else ""
        lines.append(f"{emoji} *{r['subject']}* {r['title']}{due_context} ID:{r['id']}")
        #lines.append(f"{emoji}{subj} {title}{due_str}{status_tag}  `ID:{r['id']}`")

    return "\n".join(lines)


def add_task(phone_number: str, subject: str, title: str, task_type: str,
             due_date: str, original_msg: str, status: str = 'Pending',
             priority: str = 'medium') -> int:
    ensure_user_exists(phone_number)
    with get_db() as conn:
        cursor = conn.execute('''
            INSERT INTO tasks (user_phone, subject, title, task_type, due_date,
                               original_msg, status, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (phone_number, subject, title, task_type, due_date,
              original_msg, status, priority))
        return cursor.lastrowid


def update_task(task_id: int, **fields):
    if not fields:
        return
    allowed = {"title", "subject", "task_type", "due_date", "status",
               "priority", "last_reminded"}
    safe = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not safe:
        return
    set_clause = ", ".join(f"{k} = ?" for k in safe)
    values = list(safe.values()) + [task_id]
    with get_db() as conn:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)


def remove_task(task_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def get_all_tasks_for_user(phone_number: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE user_phone = ? ORDER BY due_date ASC",
            (phone_number,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_tasks_for_user(phone_number: str) -> list[dict]:
    """Active (non-Done) tasks only."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE user_phone = ? AND status != 'Done' ORDER BY due_date ASC",
            (phone_number,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_stale_drafts(older_than_hours: int = 24) -> list[dict]:
    """Return Draft tasks created more than N hours ago."""
    with get_db() as conn:
        rows = conn.execute('''
            SELECT * FROM tasks
            WHERE status = 'Draft'
              AND datetime(created_at) <= datetime('now', ? || ' hours')
            ORDER BY created_at ASC
        ''', (f"-{older_than_hours}",)).fetchall()
    return [dict(r) for r in rows]


def get_all_active_users() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_phone FROM tasks WHERE status != 'Done'"
        ).fetchall()
    return [r["user_phone"] for r in rows]


# =============================================
# CHAT HISTORY HELPERS
# =============================================

def save_chat_message(phone_number: str, role: str, content: str):
    ensure_user_exists(phone_number)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_history (user_phone, role, content) VALUES (?, ?, ?)",
            (phone_number, role, content)
        )
        # Keep only the last 6 messages per user
        conn.execute('''
            DELETE FROM chat_history
            WHERE user_phone = ?
              AND id NOT IN (
                SELECT id FROM chat_history
                WHERE user_phone = ?
                ORDER BY id DESC
                LIMIT 10
              )
        ''', (phone_number, phone_number))


def get_chat_history(phone_number: str) -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE user_phone = ? ORDER BY id ASC",
            (phone_number,)
        ).fetchall()
    if not rows:
        return "No recent conversation."
    return "\n".join(f"{r['role'].capitalize()}: {r['content']}" for r in rows)


def get_last_user_message_time(phone_number: str):
    """Returns datetime of the user's last inbound message, or None."""
    with get_db() as conn:
        row = conn.execute('''
            SELECT timestamp FROM chat_history
            WHERE user_phone = ? AND role = 'user'
            ORDER BY id DESC LIMIT 1
        ''', (phone_number,)).fetchone()
    return row["timestamp"] if row else None
