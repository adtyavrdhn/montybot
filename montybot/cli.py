"""`montybot serve` runs the app; `montybot migrate` applies the database migrations and exits;
`montybot claude-code-login` signs this machine in to a Claude Code subscription for `claude-code:` models."""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from montybot.db import migrate
from montybot.observability import configure_observability
from montybot.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog='montybot')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve', help='run the web app and its workflows')
    commands.add_parser('migrate', help='apply database migrations')
    commands.add_parser('claude-code-login', help='sign in to a Claude Code subscription for claude-code: models')
    args = parser.parse_args()
    if args.command == 'claude-code-login':
        asyncio.run(claude_code_login())
        return
    settings = Settings.from_environment()
    if args.command == 'migrate':
        applied = asyncio.run(migrate(settings.database_url))
        print(f'applied {len(applied)} migrations')
        return
    from montybot.app import create_app

    configure_observability(settings)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level='info', access_log=False)


async def claude_code_login() -> None:
    """Sign in through any browser. On a server the browser cannot reach the localhost callback, so the address it
    ends on can be pasted here instead. Tokens go where `CLAUDE_CODE_CREDENTIALS` and `CLAUDE_CODE_AUTH_FILE` say."""
    from montybot.vendor.claude_code import login

    async def read_pasteback() -> str | None:
        text = await asyncio.to_thread(input, 'Or paste the address your browser ended on: ')
        return text.strip() or None

    await login(read_pasteback=read_pasteback)
    print('Signed in.')


if __name__ == '__main__':
    main()
