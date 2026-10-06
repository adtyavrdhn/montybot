"""Run `python -m pydantic_ai_claude_code login` to authenticate."""

from __future__ import annotations

import asyncio
import sys

from . import login


def main() -> None:
    if "login" not in sys.argv[1:]:
        print("Usage: python -m pydantic_ai_claude_code login", file=sys.stderr)
        sys.exit(2)
    asyncio.run(login())


if __name__ == "__main__":
    main()
