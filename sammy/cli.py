"""`sammy serve` runs the app; `sammy migrate` applies the database migrations and exits;
`sammy claude-code-login` signs this machine in to a Claude Code subscription for `claude-code:` models;
`sammy telegram-webhook` points the Telegram bot at this server."""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from sammy.db import migrate
from sammy.observability import configure_observability
from sammy.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog='sammy')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve', help='run the web app and its workflows')
    commands.add_parser('migrate', help='apply database migrations')
    commands.add_parser('claude-code-login', help='sign in to a Claude Code subscription for claude-code: models')
    commands.add_parser('keys', help='print a new ENCRYPTION_KEY and web push (VAPID) keys for .env')
    commands.add_parser('telegram-webhook', help='point the Telegram bot at PUBLIC_URL (setWebhook)')
    args = parser.parse_args()
    if args.command == 'claude-code-login':
        asyncio.run(claude_code_login())
        return
    if args.command == 'keys':
        from sammy.crypto import new_key
        from sammy.notifications import new_vapid_keys

        private, public = new_vapid_keys()
        print(f'ENCRYPTION_KEY={new_key()}')
        print(f'VAPID_PRIVATE_KEY={private}')
        print(f'VAPID_PUBLIC_KEY={public}')
        return
    settings = Settings.from_environment()
    if args.command == 'migrate':
        applied = asyncio.run(migrate(settings.database_url))
        print(f'applied {len(applied)} migrations')
        return
    if args.command == 'telegram-webhook':
        asyncio.run(telegram_webhook(settings))
        return
    from sammy.app import create_app

    configure_observability(settings)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level='info', access_log=False)


async def claude_code_login() -> None:
    """Sign in through any browser. On a server the browser cannot reach the localhost callback, so the address it
    ends on can be pasted here instead. Tokens go where `CLAUDE_CODE_CREDENTIALS` and `CLAUDE_CODE_AUTH_FILE` say."""
    from sammy.vendor.claude_code import login

    async def read_pasteback() -> str | None:
        text = await asyncio.to_thread(input, 'Or paste the address your browser ended on: ')
        return text.strip() or None

    await login(read_pasteback=read_pasteback)
    print('Signed in.')


async def telegram_webhook(settings: Settings) -> None:
    """`setWebhook` to `<PUBLIC_URL>/api/channels/telegram/webhook`, with `TELEGRAM_WEBHOOK_SECRET`."""
    from sammy.channels.telegram import new_channel

    channel = new_channel(settings)
    if channel is None:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET first.')
    url = f'{settings.public_url}/api/channels/telegram/webhook'
    try:
        await channel.set_webhook(url)
    finally:
        await channel.aclose()
    print(f"Telegram now sends the bot's messages to {url}")


if __name__ == '__main__':
    main()
