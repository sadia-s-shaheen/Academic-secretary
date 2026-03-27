import threading
import time
import schedule
from datetime import datetime, timezone, timedelta

from config import PULSE_INTERVAL_SECONDS, TEST_MODE
from database import (
    get_all_active_users,
    get_pending_tasks_for_user,
    get_stale_drafts,
    update_task,
    save_chat_message,
    get_last_user_message_time,
    get_user,
)
from ai_engine import (
    get_ist_now,
    _relative_date_label,
    generate_ai_reminder,
    generate_draft_clarification,
    generate_daily_digest,
)


# Imported at runtime to avoid circular imports
_send_fn = None


def register_send_function(fn):
    global _send_fn
    _send_fn = fn


def _send(phone: str, text: str):
    if _send_fn:
        _send_fn(phone, text)
    else:
        print(f"[PULSE] (no send fn) → {phone}: {text}")


# =============================================
# URGENCY SCORING  (non-draft tasks only)
# =============================================

def days_until_due(due_date_str: str) -> int | None:
    try:
        due = datetime.fromisoformat(due_date_str)
        now = get_ist_now()
        return (due.replace(tzinfo=None) - now.replace(tzinfo=None)).days
    except Exception:
        return None


def calculate_urgency_score(task: dict) -> int:
    due_days = days_until_due(task.get("due_date", ""))
    if due_days is None:
        return 0

    if due_days > 7:    base = 0
    elif 5 <= due_days <= 7: base = 1
    elif 3 <= due_days <= 4: base = 2
    elif due_days == 2:  base = 3
    elif due_days == 1:  base = 4
    elif due_days == 0:  base = 5
    else:               base = 6   # overdue

    priority_mult = 1.5 if task.get("priority") == "high" else 1.0 if task.get("priority") == "medium" else 0.7
    type_weight   = 2   if task.get("task_type") == "exam" else 1 if task.get("task_type") in ("assignment", "project") else 0
    overdue_pen   = 3   if due_days < 0 else 0

    return int(base * priority_mult + type_weight + overdue_pen)


def get_reminder_tier(score: int) -> str:
    if score == 0:   return "none"
    if score <= 2:   return "early"
    if score <= 4:   return "standard"
    if score == 5:   return "urgent"
    if score == 6:   return "critical"
    return "overdue"


def should_send_reminder(task: dict, score: int) -> bool:
    if score == 0:
        return False
    last_str = task.get("last_reminded")
    if not last_str or TEST_MODE:
        return True
    last = datetime.fromisoformat(last_str)
    hours_since = (get_ist_now().replace(tzinfo=None) - last.replace(tzinfo=None)).total_seconds() / 3600
    if score <= 2:   return hours_since > 24
    elif score <= 5: return hours_since > 12
    else:            return hours_since > 6


# =============================================
# 24-HOUR WINDOW GUARD
# =============================================

def _within_24h_window(phone: str) -> bool:
    """
    Returns True only if the user has messaged in the last 24 hours.
    WhatsApp Business API requires an inbound message within 24h before
    sending outbound messages outside approved templates.
    """
    last_ts = get_last_user_message_time(phone)
    if not last_ts:
        return False
    try:
        last_dt = datetime.fromisoformat(last_ts)
        hours_since = (datetime.utcnow() - last_dt.replace(tzinfo=None)).total_seconds() / 3600
        return hours_since <= 24
    except Exception:
        return False


# =============================================
# PULSE SCAN — REMINDERS
# =============================================

def _run_reminder_scan():
    users = get_all_active_users()
    for user in users:
        phone = user['phone_number']
        
        # 1. 24h WhatsApp Window Check
        if not _within_24h_window(phone):
            continue

        tasks = get_pending_tasks_for_user(phone)
        for t in tasks:
            score = calculate_urgency_score(t)
            
            # 2. 12-Hour Cooldown Check
            # Ensure we haven't reminded them about THIS task in the last 12 hours
            last_sent = t.get('last_reminded_at') # You may need to add this col to DB
            if last_sent:
                last_dt = datetime.fromisoformat(last_sent)
                if get_ist_now() - last_dt < timedelta(hours=1):
                    continue # Skip if sent recently

            if score >= 5: # Threshold for reminder
                category = "urgent" if score < 8 else "overdue"
                message = generate_ai_reminder(t, category)
                
                _send(phone, message)
                
                # 3. RECORD THE SEND
                # Update the task so we don't spam it again for 12 hours
                update_task(t['id'], last_reminded_at=get_ist_now().isoformat())


# =============================================
# DRAFT CLARIFICATION SCAN
# =============================================

def _run_draft_scan():
    stale_drafts = get_stale_drafts(older_than_hours=24)
    sent = skipped = 0

    for task in stale_drafts:
        phone = task["user_phone"]
        if not _within_24h_window(phone):
            skipped += 1
            continue

        message = generate_draft_clarification(task)
        _send(phone, message)
        save_chat_message(phone, "assistant", message)

        # Mark last_reminded so we don't spam every scan
        update_task(task["id"], last_reminded=datetime.now().isoformat())
        sent += 1
        print(f"[PULSE] Sent draft clarification for task {task['id']} → {phone}")

    print(f"[PULSE] Draft scan done. Sent={sent}, Skipped(window)={skipped}")


# =============================================
# DAILY DIGEST
# =============================================

def _run_digest():
    """Send each user their morning digest if current time matches their preference."""
    from database import get_all_active_users, get_pending_tasks_for_user, get_user

    now = get_ist_now()
    current_hhmm = now.strftime("%H:%M")
    users = get_all_active_users()

    for phone in users:
        user = get_user(phone)
        digest_time = (user or {}).get("digest_time", "08:00")

        # Only send within a 10-minute window of the user's digest time
        try:
            dh, dm = map(int, digest_time.split(":"))
            digest_dt = now.replace(hour=dh, minute=dm, second=0, microsecond=0)
            diff_minutes = abs((now - digest_dt).total_seconds()) / 60
        except Exception:
            continue

        if diff_minutes > 10:
            continue   # Not digest time yet

        if not _within_24h_window(phone):
            print(f"[PULSE] Digest skipped for {phone} — window closed.")
            continue

        tasks = get_pending_tasks_for_user(phone)
        message = generate_daily_digest(tasks)
        _send(phone, message)
        save_chat_message(phone, "assistant", message)
        print(f"[PULSE] Sent daily digest → {phone}")


# =============================================
# COMBINED FULL SCAN
# =============================================

def pulse_scan():
    print(f"[PULSE] 🔄 Full scan — {get_ist_now().strftime('%Y-%m-%d %H:%M')}")
    _run_reminder_scan()
    _run_draft_scan()
    _run_digest()
    print("[PULSE] ✅ Full scan complete.")


# =============================================
# START ENGINE
# =============================================

def start_pulse_engine(send_function=None):
    if send_function:
        register_send_function(send_function)

    mode_label = "TEST MODE" if TEST_MODE else "PRODUCTION"
    print(f"[PULSE] Engine starting — {mode_label}, interval={PULSE_INTERVAL_SECONDS}s")

    schedule.every(PULSE_INTERVAL_SECONDS).seconds.do(pulse_scan)

    # Also run once at startup so we don't wait a full interval
    pulse_scan()

    while True:
        schedule.run_pending()
        time.sleep(10)
