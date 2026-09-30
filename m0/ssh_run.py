"""SSH 执行助手：python ssh_run.py "命令"（凭据读 .env，只用于运维探查/对接）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cli import load_env  # noqa: E402

load_env()

import paramiko  # noqa: E402


def run(cmd: str, timeout: int = 60):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        os.environ["SSH_HOST"],
        port=int(os.environ.get("SSH_PORT", "22")),
        username=os.environ["SSH_USER"],
        password=os.environ.get("SSH_PASS", ""),
        timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        _, out, err = client.exec_command(cmd, timeout=timeout)
        stdout = out.read().decode("utf-8", "replace")
        stderr = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        return rc, stdout, stderr
    finally:
        client.close()


if __name__ == "__main__":
    code, o, e = run(" ".join(sys.argv[1:]))
    if o:
        print(o)
    if e.strip():
        print("[stderr]", e, file=sys.stderr)
    sys.exit(code)
