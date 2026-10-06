"""`montybot serve` runs the app; `montybot migrate` applies the database migrations and exits."""

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
    args = parser.parse_args()
    settings = Settings.from_environment()
    if args.command == 'migrate':
        applied = asyncio.run(migrate(settings.database_url))
        print(f'applied {len(applied)} migrations')
        return
    from montybot.app import create_app

    configure_observability(settings)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level='info', access_log=False)


if __name__ == '__main__':
    main()
