import base64
import io
import json
import os
from pathlib import Path
import secrets

import pytest
from PIL import Image, PngImagePlugin

import batch
from pipeline import config, llm, ssh, workflows
from pipeline.comfy_client import ComfyUI, build_ref2va
from test_production import api, picture
from webapp import server


def agent_headers():
    return {'Authorization': 'Bearer ' + os.environ['DIRECTOR_AGENT_TOKEN']}


def agent_body():
    return {'sentence': '蓝色便携水杯', 'image': 'data:image/png;base64,' + base64.b64encode(picture()).decode()}


def test_config_is_explicit_and_never_evaluates(tmp_path, monkeypatch):
    path = tmp_path / 'config.env'
    path.write_text('EXAMPLE_SETTING="literal $(do-not-run)"\n# ignored\n')
    monkeypatch.setenv('DIRECTOR_ENV_FILE', str(path))
    monkeypatch.delenv('EXAMPLE_SETTING', raising=False)
    config.load_env()
    assert os.environ['EXAMPLE_SETTING'] == 'literal $(do-not-run)'
    monkeypatch.setenv('EXAMPLE_SETTING', 'existing')
    config.load_env()
    assert os.environ['EXAMPLE_SETTING'] == 'existing'


def test_default_comfy_and_local_model_do_not_need_remote_secrets(monkeypatch):
    client = ComfyUI.from_env()
    assert client.base == 'http://127.0.0.1:8188'
    monkeypatch.setenv('LLM_BASE_URL', 'http://127.0.0.1:1234/v1')
    monkeypatch.setattr(llm, '_client', None)
    text_client = llm._get_client()
    assert text_client.api_key == 'local'
    text_client.close()


def test_ssh_rejects_unknown_host_keys(monkeypatch):
    calls = []

    class FakeSSH:
        def load_system_host_keys(self):
            calls.append('trusted-hosts')

        def set_missing_host_key_policy(self, policy):
            calls.append(type(policy).__name__)

        def connect(self, *args, **kwargs):
            assert kwargs['look_for_keys'] and kwargs['allow_agent']

    monkeypatch.setattr(ssh.paramiko, 'SSHClient', FakeSSH)
    monkeypatch.setenv('SSH_HOST', 'gpu.example.test')
    monkeypatch.setenv('SSH_USER', 'operator')
    ssh.connect()
    assert calls == ['trusted-hosts', 'RejectPolicy']


def test_workflow_mapping_supports_different_node_ids(tmp_path, monkeypatch):
    template, _ = workflows.load('ref2va')
    renames = {'3': '103', '18': '118', '21': '121', '17': '117'}
    converted = {}
    for identifier, node in template.items():
        for key, value in node['inputs'].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                node['inputs'][key] = [renames.get(value[0], value[0]), value[1]]
        converted[renames.get(identifier, identifier)] = node
    (tmp_path / 'custom.json').write_text(json.dumps(converted))
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'version': 1, 'ref2va': {'template': 'custom.json', 'nodes': {
        'generator': '103', 'product_image': '118', 'seed': '121', 'output': '117',
    }}}))
    monkeypatch.setenv('DIRECTOR_WORKFLOW_CONFIG', str(settings))
    workflow = build_ref2va('product scene', 'product.png', seed=42, ref_person_image_name='person.png', ref_video_file='previous.mp4')
    workflows.output_prefix(workflow, 'test/output')
    assert workflow['103']['inputs']['prompt'] == 'product scene'
    assert workflow['118']['inputs']['image'] == 'product.png'
    assert workflow['121']['inputs']['noise_seed'] == 42
    assert workflow['117']['inputs']['filename_prefix'] == 'test/output'
    assert workflow['103']['inputs']['ref_videos.ref_video_0'] == ['16', 0]


def test_image_metadata_is_removed():
    buffer = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('Author', 'private author')
    Image.new('RGB', (128, 128)).save(buffer, format='PNG', pnginfo=metadata)
    assert not server.normalized_image(buffer.getvalue()).info


def test_agent_disabled_without_independent_token(api, monkeypatch):
    monkeypatch.delenv('DIRECTOR_AGENT_TOKEN')
    assert api.post('/api/agent/submit', json=agent_body()).status_code == 503
    monkeypatch.setenv('DIRECTOR_AGENT_TOKEN', secrets.token_urlsafe(32))
    assert api.post('/api/agent/submit', json=agent_body(), headers={
        'Authorization': 'Bearer ' + server.ADMIN_PASSWORD,
    }).status_code == 401
    assert api.get('/api/agent/jobs/' + 'a' * 32).status_code == 401


@pytest.mark.parametrize('body', [None, [], 'bad', {'image': 'bad'}, {'sentence': '水杯', 'image': 'data:image/png;base64,%%%'}])
def test_agent_rejects_bad_json_without_internal_errors(api, body):
    response = api.post('/api/agent/submit', json=body, headers=agent_headers())
    assert response.status_code == 422
    assert not server.all_jobs()


def test_malformed_persona_is_not_silently_ignored(api):
    body = agent_body() | {'persona': 'data:image/png;base64,%%%','persona_authorized': True}
    assert api.post('/api/agent/submit', json=body, headers=agent_headers()).status_code == 422


def test_idempotency_survives_client_retry_and_rejects_changed_content(api, monkeypatch):
    submissions = []

    def launch(*args, **kwargs):
        submissions.append(args)
        job_id = 'c' * 32
        server.job_path(job_id).mkdir()
        job = {'job_id': job_id, 'stage': 'queued', 'created_at': 1,
               'request_key': kwargs['request_key'], 'request_digest': kwargs['request_digest']}
        server.save(job)
        return job_id, job

    monkeypatch.setattr(server, 'launch_job', launch)
    headers = agent_headers() | {'Idempotency-Key': 'request-' + 'a' * 24}
    first = api.post('/api/agent/submit', json=agent_body(), headers=headers)
    second = api.post('/api/agent/submit', json=agent_body(), headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() and len(submissions) == 1
    changed = api.post('/api/agent/submit', json=agent_body() | {'price': '12'}, headers=headers)
    assert changed.status_code == 409 and len(submissions) == 1


def test_secrets_redacted_on_disk_and_in_nested_admin_data(api, monkeypatch):
    sensitive = secrets.token_urlsafe(32)
    monkeypatch.setenv('VISION_API_KEY', sensitive)
    job_id = 'd' * 32
    server.job_path(job_id).mkdir()
    server.save({'job_id': job_id, 'created_at': 1, 'stage': 'error', 'diagnostic': sensitive,
                 'planning_note': sensitive, 'message': sensitive, 'shots': [{'error': sensitive}]})
    stored = server.read_job(job_id)
    assert stored['diagnostic'] == stored['planning_note'] == stored['message'] == '[REDACTED]'
    result = api.get('/api/admin/jobs', auth=('admin', server.ADMIN_PASSWORD))
    assert sensitive not in result.text


def test_restart_also_protects_planning_jobs(api):
    job_id = 'e' * 32
    server.job_path(job_id).mkdir()
    server.save({'job_id': job_id, 'created_at': 1, 'stage': 'planning', 'shots': []})
    server.recover()
    assert server.read_job(job_id)['stage'] == 'attention'


def test_batch_validates_every_item_before_submission(tmp_path):
    (tmp_path / 'product.png').write_bytes(picture())
    manifest = tmp_path / 'batch.json'
    manifest.write_text(json.dumps({'products': [
        {'id': 'first', 'sentence': '蓝色杯身', 'image': 'product.png'},
        {'id': 'second', 'sentence': '蓝色杯身', 'image': 'missing.png'},
    ]}))
    with pytest.raises(batch.BatchError):
        batch.load_manifest(manifest)


def test_batch_resumes_lost_submission_with_same_key(tmp_path, monkeypatch):
    submissions = []
    job_id = 'f' * 32

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, **kwargs):
            yield b'video-payload'

    class Session:
        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            if method == 'POST':
                submissions.append(kwargs['headers']['Idempotency-Key'])
                if len(submissions) == 1:
                    raise batch.requests.Timeout()
                return Response({'job_id': job_id})
            return Response({} if url.endswith('/video') else {'stage': 'done'})

    monkeypatch.setattr(batch.requests, 'Session', Session)
    prepared = [('cup', agent_body(), 'digest')]
    state_path, output = tmp_path / 'state.json', tmp_path / 'videos'
    token = secrets.token_urlsafe(32)
    with pytest.raises(batch.BatchError):
        batch.run_batch(prepared, 'http://127.0.0.1:8666', token, state_path, output)
    batch.run_batch(prepared, 'http://127.0.0.1:8666', token, state_path, output)
    batch.run_batch(prepared, 'http://127.0.0.1:8666', token, state_path, output)
    assert len(submissions) == 2 and submissions[0] == submissions[1]
    assert (output / 'cup.mp4').read_bytes() == b'video-payload'
    assert token not in state_path.read_text()
    with pytest.raises(batch.BatchError):
        batch.run_batch([('cup', agent_body(), 'changed')], 'http://127.0.0.1:8666', token, state_path, output)


def test_batch_refuses_nonlocal_destination(tmp_path):
    with pytest.raises(batch.BatchError, match='loopback'):
        batch.run_batch([], 'https://external.example.test', secrets.token_urlsafe(32), tmp_path / 'state', tmp_path / 'videos')
