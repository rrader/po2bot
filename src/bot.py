import os
import logging
import json
from typing import Dict, Optional
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
PRIVATE_GROUP_ID = int(os.getenv("PRIVATE_GROUP_ID"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_CREDS = os.getenv("GOOGLE_SHEETS_CREDS")
SPREADSHEET_ID = "1uuGXerA9I0eHTR2fNkektO8uS47T0zR1ITZIA1pnyBM"
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Test")  # Default to "Test" for staging

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
    """Add roommate data to СпівмешканціTest worksheet."""
    if not google_sheets_client:
        logger.warning("Google Sheets client not initialized, skipping sheet update")
        return False

    try:
        spreadsheet = google_sheets_client.open_by_key(SPREADSHEET_ID)

        # Get or create Співмешканці worksheet
        worksheet_name = "СпівмешканціTest"
        try:
            sheet = spreadsheet.worksheet(worksheet_name)
        except:
            # Create worksheet if doesn't exist
            sheet = spreadsheet.add_worksheet(title=worksheet_name, rows=100, cols=10)
            # Add headers
            sheet.append_row([
                "Дата/час", "Ім'я співмешканця", "Прізвище співмешканця", "Username співмешканця",
                "Телефон співмешканця", "Ім'я власника", "Телефон власника", "Номер квартири"
            ])

        # Prepare row data
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            now,
            roommate_data.get("first_name", ""),
            roommate_data.get("last_name", ""),
            roommate_data.get("username", ""),
            roommate_data.get("phone_number", ""),
            owner_data.get("Ім'я", "") + " " + owner_data.get("Прізвище", ""),
            owner_data.get("Телефон", ""),
            apartment_number,
        ]

        sheet.append_row(row)
        logger.info(f"Successfully added roommate {roommate_data.get('user_id')} to СпівмешканціTest")
        return True

    except Exception as e:
        logger.error(f"Failed to add roommate to Google Sheets: {e}")
        return False


async def parse_document_with_openai(image_url: str) -> Optional[Dict[str, str]]:
    """Parse document image using OpenAI Vision API."""
    if not openai_client:
        logger.error("OpenAI client not initialized")
        return None

    try:
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
2. Площу квартири/приміщення (в квадратних метрах)
3. Тип документа (або "Договір інвестування" або "Право власності (витяг з реєстру)")

Якщо якась інформація не розбірлива або відсутня, вкажи null для цього поля."""
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url
                            }
                        }
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
                                "description": "Площа квартири в квадратних метрах"
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

    # Clear any previous state
    context.user_data.clear()

    # Create keyboard with phone number share button
    keyboard = [
        [KeyboardButton("📱 Поділитися номером телефону", request_contact=True)]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        f"Привіт, {user.first_name}! Ласкаво просимо до процесу верифікації.\n\n"
        "Будь ласка, поділіться своїм номером телефону, натиснувши кнопку нижче.",
        reply_markup=reply_markup,
    )

    return PHONE_NUMBER


async def phone_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle phone number and ask for user type."""
    contact = update.message.contact

    if contact and contact.user_id == update.effective_user.id:
        # Store phone number
        context.user_data["phone_number"] = contact.phone_number
        context.user_data["user_id"] = update.effective_user.id
        context.user_data["username"] = update.effective_user.username
        context.user_data["first_name"] = update.effective_user.first_name
        context.user_data["last_name"] = update.effective_user.last_name

        # Ask if owner or roommate
        keyboard = [
            [KeyboardButton("🏠 Я власник квартири")],
            [KeyboardButton("👥 Я співмешканець")],
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


async def user_type_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user type selection."""
    user_type = update.message.text.strip()

    if "власник" in user_type.lower():
        # Owner flow - ask for document
        context.user_data["is_owner"] = True

        await update.message.reply_text(
            "Тепер, будь ласка, завантажте фото договору інвестування/купівлі або витягу з реєстру.\n\n"
            "⚠️ Можете заблюрити всі особисті дані, які вважаєте за потрібне.\n"
            "Головне, щоб було видно:\n"
            "• Номер приміщення\n"
            "• Площу"
        )

        return DOCUMENT

    elif "співмешканець" in user_type.lower():
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
    roommate_username = context.user_data.get("username", "Немає")
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
                f"👥 Запит на додавання співмешканця\n\n"
                f"👤 Ім'я: {roommate_name}\n"
                f"📱 Телефон: {roommate_phone}\n"
                f"👥 Username: @{roommate_username if roommate_username != 'Немає' else 'Немає'}\n\n"
                f"🏠 Квартира: {owner_data.get('Номер квартири')}\n\n"
                "Ця людина хоче приєднатися як співмешканець. Підтверджуєте?"
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
    if not update.message.photo:
        await update.message.reply_text(
            "❌ Будь ласка, надішліть фото договору або витягу з реєстру.\n\n"
            "Не забудьте заблюрити особисті дані, але залишити видимими номер приміщення та площу."
        )
        return DOCUMENT

    # Get the largest photo
    photo = update.message.photo[-1]
    context.user_data["document_file_id"] = photo.file_id

    # Show processing message
    processing_msg = await update.message.reply_text(
        "⏳ Обробляю документ, зачекайте..."
    )

    # Get photo URL for OpenAI
    file = await context.bot.get_file(photo.file_id)
    image_url = file.file_path

    # Try to parse document with OpenAI
    parsed_data = await parse_document_with_openai(image_url)

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
        # Failed to parse or incomplete data - ask manually
        logger.warning(f"Failed to parse document or incomplete data: {parsed_data}")
        await update.message.reply_text(
            "⚠️ Не вдалося автоматично розпізнати всі дані з документа.\n\n"
            "Будь ласка, введіть дані вручну.\n\n"
            "Спочатку вкажіть номер квартири:"
        )

        return APARTMENT_NUMBER


async def apartment_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle apartment number and ask for area."""
    apartment_number = update.message.text.strip()
    context.user_data["apartment_number"] = apartment_number

    await update.message.reply_text(
        f"✅ Номер квартири: {apartment_number}\n\n"
        "Тепер вкажіть площу квартири (в м²):"
    )

    return AREA


async def area_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle area and ask for document type."""
    area = update.message.text.strip()
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
    else:
        # User wants to correct data manually
        await update.message.reply_text(
            "Добре, введемо дані вручну.\n\n"
            "Спочатку вкажіть номер квартири:"
        )
        return APARTMENT_NUMBER


async def document_type_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle document type and show confirmation."""
    document_type = update.message.text.strip()
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
    username = context.user_data.get("username", "N/A")
    first_name = context.user_data.get("first_name", "")
    last_name = context.user_data.get("last_name", "")
    apartment_number = context.user_data.get("apartment_number", "")
    area = context.user_data.get("area", "")
    document_type = context.user_data.get("document_type", "")
    photo_file_id = context.user_data.get("document_file_id", "")

    # Store request
    pending_requests[user_id] = {
        "user_id": user_id,
        "phone_number": phone_number,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "document_file_id": photo_file_id,
        "apartment_number": apartment_number,
        "area": area,
        "document_type": document_type,
    }

    # Create approval keyboard
    keyboard = [
        [
            InlineKeyboardButton("✅ Затвердити", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{user_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send to admin group
    logger.info(f"Sending request to admin group {ADMIN_GROUP_ID} for user {user_id}")
    try:
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=photo_file_id,
            caption=(
                "🆕 Новий запит на доступ\n\n"
                f"👤 Ім'я: {first_name} {last_name}\n"
                f"📱 Телефон: {phone_number}\n"
                f"🆔 User ID: {user_id}\n"
                f"👥 Username: @{username if username != 'N/A' else 'Немає'}\n\n"
                f"🏠 Номер квартири: {apartment_number}\n"
                f"📐 Площа: {area} м²\n"
                f"📄 Тип документа: {document_type}\n\n"
                "Будь ласка, перегляньте документ та затвердьте або відхиліть заявку."
            ),
            reply_markup=reply_markup,
        )
        logger.info(f"Successfully sent request to admin group for user {user_id}")
    except Exception as e:
        logger.error(f"Error sending to admin group: {e}")
        raise

    await update.message.reply_text(
        "✅ Ваш запит надіслано!\n\n"
        "Адміністратор перегляне вашу інформацію, і ви отримаєте повідомлення після схвалення."
    )

    return WAITING_APPROVAL


async def handle_roommate_approval(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle roommate approval/rejection by owner."""
    parts = query.data.split("_")
    action = parts[0] + "_" + parts[1]  # approve_roommate or reject_roommate
    roommate_user_id = int(parts[2])

    if roommate_user_id not in roommate_approval_state:
        await query.edit_message_text(
            text=query.message.text + "\n\n❌ Запит застарів або вже оброблений."
        )
        return

    roommate_request = roommate_approval_state[roommate_user_id]
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
                    f"🎉 Вітаємо! Власник {owner_name} підтвердив вас як співмешканця.\n\n"
                    f"Натисніть тут, щоб приєднатися до приватної групи:\n{invite_link.invite_link}"
                ),
            )

            # Add to Google Sheets (Співмешканці worksheet)
            add_roommate_to_sheets(roommate_data, owner_data, apartment_number)

            # Update owner's message
            await query.edit_message_text(
                text=query.message.text + f"\n\n✅ ПІДТВЕРДЖЕНО {owner_name}"
            )

            logger.info(f"Roommate {roommate_user_id} approved by owner {owner_name}")

            # Clean up
            del roommate_approval_state[roommate_user_id]

        except Exception as e:
            logger.error(f"Error approving roommate {roommate_user_id}: {e}")
            await query.edit_message_text(
                text=query.message.text + f"\n\n❌ Помилка: {str(e)}"
            )

    else:  # reject_roommate
        # Notify roommate
        await context.bot.send_message(
            chat_id=roommate_user_id,
            text=f"❌ На жаль, власник {owner_name} відхилив ваш запит на додавання як співмешканця."
        )

        # Update owner's message
        await query.edit_message_text(
            text=query.message.text + f"\n\n❌ ВІДХИЛЕНО {owner_name}"
        )

        logger.info(f"Roommate {roommate_user_id} rejected by owner {owner_name}")

        # Clean up
        del roommate_approval_state[roommate_user_id]


async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle approval/rejection from admin or owner."""
    query = update.callback_query
    await query.answer()

    # Check if this is a roommate approval
    if query.data.startswith("approve_roommate_") or query.data.startswith("reject_roommate_"):
        return await handle_roommate_approval(query, context)

    # Regular owner approval by admin
    action, user_id_str = query.data.split("_")
    user_id = int(user_id_str)

    if user_id not in pending_requests:
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n❌ Запит застарів або вже оброблений."
        )
        return

    request_data = pending_requests[user_id]
    admin_name = query.from_user.first_name

    if action == "approve":
        try:
            # Invite user to private group
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=PRIVATE_GROUP_ID,
                member_limit=1,
            )

            # Notify user
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🎉 Вітаємо! Ваш запит схвалено адміністратором {admin_name}.\n\n"
                    f"Натисніть тут, щоб приєднатися до приватної групи:\n{invite_link.invite_link}"
                ),
            )

            # Add to Google Sheets
            add_to_google_sheets(request_data, admin_name)

            # Update admin message
            await query.edit_message_caption(
                caption=query.message.caption + f"\n\n✅ ЗАТВЕРДЖЕНО {admin_name}"
            )

            logger.info(f"User {user_id} approved by {admin_name}")

            # Remove from pending after successful approval
            del pending_requests[user_id]

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
        admin_rejection_state[query.message.message_id] = user_id

        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n⏳ {admin_name} відхиляє запит...\n\nБудь ласка, відповідайте на це повідомлення з причиною відхилення."
        )

        logger.info(f"Admin {admin_name} initiated rejection for user {user_id}, waiting for reason")


async def handle_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle rejection reason from admin."""
    # Check if this is a reply to a message waiting for rejection reason
    if not update.message.reply_to_message:
        return

    message_id = update.message.reply_to_message.message_id

    if message_id not in admin_rejection_state:
        return

    user_id = admin_rejection_state[message_id]
    rejection_reason = update.message.text.strip()
    admin_name = update.message.from_user.first_name

    if user_id not in pending_requests:
        await update.message.reply_text("❌ Запит застарів або вже оброблений.")
        del admin_rejection_state[message_id]
        return

    # Notify user with rejection reason
    await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"❌ На жаль, ваш запит відхилено адміністратором {admin_name}.\n\n"
            f"Причина: {rejection_reason}"
        ),
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

    await update.message.reply_text(f"✅ Запит відхилено. Користувач отримав повідомлення з причиною.")

    logger.info(f"User {user_id} rejected by {admin_name} with reason: {rejection_reason}")

    # Clean up
    del pending_requests[user_id]
    del admin_rejection_state[message_id]


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text(
        "❌ Процес верифікації скасовано. Використайте /start, щоб розпочати знову."
    )
    return ConversationHandler.END


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
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        f"✅ Бот додано до цієї групи!\n\n"
                        f"📋 Інформація про чат:\n"
                        f"Назва: {chat.title}\n"
                        f"Chat ID: `{chat.id}`\n"
                        f"Тип: {chat.type}\n\n"
                        f"Використовуйте цей Chat ID у вашій .env конфігурації."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Could not send message to chat {chat.id}: {e}")


def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in environment variables")
        return

    if not ADMIN_GROUP_ID:
        logger.error("ADMIN_GROUP_ID not found in environment variables")
        return

    if not PRIVATE_GROUP_ID:
        logger.error("PRIVATE_GROUP_ID not found in environment variables")
        return

    logger.info(f"Configuration loaded - Admin Group: {ADMIN_GROUP_ID}, Private Group: {PRIVATE_GROUP_ID}")

    # Create application
    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PHONE_NUMBER: [
                MessageHandler(filters.CONTACT, phone_number_received),
            ],
            USER_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_type_received),
            ],
            ROOMMATE_OWNER_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, roommate_owner_phone_received),
            ],
            DOCUMENT: [
                MessageHandler(filters.PHOTO, document_received),
            ],
            APARTMENT_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, apartment_number_received),
            ],
            AREA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, area_received),
            ],
            DOCUMENT_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, document_type_received),
            ],
            CONFIRM_DATA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_data_received),
            ],
            WAITING_APPROVAL: [],
            WAITING_OWNER_APPROVAL: [],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start)
        ],
    )

    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(approval_callback))
    application.add_handler(ChatMemberHandler(chat_member_updated, ChatMemberHandler.MY_CHAT_MEMBER))

    # Handler for rejection reason in admin group (must be after conv_handler)
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.REPLY & ~filters.COMMAND,
            handle_rejection_reason
        )
    )

    # Start bot
    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
