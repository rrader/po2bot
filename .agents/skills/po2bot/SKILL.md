---
name: po2bot
description: Керує, діагностує та взаємодіє з Telegram-ботом верифікації мешканців Патріотики 2 (po2bot / Patriotica 2): архітектура розпізнавання документів (OpenAI Vision), синхронізація з Google Sheets, HTTP API діалогів (порт 8088), захист токеном, повне логування подій (повідомлення, медіа, кнопки, дії адмінів), збереження завантажень у data/uploads та відображення в дашборді cicd.rmn.pp.ua.
---

# po2bot (Patriotica 2 Telegram Verification & Support Bot)

Бот верифікації мешканців житлового масиву «Патріотика 2» (Київ, Осокорки), який автоматизує збір контактних даних, верифікацію документів на право власності (через OpenAI Vision gpt-4o-mini або ручний ввід), модерацію заявок адміністраторами, автоматичну видачу одноразових посилань у закритий чат мешканців, синхронізацію з Google Sheets та зворотний зв'язок (підтримку) через адмін-групу.

- **Репозиторій на MacBook:** `projects/po2bot` (гілка `prod`, remote `git@github-personal:rrader/po2bot.git`)
- **Хост деплою:** `monica` (`/root/services/po2bot`, Docker compose профіль `prod-public`)
- **База даних:** `/root/services/po2bot/data/bot_support.db` (SQLite)
- **Сховище медіа/документів:** `/root/services/po2bot/data/uploads/{user_id}/{filename}`
- **Веб-дашборд перегляду діалогів:** `https://cicd.rmn.pp.ua/dashboard/po2bot`

---

## 1. Архітектурні інваріанти та підводні камені

### 1.1. Заборона модифікації атрибутів об'єктів `python-telegram-bot` (PTB Frozen Objects)
> [!CAUTION]
> Об'єкти `Message`, `Update`, `User` у `python-telegram-bot` успадковують `TelegramObject` і використовують закриті слоти / заморожений `__setattr__`.
> Спроба встановити довільний атрибут (наприклад, `msg._chat_logged = True`) викликає фатальну помилку:
> `AttributeError: 'Message' object has no attribute '_chat_logged'`!
> **Правило:** Ніколи не чіпляти прапорці чи атрибути на об'єкти бібліотеки Telegram. Для дедуплікації використовувати виключно внутрішній bounded ring-buffer (наприклад, `_seen_update_ids: Set[int]` та `collections.deque(maxlen=5000)` на основі `update.update_id`).

### 1.2. Безпечне обгортання методів Bot (Kwargs Sanitation)
> [!IMPORTANT]
> При обгортанні методів бота (`send_message`, `copy_message`, `send_photo`, `send_document`) для логування вихідних повідомлень:
> Користувацькі аргументи (наприклад, `_sender_type`, `_sender_name`, `_admin_name`, `_admin_text`) **ОБОВ'ЯЗКОВО вилучати через `kwargs.pop()` ДО виклику оригінального методу Telegram**. Інакше бібліотека викине `TypeError: got an unexpected keyword argument`.

### 1.3. Повне логування взаємодії (Log Everything Invariant)
В історії діалогу в SQLite повинні фіксуватися абсолютно всі дії сторін:
1. **Звичайні повідомлення та підписи (`msg.text`, `msg.caption`).**
2. **Контакти (`msg.contact`):** зберігаються як `media_type="contact"`, автоматично витягується номер телефону та оновлюється поле `phone` в таблиці `chat_users`.
3. **Медіафайли (фото, документи, аудіо, голосові, відео, стікери, геолокація):** фото й документи автоматично завантажуються в `uploads/{user_id}/` з унікальним таймстемпом і коротким хешем file_id.
4. **Натискання інлайн-кнопок (`CallbackQuery`):**
   - У приватному чаті з користувачем — логується як вхідна дія `[Кнопка: "Текст кнопки" (callback_data)]`.
   - В адмін-групі — натискання адмінами кнопок схвалення/відхилення (`approve_<id>`, `reject_<id>`) автоматично логується у відповідний діалог мешканця із зазначенням імені адміністратора!
5. **Натискання Reply-кнопок** (`🏠 Я власник квартири`, `✏️ Ввести дані вручну` тощо).

### 1.4. Класифікація документів на право власності (Document Type Invariant)
> [!NOTE]
> Договори купівлі-продажу, договори дарування, свідоцтва про право власності — це всі документи, що посвідчують право власності, поряд із витягами з ДРРП (Державного реєстру речових прав).
> - **Назва кнопки в інтерфейсі:** `🏛 Право власності (витяг, купівля-продаж тощо)`
> - **Канонічне значення в базі та Google Sheets:** `Право власності (витяг з реєстру)`
> - **Парсер ручного тексту:** регулярний вираз або перевірка ключових слів `["власност", "витяг", "купівл", "купл", "продаж", "даруван", "свідоцтв"]` автоматично мапить введення на `"Право власності (витяг з реєстру)"`.

### 1.5. Посекундна хронологія та стабільне сортування (Timeline Sorting)
- У базі SQLite дати повідомлень зберігаються у форматі `YYYY-MM-DD HH:MM:SS`.
- При вибірці з бази використовується порядок `ORDER BY created_at DESC, id DESC`, який на бекенді реверсується (`messages.reverse()`), що забезпечує прямий хронологічний порядок `created_at ASC, id ASC`.
- В UI дашборду час рендериться з секундами `HH:MM:SS` (або `YYYY-MM-DD HH:MM:SS` для минулих дат) та явно сортується за `created_at` і `id` перед побудовою HTML.

---

## 2. Схема бази даних SQLite (`bot_support.db`)

Таблиці діалогового моніторингу:
```sql
CREATE TABLE chat_users (
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
);

CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    direction TEXT NOT NULL,       -- 'in', 'out_bot', 'out_admin'
    sender_type TEXT NOT NULL,     -- 'user', 'bot', 'admin'
    sender_name TEXT,
    text TEXT,
    media_type TEXT,              -- 'text', 'photo', 'document', 'contact', 'callback_query', 'voice', etc.
    media_path TEXT,
    telegram_file_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
Індекси:
- `idx_chat_users_updated` на `chat_users(updated_at)`
- `idx_chat_users_apartment` на `chat_users(apartment)`
- `idx_chat_users_phone` на `chat_users(phone)`
- `idx_chat_users_last_msg_at` на `chat_users(last_message_at)`
- `idx_chat_messages_user_time` на `chat_messages(user_id, created_at)`
- `idx_chat_messages_created` на `chat_messages(created_at)`
- `idx_chat_messages_user_dir` на `chat_messages(user_id, direction)`
- `idx_chat_messages_media` на `chat_messages(media_type)`

---

## 3. Внутрішній HTTP API статус-сервера (порт 8088)

Бот піднімає вбудований lightweight HTTP-сервер на порті 8088 для інтеграції з дашбордом `cicd-manager`:
- `GET /health` — простий healthcheck (`{"status":"ok"}`).
- `GET /status` — стан бота, аптайм, підключення до Google Sheets та OpenAI, кількість очікуючих заявок.
- `GET /pending` — список заявок на верифікацію, що чекають апруву адмінів.
- `GET /api/dialogs?limit=50&offset=0&search=...` — пагінований список користувачів та останніх повідомлень.
  - Потребує автентифікації заголовком `Authorization: Bearer <PO2BOT_API_TOKEN>` або `X-API-Token: <token>`.
- `GET /api/dialogs/<user_id>?limit=100&offset=0` — хронологічна історія повідомлень для конкретного мешканця.
- `GET /api/media/<user_id>/<filename>` — захищена віддача завантажених фото/файлів із перевіркою відносних шляхів проти path traversal.

---

## 4. Деплой та діагностика на сервері `monica`

```bash
# 1. Перевірка статусу контейнера
ssh monica "docker ps --filter 'name=po2bot'"

# 2. Перегляд останніх логів
ssh monica "docker logs po2bot --tail 100"

# 3. Деплой оновлень
ssh monica "cd /root/services/po2bot && git pull origin prod && cd /root/services && docker compose --profile prod-public up -d --build po2bot"

# 4. Перевірка бази даних
ssh monica "sqlite3 /root/services/po2bot/data/bot_support.db 'SELECT count(*) FROM chat_messages;'"

# 5. Тест локального API бота через cicd-manager
ssh monica "docker exec cicd-manager python -c 'import urllib.request, json; print(json.loads(urllib.request.urlopen(\"http://po2bot:8088/status\").read()))'"
```
