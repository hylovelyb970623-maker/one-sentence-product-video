import base64
import io
import json
import secrets
import threading

from PIL import Image
import pytest
import requests

import doctor
from pipeline import config, h3_api, render, workflows
from pipeline.comfy_client import RemoteEnded
from pipeline.h3_api import H3Api
from test_production import api, picture, raw_media, submit
from webapp import server


def response(body=None, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    return result


@pytest.fixture
def client():
    instance = H3Api(secrets.token_urlsafe(32), 'https://api.minimax.cn', 'MiniMax-H3')
    yield instance
    instance.session.close()


@pytest.fixture
def product(client, tmp_path):
    path = tmp_path / 'product.png'
    path.write_bytes(picture())
    return client.upload_image(path)


def test_new_default_and_key_alias(monkeypatch):
    monkeypatch.delenv('VIDEO_ENGINE')
    assert config.video_engine() == server._engine_name() == 'h3api'
    credential = secrets.token_urlsafe(32)
    monkeypatch.setenv('MINIMAX_API_KEY', credential)
    instance = H3Api.from_env()
    assert instance.key == credential and instance.base == 'https://api.minimax.cn'
    assert instance.check() == {'engine': 'h3api', 'model': 'MiniMax-H3', 'resolution': '768P'}
    instance.session.close()
    preferred = secrets.token_urlsafe(32)
    monkeypatch.setenv('H3_API_KEY', preferred)
    instance = H3Api.from_env()
    assert instance.key == preferred
    instance.session.close()
    monkeypatch.setenv('VIDEO_ENGINE', 'mistyped-engine')
    with pytest.raises(ValueError, match='VIDEO_ENGINE'):
        config.video_engine()


@pytest.mark.parametrize('model,resolution,ready', [
    ('MiniMax-H3', '768P', True), ('MiniMax-H3', '2K', True),
    ('MiniMax-H3', '480P', False), ('MiniMax-H3-Max', '480P', True),
    ('MiniMax-H3-Max', '768P', True), ('MiniMax-H3-Max', '2K', False),
    ('unrecognized', '768P', False),
])
def test_model_resolution_contract(client, model, resolution, ready):
    client.model, client.resolution = model, resolution
    if ready:
        assert client.check()['model'] == model
    else:
        with pytest.raises(ValueError):
            client.check()


@pytest.mark.parametrize('options', [
    {'prompt': ''}, {'prompt': ' '}, {'prompt': '字' * 7001},
    {'duration': 3}, {'duration': 16}, {'duration': 5.5}, {'duration': True}, {'ratio': 'adaptive'},
])
def test_request_invalid_options_fail_before_network(client, product, options):
    arguments = {'prompt': '商品', 'reference': product} | options
    with pytest.raises(ValueError):
        client.build_request(**arguments)


def test_max_model_rejects_four_seconds(client, product):
    client.model = 'MiniMax-H3-Max'
    with pytest.raises(ValueError):
        client.build_request('商品', product, duration=4)


def test_request_limit_and_reference_roles(client, product, monkeypatch):
    payload = client.build_request('商品', product)
    payload['content'][1]['role'] = 'first_frame'
    with pytest.raises(ValueError, match='reference'):
        client.submit(payload)
    monkeypatch.setattr(h3_api, 'MAX_REQUEST_BYTES', 100)
    with pytest.raises(ValueError, match='64 MB'):
        client.build_request('商品', product)


@pytest.mark.parametrize('size', [(64, 128), (1600, 64), (64, 1600)])
def test_image_normalization_preserves_product_and_removes_metadata(client, tmp_path, size):
    path = tmp_path / 'product.jpg'
    image = Image.new('RGB', size, '#ed6020')
    exif = Image.Exif()
    exif[270] = 'private-location'
    image.save(path, exif=exif)
    registered = client.upload_image(path)
    encoded = client.build_request('商品', registered)['content'][1]['image_url']['url']
    with Image.open(io.BytesIO(base64.b64decode(encoded.split(',')[1]))) as clean:
        assert 256 <= min(clean.size) <= max(clean.size) <= 5760
        assert .4 <= clean.width / clean.height <= 2.5
        assert not clean.info and not clean.getexif()
        assert clean.getpixel((clean.width // 2, clean.height // 2))[0] > 200


def test_oversized_asset_is_rejected_before_read(client, tmp_path):
    path = tmp_path / 'large.png'
    with path.open('wb') as stream:
        stream.truncate(30 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match='30 MB'):
        client._data_url(client.upload_image(path))


@pytest.mark.parametrize('duration,fps,size,codec', [
    ('1', '24', '256x256', 'libx264'), ('2', '12', '256x256', 'libx264'),
    ('2', '24', '128x128', 'libx264'), ('2', '24', '256x256', 'mpeg4'),
])
def test_invalid_reference_video_never_submits(client, tmp_path, duration, fps, size, codec):
    path = tmp_path / 'reference.mp4'
    render.run(['-f', 'lavfi', '-i', f'color=s={size}:r={fps}', '-t', duration,
                '-c:v', codec, '-pix_fmt', 'yuv420p', path])
    with pytest.raises(ValueError, match='H3 reference video'):
        client._data_url(client.upload_image(path))


@pytest.mark.parametrize('identifier', ['../escape', 'a/b', 'a?b', '', None, 'a' * 129])
def test_query_identity_is_validated_before_request(client, identifier):
    with pytest.raises(ValueError, match='identity'):
        client.task_status(identifier)


@pytest.mark.parametrize('task', [
    None, {'id': 'different', 'status': 'succeeded'}, {'id': 'known', 'status': 'success'},
    {'id': 'known', 'status': 'succeeded', 'modality': 'text'},
    {'id': 'known', 'status': 'succeeded', 'task_type': 'h3_context_ir'},
])
def test_query_schema_never_falsely_reports_terminal(client, monkeypatch, task):
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: response({'task': task}))
    with pytest.raises(ValueError, match='response'):
        client.task_status('known')


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_remote_failure_is_explicit_and_does_not_echo_provider_body(client, monkeypatch, status):
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: response(
        {'task': {'id': 'known', 'status': status, 'error': {'message': client.key}}}))
    with pytest.raises(RemoteEnded) as caught:
        client.wait('known', poll_s=0)
    assert client.key not in str(caught.value)


def test_success_without_url_remains_uncertain(client, monkeypatch):
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: response(
        {'task': {'id': 'known', 'status': 'succeeded'}}))
    with pytest.raises(ValueError, match='without a video URL'):
        client.wait('known', poll_s=0)


@pytest.mark.parametrize('status,ended', [(400, True), (401, True), (402, True), (422, True),
                                        (429, True), (408, False), (500, False)])
def test_submit_rejection_vs_uncertain_outcome(client, product, monkeypatch, status, ended):
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: response({'error': client.key}, status))
    with pytest.raises(RemoteEnded if ended else RuntimeError) as caught:
        client.submit(client.build_request('商品', product))
    assert isinstance(caught.value, RemoteEnded) == ended
    assert client.key not in str(caught.value)


@pytest.mark.parametrize('body', [{}, {'task_id': '../escape'}, {'id': 'old-field'}, []])
def test_submit_missing_or_invalid_id_is_not_retryable(client, product, monkeypatch, body):
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: response(body))
    with pytest.raises(ValueError, match='identity'):
        client.submit(client.build_request('商品', product))


def test_network_exception_does_not_persist_secret_urls(client, monkeypatch):
    def failure(*args, **kwargs):
        raise requests.Timeout('https://cdn.hailuoai.com/output?signature=' + client.key)

    monkeypatch.setattr(client.session, 'request', failure)
    with pytest.raises(RuntimeError) as caught:
        client._request('POST', '/v2/video_generation')
    assert client.key not in str(caught.value) and 'signature=' not in str(caught.value)


def test_doctor_cloud_mode_needs_no_workflow_or_comfy(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('No ComfyUI workflow in cloud mode')

    monkeypatch.delenv('VIDEO_ENGINE')
    monkeypatch.setenv('H3_API_KEY', secrets.token_urlsafe(32))
    monkeypatch.setenv('LLM_API_KEY', secrets.token_urlsafe(32))
    monkeypatch.setenv('DIRECTOR_WORKFLOW_CONFIG', '/missing-template.json')
    monkeypatch.setattr(render, 'preflight', lambda: None)
    monkeypatch.setattr(workflows, 'load', blocked)
    result = doctor.check(online=True)
    assert not any(item['status'] == 'error' for item in result)
    assert any(item['check'] == 'minimax_config' and item['status'] == 'ok' for item in result)
    assert not any('comfy' in item['check'] or item['check'] == 'workflow' for item in result)
    assert 'not verified' in next(item['detail'] for item in result if item['check'] == 'minimax_config')


def test_unknown_reshoot_cannot_be_resolved_using_previous_task_id(api, monkeypatch, raw_media):
    finished = threading.Event()

    class Engine:
        submissions = 0

        def check(self):
            return {}

        def queue_size(self):
            return 0

        def upload_image(self, *args, **kwargs):
            return 'registered'

        def build_request(self, *args, **kwargs):
            return {}

        def submit(self, payload):
            self.submissions += 1
            if self.submissions == 2:
                raise RuntimeError('Submission outcome unknown')
            return 'previous-terminal-task'

        def wait(self, *args, **kwargs):
            return {}

        def download_outputs(self, *args, **kwargs):
            return [raw_media[0]]

        def task_status(self, identifier):
            return 'succeeded'

    engine = Engine()
    monkeypatch.setenv('VIDEO_ENGINE', 'h3api')
    monkeypatch.setattr(server.H3Api, 'from_env', lambda: engine)
    monkeypatch.setattr(server, 'frame_product_check', lambda *args: {'checked': True, 'early_product': True})
    original = server.release

    def release():
        original()
        finished.set()

    monkeypatch.setattr(server, 'release', release)
    result = submit(api)
    assert result.status_code == 202 and finished.wait(45)
    job = server.read_job(result.json()['job_id'])
    assert job['stage'] == 'attention' and job['shots'][0]['prompt_id'] is None
    assert engine.submissions == 2
    result = api.post(f'/api/admin/jobs/{job["job_id"]}/resolve', auth=('admin', server.ADMIN_PASSWORD))
    assert result.status_code == 409 and server.read_job(job['job_id'])['stage'] == 'attention'
