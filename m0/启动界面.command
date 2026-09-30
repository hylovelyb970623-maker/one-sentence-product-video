#!/bin/zsh
# Local-only launcher. Never stops an existing service or GPU task.
set -e
cd "$(dirname "$0")"
PORT=8666
if curl -fsS -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  printf '已有服务运行，保留现有任务并打开页面。\n'
  open "http://127.0.0.1:$PORT"
  exit 0
fi
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  printf '端口 8666 已被占用，未停止任何进程。请联系维护人员。\n'
  read '?按回车退出...'
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv --python 3.11
  else
    python3.11 -m venv .venv
  fi
fi
# Install dependencies on first launch and after requirements updates.
STAMP="$(shasum -a 256 requirements.txt | cut -d ' ' -f 1)"
if [[ ! -f ".venv/deps-$STAMP" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    .venv/bin/python -m pip install -r requirements.txt
  fi
  touch ".venv/deps-$STAMP"
fi
umask 077
mkdir -p out
nohup .venv/bin/python -m uvicorn webapp.server:app --host 127.0.0.1 --port $PORT >server.log 2>&1 &
for i in {1..30}; do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT"
    printf '服务就绪。关闭此窗口不会停止制作任务。\n'
    exit 0
  fi
  sleep 1
done
printf '启动失败，请由维护人员检查 server.log（不要公开分享日志）。\n'
read '?按回车退出...'
