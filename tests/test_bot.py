import json
import os
import sys
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

os.environ.setdefault("ADMIN_GROUP_ID", "0")
os.environ.setdefault("PRIVATE_GROUP_ID", "0")
os.environ.setdefault("BOT_TOKEN", "test-token")

sys.path.append(str(Path(__file__).resolve().parents[1]))
from src import bot


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(_FakeMessage(content))]


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeCompletions:
    def __init__(self):
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse(
            json.dumps(
                {
                    "apartment_number": "123",
                    "area": "45.6",
                    "document_type": "Договір інвестування",
                }
            )
        )


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAIClient:
    def __init__(self):
        self.chat = _FakeChat()


@pytest.mark.asyncio
async def test_parse_document_with_openai_base64(monkeypatch):
    fake_client = _FakeOpenAIClient()
    monkeypatch.setattr(bot, "openai_client", fake_client)

    parsed = await bot.parse_document_with_openai(
        "ZmFrZV9iYXNlNjQ=", is_base64=True, mime_type="image/png"
    )

    assert parsed == {
        "apartment_number": "123",
        "area": "45.6",
        "document_type": "Договір інвестування",
    }
    image_url = fake_client.chat.completions.last_kwargs["messages"][0]["content"][1][
        "image_url"
    ]["url"]
    assert image_url.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_parse_document_with_openai_http(monkeypatch):
    fake_client = _FakeOpenAIClient()
    monkeypatch.setattr(bot, "openai_client", fake_client)

    parsed = await bot.parse_document_with_openai(
        "https://example.com/doc.jpg", is_base64=False, mime_type="image/jpeg"
    )

    assert parsed["apartment_number"] == "123"
    assert parsed["area"] == "45.6"
    assert parsed["document_type"] == "Договір інвестування"
    image_url = fake_client.chat.completions.last_kwargs["messages"][0]["content"][1][
        "image_url"
    ]["url"]
    assert image_url == "https://example.com/doc.jpg"


@pytest.mark.asyncio
async def test_parse_document_without_client(monkeypatch):
    monkeypatch.setattr(bot, "openai_client", None)

    parsed = await bot.parse_document_with_openai("any-source")

    assert parsed is None


def test_normalize_phone_variations():
    assert bot.normalize_phone("+380501234567") == "380501234567"
    assert bot.normalize_phone("050 123 45 67") == "380501234567"
    assert bot.normalize_phone("501-234-567") == "380501234567"
    assert bot.normalize_phone("38050123456789") == "380501234567"
    assert bot.normalize_phone("invalid") == ""
    assert bot.normalize_phone("") == ""


class _FakeTelegramMessage:
    def __init__(self):
        self.replies = []
        self.text = None
        self.caption = None
        self.message_id = 100
        self.from_user = type("FakeUser", (), {"id": 12345, "first_name": "TestAdmin"})()
        self.reply_to_message = None

    async def reply_text(self, text, *_, **__):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self):
        self.message = _FakeTelegramMessage()
        self.effective_user = type(
            "FakeUser",
            (),
            {
                "id": 12345,
                "first_name": "Test",
                "last_name": "User",
                "username": "testuser",
            },
        )()
        self.effective_chat = type("FakeChat", (), {"id": 12345, "type": "private"})()


class _FakeContext:
    def __init__(self, user_data=None, bot_client=None, bot_data=None):
        self.user_data = user_data if user_data is not None else {}
        self.bot = bot_client
        self.bot_data = bot_data if bot_data is not None else {}
        self.args = []


@pytest.mark.asyncio
async def test_send_to_admin_uses_document_for_non_photo(monkeypatch):
    # Prepare fake bot methods
    fake_bot = type(
        "FakeBot",
        (),
        {
            "send_photo": AsyncMock(),
            "send_document": AsyncMock(),
        },
    )()

    user_data = {
        "user_id": 1,
        "phone_number": "+123",
        "username": "testuser",
        "first_name": "Test",
        "last_name": "User",
        "apartment_number": "12",
        "area": "34",
        "document_type": "PDF",
        "document_file_id": "file-id",
        "document_kind": "document",
    }

    update = _FakeUpdate()
    context = _FakeContext(user_data=user_data, bot_client=fake_bot)

    result_state = await bot.send_to_admin(update, context)

    assert result_state == bot.WAITING_APPROVAL
    fake_bot.send_photo.assert_not_awaited()
    fake_bot.send_document.assert_awaited_once()
    assert update.message.replies


@pytest.mark.asyncio
async def test_album_deduplication():
    """Verify that secondary photos in the same media group are ignored."""
    update1 = _FakeUpdate()
    update1.message.media_group_id = "group_123"
    update1.message.photo = None
    update1.message.document = None

    context = _FakeContext(user_data={}, bot_client=None)

    # First call with no valid media returns DOCUMENT
    state1 = await bot.document_received(update1, context)
    assert state1 == bot.DOCUMENT
    assert "group_123" in context.user_data["_processed_media_groups"]

    # Second call with the same media_group_id is immediately skipped
    update2 = _FakeUpdate()
    update2.message.media_group_id = "group_123"
    update2.message.photo = [type("PhotoSize", (), {"file_id": "photo_id"})()]
    state2 = await bot.document_received(update2, context)
    assert state2 == bot.DOCUMENT
    # It did not try to call get_file or reply
    assert len(update2.message.replies) == 0


@pytest.mark.asyncio
async def test_fallbacks_respond_and_stay_in_state():
    """Verify fallback handlers send instructions without breaking the conversation."""
    context = _FakeContext(user_data={}, bot_client=None)

    # Phone fallback
    up_phone = _FakeUpdate()
    s_phone = await bot.phone_number_fallback(up_phone, context)
    assert s_phone == bot.PHONE_NUMBER
    assert "поділіться своїм номером" in up_phone.message.replies[0]


    # User type fallback
    up_type = _FakeUpdate()
    s_type = await bot.user_type_fallback(up_type, context)
    assert s_type == bot.USER_TYPE
    assert "статус" in up_type.message.replies[0]

    # Document fallback
    up_doc = _FakeUpdate()
    s_doc = await bot.document_fallback(up_doc, context)
    assert s_doc == bot.DOCUMENT
    assert "фото або PDF" in up_doc.message.replies[0]

    # Waiting approval fallback (when request is actually pending)
    up_wait = _FakeUpdate()
    bot.pending_requests[up_wait.effective_user.id] = {}
    s_wait = await bot.waiting_approval_message(up_wait, context)
    assert s_wait == bot.WAITING_APPROVAL
    assert "очікує на розгляд" in up_wait.message.replies[0]
    bot.pending_requests.pop(up_wait.effective_user.id, None)

    # When request was resolved/rejected, sending text offers to contact admins
    up_resolved = _FakeUpdate()
    up_resolved.message.text = "???"
    s_resolved = await bot.waiting_approval_message(up_resolved, context)
    assert s_resolved == bot.ConversationHandler.END
    assert "Надіслати це адмінам?" in up_resolved.message.replies[0]


@pytest.mark.asyncio
async def test_unhandled_private_message():
    """Verify unhandled private messages send a friendly restart hint or feedback prompt."""
    class _FakeChat:
        def __init__(self, chat_type):
            self.type = chat_type

    # Case 1: no text (e.g. empty message / unexpected file)
    update = _FakeUpdate()
    update.effective_chat = _FakeChat("private")
    update.message.text = ""
    context = _FakeContext(user_data={}, bot_client=None)

    await bot.unhandled_private_message(update, context)
    assert len(update.message.replies) == 1
    assert "/start" in update.message.replies[0]

    # Case 2: arbitrary text outside conversation prompts feedback confirmation
    update_text = _FakeUpdate()
    update_text.effective_chat = _FakeChat("private")
    update_text.message.text = "Доброго дня, коли мене додадуть?"
    context_text = _FakeContext(user_data={}, bot_client=None)

    await bot.unhandled_private_message(update_text, context_text)
    assert len(update_text.message.replies) == 1
    assert "Надіслати це адмінам?" in update_text.message.replies[0]
    assert context_text.user_data["pending_feedback_text"] == "Доброго дня, коли мене додадуть?"

    # Case 3: Group chats are ignored
    group_update = _FakeUpdate()
    group_update.effective_chat = _FakeChat("supergroup")
    await bot.unhandled_private_message(group_update, context)
    assert len(group_update.message.replies) == 0


@pytest.mark.asyncio
async def test_stale_buttons_prevent_wrong_data():
    """Verify pressing stale buttons asks for the proper input instead of advancing."""
    context = _FakeContext(user_data={}, bot_client=None)

    # In APARTMENT_NUMBER, clicking "🏠 Я власник квартири" warns user
    up_apt = _FakeUpdate()
    up_apt.message.text = "🏠 Я власник квартири"
    s_apt = await bot.apartment_number_received(up_apt, context)
    assert s_apt == bot.APARTMENT_NUMBER
    assert "Зараз очікується номер квартири" in up_apt.message.replies[0]

    # In AREA, clicking stale button warns user
    up_area = _FakeUpdate()
    up_area.message.text = "✅ Так, все вірно"
    s_area = await bot.area_received(up_area, context)
    assert s_area == bot.AREA
    assert "Зараз очікується загальна площа" in up_area.message.replies[0]

    # In CONFIRM_DATA, unexpected input asks for button choice
    up_confirm = _FakeUpdate()
    up_confirm.message.text = "Привіт"
    s_confirm = await bot.confirm_data_received(up_confirm, context)
    assert s_confirm == bot.CONFIRM_DATA
    assert "підтвердіть правильність даних" in up_confirm.message.replies[0]


@pytest.mark.asyncio
async def test_build_application_and_post_init(tmp_path, monkeypatch):
    """Verify application builds properly and post_init syncs bot_data with globals."""
    monkeypatch.setenv("BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    monkeypatch.setenv("ADMIN_GROUP_ID", "-100123")
    monkeypatch.setenv("PRIVATE_GROUP_ID", "-100456")
    persist_file = str(tmp_path / "persistence.pickle")
    monkeypatch.setenv("PERSISTENCE_FILE", persist_file)

    app = bot.build_application()
    assert app is not None

    # Test post_init syncing
    app.bot_data["pending_requests"] = {999: {"test": "data"}}
    await bot.post_init(app)
    assert 999 in bot.pending_requests
    assert bot.pending_requests[999]["test"] == "data"
    # Clean up global
    bot.pending_requests.pop(999, None)


@pytest.mark.asyncio
async def test_heic_document_converted_to_jpeg(monkeypatch):
    """Verify that HEIC documents are automatically converted to JPEG for OpenAI parsing."""
    import io
    import base64
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()

    # Create dummy HEIF image bytes
    heif_buf = io.BytesIO()
    dummy_img = Image.new("RGB", (20, 20), color="blue")
    dummy_img.save(heif_buf, format="HEIF")
    heif_bytes = heif_buf.getvalue()

    # Fake Telegram File download
    class _FakeTgFile:
        def __init__(self, data):
            self.data = data

        async def download_to_drive(self, custom_path):
            with open(custom_path, "wb") as f:
                f.write(self.data)

    fake_bot = type(
        "FakeBot",
        (),
        {
            "get_file": AsyncMock(return_value=_FakeTgFile(heif_bytes)),
        },
    )()

    # Mock parse_document_with_openai to inspect received arguments
    captured_call = {}

    async def _mock_parse(source, *, is_base64=False, mime_type="image/jpeg"):
        captured_call["source"] = source
        captured_call["is_base64"] = is_base64
        captured_call["mime_type"] = mime_type
        return {
            "apartment_number": "77",
            "area": "88.5",
            "document_type": "Договір інвестування",
        }

    monkeypatch.setattr(bot, "parse_document_with_openai", _mock_parse)

    # Prepare document update with .heic file
    update = _FakeUpdate()
    update.message.photo = None
    update.message.media_group_id = None
    update.message.document = type(
        "Document",
        (),
        {
            "file_id": "heic_file_id",
            "file_name": "scan_contract.HEIC",
            "mime_type": "image/heic",
        },
    )()

    # Add delete mock to replies
    orig_reply = update.message.reply_text
    async def _reply_with_delete(text, *args, **kwargs):
        msg = type("Msg", (), {"delete": AsyncMock()})()
        await orig_reply(text, *args, **kwargs)
        return msg
    update.message.reply_text = _reply_with_delete

    context = _FakeContext(user_data={}, bot_client=fake_bot)

    next_state = await bot.document_received(update, context)

    assert next_state == bot.CONFIRM_DATA
    assert captured_call["is_base64"] is True
    assert captured_call["mime_type"] == "image/jpeg"

    # Verify that the converted base64 data is a valid JPEG readable by PIL
    jpeg_data = base64.b64decode(captured_call["source"])
    with Image.open(io.BytesIO(jpeg_data)) as converted_img:
        assert converted_img.format == "JPEG"
        assert converted_img.size == (20, 20)


@pytest.mark.asyncio
async def test_help_and_ask_admin_commands():
    """Verify /help and '✉️ Питання адмінам' buttons display instructions."""
    up_help = _FakeUpdate()
    res_help = await bot.help_command(up_help, _FakeContext())
    assert res_help == bot.ConversationHandler.END
    assert len(up_help.message.replies) == 1
    assert "Довідка та зв'язок" in up_help.message.replies[0]

    up_ask = _FakeUpdate()
    res_ask = await bot.ask_admin_command(up_ask, _FakeContext())
    assert res_ask == bot.ConversationHandler.END
    assert len(up_ask.message.replies) == 1
    assert "Напишіть ваше запитання" in up_ask.message.replies[0]


@pytest.mark.asyncio
async def test_feedback_callback_send_and_db_persistence(tmp_path, monkeypatch):
    """Verify sending feedback forwards to admin group, stores in SQLite, and confirms to user."""
    db_file = str(tmp_path / "test_support.db")
    monkeypatch.setattr(bot, "DB_PATH", db_file)
    bot.init_db(db_file)

    sent_admin_messages = []

    class _AdminMessage:
        def __init__(self, message_id):
            self.message_id = message_id

    fake_bot = type(
        "FakeBot",
        (),
        {
            "send_message": AsyncMock(
                side_effect=lambda **kwargs: (
                    sent_admin_messages.append(kwargs) or _AdminMessage(777)
                )
            ),
        },
    )()

    class _FakeCallbackQuery:
        def __init__(self, data):
            self.data = data
            self.edited_text = None

        async def answer(self):
            pass

        async def edit_message_text(self, text):
            self.edited_text = text

    # Case 1: Cancel feedback
    up_cancel = _FakeUpdate()
    up_cancel.callback_query = _FakeCallbackQuery("feedback_cancel")
    ctx_cancel = _FakeContext(user_data={"pending_feedback_text": "Скасуйте мене"})
    await bot.handle_feedback_callback(up_cancel, ctx_cancel)
    assert "pending_feedback_text" not in ctx_cancel.user_data
    assert "Скасовано" in up_cancel.callback_query.edited_text

    # Case 2: Send feedback
    up_send = _FakeUpdate()
    up_send.effective_user.id = 555666
    up_send.effective_user.first_name = "Олександр"
    up_send.effective_user.last_name = "Петренко"
    up_send.effective_user.username = "olexandr"
    up_send.callback_query = _FakeCallbackQuery("feedback_send")

    ctx_send = _FakeContext(
        user_data={
            "pending_feedback_text": "Коли підключать домофон?",
            "apartment_number": "144",
            "is_owner": True,
        },
        bot_client=fake_bot,
    )

    monkeypatch.setenv("ADMIN_GROUP_ID", "-100777")
    monkeypatch.setenv("ADMIN_SUPPORT_THREAD_ID", "42")

    await bot.handle_feedback_callback(up_send, ctx_send)

    # User got the exact requested confirmation
    assert up_send.callback_query.edited_text == "Отримали, відповімо тут, у боті."

    # Forwarded message verified
    assert len(sent_admin_messages) == 1
    admin_msg = sent_admin_messages[0]
    assert admin_msg["chat_id"] == -100777
    assert admin_msg["message_thread_id"] == 42
    assert "Олександр Петренко" in admin_msg["text"]
    assert "@olexandr" in admin_msg["text"]
    assert "кв. 144" in admin_msg["text"]
    assert "555666" in admin_msg["text"]
    assert "Коли підключать домофон?" in admin_msg["text"]

    # Verify persistent storage in SQLite
    stored_uid = bot.db_get_support_user_id(777, db_file)
    assert stored_uid == 555666


@pytest.mark.asyncio
async def test_admin_reply_copy_message_and_anti_duplicate(tmp_path, monkeypatch):
    """Verify admin reply is delivered via copyMessage and prevents silent duplicate replies."""
    db_file = str(tmp_path / "test_support.db")
    monkeypatch.setattr(bot, "DB_PATH", db_file)
    bot.init_db(db_file)
    bot.db_save_support_message(888, 555666, db_file)

    sent_user_messages = []
    copied_messages = []

    fake_bot = type(
        "FakeBot",
        (),
        {
            "send_message": AsyncMock(
                side_effect=lambda **kwargs: sent_user_messages.append(kwargs)
            ),
            "copy_message": AsyncMock(
                side_effect=lambda **kwargs: copied_messages.append(kwargs)
            ),
            "edit_message_text": AsyncMock(),
        },
    )()

    # First admin replies
    up_reply1 = _FakeUpdate()
    up_reply1.effective_chat = type("Chat", (), {"id": -100777, "type": "supergroup"})()
    up_reply1.message.message_id = 1001
    up_reply1.message.from_user.first_name = "Роман"
    up_reply1.message.reply_to_message = type(
        "OrigMsg",
        (),
        {
            "message_id": 888,
            "text": "✉️ Питання до адмінів\nПовідомлення: Як справи?",
            "caption": None,
        },
    )()

    ctx = _FakeContext(bot_client=fake_bot)
    await bot.handle_admin_reply_or_rejection(up_reply1, ctx)

    # Verified delivery to user via copyMessage
    assert len(sent_user_messages) == 1
    assert sent_user_messages[0]["chat_id"] == 555666
    assert "Роман" in sent_user_messages[0]["text"]

    assert len(copied_messages) == 1
    assert copied_messages[0]["chat_id"] == 555666
    assert copied_messages[0]["message_id"] == 1001

    # Verified original question message was tagged
    fake_bot.edit_message_text.assert_awaited_once()
    edited_call_args = fake_bot.edit_message_text.call_args[1]
    assert "✅ Відповів: Роман" in edited_call_args["text"]

    assert "Відповідь надіслано користувачеві" in up_reply1.message.replies[0]

    # Second admin tries to reply to the same question
    up_reply2 = _FakeUpdate()
    up_reply2.effective_chat = type("Chat", (), {"id": -100777, "type": "supergroup"})()
    up_reply2.message.message_id = 1002
    up_reply2.message.from_user.first_name = "Сергій"
    up_reply2.message.reply_to_message = type(
        "OrigMsg",
        (),
        {
            "message_id": 888,
            "text": "✉️ Питання до адмінів\n✅ Відповів: Роман",
            "caption": None,
        },
    )()

    await bot.handle_admin_reply_or_rejection(up_reply2, ctx)

    # Second admin receives duplicate warning
    assert "вже відповів Роман" in up_reply2.message.replies[0]


@pytest.mark.asyncio
async def test_support_rate_limit_and_ban(tmp_path, monkeypatch):
    """Verify rate limits and /ban /unban commands block spam."""
    db_file = str(tmp_path / "test_support.db")
    monkeypatch.setattr(bot, "DB_PATH", db_file)
    bot.init_db(db_file)

    monkeypatch.setenv("ADMIN_GROUP_ID", "-100777")
    monkeypatch.setattr(bot, "ADMIN_GROUP_ID", -100777)

    # 1. Test rate limit (sliding window of 10 messages per 60s)
    uid = 999111
    bot.db_update_rate_limit(uid, db_file)
    assert bot.db_check_rate_limit(uid, max_count=10, window_seconds=60, db_path=db_file) is None

    # Simulate 10 events
    for _ in range(9):
        bot.db_update_rate_limit(uid, db_file)
    rem = bot.db_check_rate_limit(uid, max_count=10, window_seconds=60, db_path=db_file)
    assert rem is not None and rem > 0

    # User gets blocked by rate limit in unhandled_private_message
    up_user = _FakeUpdate()
    up_user.effective_user.id = uid
    up_user.message.text = "Ще одне повідомлення"
    await bot.unhandled_private_message(up_user, _FakeContext())
    assert "зачекайте" in up_user.message.replies[0]

    # 2. Test /ban command by admin
    up_ban = _FakeUpdate()
    up_ban.effective_chat = type("Chat", (), {"id": -100777, "type": "supergroup"})()
    up_ban.message.from_user.first_name = "Admin"
    ctx_ban = _FakeContext()
    ctx_ban.args = [str(uid), "Спам", "у", "боті"]

    await bot.ban_command(up_ban, ctx_ban)
    assert "заблоковано" in up_ban.message.replies[0]
    assert bot.db_is_banned(uid, db_file) is True

    # Banned user is rejected
    up_banned_user = _FakeUpdate()
    up_banned_user.effective_user.id = uid
    up_banned_user.message.text = "Спроба написати"
    await bot.unhandled_private_message(up_banned_user, _FakeContext())
    assert "Вам обмежено можливість" in up_banned_user.message.replies[0]

    # 3. Test /unban command
    up_unban = _FakeUpdate()
    up_unban.effective_chat = type("Chat", (), {"id": -100777, "type": "supergroup"})()
    ctx_unban = _FakeContext()
    ctx_unban.args = [str(uid)]

    await bot.unban_command(up_unban, ctx_unban)
    assert "розблоковано" in up_unban.message.replies[0]
    assert bot.db_is_banned(uid, db_file) is False


@pytest.mark.asyncio
async def test_post_init_restores_support_messages_from_sqlite(tmp_path, monkeypatch):
    """Verify post_init loads support_messages from SQLite so admin replies work after restart."""
    db_file = str(tmp_path / "test_support.db")
    monkeypatch.setattr(bot, "DB_PATH", db_file)
    bot.init_db(db_file)
    bot.db_save_support_message(99901, 77701, db_file)

    # Clear memory dictionary to simulate cold restart
    bot.support_messages.clear()

    fake_app = type("App", (), {"bot_data": {}})()
    await bot.post_init(fake_app)

    assert bot.support_messages[99901] == 77701
    assert fake_app.bot_data["support_messages"][99901] == 77701


@pytest.mark.asyncio
async def test_question_text_containing_admin_words_triggers_confirmation(tmp_path, monkeypatch):
    """Verify that user questions mentioning 'питання адмінам' ask for confirmation instead of being treated as button clicks."""
    db_file = str(tmp_path / "test_support.db")
    monkeypatch.setattr(bot, "DB_PATH", db_file)
    bot.init_db(db_file)

    # 1. Text in unhandled_private_message
    up = _FakeUpdate()
    ctx = _FakeContext(user_data={})
    up.message.text = "Це тестове запитання адмінам"
    await bot.unhandled_private_message(up, ctx)

    assert "Надіслати це адмінам?" in up.message.replies[0]
    assert ctx.user_data.get("pending_feedback_text") == "Це тестове запитання адмінам"

    # 2. Exact button click in unhandled_private_message
    up_btn = _FakeUpdate()
    ctx_btn = _FakeContext(user_data={})
    up_btn.message.text = "✉️ Питання адмінам"
    await bot.unhandled_private_message(up_btn, ctx_btn)
    assert "наступним повідомленням" in up_btn.message.replies[0]
    assert "pending_feedback_text" not in ctx_btn.user_data

    # 3. Text in phone_number_fallback
    up_fallback = _FakeUpdate()
    ctx_fallback = _FakeContext(user_data={})
    up_fallback.message.text = "Це тестове запитання адмінам"
    res = await bot.phone_number_fallback(up_fallback, ctx_fallback)
    assert res == bot.ConversationHandler.END
    assert "Надіслати це адмінам?" in up_fallback.message.replies[0]
    assert ctx_fallback.user_data.get("pending_feedback_text") == "Це тестове запитання адмінам"

    # 4. Button in phone_number_fallback
    up_fallback_btn = _FakeUpdate()
    ctx_fallback_btn = _FakeContext(user_data={})
    up_fallback_btn.message.text = "✉️ Питання адмінам"
    res_btn = await bot.phone_number_fallback(up_fallback_btn, ctx_fallback_btn)
    assert res_btn == bot.ConversationHandler.END
    assert "прямо сюди в чат" in up_fallback_btn.message.replies[0]


@pytest.mark.asyncio
async def test_reply_in_private_chat_triggers_admin_confirmation(tmp_path, monkeypatch):
    """Verify that Telegram reply in private chat triggers confirmation and quotes context in admin group."""
    db_file = str(tmp_path / "test_support.db")
    monkeypatch.setattr(bot, "DB_PATH", db_file)
    bot.init_db(db_file)
    monkeypatch.setenv("ADMIN_GROUP_ID", "-100777")
    monkeypatch.setenv("ADMIN_SUPPORT_THREAD_ID", "42")

    sent_admin_messages = []
    fake_bot = type(
        "FakeBot",
        (),
        {
            "send_message": AsyncMock(
                side_effect=lambda **kwargs: (
                    sent_admin_messages.append(kwargs) or type("Msg", (), {"message_id": 999})()
                )
            ),
        },
    )()

    up = _FakeUpdate()
    up.effective_chat = type("Chat", (), {"id": 12345, "type": "private"})()
    up.effective_user.id = 12345
    up.effective_user.first_name = "Олена"
    up.message.reply_to_message = type(
        "OrigMsg", (), {"text": "Попереднє повідомлення від бота", "caption": None}
    )()
    up.message.text = "Уточніть деталі, будь ласка"

    ctx = _FakeContext(user_data={}, bot_client=fake_bot)

    # 1. handle_admin_reply_or_rejection must ignore private chat replies
    await bot.handle_admin_reply_or_rejection(up, ctx)
    assert len(up.message.replies) == 0

    # 2. unhandled_private_message handles reply and preserves quote context
    await bot.unhandled_private_message(up, ctx)
    assert len(up.message.replies) == 1
    assert "Надіслати це адмінам?" in up.message.replies[0]
    assert ctx.user_data.get("pending_feedback_text") == "Уточніть деталі, будь ласка"
    assert (
        ctx.user_data.get("pending_feedback_reply_context")
        == "Попереднє повідомлення від бота"
    )

    # 3. Sending feedback forwards quoted context to admin group
    class _FakeCallbackQuery:
        def __init__(self, data):
            self.data = data
            self.edited_text = None
        async def answer(self): pass
        async def edit_message_text(self, text): self.edited_text = text

    up_cb = _FakeUpdate()
    up_cb.effective_user.id = 12345
    up_cb.effective_user.first_name = "Олена"
    up_cb.callback_query = _FakeCallbackQuery("feedback_send")

    await bot.handle_feedback_callback(up_cb, ctx)
    assert up_cb.callback_query.edited_text == "Отримали, відповімо тут, у боті."
    assert len(sent_admin_messages) == 1
    admin_msg = sent_admin_messages[0]
    assert "У відповідь на:" in admin_msg["text"]
    assert "Попереднє повідомлення від бота" in admin_msg["text"]
    assert "Уточніть деталі, будь ласка" in admin_msg["text"]




