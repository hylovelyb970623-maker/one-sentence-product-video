"""Remove configured credentials from diagnostics before persistence or display."""
import os
import re
from urllib.parse import quote


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    for key, secret in sorted(os.environ.items(), key=lambda item: -len(item[1])):
        if len(secret) >= 4 and any(tag in key.upper() for tag in ('KEY', 'PASS', 'TOKEN', 'SECRET', 'COMFYUI_HOST', 'SSH_HOST', 'SSH_USER')):
            value = value.replace(secret, '[REDACTED]').replace(quote(secret, safe=''), '[REDACTED]')
    value = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[REDACTED]@', value)
    value = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,})', '[REDACTED]', value)
    value = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}', r'\1[REDACTED]', value)
    return value
