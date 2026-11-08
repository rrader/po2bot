import base64
import html
import json
import logging
import os
import sqlite3
import tempfile
import time
from datetime import datetime
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Optional, Set
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ChatMemberHandler,
    PicklePersistence,
    TypeHandler,
    filters,
    ContextTypes,
)
import io
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pypdfium2 as pdfium
from PIL import Image
import pillow_heif
from dotenv import load_dotenv
from openai import OpenAI

# Register HEIF opener with Pillow for HEIC image support
pillow_heif.register_heif_opener()


# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID") or 0)
PRIVATE_GROUP_ID = int(os.getenv("PRIVATE_GROUP_ID") or 0)
ADMIN_SUPPORT_THREAD_ID = int(os.getenv("ADMIN_SUPPORT_THREAD_ID") or 0) or None
ADMIN_REQUESTS_THREAD_ID = int(os.getenv("ADMIN_REQUESTS_THREAD_ID") or 0) or None
SUPPORT_RATE_LIMIT_MAX_MESSAGES = int(os.getenv("SUPPORT_RATE_LIMIT_MAX_MESSAGES") or 10)
SUPPORT_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("SUPPORT_RATE_LIMIT_WINDOW_SECONDS") or 60)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_CREDS = os.getenv("GOOGLE_SHEETS_CREDS")
SPREADSHEET_ID = "1uuGXerA9I0eHTR2fNkektO8uS47T0zR1ITZIA1pnyBM"
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Test")  # Default to "Test" for staging
ROOMMATES_WORKSHEET_NAME = os.getenv("ROOMMATES_WORKSHEET_NAME", "СпівмешканціTest")  # Default to "СпівмешканціTest" for staging
PERSISTENCE_FILE = os.getenv("PERSISTENCE_FILE", "data/bot_persistence.pickle")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(PERSISTENCE_FILE) or "data", "bot_support.db"))
PO2BOT_API_TOKEN = os.getenv("PO2BOT_API_TOKEN") or os.getenv("PO2BOT_API_SECRET") or "po2_secret_token_change_me"
UPLOADS_DIR = os.getenv("UPLOADS_DIR", os.path.join(os.path.dirname(DB_PATH) or "data", "uploads"))

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Initialize Google Sheets client
google_sheets_client = None
if GOOGLE_SHEETS_CREDS:
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = json.loads(GOOGLE_SHEETS_CREDS)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        google_sheets_client = gspread.authorize(creds)
        logger.info("Google Sheets client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheets client: {e}")

# Conversation states
PHONE_NUMBER, USER_TYPE, DOCUMENT, ROOMMATE_OWNER_PHONE, APARTMENT_NUMBER, AREA, DOCUMENT_TYPE, CONFIRM_DATA, WAITING_APPROVAL, WAITING_OWNER_APPROVAL = range(10)

# Store pending requests
pending_requests: Dict[int, dict] = {}

# Store admin rejection states (waiting for reason)
admin_rejection_state: Dict[int, int] = {}  # {message_id: user_id}

# Store roommate approval requests (waiting for owner confirmation)
roommate_approval_state: Dict[int, dict] = {}  # {message_id: {roommate_user_id, owner_phone, etc}}

# Store support ticket messages for admin replies (admin_message_id -> user_id)
support_messages: Dict[int, int] = {}


def get_pending_requests(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Dict[int, dict]:
    """Get pending requests from context.bot_data if available, otherwise global dict."""
    global pending_requests
    if context is not None and hasattr(context, "bot_data") and context.bot_data is not None:
        if "pending_requests" not in context.bot_data:
            context.bot_data["pending_requests"] = pending_requests
        return context.bot_data["pending_requests"]
    return pending_requests


def get_admin_rejection_state(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Dict[int, int]:
    """Get admin rejection state from context.bot_data if available, otherwise global dict."""
    global admin_rejection_state
    if context is not None and hasattr(context, "bot_data") and context.bot_data is not None:
        if "admin_rejection_state" not in context.bot_data:
            context.bot_data["admin_rejection_state"] = admin_rejection_state
        return context.bot_data["admin_rejection_state"]
    return admin_rejection_state


def get_roommate_approval_state(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Dict[int, dict]:
    """Get roommate approval state from context.bot_data if available, otherwise global dict."""
    global roommate_approval_state
    if context is not None and hasattr(context, "bot_data") and context.bot_data is not None:
        if "roommate_approval_state" not in context.bot_data:
            context.bot_data["roommate_approval_state"] = roommate_approval_state
        return context.bot_data["roommate_approval_state"]
    return roommate_approval_state


def get_support_messages(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Dict[int, int]:
    """Get support messages mapping from context.bot_data if available, otherwise global dict."""
    global support_messages
    if context is not None and hasattr(context, "bot_data") and context.bot_data is not None:
        if "support_messages" not in context.bot_data:
            context.bot_data["support_messages"] = support_messages
        return context.bot_data["support_messages"]
    return support_messages


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite database for support messages, rate limits, and banned users."""
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                admin_message_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                replied_by TEXT,
                replied_at TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                user_id INTEGER PRIMARY KEY,
                last_question_time REAL NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                banned_by TEXT,
                reason TEXT,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS support_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_support_events_user_time ON support_events(user_id, created_at)
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                phone TEXT,
                apartment TEXT,
                user_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_message_text TEXT,
                last_message_at TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_users_updated ON chat_users(updated_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_users_apartment ON chat_users(apartment)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_users_phone ON chat_users(phone)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_users_last_msg_at ON chat_users(last_message_at DESC)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                direction TEXT NOT NULL,
                sender_type TEXT NOT NULL,
                sender_name TEXT,
                text TEXT,
                media_type TEXT,
                media_path TEXT,
                telegram_file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_user_time ON chat_messages(user_id, created_at ASC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_created ON chat_messages(created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_user_dir ON chat_messages(user_id, direction)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_media ON chat_messages(media_type)")
        conn.commit()


def db_save_support_message(admin_message_id: int, user_id: int, db_path: Optional[str] = None) -> None:
    """Save mapping of admin group message ID to user ID in SQLite."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO support_messages (admin_message_id, user_id) VALUES (?, ?)",
                (admin_message_id, user_id),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving support message {admin_message_id} -> {user_id}: {e}")


def db_get_support_user_id(admin_message_id: int, db_path: Optional[str] = None) -> Optional[int]:
    """Retrieve user ID by admin group message ID from SQLite."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM support_messages WHERE admin_message_id = ?",
                (admin_message_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"Error getting support user ID for msg {admin_message_id}: {e}")
        return None


def db_mark_support_replied(admin_message_id: int, admin_name: str, db_path: Optional[str] = None) -> Optional[str]:
    """Mark support message as replied in SQLite. Returns previous admin name if already replied, else None."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT replied_by FROM support_messages WHERE admin_message_id = ?",
                (admin_message_id,),
            )
            row = cursor.fetchone()
            prev_admin = row[0] if row and row[0] else None

            cursor.execute(
                "UPDATE support_messages SET replied_by = ?, replied_at = CURRENT_TIMESTAMP WHERE admin_message_id = ?",
                (admin_name, admin_message_id),
            )
            conn.commit()
            return prev_admin
    except Exception as e:
        logger.error(f"Error marking support message {admin_message_id} as replied: {e}")
        return None


def db_check_rate_limit(
    user_id: int,
    max_count: int = SUPPORT_RATE_LIMIT_MAX_MESSAGES,
    window_seconds: int = SUPPORT_RATE_LIMIT_WINDOW_SECONDS,
    limit_seconds: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[float]:
    """Check if user exceeded rate limit (sliding window). Returns remaining seconds if limited, else None."""
    path = db_path or DB_PATH
    if limit_seconds is not None:
        window_seconds = limit_seconds
    try:
        init_db(path)
        now = time.time()
        window_start = now - window_seconds
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            # Clean up old events (older than 1 hour)
            cursor.execute("DELETE FROM support_events WHERE created_at < ?", (now - 3600,))
            cursor.execute(
                "SELECT created_at FROM support_events WHERE user_id = ? AND created_at >= ? ORDER BY created_at ASC",
                (user_id, window_start),
            )
            rows = cursor.fetchall()
            if len(rows) >= max_count:
                oldest_in_window = rows[0][0]
                remaining = window_seconds - (now - oldest_in_window)
                return max(1.0, remaining) if remaining > 0 else None
        return None
    except Exception as e:
        logger.error(f"Error checking rate limit for user {user_id}: {e}")
        return None


def db_update_rate_limit(user_id: int, db_path: Optional[str] = None) -> None:
    """Record current timestamp event for user's question in SQLite."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        now = time.time()
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO support_events (user_id, created_at) VALUES (?, ?)",
                (user_id, now),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error updating rate limit for user {user_id}: {e}")


def db_ban_user(user_id: int, banned_by: str = "", reason: str = "", db_path: Optional[str] = None) -> None:
    """Ban user from sending support questions in SQLite."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO banned_users (user_id, banned_by, reason) VALUES (?, ?, ?)",
                (user_id, banned_by, reason),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error banning user {user_id}: {e}")


def db_unban_user(user_id: int, db_path: Optional[str] = None) -> bool:
    """Unban user in SQLite. Returns True if user was removed."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
    except Exception as e:
        logger.error(f"Error unbanning user {user_id}: {e}")
        return False


def db_is_banned(user_id: int, db_path: Optional[str] = None) -> bool:
    """Check if user is banned."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Error checking ban status for user {user_id}: {e}")
        return False


def db_upsert_chat_user(
    user_id: int,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    username: Optional[str] = None,
    phone: Optional[str] = None,
    apartment: Optional[str] = None,
    user_type: Optional[str] = None,
    last_message_text: Optional[str] = None,
    last_message_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Insert or update user details in chat_users."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id, first_name, last_name, username, phone, apartment, user_type FROM chat_users WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if row:
                fn = first_name if first_name is not None else row[1]
                ln = last_name if last_name is not None else row[2]
                un = username if username is not None else row[3]
                ph = phone if phone is not None else row[4]
                apt = apartment if apartment is not None else row[5]
                ut = user_type if user_type is not None else row[6]

                updates = [
                    "first_name = ?",
                    "last_name = ?",
                    "username = ?",
                    "phone = ?",
                    "apartment = ?",
                    "user_type = ?",
                    "updated_at = ?",
                ]
                params = [fn, ln, un, ph, apt, ut, now]
                if last_message_text is not None:
                    updates.append("last_message_text = ?")
                    params.append(last_message_text)
                    updates.append("last_message_at = ?")
                    params.append(last_message_at or now)
                params.append(user_id)
                cursor.execute(f"UPDATE chat_users SET {', '.join(updates)} WHERE user_id = ?", params)
            else:
                cursor.execute(
                    """
                    INSERT INTO chat_users (
                        user_id, first_name, last_name, username, phone, apartment, user_type,
                        created_at, updated_at, last_message_text, last_message_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        first_name or "",
                        last_name or "",
                        username or "",
                        phone or "",
                        apartment or "",
                        user_type or "",
                        now,
                        now,
                        last_message_text,
                        last_message_at or (now if last_message_text else None),
                    ),
                )
            conn.commit()
    except Exception as e:
        logger.error(f"Error upserting chat user {user_id}: {e}")


def db_save_chat_message(
    user_id: int,
    chat_id: int,
    message_id: Optional[int],
    direction: str,
    sender_type: str,
    sender_name: Optional[str],
    text: Optional[str],
    media_type: Optional[str] = "text",
    media_path: Optional[str] = None,
    telegram_file_id: Optional[str] = None,
    created_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[int]:
    """Save message to chat_messages and update chat_users last_message."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        now = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO chat_messages (
                    user_id, chat_id, message_id, direction, sender_type, sender_name,
                    text, media_type, media_path, telegram_file_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    chat_id,
                    message_id,
                    direction,
                    sender_type,
                    sender_name,
                    text,
                    media_type,
                    media_path,
                    telegram_file_id,
                    now,
                ),
            )
            msg_id = cursor.lastrowid

            snippet = text or (f"[{media_type}]" if media_type else "")
            if len(snippet) > 100:
                snippet = snippet[:97] + "..."

            cursor.execute("SELECT user_id FROM chat_users WHERE user_id = ?", (user_id,))
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE chat_users SET last_message_text = ?, last_message_at = ?, updated_at = ? WHERE user_id = ?",
                    (snippet, now, now, user_id),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO chat_users (
                        user_id, first_name, last_name, username, created_at, updated_at,
                        last_message_text, last_message_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, sender_name or "", "", "", now, now, snippet, now),
                )
            conn.commit()
            return msg_id
    except Exception as e:
        logger.error(f"Error saving chat message for user {user_id}: {e}")
        return None


def db_get_chat_users(
    limit: int = 50, offset: int = 0, search: Optional[str] = None, db_path: Optional[str] = None
) -> list:
    """Retrieve list of chat users with message counts, sorted by last message time."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = """
                SELECT u.user_id, u.first_name, u.last_name, u.username, u.phone, u.apartment, u.user_type,
                       u.created_at, u.updated_at, u.last_message_text, u.last_message_at,
                       COUNT(m.id) as message_count
                FROM chat_users u
                LEFT JOIN chat_messages m ON u.user_id = m.user_id
            """
            params = []
            if search and search.strip():
                s = f"%{search.strip()}%"
                query += " WHERE (u.first_name LIKE ? OR u.last_name LIKE ? OR u.username LIKE ? OR u.phone LIKE ? OR u.apartment LIKE ? OR CAST(u.user_id AS TEXT) LIKE ?)"
                params.extend([s, s, s, s, s, s])

            query += " GROUP BY u.user_id ORDER BY COALESCE(u.last_message_at, u.updated_at) DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error querying chat users: {e}")
        return []


def db_get_chat_messages(
    user_id: int, limit: int = 100, offset: int = 0, db_path: Optional[str] = None
) -> list:
    """Retrieve messages for a specific user ordered chronologically."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, user_id, chat_id, message_id, direction, sender_type, sender_name,
                       text, media_type, media_path, telegram_file_id, created_at
                FROM chat_messages
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            )
            rows = cursor.fetchall()
            messages = [dict(row) for row in rows]
            messages.reverse()
            return messages
    except Exception as e:
        logger.error(f"Error querying chat messages for {user_id}: {e}")
        return []


def db_get_chat_user(user_id: int, db_path: Optional[str] = None) -> Optional[dict]:
    """Retrieve single user details from chat_users."""
    path = db_path or DB_PATH
    try:
        init_db(path)
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM chat_users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error getting chat user {user_id}: {e}")
        return None


async def save_media_file(
    bot_client,
    file_id: str,
    user_id: int,
    original_filename: Optional[str] = None,
    uploads_dir: Optional[str] = None,
) -> Optional[str]:
    """Download media from Telegram and save to uploads/{user_id}/{filename}. Returns relative path."""
    target_dir = uploads_dir or UPLOADS_DIR
    try:
        user_folder = os.path.join(target_dir, str(user_id))
        os.makedirs(user_folder, exist_ok=True)

        tg_file = await bot_client.get_file(file_id)
        ext = ""
        if original_filename:
            ext = os.path.splitext(original_filename)[1].lower()
        if not ext and getattr(tg_file, "file_path", None):
            ext = os.path.splitext(tg_file.file_path)[1].lower()
        if not ext:
            ext = ".jpg"

        timestamp = int(time.time())
        safe_file_id = "".join(c for c in file_id if c.isalnum())[:10]
        filename = f"{timestamp}_{safe_file_id}{ext}"
        destination = os.path.join(user_folder, filename)

        await tg_file.download_to_drive(custom_path=destination)
        return os.path.join("uploads", str(user_id), filename)
    except Exception as e:
        logger.error(f"Failed to save media {file_id} for user {user_id}: {e}")
        return None


async def post_init(application: Application) -> None:
    """Synchronize global in-memory state with persisted application.bot_data and SQLite on startup."""
    global pending_requests, admin_rejection_state, roommate_approval_state, support_messages
    if "pending_requests" in application.bot_data:
        pending_requests.update(application.bot_data["pending_requests"])
    application.bot_data["pending_requests"] = pending_requests

    if "admin_rejection_state" in application.bot_data:
        admin_rejection_state.update(application.bot_data["admin_rejection_state"])
    application.bot_data["admin_rejection_state"] = admin_rejection_state

    if "roommate_approval_state" in application.bot_data:
        roommate_approval_state.update(application.bot_data["roommate_approval_state"])
    application.bot_data["roommate_approval_state"] = roommate_approval_state

    # Load support messages from SQLite into in-memory dictionary
    try:
        init_db()
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT admin_message_id, user_id FROM support_messages")
            for msg_id, uid in cursor.fetchall():
                support_messages[msg_id] = uid
    except Exception as e:
        logger.warning(f"Error preloading support messages from SQLite: {e}")

    if "support_messages" in application.bot_data:
        support_messages.update(application.bot_data["support_messages"])
    application.bot_data["support_messages"] = support_messages



DAH_INVITE_SUGGESTION = (
    "\n\n🏠 Щоб скоріше створити ОСББ, також пропонуємо приєднатися до застосунку \"Дах\" для мешканців будинку:\n"
    "http://app.dah.in.ua/oNCk"
)



def normalize_phone(phone: str) -> str:
    """Normalize phone number to format 380XXXXXXXXX."""
    # Remove all non-digit characters (including +, spaces, dashes, etc.)
    digits = ''.join(filter(str.isdigit, phone))

    # Handle empty string
    if not digits:
        return ""

    # Convert to 380 format
    if digits.startswith('380') and len(digits) == 12:
        # Already in correct format: 380501234567
        pass
    elif digits.startswith('0') and len(digits) == 10:
        # Format: 0501234567 -> 380501234567
        digits = '38' + digits
    elif len(digits) == 9:
        # Format: 501234567 -> 380501234567
        digits = '380' + digits
    elif digits.startswith('38') and len(digits) == 11:
        # Format: 38501234567 -> 380501234567
        digits = '380' + digits[2:]
    elif digits.startswith('380') and len(digits) > 12:
        # Trim extra digits
        digits = digits[:12]

    return digits


def find_owner_by_phone_or_username(search_value: str) -> Optional[Dict[str, any]]:
    """Find owner in Google Sheets by phone number or username. Returns record with Telegram User ID."""
    if not google_sheets_client:
        logger.warning("Google Sheets client not initialized")
        return None

    try:
        spreadsheet = google_sheets_client.open_by_key(SPREADSHEET_ID)
        sheet = spreadsheet.worksheet(WORKSHEET_NAME)

        # Get all values and create records manually to avoid duplicate header issues
        all_values = sheet.get_all_values()
        if not all_values or len(all_values) < 3:
            logger.warning("Sheet is empty or has no data rows")
            return None

        # Second row is headers (first row might be empty or title)
        headers = all_values[1]

        records = []
        for row in all_values[2:]:  # Skip first two rows (title + headers)
            if row:  # Skip empty rows
                record = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
                records.append(record)

        # Check if search value looks like username
        is_username = search_value.startswith('@') or not any(c.isdigit() for c in search_value)

        if is_username:
            # Search by username (remove @ if present)
            username_to_search = search_value.lstrip('@').strip().lower()
            for record in records:
                record_username = str(record.get("Username", "")).strip().lower()
                if record_username == username_to_search:
                    logger.info(f"Found owner with username {search_value}: {record}")
                    return record
            logger.info(f"No owner found with username {search_value}")
        else:
            # Search by phone number
            normalized_phone = normalize_phone(search_value)

            for record in records:
                record_phone = normalize_phone(str(record.get("Телефон", "")))
                if record_phone == normalized_phone:
                    logger.info(f"Found owner with phone {search_value}")
                    return record
            logger.info(f"No owner found with phone {search_value}")

        return None

    except Exception as e:
        logger.error(f"Error searching for owner: {e}")
        return None


def find_registered_by_phone(phone: str) -> Optional[Dict[str, any]]:
    """Check if a phone number is already registered in owners or roommates sheet.

    Returns the first matching record (owners sheet takes priority), or None.
    """
    if not google_sheets_client:
        return None

    normalized = normalize_phone(phone)
    if not normalized:
        return None

    try:
        spreadsheet = google_sheets_client.open_by_key(SPREADSHEET_ID)

        # Check owners sheet
        try:
            sheet = spreadsheet.worksheet(WORKSHEET_NAME)
            all_values = sheet.get_all_values()
            if all_values and len(all_values) >= 3:
                headers = all_values[1]
                for row in all_values[2:]:
                    if row:
                        record = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
                        if normalize_phone(str(record.get("Телефон", ""))) == normalized:
                            logger.info(f"Phone {phone} found in owners sheet")
                            return record
        except Exception as e:
            logger.warning(f"Error checking owners sheet: {e}")

        # Check roommates sheet
        try:
            sheet = spreadsheet.worksheet(ROOMMATES_WORKSHEET_NAME)
            all_values = sheet.get_all_values()
            if all_values and len(all_values) >= 2:
                headers = all_values[0]
                for row in all_values[1:]:
                    if row:
                        record = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
                        if normalize_phone(str(record.get("Телефон", ""))) == normalized:
                            logger.info(f"Phone {phone} found in roommates sheet")
                            return record
        except Exception as e:
            logger.warning(f"Error checking roommates sheet: {e}")

    except Exception as e:
        logger.error(f"Error in find_registered_by_phone: {e}")

    return None


def get_user_apartment_info(user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> str:
    """Look up registered apartment number and status for a Telegram user ID."""
    if context and context.user_data and context.user_data.get("apartment_number"):
        apt = str(context.user_data.get("apartment_number")).strip()
        is_owner = context.user_data.get("is_owner")
        role = "власник" if is_owner else "мешканець"
        return f"кв. {apt} ({role})"

    if not google_sheets_client:
        return "не визначено"

    try:
        spreadsheet = google_sheets_client.open_by_key(SPREADSHEET_ID)

        # 1. Check owners sheet
        try:
            sheet = spreadsheet.worksheet(WORKSHEET_NAME)
            all_values = sheet.get_all_values()
            if all_values and len(all_values) >= 3:
                headers = all_values[1]
                if "Telegram User ID" in headers and "Номер квартири" in headers:
                    uid_col = headers.index("Telegram User ID")
                    apt_col = headers.index("Номер квартири")
                    for row in all_values[2:]:
                        if len(row) > uid_col and str(row[uid_col]).strip() == str(user_id):
                            apt = str(row[apt_col]).strip() if len(row) > apt_col else ""
                            if apt:
                                return f"кв. {apt} (власник)"
        except Exception as e:
            logger.warning(f"Error checking owners sheet for user {user_id}: {e}")

        # 2. Check roommates sheet
        try:
            sheet = spreadsheet.worksheet(ROOMMATES_WORKSHEET_NAME)
            all_values = sheet.get_all_values()
            if all_values and len(all_values) >= 2:
                headers = None
                data_start = 1
                for r_idx in range(min(3, len(all_values))):
                    if "Telegram User ID" in all_values[r_idx]:
                        headers = all_values[r_idx]
                        data_start = r_idx + 1
                        break
                if headers and "Номер квартири" in headers:
                    uid_col = headers.index("Telegram User ID")
                    apt_col = headers.index("Номер квартири")
                    for row in all_values[data_start:]:
                        if len(row) > uid_col and str(row[uid_col]).strip() == str(user_id):
                            apt = str(row[apt_col]).strip() if len(row) > apt_col else ""
                            if apt:
                                return f"кв. {apt} (мешканець)"
        except Exception as e:
            logger.warning(f"Error checking roommates sheet for user {user_id}: {e}")

    except Exception as e:
        logger.error(f"Error finding apartment for user {user_id}: {e}")

    return "не зареєстрований"


def add_to_google_sheets(user_data: dict, admin_name: str, worksheet_name: str = None) -> bool:
    """Add approved user data to Google Sheets."""
    if not google_sheets_client:
        logger.warning("Google Sheets client not initialized, skipping sheet update")
        return False

    try:
        spreadsheet = google_sheets_client.open_by_key(SPREADSHEET_ID)
        sheet = spreadsheet.worksheet(worksheet_name or WORKSHEET_NAME)

        # Prepare row data
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            now,  # Дата/час
            user_data.get("first_name", ""),  # Ім'я
            user_data.get("last_name", ""),  # Прізвище
            user_data.get("username", ""),  # Username
            user_data.get("phone_number", ""),  # Телефон
            user_data.get("user_id", ""),  # Telegram User ID
            user_data.get("apartment_number", ""),  # Номер квартири
            user_data.get("area", ""),  # Площа
            user_data.get("document_type", ""),  # Тип документа
            admin_name,  # Хто затвердив
        ]

        sheet.append_row(row)
        logger.info(f"Successfully added user {user_data.get('user_id')} to Google Sheets ({worksheet_name or WORKSHEET_NAME})")
        return True

    except Exception as e:
        logger.error(f"Failed to add to Google Sheets: {e}")
        return False


def add_roommate_to_sheets(roommate_data: dict, owner_data: dict, apartment_number: str) -> bool:
    """Add roommate data to roommates worksheet."""
    if not google_sheets_client:
        logger.warning("Google Sheets client not initialized, skipping sheet update")
        return False

    try:
        spreadsheet = google_sheets_client.open_by_key(SPREADSHEET_ID)

        # Get or create roommates worksheet
        try:
            sheet = spreadsheet.worksheet(ROOMMATES_WORKSHEET_NAME)
        except:
            # Create worksheet if doesn't exist
            sheet = spreadsheet.add_worksheet(title=ROOMMATES_WORKSHEET_NAME, rows=100, cols=10)
            # Add headers
            sheet.append_row([
                "Дата/час", "Telegram User ID", "Ім'я", "Прізвище", "Username",
                "Телефон", "Ім'я власника", "Телефон власника", "Номер квартири"
            ])

        # Prepare row data
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            now,
            roommate_data.get("user_id", ""),
            roommate_data.get("first_name", ""),
            roommate_data.get("last_name", ""),
            roommate_data.get("username", ""),
            roommate_data.get("phone_number", ""),
            owner_data.get("Ім'я", "") + " " + owner_data.get("Прізвище", ""),
            owner_data.get("Телефон", ""),
            apartment_number,
        ]

        sheet.append_row(row)
        logger.info(f"Successfully added roommate {roommate_data.get('user_id')} to {ROOMMATES_WORKSHEET_NAME}")
        return True

    except Exception as e:
        logger.error(f"Failed to add roommate to Google Sheets: {e}")
        return False


async def parse_document_with_openai(
    image_source: str, *, is_base64: bool = False, mime_type: str = "image/jpeg"
) -> Optional[Dict[str, str]]:
    """Parse document image using OpenAI Vision API."""
    if not openai_client:
        logger.error("OpenAI client not initialized")
        return None

    try:
        image_url = (
            f"data:{mime_type};base64,{image_source}" if is_base64 else image_source
        )

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Проаналізуй це зображення документа (договір інвестування або витяг з реєстру права власності) та витягни наступну інформацію:
1. Номер квартири/приміщення
2. Загальну площу квартири/приміщення (в квадратних метрах). Якщо у документі згадано кілька площ (наприклад, житлова, балкон, коридор тощо), поверни лише загальну площу всієї квартири.
3. Тип документа (або "Договір інвестування" або "Право власності (витяг з реєстру)")

Якщо якась інформація не розбірлива або відсутня, вкажи null для цього поля."""
                        },
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                }
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "document_data",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "apartment_number": {
                                "type": ["string", "null"],
                                "description": "Номер квартири/приміщення"
                            },
                            "area": {
                                "type": ["string", "null"],
                                "description": "Загальна площа квартири в квадратних метрах (а не житлова чи інша часткова площа)"
                            },
                            "document_type": {
                                "type": ["string", "null"],
                                "description": "Тип документа: або 'Договір інвестування' або 'Право власності (витяг з реєстру)'"
                            }
                        },
                        "required": ["apartment_number", "area", "document_type"],
                        "additionalProperties": False
                    }
                }
            },
            max_tokens=300
        )

        content = response.choices[0].message.content.strip()
        logger.info(f"OpenAI response: {content}")

        # Parse JSON response (guaranteed to be valid JSON with structured output)
        parsed_data = json.loads(content)
        return parsed_data

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OpenAI JSON response: {e}")
        logger.error(f"Content was: {content}")
        return None
    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}")
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and ask for phone number."""
    user = update.effective_user

    # Check if this interaction was initiated with a specific button/intent
    incoming_text = (getattr(update.message, "text", "") or "").strip().lower()
    is_owner_intent = "власник" in incoming_text
    is_roommate_intent = "інший" in incoming_text or "користувач" in incoming_text

    # Clear any previous state
    context.user_data.clear()

    if is_owner_intent:
        context.user_data["preselected_is_owner"] = True
    elif is_roommate_intent:
        context.user_data["preselected_is_owner"] = False

    # Create keyboard with phone number share button and support button
    keyboard = [
        [KeyboardButton("📱 Поділитися номером телефону", request_contact=True)],
        [KeyboardButton("✉️ Питання адмінам")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    if is_owner_intent:
        greeting = (
            f"Привіт, {user.first_name}! Ласкаво просимо до верифікації власників квартир ПО 2.\n\n"
            "Щоб підтвердити статус власника, будь ласка, поділіться своїм номером телефону, "
            "натиснувши кнопку нижче, або оберіть «✉️ Питання адмінам», якщо вам потрібна допомога."
        )
    elif is_roommate_intent:
        greeting = (
            f"Привіт, {user.first_name}! Ласкаво просимо до процесу верифікації співмешканців ПО 2.\n\n"
            "Будь ласка, поділіться своїм номером телефону, натиснувши кнопку нижче, "
            "або оберіть «✉️ Питання адмінам», якщо вам потрібна допомога."
        )
    else:
        greeting = (
            f"Привіт, {user.first_name}! Ласкаво просимо до процесу верифікації ПО 2.\n\n"
            "Іноді бот може тимчасово не відповідати. Якщо це сталося, скористайтеся командою /start, щоб почати спочатку.\n\n"
            "Будь ласка, поділіться своїм номером телефону, натиснувши кнопку нижче, "
            "або оберіть «✉️ Питання адмінам», якщо вам потрібна допомога."
        )

    await update.message.reply_text(greeting, reply_markup=reply_markup)
    return PHONE_NUMBER


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show help instructions and support option."""
    keyboard = [
        [KeyboardButton("📱 Поділитися номером телефону", request_contact=True)],
        [KeyboardButton("✉️ Питання адмінам")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        "ℹ️ Довідка та зв'язок з адміністраторами:\n\n"
        "• /start — розпочати процес верифікації для доступу до групи будинку\n"
        "• /cancel — скасувати поточне заповнення анкети\n"
        "• ✉️ Питання адмінам — надіслати повідомлення або запитання адміністраторам\n\n"
        "💡 Ви також можете просто написати будь-яке запитання сюди в чат, "
        "і бот запитає, чи надіслати його адміністраторам.",
        reply_markup=reply_markup,
    )
    return ConversationHandler.END


async def ask_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle click on '✉️ Питання адмінам' button."""
    context.user_data["waiting_for_admin_question"] = True
    await update.message.reply_text(
        "✉️ Будь ласка, напишіть ваше запитання до адміністраторів наступним повідомленням.\n\n"
        "Перед відправкою бот запитає ваше підтвердження.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END



async def phone_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle phone number and ask for user type."""
    contact = update.message.contact

    if contact and contact.user_id == update.effective_user.id:
        # Store phone number and user info
        context.user_data["phone_number"] = contact.phone_number
        context.user_data["user_id"] = update.effective_user.id
        context.user_data["username"] = update.effective_user.username or ""
        context.user_data["first_name"] = update.effective_user.first_name or ""
        context.user_data["last_name"] = update.effective_user.last_name or ""

        # Check if already registered (owner or co-owner)
        existing = find_registered_by_phone(contact.phone_number)
        if existing:
            context.user_data["is_owner"] = True
            context.user_data["already_registered"] = True
            keyboard = [
                [KeyboardButton("➕ Додати ще одну квартиру")],
                [KeyboardButton("❌ Скасувати")],
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                f"✅ Номер телефону отримано: {contact.phone_number}\n\n"
                "Цей номер вже зареєстрований у системі.\n"
                "Бажаєте додати ще одну квартиру до свого профілю?",
                reply_markup=reply_markup,
            )
            return USER_TYPE

        preselected = context.user_data.get("preselected_is_owner")
        if preselected is True:
            context.user_data["is_owner"] = True
            await update.message.reply_text(
                f"✅ Номер телефону отримано: {contact.phone_number}\n\n"
                f"{DOCUMENT_PROMPT}",
                reply_markup=ReplyKeyboardRemove(),
            )
            return DOCUMENT
        elif preselected is False:
            context.user_data["is_owner"] = False
            await update.message.reply_text(
                f"✅ Номер телефону отримано: {contact.phone_number}\n\n"
                "Будь ласка, вкажіть номер телефону власника квартири (380XXXXXXXXX або 0XXXXXXXXX) "
                "або його Telegram @username для підтвердження.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ROOMMATE_OWNER_PHONE

        # Ask if owner or other user
        keyboard = [
            [KeyboardButton("🏠 Я власник квартири")],
            [KeyboardButton("👥 Інший користувач")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

        await update.message.reply_text(
            f"✅ Номер телефону отримано: {contact.phone_number}\n\n"
            "Оберіть ваш статус:",
            reply_markup=reply_markup
        )

        return USER_TYPE
    else:
        await update.message.reply_text(
            "❌ Будь ласка, поділіться своїм власним номером телефону, використовуючи кнопку."
        )
        return PHONE_NUMBER


DOCUMENT_PROMPT = (
    "Тепер, будь ласка, завантажте фото або PDF договору інвестування/купівлі чи витягу з реєстру.\n\n"
    "⚠️ Можете заблюрити всі особисті дані, які вважаєте за потрібне.\n"
    "Головне, щоб було видно:\n"
    "• Номер приміщення\n"
    "• Площу\n\n"
    "ℹ️ Які дані ми збираємо:\n"
    "• Номер телефону (для пошуку співмешканців)\n"
    "• Telegram username (для пошуку співмешканців)\n"
    "• Номер квартири та площу (для голосування)\n\n"
    "🔒 Ваші дані не передаються третім особам і використовуються виключно 1) для роботи бота 2) підрахунку загальної площі власників 3) інформування про можливості голосування.\n"
    "Надаючи цю інформацію, ви даєте згоду на її обробку та зберігання для вищезгаданих потреб."
)


async def user_type_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user type selection."""
    user_type = update.message.text.strip()

    if "додати ще одну квартиру" in user_type.lower():
        # Already-registered user adding another apartment — skip to document upload
        context.user_data["is_owner"] = True
        await update.message.reply_text(DOCUMENT_PROMPT, reply_markup=ReplyKeyboardRemove())
        return DOCUMENT

    elif "скасувати" in user_type.lower():
        await update.message.reply_text(
            "Скасовано. Використайте /start щоб почати спочатку.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    elif "власник" in user_type.lower():
        # Owner flow - ask for document
        context.user_data["is_owner"] = True
        await update.message.reply_text(DOCUMENT_PROMPT, reply_markup=ReplyKeyboardRemove())
        return DOCUMENT

    elif "інший" in user_type.lower() or "користувач" in user_type.lower():
        # Roommate flow - ask for owner's phone
        context.user_data["is_owner"] = False

        await update.message.reply_text(
            "Будь ласка, вкажіть номер телефону або username власника квартири.\n\n"
            "Формат телефону: 380501234567 або 0501234567\n"
            "(без пробілів, без +)\n\n"
            "Або username: @username (можна без @)"
        )

        return ROOMMATE_OWNER_PHONE

    else:
        await update.message.reply_text(
            "❌ Будь ласка, оберіть один з варіантів, використовуючи кнопки."
        )
        return USER_TYPE


async def roommate_owner_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle owner phone number or username for roommate."""
    owner_search = update.message.text.strip()
    context.user_data["owner_search"] = owner_search

    # Search for owner in Google Sheets
    owner_data = find_owner_by_phone_or_username(owner_search)

    if not owner_data:
        await update.message.reply_text(
            "❌ Власника з таким номером телефону або username не знайдено в системі.\n\n"
            "Переконайтеся, що:\n"
            "• Власник вже пройшов верифікацію та доданий до групи\n"
            "• Телефон вказаний у форматі 380501234567 або 0501234567\n"
            "• Username вказаний правильно\n\n"
            "Використайте /start щоб почати спочатку."
        )
        return ConversationHandler.END

    context.user_data["owner_data"] = owner_data
    context.user_data["apartment_number"] = owner_data.get("Номер квартири", "")

    # Get owner's Telegram User ID
    owner_user_id = owner_data.get("Telegram User ID")

    if not owner_user_id:
        await update.message.reply_text(
            "❌ Власник знайдений, але у нього немає Telegram User ID в системі.\n\n"
            "Це означає, що власник був доданий до старої версії бота.\n"
            "Попросіть власника зв'язатися з адміністратором."
        )
        return ConversationHandler.END

    # Send approval request to owner
    roommate_name = f"{context.user_data['first_name']} {context.user_data.get('last_name', '')}"
    roommate_phone = context.user_data["phone_number"]
    roommate_username = context.user_data.get("username", "")
    roommate_user_id = context.user_data['user_id']

    # Create approval keyboard
    keyboard = [
        [
            InlineKeyboardButton("✅ Підтверджую", callback_data=f"approve_roommate_{roommate_user_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_roommate_{roommate_user_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Store roommate request for owner approval
    roommate_approval_state[roommate_user_id] = {
        "roommate_user_id": roommate_user_id,
        "roommate_data": {
            "first_name": context.user_data['first_name'],
            "last_name": context.user_data.get('last_name', ''),
            "username": context.user_data.get('username', ''),
            "phone_number": roommate_phone,
            "user_id": roommate_user_id,
        },
        "owner_data": owner_data,
        "apartment_number": owner_data.get("Номер квартири", ""),
    }

    # Send request to owner
    try:
        await context.bot.send_message(
            chat_id=int(owner_user_id),
            text=(
                f"👥 Запит на додавання користувача\n\n"
                f"👤 Ім'я: {roommate_name}\n"
                f"📱 Телефон: {roommate_phone}\n"
                f"{'👥 Username: @' + roommate_username if roommate_username else ''}"
                f"{chr(10) if roommate_username else ''}"
                f"🏠 Квартира: {owner_data.get('Номер квартири')}\n\n"
                "Ця людина хоче приєднатися до групи. Підтверджуєте?"
            ),
            reply_markup=reply_markup
        )

        owner_first_name = owner_data.get("Ім'я", "")
        owner_last_name = owner_data.get("Прізвище", "")
        apartment = owner_data.get("Номер квартири", "")

        # Build owner name - show only what's available
        owner_name_parts = [owner_first_name, owner_last_name]
        owner_full_name = " ".join(part for part in owner_name_parts if part)

        await update.message.reply_text(
            f"✅ Знайдено власника: {owner_full_name if owner_full_name else 'Без імені'}\n"
            f"Квартира: {apartment}\n\n"
            "⏳ Запит надіслано власнику. Очікуйте підтвердження..."
        )

        return WAITING_OWNER_APPROVAL

    except Exception as e:
        logger.error(f"Error sending message to owner: {e}")
        await update.message.reply_text(
            "❌ Не вдалося надіслати запит власнику. Спробуйте пізніше або зверніться до адміністратора."
        )
        return ConversationHandler.END


async def document_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle document upload and try to parse it with OpenAI."""
    message = update.message
    if not message:
        return DOCUMENT

    # Check for media groups (albums) and skip duplicate processing of secondary files
    media_group_id = message.media_group_id
    if media_group_id:
        processed_groups = context.user_data.setdefault("_processed_media_groups", set())
        if media_group_id in processed_groups:
            logger.info(f"Skipping additional media item from album group {media_group_id}")
            return DOCUMENT
        processed_groups.add(media_group_id)

    document = message.document
    photo = message.photo[-1] if message.photo else None

    if not (photo or document):
        await update.message.reply_text(
            "❌ Будь ласка, завантажте фото або PDF договору/витягу. "
            "Файли інших типів поки не підтримуємо."
        )
        return DOCUMENT

    image_source = None
    is_base64 = False
    mime_type = "image/jpeg"
    effective_uid = update.effective_user.id if update.effective_user else context.user_data.get("user_id", 0)

    if photo:
        context.user_data["document_file_id"] = photo.file_id
        context.user_data["document_kind"] = "photo"
        saved_path = await save_media_file(context.bot, photo.file_id, effective_uid, "document.jpg")
        if saved_path:
            context.user_data["document_media_path"] = saved_path
        file = await context.bot.get_file(photo.file_id)
        image_source = file.file_path
    elif document:
        mime_type = (document.mime_type or "").lower()
        file_name = (document.file_name or "").lower()
        file_ext = os.path.splitext(file_name)[1]
        context.user_data["document_file_id"] = document.file_id
        context.user_data["document_kind"] = "document"
        saved_path = await save_media_file(context.bot, document.file_id, effective_uid, document.file_name)
        if saved_path:
            context.user_data["document_media_path"] = saved_path
        file = await context.bot.get_file(document.file_id)

        # 1. HEIC / HEIF image conversion
        if (
            mime_type in ("image/heic", "image/heif")
            or file_ext in (".heic", ".heif")
        ):
            temp_heic_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=file_ext or ".heic", delete=False) as temp_heic:
                    temp_heic_path = temp_heic.name
                    await file.download_to_drive(custom_path=temp_heic.name)

                with Image.open(temp_heic_path) as img:
                    img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=90)
                    image_bytes = buf.getvalue()

                image_source = base64.b64encode(image_bytes).decode("utf-8")
                is_base64 = True
                mime_type = "image/jpeg"
            except Exception as e:
                logger.error(f"Failed to convert HEIC to JPEG: {e}")
                await update.message.reply_text(
                    "❌ Не вдалося обробити файл HEIC. Переконайтеся, що файл не пошкоджений, "
                    "або надішліть його як звичайне фото чи PDF."
                )
                return DOCUMENT
            finally:
                if temp_heic_path:
                    try:
                        os.remove(temp_heic_path)
                    except OSError:
                        logger.warning("Failed to remove temporary HEIC file")

        # 2. Standard image formats (JPEG, PNG, WEBP)
        elif (
            mime_type in ("image/jpeg", "image/jpg", "image/png", "image/webp")
            or file_ext in (".jpg", ".jpeg", ".png", ".webp")
            or (mime_type.startswith("image/") and mime_type not in ("image/heic", "image/heif"))
        ):
            image_source = file.file_path

        # 3. PDF document
        elif (
            mime_type == "application/pdf"
            or file_ext == ".pdf"
        ):
            temp_img_path = None

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_pdf:
                await file.download_to_drive(custom_path=temp_pdf.name)

            try:
                pdf_doc = pdfium.PdfDocument(temp_pdf.name)
                page = pdf_doc[0]
                renderer = page.render(scale=2)
                pil_image = renderer.to_pil()

                if not pil_image:
                    raise RuntimeError("Pillow is required to convert PDF pages to images")

                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_img:
                    pil_image.save(temp_img.name, format="JPEG")
                    temp_img.seek(0)
                    image_bytes = temp_img.read()
                    temp_img_path = temp_img.name

                image_source = base64.b64encode(image_bytes).decode("utf-8")
                is_base64 = True
                mime_type = "image/jpeg"
            except Exception as e:
                logger.error(f"Failed to convert PDF to image: {e}")
                await update.message.reply_text(
                    "❌ Не вдалося обробити PDF. Переконайтеся, що файл не пошкоджений, "
                    "або спробуйте надіслати фото договору."
                )
                return DOCUMENT
            finally:
                try:
                    os.remove(temp_pdf.name)
                except OSError:
                    logger.warning("Failed to remove temporary PDF file")

                if temp_img_path:
                    try:
                        os.remove(temp_img_path)
                    except OSError:
                        logger.warning("Failed to remove temporary image file")
        else:
            await update.message.reply_text(
                "❌ Ми підтримуємо лише зображення (JPG, PNG, HEIC) та PDF. "
                "Завантажте фото договору або PDF-версію, будь ласка."
            )
            return DOCUMENT


    if not image_source:
        await update.message.reply_text(
            "❌ Не вдалося обробити файл. Спробуйте інший формат (JPG, PNG, PDF)."
        )
        return DOCUMENT

    processing_msg = await update.message.reply_text(
        "⏳ Обробляю документ, зачекайте..."
    )

    parsed_data = await parse_document_with_openai(
        image_source, is_base64=is_base64, mime_type=mime_type
    )

    # Delete processing message
    await processing_msg.delete()

    if parsed_data and all(parsed_data.get(k) for k in ["apartment_number", "area", "document_type"]):
        # Successfully parsed all data
        context.user_data["apartment_number"] = parsed_data["apartment_number"]
        context.user_data["area"] = parsed_data["area"]
        context.user_data["document_type"] = parsed_data["document_type"]

        # Create confirmation keyboard
        keyboard = [
            [KeyboardButton("✅ Так, все вірно")],
            [KeyboardButton("✏️ Ні, я виправлю вручну")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

        await update.message.reply_text(
            f"✅ Документ оброблено!\n\n"
            f"📋 Виявлені дані:\n"
            f"🏠 Номер квартири: {parsed_data['apartment_number']}\n"
            f"📐 Площа: {parsed_data['area']} м²\n"
            f"📄 Тип документа: {parsed_data['document_type']}\n\n"
            f"Чи всі дані вірні?",
            reply_markup=reply_markup
        )

        return CONFIRM_DATA
    else:
        # Failed to parse or incomplete data - offer to retry or enter manually
        logger.warning(f"Failed to parse document or incomplete data: {parsed_data}")

        # Create keyboard with options
        keyboard = [
            [KeyboardButton("📷 Завантажити нове фото")],
            [KeyboardButton("✏️ Ввести дані вручну")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

        await update.message.reply_text(
            "⚠️ Не вдалося автоматично розпізнати всі дані з документа.\n\n"
            "Оберіть, що робити далі:",
            reply_markup=reply_markup
        )

        return APARTMENT_NUMBER


async def apartment_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle apartment number or photo re-upload option."""
    user_input = update.message.text.strip()

    # Check if user wants to upload new photo
    if "завантажити" in user_input.lower() or "📷" in user_input:
        await update.message.reply_text(
            "Добре! Завантажте нове фото або PDF договору інвестування/купівлі чи витягу з реєстру.\n\n"
            "⚠️ Можете заблюрити всі особисті дані, які вважаєте за потрібне.\n"
            "Головне, щоб було видно:\n"
            "• Номер приміщення\n"
            "• Площу",
            reply_markup=ReplyKeyboardRemove(),
        )
        return DOCUMENT

    # Check if user wants to enter manually
    if "вручну" in user_input.lower() or "✏️" in user_input:
        await update.message.reply_text(
            "Добре, введемо дані вручну.\n\n"
            "Спочатку вкажіть номер квартири:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return APARTMENT_NUMBER

    # Check if stale button from earlier steps was clicked
    if user_input in ("🏠 Я власник квартири", "👥 Інший користувач", "➕ Додати ще одну квартиру"):
        await update.message.reply_text(
            "⚠️ Зараз очікується номер квартири/приміщення (наприклад: 42 або 15-А).\n\n"
            "Введіть номер квартири або скористайтеся /start, щоб почати спочатку.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return APARTMENT_NUMBER

    # Regular apartment number input
    apartment_number = user_input
    context.user_data["apartment_number"] = apartment_number

    await update.message.reply_text(
        f"✅ Номер квартири: {apartment_number}\n\n"
        "Тепер вкажіть площу квартири (в м²):",
        reply_markup=ReplyKeyboardRemove(),
    )

    return AREA


async def area_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle area and ask for document type."""
    area = update.message.text.strip()

    # Check if stale button was pressed
    if area in ("🏠 Я власник квартири", "👥 Інший користувач", "✅ Так, все вірно", "✏️ Ні, я виправлю вручну"):
        await update.message.reply_text(
            "⚠️ Зараз очікується загальна площа квартири в м² (наприклад: 45.6 або 54).\n\n"
            "Введіть число або скористайтеся /start, щоб почати спочатку."
        )
        return AREA

    context.user_data["area"] = area

    # Create keyboard for document type
    keyboard = [
        [KeyboardButton("📄 Договір інвестування")],
        [KeyboardButton("🏛 Право власності (витяг з реєстру)")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        f"✅ Площа: {area} м²\n\n"
        "Оберіть тип документа:",
        reply_markup=reply_markup,
    )

    return DOCUMENT_TYPE


async def confirm_data_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle data confirmation."""
    response = update.message.text.strip()

    if "так" in response.lower() or "✅" in response:
        # User confirmed data is correct, proceed to send to admin
        return await send_to_admin(update, context)
    elif "ні" in response.lower() or "виправ" in response.lower() or "✏️" in response:
        # User wants to correct data manually
        await update.message.reply_text(
            "Добре, введемо дані вручну.\n\n"
            "Спочатку вкажіть номер квартири:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return APARTMENT_NUMBER
    else:
        keyboard = [
            [KeyboardButton("✅ Так, все вірно")],
            [KeyboardButton("✏️ Ні, я виправлю вручну")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "⚠️ Будь ласка, підтвердіть правильність даних, обравши один з варіантів на кнопках:",
            reply_markup=reply_markup,
        )
        return CONFIRM_DATA


async def document_type_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle document type and show confirmation."""
    raw_doc_type = update.message.text.strip()

    if "інвест" in raw_doc_type.lower():
        document_type = "Договір інвестування"
    elif "власност" in raw_doc_type.lower() or "витяг" in raw_doc_type.lower():
        document_type = "Право власності (витяг з реєстру)"
    else:
        keyboard = [
            [KeyboardButton("📄 Договір інвестування")],
            [KeyboardButton("🏛 Право власності (витяг з реєстру)")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "⚠️ Будь ласка, оберіть тип документа за допомогою кнопок нижче:",
            reply_markup=reply_markup,
        )
        return DOCUMENT_TYPE

    context.user_data["document_type"] = document_type

    apartment_number = context.user_data.get("apartment_number", "")
    area = context.user_data.get("area", "")

    # Create confirmation keyboard
    keyboard = [
        [KeyboardButton("✅ Так, все вірно")],
        [KeyboardButton("✏️ Ні, я виправлю вручну")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        f"📋 Перевірте введені дані:\n\n"
        f"🏠 Номер квартири: {apartment_number}\n"
        f"📐 Площа: {area} м²\n"
        f"📄 Тип документа: {document_type}\n\n"
        f"Чи всі дані вірні?",
        reply_markup=reply_markup
    )

    return CONFIRM_DATA


async def send_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send request to admin group."""

    user_id = context.user_data["user_id"]
    phone_number = context.user_data["phone_number"]
    username = context.user_data.get("username", "")
    first_name = context.user_data.get("first_name", "")
    last_name = context.user_data.get("last_name", "")
    apartment_number = context.user_data.get("apartment_number", "")
    area = context.user_data.get("area", "")
    document_type = context.user_data.get("document_type", "")
    photo_file_id = context.user_data.get("document_file_id", "")
    document_kind = context.user_data.get("document_kind", "photo")

    # Store request
    requests_dict = get_pending_requests(context)
    requests_dict[user_id] = {
        "user_id": user_id,
        "phone_number": phone_number,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "document_file_id": photo_file_id,
        "apartment_number": apartment_number,
        "area": area,
        "document_type": document_type,
        "document_media_path": context.user_data.get("document_media_path", ""),
    }
    pending_requests[user_id] = requests_dict[user_id]

    # Create approval keyboard
    keyboard = [
        [
            InlineKeyboardButton("✅ Затвердити", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{user_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    already_registered = context.user_data.get("already_registered", False)
    header = "➕ Додаткова квартира вже зареєстрованого користувача" if already_registered else "🆕 Новий запит на доступ"
    admin_caption = (
        f"{header}\n\n"
        f"👤 Ім'я: {first_name} {last_name}\n"
        f"📱 Телефон: {phone_number}\n"
        f"🆔 User ID: {user_id}\n"
        f"{'👥 Username: @' + username + chr(10) if username else ''}"
        f"🏠 Номер квартири: {apartment_number}\n"
        f"📐 Площа: {area} м²\n"
        f"📄 Тип документа: {document_type}\n\n"
        "Будь ласка, перегляньте документ та затвердьте або відхиліть заявку."
    )

    admin_chat_id = int(os.getenv("ADMIN_GROUP_ID") or ADMIN_GROUP_ID or 0)
    requests_thread_id = int(os.getenv("ADMIN_REQUESTS_THREAD_ID") or 0) or None
    extra_kwargs = {}
    if requests_thread_id:
        extra_kwargs["message_thread_id"] = requests_thread_id

    # Send to admin group
    logger.info(f"Sending request to admin group {admin_chat_id} for user {user_id}")
    try:
        if document_kind == "photo":
            await context.bot.send_photo(
                chat_id=admin_chat_id,
                photo=photo_file_id,
                caption=admin_caption,
                reply_markup=reply_markup,
                **extra_kwargs,
            )
        else:
            await context.bot.send_document(
                chat_id=admin_chat_id,
                document=photo_file_id,
                caption=admin_caption,
                reply_markup=reply_markup,
                **extra_kwargs,
            )
        logger.info(f"Successfully sent request to admin group for user {user_id}")
    except Exception as e:
        logger.error(f"Error sending to admin group: {e}")
        raise

    await update.message.reply_text(
        "✅ Ваш запит надіслано!\n\n"
        "Адміністратор перегляне вашу інформацію, і ви отримаєте повідомлення після схвалення.",
        reply_markup=ReplyKeyboardRemove(),
    )

    return WAITING_APPROVAL



async def reset_user_conversation(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Reset active conversation and clear user state when request is resolved (approved/rejected)."""
    app = getattr(context, "application", None)
    if not app:
        return

    key = (user_id, user_id)
    # 1. Clear in-memory conversation state from any ConversationHandler
    handlers_dict = getattr(app, "handlers", {})
    for handler_list in handlers_dict.values():
        for h in handler_list:
            if isinstance(h, ConversationHandler):
                if hasattr(h, "_conversations") and key in h._conversations:
                    h._conversations.pop(key, None)

    # 2. Update persistence if enabled
    if hasattr(app, "persistence") and app.persistence:
        try:
            await app.persistence.update_conversation("verification_conversation", key, None)
        except Exception as e:
            logger.warning(f"Error resetting persistence conversation for user {user_id}: {e}")


async def handle_roommate_approval(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle roommate approval/rejection by owner."""
    try:
        parts = query.data.split("_")
        if len(parts) != 3 or parts[0] not in ("approve", "reject") or parts[1] != "roommate":
            logger.warning(f"Unexpected roommate callback data: {query.data}")
            await query.answer("Невідома або застаріла дія.", show_alert=True)
            return
        action = parts[0] + "_" + parts[1]  # approve_roommate or reject_roommate
        roommate_user_id = int(parts[2])
    except Exception as e:
        logger.warning(f"Error parsing roommate callback query {query.data}: {e}")
        await query.answer("Не вдалося розпізнати дію.", show_alert=True)
        return

    roommates_dict = get_roommate_approval_state(context)
    if roommate_user_id not in roommates_dict:
        try:
            await query.edit_message_text(
                text=query.message.text + "\n\n❌ Запит застарів або вже оброблений."
            )
        except Exception:
            pass
        await query.answer("Запит застарів або вже оброблений.", show_alert=True)
        return

    roommate_request = roommates_dict[roommate_user_id]
    roommate_data = roommate_request["roommate_data"]
    owner_data = roommate_request["owner_data"]
    apartment_number = roommate_request["apartment_number"]
    owner_name = query.from_user.first_name

    if action == "approve_roommate":
        try:
            # Create invite link for roommate
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=PRIVATE_GROUP_ID,
                member_limit=1,
            )

            # Notify roommate
            await context.bot.send_message(
                chat_id=roommate_user_id,
                text=(
                    f"🎉 Вітаємо! Власник {owner_name} підтвердив ваш запит.\n\n"
                    f"Натисніть тут, щоб приєднатися до приватної групи:\n{invite_link.invite_link}"
                    f"{DAH_INVITE_SUGGESTION}"
                ),
                reply_markup=ReplyKeyboardRemove(),
            )

            # Add to Google Sheets (Співмешканці worksheet)
            add_roommate_to_sheets(roommate_data, owner_data, apartment_number)

            # Update owner's message
            await query.edit_message_text(
                text=query.message.text + f"\n\n✅ ПІДТВЕРДЖЕНО {owner_name}"
            )

            logger.info(f"Roommate {roommate_user_id} approved by owner {owner_name}")

            # Clean up and reset conversation state
            if roommate_user_id in roommates_dict:
                del roommates_dict[roommate_user_id]
            if roommate_user_id in roommate_approval_state:
                del roommate_approval_state[roommate_user_id]
            await reset_user_conversation(context, roommate_user_id)

        except Exception as e:
            logger.error(f"Error approving roommate {roommate_user_id}: {e}")
            await query.edit_message_text(
                text=query.message.text + f"\n\n❌ Помилка: {str(e)}"
            )

    else:  # reject_roommate
        # Notify roommate
        await context.bot.send_message(
            chat_id=roommate_user_id,
            text=f"❌ На жаль, власник {owner_name} відхилив ваш запит на додавання.",
            reply_markup=ReplyKeyboardRemove(),
        )

        # Update owner's message
        await query.edit_message_text(
            text=query.message.text + f"\n\n❌ ВІДХИЛЕНО {owner_name}"
        )

        logger.info(f"Roommate {roommate_user_id} rejected by owner {owner_name}")

        # Clean up and reset conversation state
        if roommate_user_id in roommates_dict:
            del roommates_dict[roommate_user_id]
        if roommate_user_id in roommate_approval_state:
            del roommate_approval_state[roommate_user_id]
        await reset_user_conversation(context, roommate_user_id)


async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle approval/rejection from admin or owner."""
    query = update.callback_query
    await query.answer()

    # Check if this is a roommate approval
    if query.data.startswith("approve_roommate_") or query.data.startswith("reject_roommate_"):
        return await handle_roommate_approval(query, context)

    # Regular owner approval by admin
    try:
        parts = query.data.split("_")
        if len(parts) != 2 or parts[0] not in ("approve", "reject"):
            logger.warning(f"Unexpected callback query data format: {query.data}")
            await query.answer("Невідома або застаріла дія.", show_alert=True)
            return
        action, user_id_str = parts
        user_id = int(user_id_str)
    except Exception as e:
        logger.warning(f"Error parsing callback query {query.data}: {e}")
        await query.answer("Не вдалося розпізнати дію.", show_alert=True)
        return

    pending = get_pending_requests(context)
    if user_id not in pending:
        try:
            if query.message.caption:
                await query.edit_message_caption(
                    caption=query.message.caption + "\n\n❌ Запит застарів або вже оброблений."
                )
            elif query.message.text:
                await query.edit_message_text(
                    text=query.message.text + "\n\n❌ Запит застарів або вже оброблений."
                )
        except Exception:
            pass
        await query.answer("Запит застарів або вже оброблений.", show_alert=True)
        return

    request_data = pending[user_id]
    admin_name = query.from_user.first_name

    if action == "approve":
        try:
            # Check if user is already in the private group
            already_in_group = False
            try:
                member = await context.bot.get_chat_member(chat_id=PRIVATE_GROUP_ID, user_id=user_id)
                already_in_group = member.status not in ("left", "kicked")
            except Exception:
                pass

            if already_in_group:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🎉 Ваш запит на нову квартиру схвалено адміністратором {admin_name}.\n\n"
                        f"Ви вже є учасником приватної групи."
                        f"{DAH_INVITE_SUGGESTION}"
                    ),
                    reply_markup=ReplyKeyboardRemove(),
                )
            else:
                # Invite user to private group
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=PRIVATE_GROUP_ID,
                    member_limit=1,
                )
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🎉 Вітаємо! Ваш запит схвалено адміністратором {admin_name}.\n\n"
                        f"Натисніть тут, щоб приєднатися до приватної групи:\n{invite_link.invite_link}"
                        f"{DAH_INVITE_SUGGESTION}"
                    ),
                    reply_markup=ReplyKeyboardRemove(),
                )

            # Add to Google Sheets
            add_to_google_sheets(request_data, admin_name)

            # Update admin message
            await query.edit_message_caption(
                caption=query.message.caption + f"\n\n✅ ЗАТВЕРДЖЕНО {admin_name}"
            )

            logger.info(f"User {user_id} approved by {admin_name}")

            # Remove from pending after successful approval
            if user_id in pending:
                del pending[user_id]
            if user_id in pending_requests:
                del pending_requests[user_id]
            await reset_user_conversation(context, user_id)

        except Exception as e:
            logger.error(f"Error approving user {user_id}: {e}")
            await query.edit_message_caption(
                caption=query.message.caption + f"\n\n❌ Помилка: {str(e)}"
            )
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Виникла помилка при обробці вашого запиту. Будь ласка, зверніться до служби підтримки.",
            )

    else:  # reject
        # Ask admin for rejection reason
        rejection_dict = get_admin_rejection_state(context)
        rejection_dict[query.message.message_id] = user_id
        admin_rejection_state[query.message.message_id] = user_id

        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n⏳ {admin_name} відхиляє запит...\n\nБудь ласка, відповідайте на це повідомлення з причиною відхилення."
        )

        logger.info(f"Admin {admin_name} initiated rejection for user {user_id}, waiting for reason")


async def handle_admin_reply_or_rejection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle replies from admins (either rejection reason or response to user question)."""
    # Check if this is a reply to a message in the admin group
    if not update.message or not update.message.reply_to_message:
        return
    if update.effective_chat and update.effective_chat.type == "private":
        return

    message_id = update.message.reply_to_message.message_id
    rejection_dict = get_admin_rejection_state(context)
    support_dict = get_support_messages(context)

    # 1. Check if this is a reply to a user's support question (check SQLite and in-memory dict)
    user_id = db_get_support_user_id(message_id) or support_dict.get(message_id)
    if user_id:
        admin_name = update.message.from_user.first_name or "Адміністратор"
        prev_replied_by = db_mark_support_replied(message_id, admin_name)
        from_chat_id = update.effective_chat.id if update.effective_chat else getattr(update.message, "chat_id", None)

        try:
            # Deliver reply to user via intro message and copy_message
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✉️ Відповідь адміністратора {admin_name}:",
            )
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=from_chat_id,
                message_id=update.message.message_id,
            )

            # Update original question message in admin group to prevent duplicate replies
            reply_to_msg = update.message.reply_to_message
            orig_text = reply_to_msg.text or reply_to_msg.caption or ""
            status_tag = f"\n\n✅ Відповів: {admin_name}"
            if status_tag not in orig_text:
                try:
                    if getattr(reply_to_msg, "text", None):
                        await context.bot.edit_message_text(
                            chat_id=from_chat_id,
                            message_id=message_id,
                            text=orig_text + status_tag,
                        )
                    elif getattr(reply_to_msg, "caption", None):
                        await context.bot.edit_message_caption(
                            chat_id=from_chat_id,
                            message_id=message_id,
                            caption=orig_text + status_tag,
                        )
                except Exception as edit_err:
                    logger.warning(f"Could not update original support message text: {edit_err}")

            if prev_replied_by:
                await update.message.reply_text(
                    f"ℹ️ Увага: на це питання раніше вже відповів {prev_replied_by}.\n"
                    f"Додаткову відповідь також доставлено користувачеві (ID: {user_id})."
                )
            else:
                await update.message.reply_text(f"✅ Відповідь надіслано користувачеві (ID: {user_id}).")
            logger.info(f"Admin {admin_name} delivered reply to user {user_id} via copyMessage")
        except Exception as e:
            logger.error(f"Failed to deliver admin reply to user {user_id}: {e}")
            await update.message.reply_text(f"❌ Не вдалося надіслати відповідь користувачеві: {e}")
        return

    # 2. Check if this is a rejection reason
    if message_id not in rejection_dict:
        return

    user_id = rejection_dict[message_id]
    rejection_reason = (update.message.text or update.message.caption or "").strip()
    if not rejection_reason:
        await update.message.reply_text("❌ Будь ласка, напишіть причину відхилення текстом.")
        return
    admin_name = update.message.from_user.first_name

    pending = get_pending_requests(context)
    if user_id not in pending:
        await update.message.reply_text("❌ Запит застарів або вже оброблений.")
        if message_id in rejection_dict:
            del rejection_dict[message_id]
        if message_id in admin_rejection_state:
            del admin_rejection_state[message_id]
        return

    # Notify user with rejection reason
    await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"❌ На жаль, ваш запит відхилено адміністратором {admin_name}.\n\n"
            f"Причина: {rejection_reason}\n\n"
            "Якщо у вас є запитання, напишіть їх сюди в чат (бот запропонує надіслати їх адмінам), "
            "або скористайтеся /start щоб почати спочатку."
        ),
        reply_markup=ReplyKeyboardRemove(),
    )

    # Update admin message
    try:
        await context.bot.edit_message_caption(
            chat_id=update.message.chat_id,
            message_id=message_id,
            caption=update.message.reply_to_message.caption + f"\n\n❌ ВІДХИЛЕНО {admin_name}\n📝 Причина: {rejection_reason}"
        )
    except Exception as e:
        logger.error(f"Error updating admin message: {e}")

    await update.message.reply_text("✅ Запит відхилено. Користувач отримав повідомлення з причиною.")

    logger.info(f"User {user_id} rejected by {admin_name} with reason: {rejection_reason}")

    # Clean up and reset conversation state
    if user_id in pending:
        del pending[user_id]
    if user_id in pending_requests:
        del pending_requests[user_id]
    if message_id in rejection_dict:
        del rejection_dict[message_id]
    if message_id in admin_rejection_state:
        del admin_rejection_state[message_id]
    await reset_user_conversation(context, user_id)


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ban a user from sending support questions (Admin only)."""
    admin_group_id = int(os.getenv("ADMIN_GROUP_ID") or ADMIN_GROUP_ID or 0)
    if not update.effective_chat or update.effective_chat.id != admin_group_id:
        return

    admin_user = update.effective_user
    admin_name = admin_user.first_name or "Адміністратор"
    target_user_id = None
    reason = "Спам"

    # Case 1: Reply to a forwarded support message
    if update.message and update.message.reply_to_message:
        rep_msg_id = update.message.reply_to_message.message_id
        target_user_id = db_get_support_user_id(rep_msg_id) or get_support_messages(context).get(rep_msg_id)
        if context.args:
            reason = " ".join(context.args)

    # Case 2: User ID passed as argument: /ban <user_id> [причина]
    elif context.args:
        try:
            target_user_id = int(context.args[0])
            if len(context.args) > 1:
                reason = " ".join(context.args[1:])
        except ValueError:
            await update.message.reply_text("❌ Формат: `/ban <user_id> [причина]` або зробіть Reply на повідомлення з питанням.")
            return

    if not target_user_id:
        await update.message.reply_text(
            "❌ Не вдалося визначити ID користувача. Зробіть Reply на повідомлення з питанням або напишіть: `/ban <user_id>`"
        )
        return

    db_ban_user(target_user_id, banned_by=admin_name, reason=reason)
    await update.message.reply_text(
        f"⛔️ Користувача `{target_user_id}` заблоковано в боті.\n"
        f"Причина: {reason}\n"
        f"Він більше не зможе надсилати запитання до адміністраторів."
    )
    logger.info(f"Admin {admin_name} banned user {target_user_id} with reason: {reason}")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unban a user from sending support questions (Admin only)."""
    admin_group_id = int(os.getenv("ADMIN_GROUP_ID") or ADMIN_GROUP_ID or 0)
    if not update.effective_chat or update.effective_chat.id != admin_group_id:
        return

    target_user_id = None
    if update.message and update.message.reply_to_message:
        rep_msg_id = update.message.reply_to_message.message_id
        target_user_id = db_get_support_user_id(rep_msg_id) or get_support_messages(context).get(rep_msg_id)
    elif context.args:
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Формат: `/unban <user_id>`")
            return

    if not target_user_id:
        await update.message.reply_text("❌ Вкажіть user_id: `/unban <user_id>` або зробіть Reply на повідомлення.")
        return

    if db_unban_user(target_user_id):
        await update.message.reply_text(f"✅ Користувача `{target_user_id}` розблоковано.")
    else:
        await update.message.reply_text(f"ℹ️ Користувач `{target_user_id}` не був заблокований.")


# Backward compatibility alias
handle_rejection_reason = handle_admin_reply_or_rejection


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Процес верифікації скасовано. Використайте /start, щоб розпочати знову.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ============================================================================
# Fallback / Informational handlers for unexpected input at each step
# ============================================================================

def extract_reply_context(update: Update) -> Optional[str]:
    """Extract a clean short preview of the message being replied to, if any."""
    if not update.message:
        return None
    reply_to = getattr(update.message, "reply_to_message", None)
    if not reply_to:
        return None
    quoted = getattr(reply_to, "text", "") or getattr(reply_to, "caption", "") or ""
    if not quoted:
        return None
    clean = quoted.strip()
    return clean[:117] + "..." if len(clean) > 120 else clean


async def phone_number_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages when waiting for contact."""
    text = (getattr(update.message, "text", "") or "").strip()
    if text:
        clean_text = text.strip().lower().replace("✉️", "").strip()
        if clean_text in ("питання адмінам", "запитання адмінам"):
            return await ask_admin_command(update, context)
        elif "власник" in clean_text:
            context.user_data["preselected_is_owner"] = True
        elif "інший" in clean_text or "користувач" in clean_text:
            context.user_data["preselected_is_owner"] = False

    keyboard = [
        [KeyboardButton("📱 Поділитися номером телефону", request_contact=True)],
        [KeyboardButton("✉️ Питання адмінам")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "📱 Будь ласка, поділіться своїм номером телефону за допомогою кнопки нижче.\n\n"
        "Якщо кнопка зникла або ви хочете почати спочатку — надішліть /start, "
        "або натисніть «✉️ Питання адмінам».",
        reply_markup=reply_markup,
    )
    return PHONE_NUMBER


async def user_type_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages when waiting for user type selection."""
    if context.user_data.get("already_registered"):
        keyboard = [
            [KeyboardButton("➕ Додати ще одну квартиру")],
            [KeyboardButton("❌ Скасувати")],
        ]
    else:
        keyboard = [
            [KeyboardButton("🏠 Я власник квартири")],
            [KeyboardButton("👥 Інший користувач")],
        ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "⚠️ Будь ласка, оберіть ваш статус за допомогою кнопок нижче:\n"
        "• «🏠 Я власник квартири» — якщо ви є власником\n"
        "• «👥 Інший користувач» — якщо ви орендар або співмешканець\n\n"
        "Або надішліть /start для перезапуску.",
        reply_markup=reply_markup,
    )
    return USER_TYPE


async def roommate_owner_phone_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages when waiting for roommate's owner search info."""
    await update.message.reply_text(
        "Будь ласка, вкажіть номер телефону власника (380XXXXXXXXX або 0XXXXXXXXX) "
        "або його Telegram @username текстом.\n\n"
        "Якщо хочете скасувати — надішліть /cancel або /start."
    )
    return ROOMMATE_OWNER_PHONE


async def document_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages when waiting for document upload."""
    await update.message.reply_text(
        "📄 Очікується фото або PDF-файл документа (договір або витяг з реєстру).\n\n"
        "Будь ласка, завантажте документ як фотографію або файл PDF.\n"
        "Якщо хочете почати спочатку — надішліть /start."
    )
    return DOCUMENT


async def apartment_number_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages when waiting for apartment number."""
    keyboard = [
        [KeyboardButton("📷 Завантажити нове фото")],
        [KeyboardButton("✏️ Ввести дані вручну")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Будь ласка, введіть номер квартири текстом, або завантажте нове фото/PDF документа.",
        reply_markup=reply_markup,
    )
    return APARTMENT_NUMBER


async def area_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages when waiting for area."""
    await update.message.reply_text(
        "Будь ласка, вкажіть загальну площу квартири в м² текстом (наприклад: 45.6):"
    )
    return AREA


async def document_type_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages when waiting for document type."""
    keyboard = [
        [KeyboardButton("📄 Договір інвестування")],
        [KeyboardButton("🏛 Право власності (витяг з реєстру)")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Будь ласка, оберіть тип документа за допомогою кнопок нижче:",
        reply_markup=reply_markup,
    )
    return DOCUMENT_TYPE


async def confirm_data_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages when waiting for data confirmation."""
    keyboard = [
        [KeyboardButton("✅ Так, все вірно")],
        [KeyboardButton("✏️ Ні, я виправлю вручну")],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Будь ласка, підтвердіть правильність даних кнопкою «✅ Так, все вірно» або оберіть «✏️ Ні, я виправлю вручну».",
        reply_markup=reply_markup,
    )
    return CONFIRM_DATA


async def waiting_approval_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reply when user sends messages while request is under admin review."""
    user_id = update.effective_user.id if update.effective_user else 0
    pending = get_pending_requests(context)

    # If request was already approved or rejected, user is no longer pending
    if user_id not in pending and user_id not in pending_requests:
        await reset_user_conversation(context, user_id)
        await unhandled_private_message(update, context)
        return ConversationHandler.END

    text = (getattr(update.message, "text", "") or getattr(update.message, "caption", "") or "").strip()
    if text:
        # User is asking something while waiting for approval — offer to send to admins
        context.user_data["pending_feedback_text"] = text
        reply_ctx = extract_reply_context(update)
        if reply_ctx:
            context.user_data["pending_feedback_reply_context"] = reply_ctx
        else:
            context.user_data.pop("pending_feedback_reply_context", None)
        keyboard = [
            [
                InlineKeyboardButton("✅ Так", callback_data="feedback_send"),
                InlineKeyboardButton("❌ Ні", callback_data="feedback_cancel"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        preview = text if len(text) <= 300 else text[:297] + "..."
        await update.message.reply_text(
            f"💬 Ви написали:\n«{preview}»\n\n"
            "Ваша заявка очікує на розгляд. Надіслати це повідомлення як запитання адмінам?",
            reply_markup=reply_markup,
        )
        return WAITING_APPROVAL

    await update.message.reply_text(
        "⏳ Ваша заявка вже передана адміністраторам і очікує на розгляд.\n\n"
        "Бот обов'язково сповістить вас, щойно статус зміниться.\n"
        "Якщо вам необхідно надіслати нову заявку — надішліть /start."
    )
    return WAITING_APPROVAL


async def waiting_owner_approval_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Reply when roommate sends messages while waiting for owner review."""
    user_id = update.effective_user.id if update.effective_user else 0
    roommates_dict = get_roommate_approval_state(context)

    if user_id not in roommates_dict and user_id not in roommate_approval_state:
        await reset_user_conversation(context, user_id)
        await unhandled_private_message(update, context)
        return ConversationHandler.END

    text = (getattr(update.message, "text", "") or getattr(update.message, "caption", "") or "").strip()
    if text:
        context.user_data["pending_feedback_text"] = text
        reply_ctx = extract_reply_context(update)
        if reply_ctx:
            context.user_data["pending_feedback_reply_context"] = reply_ctx
        else:
            context.user_data.pop("pending_feedback_reply_context", None)
        keyboard = [
            [
                InlineKeyboardButton("✅ Так", callback_data="feedback_send"),
                InlineKeyboardButton("❌ Ні", callback_data="feedback_cancel"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        preview = text if len(text) <= 300 else text[:297] + "..."
        await update.message.reply_text(
            f"💬 Ви написали:\n«{preview}»\n\n"
            "Ваш запит очікує на підтвердження власника. Надіслати це повідомлення як запитання адмінам?",
            reply_markup=reply_markup,
        )
        return WAITING_OWNER_APPROVAL

    await update.message.reply_text(
        "⏳ Запит надіслано власнику квартири і очікує на підтвердження.\n\n"
        "Щойно власник відреагує, бот надішле вам сповіщення.\n"
        "Якщо ви хочете скасувати або почати заново — надішліть /start."
    )
    return WAITING_OWNER_APPROVAL


async def unhandled_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to messages sent outside active conversation with an option to forward to admins."""
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    if not update.message:
        return

    effective_user = getattr(update, "effective_user", None)
    user_id = effective_user.id if effective_user else 0
    if user_id and db_is_banned(user_id):
        await update.message.reply_text("⛔️ Вам обмежено можливість надсилати запитання до адміністраторів.")
        return

    text = (getattr(update.message, "text", "") or getattr(update.message, "caption", "") or "").strip()

    # User clicked support button outside conversation (exact match)
    clean_text = text.strip().lower().replace("✉️", "").strip()
    if clean_text in ("питання адмінам", "запитання адмінам"):
        await ask_admin_command(update, context)
        return

    # User clicked role button outside active conversation
    if text.strip() in ("🏠 Я власник квартири", "👥 Інший користувач", "➕ Додати ще одну квартиру"):
        await start(update, context)
        return

    # User sent a file without text outside conversation
    if not text:
        await update.message.reply_text(
            "🤖 Бот не очікує цього файлу поза анкетою.\n\n"
            "Скористайтеся /start, щоб розпочати верифікацію, або /help для довідки.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Check rate limit before presenting confirmation dialog
    if user_id:
        rate_remaining = db_check_rate_limit(user_id)
        if rate_remaining is not None and rate_remaining > 0:
            rem_min = int(rate_remaining // 60)
            rem_sec = int(round(rate_remaining % 60))
            time_str = f"{rem_min} хв {rem_sec} с" if rem_min > 0 else f"{rem_sec} с"
            await update.message.reply_text(
                f"⏳ Ви надіслали багато повідомлень за короткий час.\n\n"
                f"Будь ласка, зачекайте {time_str} перед відправкою наступного запитання."
            )
            return

    # Save pending feedback text in user_data
    context.user_data["pending_feedback_text"] = text
    reply_ctx = extract_reply_context(update)
    if reply_ctx:
        context.user_data["pending_feedback_reply_context"] = reply_ctx
    else:
        context.user_data.pop("pending_feedback_reply_context", None)

    keyboard = [
        [
            InlineKeyboardButton("✅ Так", callback_data="feedback_send"),
            InlineKeyboardButton("❌ Ні", callback_data="feedback_cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    preview = text if len(text) <= 300 else text[:297] + "..."
    await update.message.reply_text(
        f"💬 Ви написали:\n«{preview}»\n\n"
        "Надіслати це адмінам?",
        reply_markup=reply_markup,
    )


async def handle_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmation buttons for sending message to admins."""
    query = update.callback_query
    await query.answer()

    if query.data == "feedback_cancel":
        context.user_data.pop("pending_feedback_text", None)
        context.user_data.pop("pending_feedback_reply_context", None)
        await query.edit_message_text(
            "Скасовано. Якщо ви хочете розпочати верифікацію — надішліть /start, "
            "або /help для отримання довідки."
        )
        return

    if query.data == "feedback_send":
        text = context.user_data.pop("pending_feedback_text", None)
        reply_context = context.user_data.pop("pending_feedback_reply_context", None)
        if not text:
            await query.edit_message_text("❌ Повідомлення застаріло. Напишіть нове запитання, якщо потрібно.")
            return

        user = update.effective_user
        user_id = user.id

        if db_is_banned(user_id):
            await query.edit_message_text("⛔️ Вам обмежено можливість надсилати запитання до адміністраторів.")
            return

        rate_remaining = db_check_rate_limit(user_id)
        if rate_remaining is not None and rate_remaining > 0:
            rem_min = int(rate_remaining // 60)
            rem_sec = int(round(rate_remaining % 60))
            time_str = f"{rem_min} хв {rem_sec} с" if rem_min > 0 else f"{rem_sec} с"
            await query.edit_message_text(
                f"⏳ Ви надіслали багато повідомлень за короткий час.\n\n"
                f"Будь ласка, зачекайте {time_str} перед відправкою наступного запитання."
            )
            return

        name_parts = [user.first_name, user.last_name]
        raw_full_name = " ".join(p for p in name_parts if p) or "Без імені"
        raw_username_str = f"@{user.username}" if user.username else "немає"
        raw_apartment_info = get_user_apartment_info(user_id, context)

        full_name = html.escape(raw_full_name)
        username_str = html.escape(raw_username_str)
        apartment_info = html.escape(raw_apartment_info)
        escaped_text = html.escape(text)

        reply_quote_html = f"↩️ <b>У відповідь на:</b> <i>«{html.escape(reply_context)}»</i>\n\n" if reply_context else ""
        admin_html = (
            "✉️ <b>Питання до адмінів</b>\n\n"
            f"👤 <b>Ім'я:</b> {full_name}\n"
            f"👥 <b>@нік:</b> {username_str}\n"
            f"🏠 <b>Квартира:</b> {apartment_info}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
            f"{reply_quote_html}"
            f"💬 <b>Повідомлення:</b>\n{escaped_text}\n\n"
            "ℹ️ <i>Щоб відповісти користувачеві, зробіть Reply на це повідомлення.</i>"
        )

        admin_chat_id = int(os.getenv("ADMIN_GROUP_ID") or ADMIN_GROUP_ID or 0)
        support_thread_id = int(os.getenv("ADMIN_SUPPORT_THREAD_ID") or 0) or None
        extra_kwargs = {}
        if support_thread_id:
            extra_kwargs["message_thread_id"] = support_thread_id

        try:
            try:
                admin_msg = await context.bot.send_message(
                    chat_id=admin_chat_id,
                    text=admin_html,
                    parse_mode="HTML",
                    **extra_kwargs,
                )
            except Exception as html_err:
                logger.warning(f"Failed to send HTML formatted support message ({html_err}), falling back to plain text")
                reply_quote_plain = f"↩️ У відповідь на: «{reply_context}»\n\n" if reply_context else ""
                plain_text = (
                    "✉️ Питання до адмінів\n\n"
                    f"👤 Ім'я: {raw_full_name}\n"
                    f"👥 @нік: {raw_username_str}\n"
                    f"🏠 Квартира: {raw_apartment_info}\n"
                    f"🆔 User ID: {user_id}\n\n"
                    f"{reply_quote_plain}"
                    f"💬 Повідомлення:\n{text}\n\n"
                    "ℹ️ Щоб відповісти користувачеві, зробіть Reply на це повідомлення."
                )
                admin_msg = await context.bot.send_message(
                    chat_id=admin_chat_id,
                    text=plain_text,
                    **extra_kwargs,
                )

            # Save mapping in SQLite and in-memory dicts
            db_save_support_message(admin_msg.message_id, user_id)
            db_update_rate_limit(user_id)

            support_msgs = get_support_messages(context)
            support_msgs[admin_msg.message_id] = user_id
            support_messages[admin_msg.message_id] = user_id

            await query.edit_message_text("Отримали, відповімо тут, у боті.")
            logger.info(f"Feedback from user {user_id} forwarded to admin group (msg_id {admin_msg.message_id})")
        except Exception as e:
            logger.error(f"Error forwarding feedback to admin group: {e}")
            await query.edit_message_text(
                "❌ Не вдалося надіслати повідомлення адміністраторам. Спробуйте пізніше або зверніться до підтримки."
            )



async def chat_member_updated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log when bot is added to a group."""
    result = update.my_chat_member
    chat = result.chat
    new_status = result.new_chat_member.status
    old_status = result.old_chat_member.status

    # Check if bot was added to a group/channel
    if chat.type in ["group", "supergroup", "channel"]:
        if old_status in ["left", "kicked"] and new_status in ["member", "administrator"]:
            logger.info(
                f"Bot added to {chat.type}: '{chat.title}'\n"
                f"Chat ID: {chat.id}\n"
                f"Status: {new_status}"
            )

            # Try to send a message with chat info
            try:
                chat_title_escaped = html.escape(chat.title or "")
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        f"✅ Бот додано до цієї групи!\n\n"
                        f"📋 Інформація про чат:\n"
                        f"Назва: {chat_title_escaped}\n"
                        f"Chat ID: <code>{chat.id}</code>\n"
                        f"Тип: {chat.type}\n\n"
                        f"Використовуйте цей Chat ID у вашій .env конфігурації."
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Could not send message to chat {chat.id}: {e}")


async def any_private_message_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for any message or button click in private chat outside active conversation."""
    if not update.effective_chat or update.effective_chat.type != "private":
        return ConversationHandler.END
    if not update.message:
        return ConversationHandler.END

    effective_user = getattr(update, "effective_user", None)
    user_id = effective_user.id if effective_user else 0
    if user_id and db_is_banned(user_id):
        await update.message.reply_text("⛔️ Вам обмежено можливість надсилати запитання до адміністраторів.")
        return ConversationHandler.END

    text = (getattr(update.message, "text", "") or getattr(update.message, "caption", "") or "").strip()
    clean_text = text.lower().replace("✉️", "").strip()

    if clean_text in ("питання адмінам", "запитання адмінам"):
        return await ask_admin_command(update, context)

    if context.user_data.get("waiting_for_admin_question"):
        context.user_data.pop("waiting_for_admin_question", None)
        await unhandled_private_message(update, context)
        return ConversationHandler.END

    return await start(update, context)


async def log_incoming_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log incoming messages in private chats to SQLite and update chat_users."""
    try:
        if not update or not getattr(update, "message", None):
            return
        msg = update.message
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        if not chat or chat.type != "private" or not user:
            return

        user_id = user.id
        first_name = user.first_name or ""
        last_name = user.last_name or ""
        username = user.username or ""

        if getattr(msg, "_chat_logged", False):
            return
        msg._chat_logged = True

        text = msg.text or msg.caption or ""
        media_type = "text"
        media_path = None
        file_id = None

        contact = getattr(msg, "contact", None)
        photos = getattr(msg, "photo", None)
        document = getattr(msg, "document", None)

        if contact:
            media_type = "contact"
            text = f"📱 Контакт: {contact.phone_number} ({contact.first_name} {contact.last_name or ''})".strip()
            db_upsert_chat_user(
                user_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
                phone=contact.phone_number,
            )
        elif photos:
            media_type = "photo"
            photo = photos[-1]
            file_id = photo.file_id
            media_path = await save_media_file(context.bot, file_id, user_id, "photo.jpg")
            if not text:
                text = "📷 [Фото]"
        elif document:
            media_type = "document"
            doc = document
            file_id = doc.file_id
            orig_name = doc.file_name or "document"
            media_path = await save_media_file(context.bot, file_id, user_id, orig_name)
            if not text:
                text = f"📄 [Документ: {orig_name}]"

        apartment = None
        user_type = None
        if context and hasattr(context, "user_data") and context.user_data:
            apartment = context.user_data.get("apartment_number")
            user_type = context.user_data.get("user_type")

        db_upsert_chat_user(
            user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
            apartment=apartment,
            user_type=user_type,
        )

        user_display_name = getattr(user, "full_name", None) or f"{first_name} {last_name}".strip() or "Користувач"
        msg_id = getattr(msg, "message_id", None)

        db_save_chat_message(
            user_id=user_id,
            chat_id=chat.id,
            message_id=msg_id,
            direction="in",
            sender_type="user",
            sender_name=user_display_name,
            text=text,
            media_type=media_type,
            media_path=media_path,
            telegram_file_id=file_id,
        )
    except Exception as e:
        logger.error(f"Error logging incoming update: {e}", exc_info=True)


def wrap_bot_for_chat_logging(bot_instance):
    """Wrap bot methods to log outgoing messages in private chats."""
    orig_send_message = bot_instance.send_message
    orig_copy_message = bot_instance.copy_message
    orig_send_photo = bot_instance.send_photo
    orig_send_document = bot_instance.send_document

    async def wrapped_send_message(chat_id, text, *args, **kwargs):
        res = await orig_send_message(chat_id, text, *args, **kwargs)
        try:
            int_chat_id = int(chat_id)
            if int_chat_id > 0:
                sender_type = kwargs.pop("_sender_type", "bot")
                sender_name = kwargs.pop("_sender_name", "Po2Bot")
                direction = "out_admin" if sender_type == "admin" else "out_bot"
                db_save_chat_message(
                    user_id=int_chat_id,
                    chat_id=int_chat_id,
                    message_id=getattr(res, "message_id", None),
                    direction=direction,
                    sender_type=sender_type,
                    sender_name=sender_name,
                    text=str(text or ""),
                    media_type="text",
                )
        except Exception as e:
            logger.debug(f"Error logging outgoing send_message: {e}")
        return res

    async def wrapped_copy_message(chat_id, from_chat_id, message_id, *args, **kwargs):
        res = await orig_copy_message(chat_id, from_chat_id, message_id, *args, **kwargs)
        try:
            int_chat_id = int(chat_id)
            if int_chat_id > 0:
                admin_name = kwargs.pop("_admin_name", "Адміністратор")
                db_save_chat_message(
                    user_id=int_chat_id,
                    chat_id=int_chat_id,
                    message_id=getattr(res, "message_id", None),
                    direction="out_admin",
                    sender_type="admin",
                    sender_name=admin_name,
                    text="✉️ [Повідомлення від адміністратора]",
                    media_type="copy",
                )
        except Exception as e:
            logger.debug(f"Error logging outgoing copy_message: {e}")
        return res

    async def wrapped_send_photo(chat_id, photo, *args, **kwargs):
        res = await orig_send_photo(chat_id, photo, *args, **kwargs)
        try:
            int_chat_id = int(chat_id)
            if int_chat_id > 0:
                caption = kwargs.get("caption") or "[Фото]"
                db_save_chat_message(
                    user_id=int_chat_id,
                    chat_id=int_chat_id,
                    message_id=getattr(res, "message_id", None),
                    direction="out_bot",
                    sender_type="bot",
                    sender_name="Po2Bot",
                    text=caption,
                    media_type="photo",
                )
        except Exception as e:
            logger.debug(f"Error logging outgoing send_photo: {e}")
        return res

    async def wrapped_send_document(chat_id, document, *args, **kwargs):
        res = await orig_send_document(chat_id, document, *args, **kwargs)
        try:
            int_chat_id = int(chat_id)
            if int_chat_id > 0:
                caption = kwargs.get("caption") or "[Документ]"
                db_save_chat_message(
                    user_id=int_chat_id,
                    chat_id=int_chat_id,
                    message_id=getattr(res, "message_id", None),
                    direction="out_bot",
                    sender_type="bot",
                    sender_name="Po2Bot",
                    text=caption,
                    media_type="document",
                )
        except Exception as e:
            logger.debug(f"Error logging outgoing send_document: {e}")
        return res

    object.__setattr__(bot_instance, "send_message", wrapped_send_message)
    object.__setattr__(bot_instance, "copy_message", wrapped_copy_message)
    object.__setattr__(bot_instance, "send_photo", wrapped_send_photo)
    object.__setattr__(bot_instance, "send_document", wrapped_send_document)


def build_application() -> Optional[Application]:
    """Build and configure the Application instance."""
    token = os.getenv("BOT_TOKEN") or BOT_TOKEN
    admin_group_id = int(os.getenv("ADMIN_GROUP_ID") or ADMIN_GROUP_ID or 0)
    private_group_id = int(os.getenv("PRIVATE_GROUP_ID") or PRIVATE_GROUP_ID or 0)

    if not token:
        logger.error("BOT_TOKEN not found in environment variables")
        return None

    if not admin_group_id:
        logger.error("ADMIN_GROUP_ID not found in environment variables")
        return None

    if not private_group_id:
        logger.error("PRIVATE_GROUP_ID not found in environment variables")
        return None

    logger.info(f"Configuration loaded - Admin Group: {admin_group_id}, Private Group: {private_group_id}")

    builder = Application.builder().token(token)

    # Configure persistence if file path is provided
    is_persistent = False

    if PERSISTENCE_FILE and PERSISTENCE_FILE.lower() not in ("none", "false", "0"):
        os.makedirs(os.path.dirname(PERSISTENCE_FILE) or ".", exist_ok=True)
        persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
        builder = builder.persistence(persistence)
        builder = builder.post_init(post_init)
        is_persistent = True

    application = builder.build()
    wrap_bot_for_chat_logging(application.bot)
    application.add_handler(TypeHandler(Update, log_incoming_update), group=-1)

    # Conversation handler
    conv_handler = ConversationHandler(
        name="verification_conversation",
        persistent=is_persistent,
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.CONTACT, phone_number_received),
            MessageHandler(
                filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.Regex(r"^✉️\s*Питання адмінам\s*$"),
                any_private_message_entry,
            ),
        ],
        states={
            PHONE_NUMBER: [
                MessageHandler(filters.Regex(r"^✉️\s*Питання адмінам\s*$"), ask_admin_command),
                MessageHandler(filters.CONTACT, phone_number_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, phone_number_fallback),
            ],
            USER_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_type_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, user_type_fallback),
            ],
            ROOMMATE_OWNER_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, roommate_owner_phone_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, roommate_owner_phone_fallback),
            ],
            DOCUMENT: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, document_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, document_fallback),
            ],
            APARTMENT_NUMBER: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, document_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, apartment_number_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, apartment_number_fallback),
            ],
            AREA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, area_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, area_fallback),
            ],
            DOCUMENT_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, document_type_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, document_type_fallback),
            ],
            CONFIRM_DATA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_data_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, confirm_data_fallback),
            ],
            WAITING_APPROVAL: [
                MessageHandler(filters.ALL & ~filters.COMMAND, waiting_approval_message),
            ],
            WAITING_OWNER_APPROVAL: [
                MessageHandler(filters.ALL & ~filters.COMMAND, waiting_owner_approval_message),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            MessageHandler(filters.Regex(r"^✉️\s*Питання адмінам\s*$"), ask_admin_command),
        ],
    )

    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(MessageHandler(filters.Regex(r"^✉️\s*Питання адмінам\s*$"), ask_admin_command))
    application.add_handler(CallbackQueryHandler(handle_feedback_callback, pattern=r"^feedback_"))
    application.add_handler(CallbackQueryHandler(approval_callback))
    application.add_handler(ChatMemberHandler(chat_member_updated, ChatMemberHandler.MY_CHAT_MEMBER))

    # Handler for admin replies (response to user question or rejection reason) in admin group
    application.add_handler(
        MessageHandler(
            ~filters.ChatType.PRIVATE & filters.REPLY & ~filters.COMMAND,
            handle_admin_reply_or_rejection,
        )
    )

    # Catch-all for private messages outside active conversation (lowest priority)
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            unhandled_private_message,
        )
    )

    return application


def main() -> None:
    """Start the bot."""
    application = build_application()
    if not application:
        return

    # Start HTTP status server
    start_status_server(port=8088)

    # Start bot
    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)



_bot_start_time = datetime.now()


def check_token_auth(handler: BaseHTTPRequestHandler) -> bool:
    """Validate Bearer token, X-API-Token header, or query param token."""
    expected = os.getenv("PO2BOT_API_TOKEN") or os.getenv("PO2BOT_API_SECRET") or PO2BOT_API_TOKEN
    if not expected:
        return True

    # 1. Authorization: Bearer <token>
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        if auth_header[7:].strip() == expected:
            return True

    # 2. X-API-Token or X-Api-Key headers
    if handler.headers.get("X-API-Token", "").strip() == expected:
        return True
    if handler.headers.get("X-Api-Key", "").strip() == expected:
        return True

    # 3. Query parameter ?token=...
    parsed = urllib.parse.urlparse(handler.path)
    qs = urllib.parse.parse_qs(parsed.query)
    if "token" in qs and qs["token"] and qs["token"][0] == expected:
        return True

    return False


class BotStatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path_only = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if path_only == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            elif path_only == "/status":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                uptime_sec = int((datetime.now() - _bot_start_time).total_seconds())
                data = {
                    "status": "running",
                    "uptime_seconds": uptime_sec,
                    "worksheet": WORKSHEET_NAME,
                    "roommates_worksheet": ROOMMATES_WORKSHEET_NAME,
                    "pending_count": len(pending_requests),
                    "google_sheets_connected": google_sheets_client is not None,
                    "openai_connected": openai_client is not None,
                }
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            elif path_only == "/pending":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                items = []
                for uid, req in pending_requests.items():
                    items.append({
                        "user_id": uid,
                        "phone": req.get("phone", ""),
                        "apartment": req.get("apartment_number", ""),
                        "user_type": req.get("user_type", ""),
                        "document_type": req.get("document_type", ""),
                        "document_media_path": req.get("document_media_path", ""),
                    })
                self.wfile.write(json.dumps({"pending": items}, ensure_ascii=False).encode("utf-8"))
            elif path_only == "/api/dialogs":
                if not check_token_auth(self):
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success":false,"error":"Unauthorized"}')
                    return
                limit = int(qs.get("limit", [50])[0])
                offset = int(qs.get("offset", [0])[0])
                search = qs.get("search", [None])[0]
                users = db_get_chat_users(limit=limit, offset=offset, search=search)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "users": users}, ensure_ascii=False).encode("utf-8"))
            elif path_only.startswith("/api/dialogs/"):
                if not check_token_auth(self):
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success":false,"error":"Unauthorized"}')
                    return
                try:
                    uid_str = path_only.split("/")[3]
                    target_uid = int(uid_str)
                    limit = int(qs.get("limit", [100])[0])
                    offset = int(qs.get("offset", [0])[0])
                    user = db_get_chat_user(target_uid)
                    messages = db_get_chat_messages(user_id=target_uid, limit=limit, offset=offset)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "user": user, "messages": messages}, ensure_ascii=False).encode("utf-8"))
                except Exception as e:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            elif path_only.startswith("/api/media/"):
                if not check_token_auth(self):
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success":false,"error":"Unauthorized"}')
                    return
                parts = path_only.strip("/").split("/")
                if len(parts) >= 4:
                    m_user_id = parts[2]
                    m_filename = parts[3]
                    if ".." in m_filename or "/" in m_filename or "\\" in m_filename:
                        self.send_response(400)
                        self.end_headers()
                        return
                    full_media_path = os.path.join(UPLOADS_DIR, m_user_id, m_filename)
                    if os.path.isfile(full_media_path):
                        ext = os.path.splitext(m_filename)[1].lower()
                        types_map = {
                            ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg",
                            ".png": "image/png",
                            ".webp": "image/webp",
                            ".pdf": "application/pdf",
                            ".heic": "image/heic",
                        }
                        c_type = types_map.get(ext, "application/octet-stream")
                        self.send_response(200)
                        self.send_header("Content-Type", c_type)
                        self.send_header("Content-Length", str(os.path.getsize(full_media_path)))
                        self.end_headers()
                        with open(full_media_path, "rb") as mf:
                            self.wfile.write(mf.read())
                        return
                self.send_response(404)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def log_message(self, format, *args):
        pass


def start_status_server(port=8088):
    try:
        server = HTTPServer(("0.0.0.0", port), BotStatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Po2Bot HTTP Status Server running on port {port}")
    except Exception as e:
        logger.error(f"Failed to start Po2Bot HTTP Status Server: {e}")


if __name__ == "__main__":
    main()
