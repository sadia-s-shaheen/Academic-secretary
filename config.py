import os
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# =============================================
# META / WHATSAPP CONFIG
# =============================================
META_TOKEN = os.getenv("META_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vibecoding")

# =============================================
# AI CONFIG (Multi-Model Strategy)
# =============================================
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Primary model for chat (High reasoning)
REACTIVE_MODEL = "openai/gpt-oss-safeguard-20b" 

# Efficient model for background tasks (Fast/Cheap)
PULSE_MODEL = "llama-3.1-8b-instant" 

# =============================================
# DATABASE & SYSTEM CONFIG
# =============================================
DB_PATH = "secretary.db"
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_DIGEST_TIME = "08:00"

# =============================================
# TESTING & PULSE CONFIG
# =============================================
# config.py
TEST_MODE = False # Turn off test mode to stop the 30s spam
PULSE_INTERVAL_SECONDS = 7200  # 2 hours
FAKE_DATE_OFFSET_DAYS = 0