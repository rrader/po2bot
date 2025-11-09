import os
import logging
from typing import Dict
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

# Conversation states
PHONE_NUMBER, DOCUMENT, WAITING_APPROVAL = range(3)

# Store pending requests
pending_requests: Dict[int, dict] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and ask for phone number."""
    user = update.effective_user

    # Create keyboard with phone number share button
    keyboard = [
        [KeyboardButton("📱 Share Phone Number", request_contact=True)]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        f"Hi {user.first_name}! Welcome to the verification process.\n\n"
        "Please share your phone number by clicking the button below.",
        reply_markup=reply_markup,
    )

    return PHONE_NUMBER


async def phone_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle phone number and ask for document."""
    contact = update.message.contact

    if contact and contact.user_id == update.effective_user.id:
        # Store phone number
        context.user_data["phone_number"] = contact.phone_number
        context.user_data["user_id"] = update.effective_user.id
        context.user_data["username"] = update.effective_user.username
        context.user_data["first_name"] = update.effective_user.first_name
        context.user_data["last_name"] = update.effective_user.last_name

        await update.message.reply_text(
            f"✅ Phone number received: {contact.phone_number}\n\n"
            "Now, please upload a photo of your document (ID, passport, etc.)"
        )

        return DOCUMENT
    else:
        await update.message.reply_text(
            "❌ Please share your own phone number using the button."
        )
        return PHONE_NUMBER


async def document_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle document upload and send to admin group."""
    if not update.message.photo:
        await update.message.reply_text(
            "❌ Please send a photo of your document."
        )
        return DOCUMENT

    # Get the largest photo
    photo = update.message.photo[-1]
    context.user_data["document_file_id"] = photo.file_id

    user_id = context.user_data["user_id"]
    phone_number = context.user_data["phone_number"]
    username = context.user_data.get("username", "N/A")
    first_name = context.user_data.get("first_name", "")
    last_name = context.user_data.get("last_name", "")

    # Store request
    pending_requests[user_id] = {
        "phone_number": phone_number,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "document_file_id": photo.file_id,
    }

    # Create approval keyboard
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send to admin group
    logger.info(f"Sending request to admin group {ADMIN_GROUP_ID} for user {user_id}")
    try:
        await context.bot.send_photo(
            chat_id=ADMIN_GROUP_ID,
            photo=photo.file_id,
            caption=(
                "🆕 New Access Request\n\n"
                f"👤 Name: {first_name} {last_name}\n"
                f"📱 Phone: {phone_number}\n"
                f"🆔 User ID: {user_id}\n"
                f"👥 Username: @{username if username != 'N/A' else 'None'}\n\n"
                "Please review the document and approve or reject."
            ),
            reply_markup=reply_markup,
        )
        logger.info(f"Successfully sent request to admin group for user {user_id}")
    except Exception as e:
        logger.error(f"Error sending to admin group: {e}")
        raise

    await update.message.reply_text(
        "✅ Your request has been submitted!\n\n"
        "An admin will review your information and you'll be notified once approved."
    )

    return WAITING_APPROVAL


async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle approval/rejection from admin."""
    query = update.callback_query
    await query.answer()

    action, user_id_str = query.data.split("_")
    user_id = int(user_id_str)

    if user_id not in pending_requests:
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n❌ Request expired or already processed."
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
                    f"🎉 Congratulations! Your request has been approved by {admin_name}.\n\n"
                    f"Click here to join the private group:\n{invite_link.invite_link}"
                ),
            )

            # Update admin message
            await query.edit_message_caption(
                caption=query.message.caption + f"\n\n✅ APPROVED by {admin_name}"
            )

            logger.info(f"User {user_id} approved by {admin_name}")

        except Exception as e:
            logger.error(f"Error approving user {user_id}: {e}")
            await query.edit_message_caption(
                caption=query.message.caption + f"\n\n❌ Error: {str(e)}"
            )
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ There was an error processing your approval. Please contact support.",
            )

    else:  # reject
        # Notify user
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ Unfortunately, your request has been rejected by {admin_name}.",
        )

        # Update admin message
        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n❌ REJECTED by {admin_name}"
        )

        logger.info(f"User {user_id} rejected by {admin_name}")

    # Remove from pending
    del pending_requests[user_id]


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation."""
    await update.message.reply_text(
        "❌ Verification process cancelled. Use /start to begin again."
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
                        f"✅ Bot has been added to this {chat.type}!\n\n"
                        f"📋 Chat Information:\n"
                        f"Title: {chat.title}\n"
                        f"Chat ID: `{chat.id}`\n"
                        f"Type: {chat.type}\n\n"
                        f"Use this Chat ID in your .env configuration."
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
            DOCUMENT: [
                MessageHandler(filters.PHOTO, document_received),
            ],
            WAITING_APPROVAL: [],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(approval_callback))
    application.add_handler(ChatMemberHandler(chat_member_updated, ChatMemberHandler.MY_CHAT_MEMBER))

    # Start bot
    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
