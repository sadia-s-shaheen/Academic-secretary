import threading
from flask import Flask, request
import requests

from config import META_TOKEN, PHONE_NUMBER_ID, VERIFY_TOKEN
from database import setup_database, clean_old_completed_tasks, save_chat_message
from ai_engine import (
    create_user_prompt,
    talk_to_ai,
    execute_db_actions,
    get_ist_now,
)
from pulse import start_pulse_engine

app = Flask(__name__)


# =============================================
# META / WHATSAPP MESSAGING
# =============================================

def send_meta_message(to_phone: str, text: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text}
    }
    try:
        r = requests.post(url, json=payload, headers=headers)
        r.raise_for_status()
    except Exception as e:
        print(f"[SEND ERROR] {e}")


# =============================================
# MESSAGE PROCESSING
# =============================================

def process_message(phone_number: str, text: str) -> str:
    clean_old_completed_tasks()

    # Save inbound message before calling AI (keeps window timestamp fresh)
    save_chat_message(phone_number, "user", text)

    user_prompt = create_user_prompt(phone_number, text)
    ai_response = talk_to_ai(user_prompt)

    execute_db_actions(phone_number, ai_response, text)

    reply = ai_response.get("assistant_reply", "Got it! ✅")
    save_chat_message(phone_number, "assistant", reply)

    return reply


# =============================================
# WEBHOOK
# =============================================

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    if request.method == "POST":
        try:
            data = request.get_json()
            if not data or "entry" not in data:
                return "OK", 200

            entry  = data["entry"][0]
            change = entry.get("changes", [{}])[0]
            value  = change.get("value", {})

            if "messages" not in value:
                return "OK", 200

            message = value["messages"][0]
            if message.get("type") != "text":
                return "OK", 200

            phone   = message["from"]
            in_text = message["text"]["body"]

            reply = process_message(phone, in_text)
            send_meta_message(phone, reply)

        except Exception as e:
            print(f"[WEBHOOK ERROR] {e}")

        return "OK", 200

    return "Method not allowed", 405


# =============================================
# ENTRYPOINT
# =============================================

if __name__ == "__main__":
    setup_database()

    pulse_thread = threading.Thread(
        target=start_pulse_engine,
        kwargs={"send_function": send_meta_message},
        daemon=True
    )
    pulse_thread.start()
    print("[APP] Pulse engine started in background.")

    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
