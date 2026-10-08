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

    async def reply_text(self, text, *_, **__):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self):
        self.message = _FakeTelegramMessage()


class _FakeContext:
    def __init__(self, user_data, bot_client):
        self.user_data = user_data
        self.bot = bot_client


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

    # Waiting approval fallback
    up_wait = _FakeUpdate()
    s_wait = await bot.waiting_approval_message(up_wait, context)
    assert s_wait == bot.WAITING_APPROVAL
    assert "очікує на розгляд" in up_wait.message.replies[0]


@pytest.mark.asyncio
async def test_unhandled_private_message():
    """Verify unhandled private messages send a friendly restart hint."""
    class _FakeChat:
        def __init__(self, chat_type):
            self.type = chat_type

    update = _FakeUpdate()
    update.effective_chat = _FakeChat("private")
    context = _FakeContext(user_data={}, bot_client=None)

    await bot.unhandled_private_message(update, context)
    assert len(update.message.replies) == 1
    assert "/start" in update.message.replies[0]

    # Group chats are ignored
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


