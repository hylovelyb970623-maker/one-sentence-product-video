"""Local configuration loading without evaluating shell syntax."""
import os
from pathlib import Path
import re


def load_env():
    configured = os.environ.get('DIRECTOR_ENV_FILE')
    if configured == '':
        return
    path = Path(configured) if configured else Path(__file__).resolve().parents[1] / '.env'
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        key, separator, value = line.strip().partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
