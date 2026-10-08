"""Copy only nonempty, allowlisted Actions secrets over SSH stdin, never command arguments or logs.

The VM keeps existing values when a repo secret is unset. This runs only during a trusted main deploy.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

KEYS = ('TYPESAFE_API_KEY', 'LOGFIRE_TOKEN', 'MONTY_EXECUTION_KEY', 'COMPOSIO_API_KEY')
TOKEN = re.compile(r'[A-Za-z0-9_.:/+=-]+')


def update(path: Path, payload: dict[str, str]) -> None:
    if any(
        key not in KEYS or not isinstance(value, str) or not TOKEN.fullmatch(value) for key, value in payload.items()
    ):
        raise ValueError('Invalid secret update')
    if not payload:
        return
    lines = path.read_text().splitlines()
    lines = [line for line in lines if line.split('=', 1)[0] not in payload]
    lines.extend(f"{key}='{value}'" for key, value in payload.items())
    # Write atomically with owner-only permissions, retaining all unrelated VM settings.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.env-')
    try:
        with os.fdopen(fd, 'w') as file:
            file.write('\n'.join(lines) + '\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main() -> None:
    if sys.argv[1] == '--apply':
        update(Path(sys.argv[2]), json.load(sys.stdin))
        return
    payload = {key: os.environ[key] for key in KEYS if os.environ.get(key)}
    if not payload:
        return
    # Bootstrap is idempotent and generates VM-local secrets only on the first deployment.
    command = 'sh ~/montybot-release/deploy/bootstrap.sh && python3 ~/montybot-release/deploy/update-secrets.py --apply /opt/montybot/.env'
    args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20']
    if key := os.environ.get('SSH_KEY'):
        args.extend(['-i', key])
    # Values never enter command arguments; stdin travels inside SSH with host-key verification.
    args.extend([sys.argv[1], 'DOMAIN=' + shlex.quote(os.environ.get('DOMAIN', '')) + ' ' + command])
    result = subprocess.run(args, input=json.dumps(payload), text=True, check=False)
    if result.returncode:
        raise SystemExit('Secret delivery failed')


if __name__ == '__main__':
    main()
