# Telegram Access Bot

A Telegram bot that manages user verification and access to a private group. Users submit their phone number and document photo, which is reviewed by admins before granting access.

## Features

- 📱 Phone number verification with Telegram's native contact sharing
- 📄 Document upload (ID, passport, etc.)
- 👥 Admin approval system in a dedicated admin group
- 🔐 Automatic invite link generation for approved users
- ✅ Inline approval/rejection buttons for admins
- 📊 Detailed logging and error handling

## Workflow

1. User starts conversation with `/start`
2. Bot requests phone number (using Telegram's contact share button)
3. Bot requests document photo upload
4. Bot sends request to admin group with approval buttons
5. Admin approves or rejects the request
6. If approved, user receives invite link to private group
7. If rejected, user is notified

## Installation

### Prerequisites

- Python 3.8 or higher
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Admin group ID
- Private group ID

### Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd po2bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create `.env` file from example:
```bash
cp .env.example .env
```

4. Configure your `.env` file with:
   - `BOT_TOKEN`: Get from [@BotFather](https://t.me/BotFather)
   - `ADMIN_GROUP_ID`: Your admin group ID (negative number)
   - `PRIVATE_GROUP_ID`: Your private group ID (negative number)

### Getting Group IDs

To get group IDs:

1. Add your bot to the groups
2. Make the bot an admin with these permissions:
   - **Admin Group**: Ability to read messages
   - **Private Group**: Ability to invite users via link
3. Send a message in the group
4. Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
5. Look for `"chat":{"id":-1001234567890,...}` in the response

Alternatively, use a helper bot like [@RawDataBot](https://t.me/RawDataBot).

## Usage

Run the bot:

```bash
python src/bot.py
```

Or with poetry/pipenv:

```bash
poetry run python src/bot.py
```

### Commands

- `/start` - Begin the verification process
- `/cancel` - Cancel the current verification

## Bot Permissions

The bot needs the following permissions:

### Admin Group
- Read messages
- Send messages
- Send photos

### Private Group
- Create invite links (requires admin privileges)

## Development

### Project Structure

```
po2bot/
├── src/
│   └── bot.py          # Main bot logic
├── .env.example        # Environment variables template
├── requirements.txt    # Python dependencies
└── README.md          # This file
```

### Key Components

- **ConversationHandler**: Manages the multi-step verification flow
- **CallbackQueryHandler**: Handles admin approval/rejection buttons
- **Pending Requests**: In-memory storage for active requests

## Security Considerations

- Store bot token securely (never commit `.env` to git)
- Ensure bot has minimum required permissions
- Admin group should be private and secure
- Consider implementing rate limiting for production use
- Add `.env` to `.gitignore`

## Troubleshooting

### Bot not receiving messages
- Ensure bot is added to groups
- Check bot has correct permissions
- Verify group IDs are negative numbers

### Invite links not working
- Bot must be admin in private group
- Bot needs "Invite users via link" permission

### Document photos not uploading
- Check file size limits
- Ensure bot can send photos in admin group

## License

MIT

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.
