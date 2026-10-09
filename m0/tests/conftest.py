import os
from pathlib import Path
import secrets
import sys
import tempfile

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_sandbox = tempfile.TemporaryDirectory(prefix='product-video-tests-')
for _key in tuple(os.environ):
    if _key.startswith(('DIRECTOR_', 'COMFYUI_', 'SSH_', 'DEEPSEEK_', 'VISION_', 'LLM_', 'H3_', 'MINIMAX_', 'VIDEO_ENGINE')):
        os.environ.pop(_key)
os.environ['DIRECTOR_ENV_FILE'] = ''
os.environ['VIDEO_ENGINE'] = 'comfyui'
os.environ['DIRECTOR_OUT'] = str(Path(_sandbox.name) / 'out')
os.environ['DIRECTOR_PRIVATE'] = str(Path(_sandbox.name) / 'private')
os.environ['DIRECTOR_ADMIN_PASSWORD'] = secrets.token_urlsafe(32)
os.environ['DIRECTOR_AGENT_TOKEN'] = secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    import requests
    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError('Tests must not call external services')

    monkeypatch.setattr(requests.Session, 'request', blocked)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', blocked)
