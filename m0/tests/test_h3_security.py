import secrets

import pytest
import requests

from pipeline.h3_api import H3Api
from test_production import api
from webapp import server


def test_download_session_never_receives_provider_key(monkeypatch):
    calls = []

    class Session:
        def __init__(self):
            self.headers = {}
            self.trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            calls.append((url, dict(self.headers), self.trust_env, kwargs))
            response = requests.Response()
            response.status_code = 200
            response._content = b'video'
            return response

    monkeypatch.setattr(requests, 'Session', Session)
    monkeypatch.setenv('H3_API_DOWNLOAD_HOSTS', 'cdn.example.test')
    credential = secrets.token_urlsafe(32)
    client = H3Api(credential, 'https://api.example.test/v1', 'configured-model')
    client._request('GET', '/status')
    client._request('GET', 'https://cdn.example.test/video.mp4', absolute=True)
    assert calls[0][1]['Authorization'] == 'Bearer ' + credential
    assert 'Authorization' not in calls[1][1]
    assert all(not entry[2] and entry[3]['allow_redirects'] is False for entry in calls)


@pytest.mark.parametrize('url', [
    'http://api.example.test/video.mp4',
    'https://unknown.example.test/video.mp4',
    'https://localhost/video.mp4',
    'https://' + 'account:password' + '@api.example.test/video.mp4',
    'https://api.example.test:9443/video.mp4',
])
def test_download_refuses_unapproved_destinations(url):
    client = H3Api(secrets.token_urlsafe(32), 'https://api.example.test/v1', 'configured-model')
    with pytest.raises(ValueError, match='Unapproved'):
        client._request('GET', url, absolute=True)


def test_api_redirect_is_never_followed(monkeypatch):
    client = H3Api(secrets.token_urlsafe(32), 'https://api.example.test/v1', 'configured-model')
    response = requests.Response()
    response.status_code = 302
    response._content_consumed = True
    response.headers['Location'] = 'https://unknown.example.test'
    monkeypatch.setattr(client.session, 'request', lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match='Redirected'):
        client._request('GET', '/status')


def test_api_key_never_uses_plain_http():
    with pytest.raises(ValueError, match='HTTPS'):
        H3Api(secrets.token_urlsafe(32), 'http://api.example.test/v1', 'configured-model')


def test_remote_identity_cannot_escape_output_directory(tmp_path):
    client = H3Api(secrets.token_urlsafe(32), 'https://api.example.test/v1', 'configured-model')
    with pytest.raises(ValueError, match='identity'):
        client.download_outputs({'task_id': '../../escape', 'download_url': 'https://api.example.test/video.mp4'}, str(tmp_path / 'output'))
    assert not list(tmp_path.rglob('*.mp4'))


def test_api_only_reads_registered_assets(tmp_path):
    private = tmp_path / 'private.txt'
    private.write_text(secrets.token_urlsafe(32))
    client = H3Api(secrets.token_urlsafe(32), 'https://api.example.test/v1', 'configured-model')
    with pytest.raises(ValueError, match='unregistered'):
        client._data_url(str(private))
    with pytest.raises(RuntimeError, match='no global queue'):
        client.queue()


def test_recovery_uses_recorded_engine_and_checks_task_status(api, monkeypatch):
    job_id = '7' * 32
    server.job_path(job_id).mkdir()
    server.save({'job_id': job_id, 'created_at': 1, 'stage': 'attention', 'engine': 'h3api',
                 'shots': [{'status': 'running', 'prompt_id': 'task-existing'}]})
    monkeypatch.setenv('VIDEO_ENGINE', 'comfyui')
    remote_state = {'status': 'generating'}

    class Client:
        def task_status(self, identifier):
            assert identifier == 'task-existing'
            return remote_state['status']

    monkeypatch.setattr(server.H3Api, 'from_env', lambda: Client())
    endpoint = f'/api/admin/jobs/{job_id}/resolve'
    assert api.post(endpoint, auth=('admin', server.ADMIN_PASSWORD)).status_code == 409
    assert server.read_job(job_id)['stage'] == 'attention'
    remote_state['status'] = 'success'
    assert api.post(endpoint, auth=('admin', server.ADMIN_PASSWORD)).status_code == 200


def test_missing_api_task_id_never_counts_as_idle(api, monkeypatch):
    job_id = '8' * 32
    server.job_path(job_id).mkdir()
    server.save({'job_id': job_id, 'created_at': 1, 'stage': 'attention', 'engine': 'h3api',
                 'shots': [{'status': 'submitting', 'prompt_id': None}]})
    monkeypatch.setattr(server.H3Api, 'from_env', lambda: object())
    response = api.post(f'/api/admin/jobs/{job_id}/resolve', auth=('admin', server.ADMIN_PASSWORD))
    assert response.status_code == 409
    assert server.read_job(job_id)['stage'] == 'attention'
