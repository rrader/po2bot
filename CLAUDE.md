# Po2Bot Guidelines & Architecture

## Overview
Po2Bot is a Telegram bot managing verification and private group access for Patriotica 2 (ПО 2) residents in Kyiv. It handles contact sharing, document OCR/parsing with OpenAI Vision, manual fallbacks, admin group approvals, Google Sheets synchronization, dialogue logging into SQLite, and dialogue proxying to CICD Manager.

## Critical Invariants

### 1. PTB Frozen Object Invariant
Never attempt to set custom attributes on `telegram.Message`, `telegram.Update`, or any `TelegramObject` (e.g. `msg._chat_logged = True`). PTB uses frozen objects/slots, and setting attributes raises `AttributeError`. Use bounded ring-buffer deques for deduplication (`_seen_update_ids`, `collections.deque(maxlen=5000)` based on `update.update_id`).

### 2. Bot Wrapper Kwargs Invariant
When wrapping `bot.send_message`, `bot.copy_message`, `bot.send_photo`, `bot.send_document`, always pop custom kwargs (`_sender_type`, `_sender_name`, `_admin_name`, `_admin_text`) BEFORE calling the original PTB method to prevent `TypeError`.

### 3. Log Everything Invariant
All messages, contact cards, uploaded media, ReplyKeyboard clicks, and inline `CallbackQuery` clicks (both user clicks and admin approval clicks in the admin group) must be saved into SQLite `chat_messages` and `chat_users`.

### 4. Document Types Invariant
Notary purchase agreements (`Договір купівлі-продажу`), deeds of gift, etc., are valid ownership titles.
- Button text: `🏛 Право власності (витяг, купівля-продаж тощо)`
- Canonical DB / Sheet value: `Право власності (витяг з реєстру)`
- Parser matches keywords: `["власност", "витяг", "купівл", "купл", "продаж", "даруван", "свідоцтв"]`.

### 5. Timeline Sorting Invariant
Always store timestamps with second precision (`YYYY-MM-DD HH:MM:SS`) and retrieve using `ORDER BY created_at DESC, id DESC` (reversed to `ASC`), so user and bot turns remain strictly chronological.

## Local Testing
```bash
venv/bin/pytest tests
```

## Git Pre-Push Checklist
- Committer email: `antigluk@gmail.com`
- Origin remote: `git@github-personal:rrader/po2bot.git`
- Push through `github-personal` SSH alias.
