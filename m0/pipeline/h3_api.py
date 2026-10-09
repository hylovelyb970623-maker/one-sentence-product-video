"""MiniMax H3 V2 reference-to-video, independent of ComfyUI workflows."""
import base64
import io
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import warnings

from PIL import Image, ImageOps
import requests

from . import render
from .comfy_client import RemoteEnded

MODELS = {'MiniMax-H3': ({'768P', '2K'}, 4), 'MiniMax-H3-Max': ({'480P', '768P'}, 5)}
DOWNLOAD_HOSTS = {'video-product.cdn.minimax.io', 'cdn.hailuoai.com', 'filecdn.minimax.chat'}
STATUSES = {'queued', 'running', 'succeeded', 'failed', 'cancelled'}
MAX_REQUEST_BYTES = 64 * 1024 * 1024


def task_identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid H3 task identity')
    return value


class H3Api:
    def __init__(self, api_key: str, base_url: str, model: str, resolution: str = '768P'):
        parsed = urlsplit(base_url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            raise ValueError('H3 API requires an HTTPS base URL without embedded credentials')
        self.key = api_key
        self.base = base_url.rstrip('/')
        self.model = model
        self.resolution = resolution
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers['Authorization'] = f'Bearer {api_key}'
        self._files = {}
        self._assets = {}

    @classmethod
    def from_env(cls):
        key = os.environ.get('H3_API_KEY') or os.environ.get('MINIMAX_API_KEY')
        if not key:
            raise RuntimeError('H3_API_KEY / MINIMAX_API_KEY 未配置：需要自己的 MiniMax 开放平台 Key')
        return cls(key, os.environ.get('H3_API_BASE_URL', 'https://api.minimax.cn'),
                   os.environ.get('H3_API_MODEL', 'MiniMax-H3'),
                   os.environ.get('H3_API_RESOLUTION', '768P'))

    def check(self) -> dict:
        """Configuration only; does not authenticate, submit or spend credits."""
        if not self.key or self.model not in MODELS or self.resolution not in MODELS[self.model][0]:
            raise ValueError('Check H3_API_KEY, H3_API_MODEL and H3_API_RESOLUTION against the official V2 model options')
        return {'engine': 'h3api', 'model': self.model, 'resolution': self.resolution}

    def queue_size(self) -> int:
        return 0

    def queue(self) -> dict:
        raise RuntimeError('H3 API has no global queue view; inspect each persisted task ID')

    def _task(self, identifier):
        identifier = task_identity(identifier)
        response = self._request('GET', f'/v2/query/video_generation/{identifier}')
        if response.status_code != 200:
            raise RuntimeError(f'H3 task query failed (HTTP {response.status_code}); remote state remains unknown')
        body = response.json()
        task = body.get('task') if isinstance(body, dict) else None
        if (not isinstance(task, dict) or task.get('id') != identifier or task.get('status') not in STATUSES
                or task.get('task_type', 'generation') != 'generation'
                or task.get('modality', 'video') != 'video'):
            raise ValueError('Invalid H3 task response; remote state remains unknown')
        return task

    def task_status(self, task_id: str) -> str:
        return self._task(task_id)['status']

    def upload_image(self, path: str, name: str = None) -> str:
        """Register a local image or continuity video, without uploading it yet."""
        path = Path(path)
        key = name or path.name
        if key in self._files and self._files[key] != path:
            raise ValueError('Duplicate H3 asset identity')
        self._files[key] = path
        return key

    def build_request(self, prompt, reference, *, persona=None, video=None, ratio='9:16', duration=5):
        self.check()
        content = [{'type': 'text', 'text': prompt}]
        for asset in (reference, persona):
            if asset is not None:
                content.append({'type': 'image_url', 'image_url': {'url': self._data_url(asset)},
                                'role': 'reference_image'})
        if video is not None:
            content.append({'type': 'video_url', 'video_url': {'url': self._data_url(video)},
                            'role': 'reference_video'})
        payload = {'model': self.model, 'content': content, 'resolution': self.resolution,
                   'duration': duration, 'ratio': ratio}
        self._validate_request(payload)
        return payload

    def _validate_request(self, payload):
        self.check()
        if (not isinstance(payload, dict) or set(payload) != {'model', 'content', 'resolution', 'duration', 'ratio'}
                or payload['model'] != self.model or payload['resolution'] != self.resolution
                or type(payload['duration']) is not int or not MODELS[self.model][1] <= payload['duration'] <= 15
                or payload['ratio'] not in {'9:16', '16:9'}):
            raise ValueError('Invalid H3 V2 generation options')
        content = payload['content']
        if (not isinstance(content, list) or not 2 <= len(content) <= 4 or not isinstance(content[0], dict)
                or set(content[0]) != {'type', 'text'} or content[0]['type'] != 'text'
                or not isinstance(content[0]['text'], str) or not content[0]['text'].strip()
                or len(content[0]['text']) > 7000):
            raise ValueError('H3 requires a nonempty prompt of at most 7000 characters and registered reference media')
        images, videos = 0, 0
        for item in content[1:]:
            kind = item.get('type') if isinstance(item, dict) else None
            role = {'image_url': 'reference_image', 'video_url': 'reference_video'}.get(kind)
            if (not role or set(item) != {'type', kind, 'role'} or item['role'] != role
                    or not isinstance(item[kind], dict) or set(item[kind]) != {'url'}
                    or item[kind]['url'] not in self._assets.values()
                    or not item[kind]['url'].startswith('data:image/' if kind == 'image_url' else 'data:video/')):
                raise ValueError('H3 request contains an unregistered or unsupported reference')
            images += kind == 'image_url'
            videos += kind == 'video_url'
        if not 1 <= images <= 2 or videos > 1:
            raise ValueError('This director supports one product, one optional persona and one continuity video')
        if len(json.dumps(payload, allow_nan=False).encode('utf-8')) > MAX_REQUEST_BYTES:
            raise ValueError('H3 request exceeds 64 MB; use smaller authorized media')

    def submit(self, payload: dict) -> str:
        self._validate_request(payload)
        response = self._request('POST', '/v2/video_generation', json=payload)
        if response.status_code in {400, 401, 402, 403, 404, 413, 422, 429}:
            raise RemoteEnded(f'H3 API rejected submission (HTTP {response.status_code}); check credentials, balance and input limits')
        if response.status_code != 200:
            raise RuntimeError(f'H3 submission outcome unknown (HTTP {response.status_code}); do not resubmit automatically')
        body = response.json()
        return task_identity(body.get('task_id') if isinstance(body, dict) else None)

    def wait(self, prompt_id: str, timeout_s: int = 3600, log_every_s: int = 30, poll_s: int = 10) -> dict:
        task_identity(prompt_id)
        start = time.monotonic()
        last_log = start
        failures = 0
        while time.monotonic() - start < timeout_s:
            try:
                task = self._task(prompt_id)
                failures = 0
            except (requests.RequestException, OSError, ValueError, RuntimeError):
                failures += 1
                if failures > 30:
                    raise
                time.sleep(poll_s)
                continue
            status = task['status']
            if status == 'succeeded':
                content = task.get('content')
                url = content.get('url') if isinstance(content, dict) else None
                if not isinstance(url, str) or not url:
                    raise ValueError('H3 succeeded without a video URL; inspect the existing task, do not regenerate')
                return {'task_id': prompt_id, 'download_url': url}
            if status in {'failed', 'cancelled'}:
                raise RemoteEnded(f'H3 task {status}; no automatic resubmission')
            if time.monotonic() - last_log >= log_every_s:
                print(f'[h3api] 等待中… 状态 {status}', flush=True)
                last_log = time.monotonic()
            time.sleep(poll_s)
        raise TimeoutError(f'等待 {timeout_s}s 超时；请核对已保存的任务，不要重复提交')

    def download_outputs(self, entry: dict, save_dir: str) -> list[str]:
        identifier = task_identity(entry.get('task_id'))
        url = entry.get('download_url')
        if not isinstance(url, str) or not url:
            raise RuntimeError('H3 response did not include a video download URL')
        response = self._request('GET', url, absolute=True, timeout=600)
        if response.status_code != 200:
            raise RuntimeError(f'H3 output download failed (HTTP {response.status_code}); requery the existing task for a fresh URL')
        os.makedirs(save_dir, exist_ok=True)
        out = Path(save_dir) / f'h3_{identifier}.mp4'
        out.write_bytes(response.content)
        return [str(out)]

    def _request(self, method: str, path: str, absolute: bool = False, timeout: int = 60, **kwargs):
        try:
            if absolute:
                parsed = urlsplit(path)
                allowed = DOWNLOAD_HOSTS | {urlsplit(self.base).hostname} | {
                    host.strip().lower() for host in os.environ.get('H3_API_DOWNLOAD_HOSTS', '').split(',') if host.strip()
                }
                if (parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password
                        or parsed.port not in (None, 443) or parsed.fragment):
                    raise ValueError('Unapproved video download host; verify the provider CDN before allowing it')
                with requests.Session() as downloads:
                    downloads.trust_env = False
                    response = downloads.request(method, path, timeout=timeout, allow_redirects=False, **kwargs)
            else:
                if not path.startswith('/') or path.startswith('//'):
                    raise ValueError('H3 API paths must be relative to the configured provider')
                response = self.session.request(method, f'{self.base}{path}', timeout=timeout, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise RuntimeError('H3 network request failed; inspect the persisted task before retrying') from None
        if 300 <= response.status_code < 400:
            response.close()
            raise RuntimeError('Redirected H3 API or download responses are not accepted')
        return response

    def _data_url(self, name: str) -> str:
        if name not in self._files:
            raise ValueError('H3 references an unregistered local asset')
        if name in self._assets:
            return self._assets[name]
        path = self._files[name]
        extension = path.suffix.lower()
        limit = 50 if extension in {'.mp4', '.mov'} else 30
        if path.stat().st_size > limit * 1024 * 1024:
            raise ValueError(f'H3 reference exceeds {limit} MB')
        if extension in {'.png', '.jpg', '.jpeg', '.webp'}:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(path) as source:
                    if source.format not in {'PNG', 'JPEG', 'WEBP'} or getattr(source, 'n_frames', 1) != 1:
                        raise ValueError('H3 reference must be a static supported image')
                    if source.width * source.height > 24_000_000:
                        raise ValueError('H3 reference image exceeds the local pixel limit')
                    image = ImageOps.exif_transpose(source).convert('RGB')
                    scale = min(max(1, 256 / min(image.size)), 5760 / max(image.size))
                    image = image.resize(tuple(max(1, round(value * scale)) for value in image.size), Image.Resampling.LANCZOS)
                    width = max(256, image.width, math.ceil(image.height * .4))
                    height = max(256, image.height, math.ceil(image.width / 2.5))
                    clean = Image.new('RGB', (width, height), 'white')
                    clean.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
                    buffer = io.BytesIO()
                    clean.save(buffer, 'PNG')
                    data, mime = buffer.getvalue(), 'image/png'
        elif extension in {'.mp4', '.mov'}:
            info = render.run(['-i', path, '-map', '0:v:0', '-an', '-frames:v', '1', '-f', 'null', '-']).stderr
            stream = re.search(r'Stream #[^\n]*Video: (h264|hevc)[^\n]*?,\s+(\d+)x(\d+)(?:\s|,)[^\n]*? ([\d.]+) fps', info)
            duration = re.search(r'Duration: (\d+):(\d+):([\d.]+)', info)
            if not stream or not duration:
                raise ValueError('H3 reference video requires H.264/H.265 with known duration and frame rate')
            width, height, fps = int(stream[2]), int(stream[3]), float(stream[4])
            seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
            if (not 256 <= min(width, height) <= max(width, height) <= 5760 or not .4 <= width / height <= 2.5
                    or not 23.976 <= fps <= 60 or not 2 <= seconds <= 15):
                raise ValueError('H3 reference video exceeds official duration, dimension, ratio or frame-rate limits')
            data, mime = path.read_bytes(), 'video/mp4' if extension == '.mp4' else 'video/quicktime'
        else:
            raise ValueError('Unsupported H3 reference format')
        if len(data) > limit * 1024 * 1024:
            raise ValueError(f'Normalized H3 reference exceeds {limit} MB')
        self._assets[name] = f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')
        return self._assets[name]
