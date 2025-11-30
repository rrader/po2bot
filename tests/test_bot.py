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
