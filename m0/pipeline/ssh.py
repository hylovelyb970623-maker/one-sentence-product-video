"""SSH connections with explicit host verification and optional key authentication."""
import os

import paramiko


def connect():
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if os.environ.get('SSH_KNOWN_HOSTS'):
        client.load_host_keys(os.path.expanduser(os.environ['SSH_KNOWN_HOSTS']))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            os.environ['SSH_HOST'], port=int(os.environ.get('SSH_PORT', '22')),
            username=os.environ['SSH_USER'], password=os.environ.get('SSH_PASS') or None,
            key_filename=os.path.expanduser(os.environ['SSH_KEY_FILE']) if os.environ.get('SSH_KEY_FILE') else None,
            timeout=15, look_for_keys=True, allow_agent=True,
        )
    except Exception:
        client.close()
        raise
    return client
