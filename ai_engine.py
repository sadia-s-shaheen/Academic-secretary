import json
from datetime import datetime, timezone, timedelta
from groq import Groq
from config import GROQ_API_KEY, REACTIVE_MODEL, PULSE_MODEL, FAKE_DATE_OFFSET_DAYS
from database import (add_task, update_task, remove_task,
                      get_formatted_records, get_chat_history)

ai_client = Groq(api_key=GROQ_API_KEY)


# =============================================
# TIME HELPERS
# =============================================

def get_ist_now() -> datetime:
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    if FAKE_DATE_OFFSET_DAYS != 0:
        now += timedelta(days=FAKE_DATE_OFFSET_DAYS)
    return now


def get_current_date() -> str:
    return get_ist_now().strftime("%A, %B %d, %Y")


def _relative_date_label(due_date_str: str) -> str:
    """Convert YYYY-MM-DD to a human-friendly label for WhatsApp messages."""
    try:
        due = datetime.fromisoformat(due_date_str)
        today = get_ist_now().replace(hour=0, minute=0, second=0, microsecond=0)
        delta = (due.replace(tzinfo=None) - today.replace(tzinfo=None)).days
        if delta == 0: return "Today"
        if delta == 1: return "Tomorrow"
        if delta == -1: return "Yesterday"
        if 2 <= delta <= 6: return due.strftime("This %A")
        if 7 <= delta <= 13: return due.strftime("Next %A")
        # Only include year if it's 2027+
        if due.year >= 2027:
            return due.strftime("%-d %B %Y")
        return due.strftime("%-d %B")
    except Exception:
        return due_date_str


# =============================================
# SYSTEM PROMPT
# =============================================

def create_system_prompt() -> str:
    now = get_ist_now()
    today_str = now.strftime("%A, %B %d, %Y")
    today_date = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    day_name = now.strftime("%A")

    return f"""You are an Academic Secretary AI for college students.
TODAY: {today_str} ({day_name})

RESPONSE FORMAT — always valid JSON, nothing else:
{{
  "db_actions": [
    {{
      "intent": "ADD|UPDATE|REMOVE|QUERY|CHAT",
      "record_id": integer or null,
      "title": "string or null",
      "subject": "string or null",
      "task_type": "exam|assignment|lab_report|project|event|task or null",
      "due_date": "YYYY-MM-DD or null",
      "due_time": "HH:MM or null",
      "priority": "high|medium|low or null",
      "status": "Draft|Pending|Done or null"
    }}
  ],
  "assistant_reply": "reply string"
}}

DECISION PROCESS:
1. CLASSIFY: task-input | query | status-update | conversation | multiple | delete | clear multiple
2. EXTRACT: title, subject, task_type, due_date, priority from message
3. INFER what you can (rules below)
4. COMPLETENESS: if due_date missing -> set status=Draft + ask for it in reply
5. DUPLICATE CHECK: if same subject+type+date exists -> warn, don't silently add
6. EXECUTE + reply briefly
7. DELETE more than 3: ask clarification first.

INFERENCE RULES:
- task_type: exam/test/viva/quiz/midsem/endsem/practical -> exam
             assignment/homework/hw/submission/submit -> assignment
             lab/lab report -> lab_report | project -> project
             fest/event/seminar -> event | else -> task
- priority: exam or project -> high | due within 2 days -> bump up | default -> medium
- due_date: today -> {today_date} | tomorrow/tmrw -> {tomorrow}
            "this [weekday]" -> next occurrence of that weekday
            "next week" alone -> next Monday AND ask which day
- subject: phy/phys -> Physics | chem -> Chemistry | maths/math/m1/m2 -> Mathematics
           unknown abbreviation -> keep as-is

SLANG/TYPOS: understand intent, not literal text. "asignment"=assignment, "tmrw"=tomorrow.
MULTI-INTENT: one message with N tasks -> N db_actions.
IMPLICIT DONE: "finished X" -> UPDATE matching record to Done. If ambiguous -> ask which one.

REPLY FORMAT:
- Use WhatsApp Markdown list.
- Dates in replies: use relative labels (Today, Tomorrow, "this Friday", "14 April"), never include year unless 2027+
- Tone: brief, warm, professional. Max 2–3 sentences.
- Do NOT repeat back every field. Just confirm what matters."""


# =============================================
# USER PROMPT
# =============================================

def create_user_prompt(phone_number: str, user_message: str) -> str:
    today = get_current_date()
    day_name = get_ist_now().strftime("%A")
    existing = get_formatted_records(phone_number)
    history = get_chat_history(phone_number)

    return f"""TODAY: {today} ({day_name})

━━━ EXISTING RECORDS ━━━
{existing}

━━━ RECENT CONVERSATION ━━━
{history}

━━━ USER MESSAGE ━━━
{user_message}

Process this message. Follow the decision process. Return JSON only."""


# =============================================
# CORE AI CALL
# =============================================

def talk_to_ai(user_prompt: str) -> dict:
    try:
        response = ai_client.chat.completions.create(
            messages=[
                {"role": "system", "content": create_system_prompt()},
                {"role": "user", "content": user_prompt}
            ],
            model=REACTIVE_MODEL,
            temperature=0.2,
            max_tokens=2000,
            response_format={"type": "json_object"}
        )
        raw = response.choices[0].message.content.strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[AI] Error calling Groq: {e}")
        return {"assistant_reply": "Sorry, something went wrong. Can you repeat?", "db_actions": []}


# =============================================
# EXECUTE DB ACTIONS
# =============================================

def execute_db_actions(phone_number: str, ai_response: dict, original_msg: str):
    for item in ai_response.get("db_actions", []):
        intent = item.get("intent", "CHAT").upper()

        if intent in ("CHAT", "QUERY", "ERROR"):
            continue

        if intent == "ADD":
            add_task(
                phone_number=phone_number,
                subject=item.get("subject") or "General",
                title=item.get("title") or "Untitled Task",
                task_type=item.get("task_type") or "task",
                due_date=item.get("due_date") or "No due date",
                original_msg=original_msg,
                status=item.get("status") or "Pending",
                priority=item.get("priority") or "medium"
            )

        elif intent == "UPDATE":
            record_id = item.get("record_id")
            if record_id:
                update_task(record_id,
                            title=item.get("title"),
                            subject=item.get("subject"),
                            task_type=item.get("task_type"),
                            due_date=item.get("due_date"),
                            status=item.get("status"),
                            priority=item.get("priority"))

        elif intent == "REMOVE":
            record_id = item.get("record_id")
            if record_id:
                remove_task(record_id)


# =============================================
# REMINDER AI GENERATION
# =============================================

REMINDER_SYSTEM = """You are a friendly academic secretary AI sending a WhatsApp reminder to a college student.
Write a single short, natural-sounding reminder message (1–2 sentences, max 30 words).
Vary the phrasing — don't sound robotic. Use a warm tone. Use WhatsApp Markdown sparingly (*bold* for subject/title only).
Return ONLY the message text, nothing else."""

def generate_ai_reminder(task: dict, tier: str, urgency_score: int) -> str:
    """Use AI to craft a varied, human-sounding reminder nudge."""
    due_label = _relative_date_label(task.get("due_date", "")) if task.get("due_date") else "soon"
    task_desc = f"{task.get('subject', '')} {task.get('title', 'task')}".strip()

    tone_hints = {
        "early":    "casual, just a heads-up",
        "standard": "friendly reminder",
        "urgent":   "a bit pressing, due tomorrow",
        "critical": "very urgent, due today",
        "overdue":  "gentle but concerned — it's past due",
    }
    hint = tone_hints.get(tier, "friendly reminder")

    prompt = (
        f"Task: *{task_desc}* | Due: {due_label} | Urgency: {hint}\n"
        f"Write a short WhatsApp reminder nudge for this student."
    )

    try:
        response = ai_client.chat.completions.create(
            messages=[
                {"role": "system", "content": REMINDER_SYSTEM},
                {"role": "user", "content": prompt}
            ],
            model=PULSE_MODEL,
            temperature=0.8,
            max_tokens=80
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[AI] Reminder generation failed: {e}")
        # Fallback static messages
        fallbacks = {
            "early":    f"🌼 Heads-up: *{task_desc}* is due {due_label}.",
            "standard": f"📝 Reminder: *{task_desc}* due {due_label}.",
            "urgent":   f"⚠️ *{task_desc}* is due tomorrow!",
            "critical": f"🚨 *{task_desc}* is due TODAY!",
            "overdue":  f"⏰ *{task_desc}* was due {due_label} — did you finish?",
        }
        return fallbacks.get(tier, f"Reminder: *{task_desc}* due {due_label}.")


# =============================================
# DRAFT CLARIFICATION AI
# =============================================

DRAFT_SYSTEM = """You are a friendly academic secretary AI for a college student on WhatsApp.
A task was saved as a Draft because the due date was missing.
Write a single short, casual message (1–2 sentences) asking the student to clarify the due date for this task.
Return ONLY the message text."""

def generate_draft_clarification(task: dict) -> str:
    task_desc = f"{task.get('subject', '')} {task.get('title', 'task')}".strip()
    prompt = f"Draft task: *{task_desc}* (ID: {task['id']}). Ask the student for the due date."
    try:
        response = ai_client.chat.completions.create(
            messages=[
                {"role": "system", "content": DRAFT_SYSTEM},
                {"role": "user", "content": prompt}
            ],
            model=PULSE_MODEL,
            temperature=0.7,
            max_tokens=60
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[AI] Draft clarification failed: {e}")
        return f"Hey! When is your *{task_desc}* due? It's still saved as a draft. 📝"


# =============================================
# DAILY DIGEST AI
# =============================================

DIGEST_SYSTEM = """You are an academic secretary AI. Generate a concise, friendly morning WhatsApp digest for a college student.
Format:
- Start with "Good morning! 🌅 Here's your day:"
- 1-line summary of counts (e.g. "2 pending tasks, 1 exam today, nothing overdue")
- Then a clean bullet list of today's/urgent tasks
- Close with one short motivational line
Use WhatsApp Markdown. Keep it under 200 words. Return ONLY the message text."""

def generate_daily_digest(tasks: list[dict]) -> str:
    """Generate an AI-crafted morning digest from a user's task list."""
    if not tasks:
        return "Good morning! 🌅 You're all clear — no pending tasks. Enjoy your day! ☀️"

    task_summary = []
    for t in tasks:
        due_label = _relative_date_label(t["due_date"]) if t.get("due_date") else "No date"
        task_summary.append(
            f"- {t.get('subject', '')} {t.get('title', '')} ({t.get('task_type', 'task')}) — {due_label} [{t['status']}]"
        )

    prompt = "Student's tasks:\n" + "\n".join(task_summary)

    try:
        response = ai_client.chat.completions.create(
            messages=[
                {"role": "system", "content": DIGEST_SYSTEM},
                {"role": "user", "content": prompt}
            ],
            model=PULSE_MODEL,
            temperature=0.6,
            max_tokens=300
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[AI] Digest generation failed: {e}")
        lines = ["Good morning! 🌅 Here's your day:\n"]
        for t in tasks:
            due_label = _relative_date_label(t["due_date"]) if t.get("due_date") else "No date"
            emoji = {"exam": "📝", "assignment": "📄", "lab_report": "🔬",
                     "project": "🗂️"}.get(t.get("task_type"), "📌")
            lines.append(f"{emoji} *{t.get('subject', '')} {t.get('title', '')}* — {due_label}")
        return "\n".join(lines)
