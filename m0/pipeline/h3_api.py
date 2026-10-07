"""MiniMax H3 官方 API 引擎（与 ComfyUI 引擎同接口，可 env 切换、A/B 对比）。

设计约定：
- 与 pipeline.comfy_client.ComfyUI 暴露同一组方法（from_env/check/queue/queue_size/
  upload_image/submit/wait/download_outputs），webapp.server.make_engine() 按
  VIDEO_ENGINE 选择引擎，任务系统/质检门/剪辑链路零改动。
- submit() 接收的仍是 build_ref2va() 产出的工作流 dict：由本客户端负责把它翻译成
  官方 API 请求（提示词/宽高/时长/种子/参考图/参考视频）。
- 所有官方字段名集中在 _payload() 一处：拿到平台文档后只需改这一个函数。
  （提交/查询/下载的 URL 路径可用 H3_API_SUBMIT_PATH 等 env 覆盖。）

已知取舍：官方 API 按秒档位返回（时长≈请求值），帧率若非 24fps，合成取前 120 帧
会把动作轻微放慢约 4%，不影响字幕同步（字幕按最终时间轴烧录）。
"""
import base64
import json
import os
import re
import time
from urllib.parse import urlsplit

import requests

from .comfy_client import RemoteEnded
from .privacy import redact
from .workflows import DEFAULT_ROLES, configuration

MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.webp': 'image/webp', '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm'}


class H3Api:
    def __init__(self, api_key: str, base_url: str, model: str):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('H3 API requires an HTTPS base URL without embedded credentials')
        self.key = api_key
        self.base = base_url.rstrip('/')
        self.model = model
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers['Authorization'] = f'Bearer {api_key}'
        self._files = {}  # upload_image 注册表：name -> 本地路径

    @classmethod
    def from_env(cls):
        if not os.environ.get('H3_API_KEY'):
            raise RuntimeError('H3_API_KEY 未配置：官方 API 引擎需要 MiniMax 开放平台的 Key')
        return cls(os.environ['H3_API_KEY'],
                   os.environ.get('H3_API_BASE_URL', 'https://api.minimaxi.com/v1'),
                   os.environ.get('H3_API_MODEL', 'MiniMax-H3'))

    # ---- 与 ComfyUI 相同的契约 ----

    def check(self) -> dict:
        """配置自检（不消耗生成额度）：Key/模型必填，URL 可达性放到首次提交时验证。"""
        if not self.key or not self.model:
            raise RuntimeError('H3 引擎配置不完整（H3_API_KEY / H3_API_MODEL）')
        return {'engine': 'h3api', 'model': self.model}

    def queue_size(self) -> int:
        return 0  # 平台侧自带排队，本地无需等待

    def queue(self) -> dict:
        raise RuntimeError('H3 API has no global queue view; inspect each persisted task ID')

    def task_status(self, task_id: str) -> str:
        response = self._request('GET', os.environ.get('H3_API_QUERY_PATH', '/query/video_generation'),
                                 params={'task_id': task_id})
        response.raise_for_status()
        return str(response.json().get('status', '')).lower()

    def upload_image(self, path: str, name: str = None) -> str:
        """注册素材（官方 API 直接随请求传输 base64，无需预上传）。"""
        key = name or os.path.basename(path)
        self._files[key] = str(path)
        return key

    def submit(self, workflow: dict) -> str:
        payload = self._payload(workflow)
        r = self._request('POST', os.environ.get('H3_API_SUBMIT_PATH', '/video_generation'), json=payload)
        if r.status_code != 200:
            raise RuntimeError(redact(f'H3 API 提交失败 {r.status_code}：{r.text[:500]}'))
        data = r.json()
        task_id = data.get('task_id') or data.get('id')
        if not task_id:
            raise RuntimeError('H3 API response did not include a task ID')
        return str(task_id)

    def wait(self, prompt_id: str, timeout_s: int = 3600, log_every_s: int = 30, poll_s: int = 5) -> dict:
        """轮询任务状态直到成功/失败；失败属于远端明确结束（RemoteEnded，可重试）。"""
        start = time.time()
        failures = 0
        while time.time() - start < timeout_s:
            try:
                path = os.environ.get('H3_API_QUERY_PATH', '/query/video_generation')
                r = self._request('GET', path, params={'task_id': prompt_id})
                r.raise_for_status()
                data = r.json()
                failures = 0
            except (requests.RequestException, OSError, ValueError):
                failures += 1
                if failures > 30:
                    raise  # 长时间不可达：按不确定态交给上层
                time.sleep(poll_s)
                continue
            status = str(data.get('status', '')).lower()
            if status in ('success', 'succeed', 'completed'):
                return {'task_id': prompt_id, 'file_id': data.get('file_id'),
                        'download_url': data.get('download_url') or data.get('file', {}).get('download_url')}
            if status in ('fail', 'failed', 'error'):
                raise RemoteEnded(redact(f'H3 API 生成失败：{json.dumps(data, ensure_ascii=False)[:800]}'))
            if int(time.time() - start) % log_every_s < 5:
                print(f"[h3api] 等待中… 已 {int(time.time() - start)}s，状态 {status or '未知'}", flush=True)
            time.sleep(poll_s)
        raise TimeoutError(f'等待 {timeout_s}s 超时')

    def download_outputs(self, entry: dict, save_dir: str) -> list[str]:
        os.makedirs(save_dir, exist_ok=True)
        url = entry.get('download_url')
        if not url and entry.get('file_id'):
            path = os.environ.get('H3_API_FILES_PATH', '/files/retrieve')
            r = self._request('GET', path, params={'file_id': entry['file_id']})
            r.raise_for_status()
            url = r.json().get('download_url') or r.json().get('file', {}).get('download_url')
        if not url:
            raise RuntimeError('H3 API response did not include a video download URL')
        task_id = str(entry.get('task_id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
            raise ValueError('Invalid H3 task identity for output download')
        r = self._request('GET', url, absolute=True, timeout=600)
        r.raise_for_status()
        # 远端文件名不可信：本地固定命名，杜绝路径穿越
        out = os.path.join(save_dir, f'h3_{task_id}.mp4')
        with open(out, 'wb') as f:
            f.write(r.content)
        return [out]

    # ---- 内部 ----

    def _request(self, method: str, path: str, absolute: bool = False, timeout: int = 60, **kw):
        if absolute:
            parsed = urlsplit(path)
            allowed = {urlsplit(self.base).hostname} | {
                host.strip().lower() for host in os.environ.get('H3_API_DOWNLOAD_HOSTS', '').split(',') if host.strip()
            }
            if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ValueError('Unapproved video download host; configure H3_API_DOWNLOAD_HOSTS after verifying the provider CDN')
            with requests.Session() as downloads:
                downloads.trust_env = False
                response = downloads.request(method, path, timeout=timeout, allow_redirects=False, **kw)
        else:
            if not path.startswith('/') or path.startswith('//'):
                raise ValueError('H3 API paths must be relative to the configured provider')
            response = self.session.request(method, f'{self.base}{path}', timeout=timeout, allow_redirects=False, **kw)
        if 300 <= response.status_code < 400:
            response.close()
            raise RuntimeError('Redirected H3 API or download responses are not accepted')
        return response

    def _data_url(self, name: str) -> str:
        if name not in self._files:
            raise ValueError('Workflow references an unregistered local asset')
        path = self._files[name]
        with open(path, 'rb') as f:
            mime = MIME.get(os.path.splitext(path)[1].lower(), 'application/octet-stream')
            return f'data:{mime};base64,' + base64.b64encode(f.read()).decode()

    def _payload(self, wf: dict) -> dict:
        """build_ref2va 工作流 → 官方 API 请求体。【官方字段核对点：仅此一处】"""
        configured, _ = configuration()
        nodes = DEFAULT_ROLES | configured.get('ref2va', {}).get('nodes', {})
        n3 = wf[nodes['generator']]['inputs']
        images, videos = [], []

        def _asset(node_id: str) -> str:
            """沿连线找真实素材节点：LoadImage.image 或 LoadVideo→GetVideoComponents→file。"""
            for _ in range(3):
                node = wf.get(node_id)
                if not node:
                    break
                asset = node['inputs'].get('image') or node['inputs'].get('file')
                if asset and not (isinstance(asset, str) and asset == 'None'):
                    return asset
                node_id = next((v[0] for v in node['inputs'].values() if isinstance(v, list)), None)
                if node_id is None:
                    break
            raise ValueError('Workflow reference does not lead to a registered asset node')

        def _refs(field, sink):
            for key in sorted(k for k in n3 if k.startswith(field)):
                sink.append(self._data_url(_asset(n3[key][0])))

        _refs('ref_images.ref_image_', images)      # <Picture 1> 商品图，<Picture 2> 人物照（如有）
        _refs('ref_videos.ref_video_', videos)      # 上一镜收尾参考视频（跨镜一致性）

        seed = wf[nodes['seed']]['inputs']['noise_seed']
        frames = int(n3.get('length', 124))
        payload = {
            'model': self.model,
            'prompt': n3['prompt'],
            'prompt_tags': {                        # H3 参考标签协议：与 ComfyUI 提示词内 <Picture n> 对应
                'images': images,
                'videos': videos,
            },
            'duration': max(4, min(15, round(frames / 24))),   # 124 帧@24fps ≈ 5.17s → 5s 档
            'width': int(n3['width']),
            'height': int(n3['height']),
            'seed': seed,
        }
        return payload
