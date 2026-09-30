"""ComfyUI HTTP 客户端 + SSH 隧道（走服务器本机 8188，不暴露公网）。

用法：
    from pipeline.comfy_client import ComfyUI
    comfy = ComfyUI.from_env()          # 有 COMFYUI_HOST 直连，否则自动起 SSH 隧道
    comfy.submit(workflow_dict) -> prompt_id
    comfy.wait(prompt_id) -> history 条目
    comfy.download_outputs(entry, save_dir) -> 文件列表
"""
import atexit
import json
import os
import select
import socketserver
import threading
import time
import urllib.parse

import paramiko
import requests


class _ForwardHandler(socketserver.BaseRequestHandler):
    ssh_transport = None  # 类属性由 _open_ssh_tunnel 注入
    chain_host, chain_port = "127.0.0.1", 8188

    def handle(self):
        try:
            chan = self.ssh_transport.open_channel(
                "direct-tcpip", (self.chain_host, self.chain_port), self.request.getpeername()
            )
        except Exception:
            return
        if chan is None:
            return
        try:
            while True:
                r, _, _ = select.select([self.request, chan], [], [], 60)
                if not r:
                    break
                if self.request in r:
                    data = self.request.recv(16384)
                    if len(data) == 0:
                        break
                    chan.send(data)
                if chan in r:
                    data = chan.recv(16384)
                    if len(data) == 0:
                        break
                    self.request.send(data)
        finally:
            chan.close()
            self.request.close()


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _open_ssh_tunnel() -> int:
    """paramiko 直连 + direct-tcpip 端口转发（替代失修的 sshtunnel），返回本地端口。"""
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
    handler = type("H", (_ForwardHandler,), {
        "ssh_transport": client.get_transport(),
        "chain_host": "127.0.0.1",
        "chain_port": 8188,
    })
    server = _ForwardServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    atexit.register(server.shutdown)
    return server.server_address[1]


class RemoteEnded(RuntimeError):
    """远端任务已明确结束（执行报错或被中断）：不是"结果不确定"，可直接重试。"""


class ComfyUI:
    def __init__(self, base_url: str, tunnel=None):
        self.base = base_url.rstrip("/")
        self.tunnel = tunnel  # 持有引用防止隧道被回收
        self.session = requests.Session()
        if os.environ.get("COMFYUI_AUTH_USER"):
            self.session.auth = (os.environ["COMFYUI_AUTH_USER"], os.environ.get("COMFYUI_AUTH_PASS", ""))

    @classmethod
    def from_env(cls):
        host = os.environ.get("COMFYUI_HOST")
        if host:
            return cls(host)
        port = _open_ssh_tunnel()
        inst = cls(f"http://127.0.0.1:{port}")
        inst.check()
        return inst

    def check(self) -> dict:
        r = self.session.get(f"{self.base}/system_stats", timeout=15)
        r.raise_for_status()
        return r.json()

    def upload_image(self, path: str, name: str = None) -> str:
        with open(path, "rb") as f:
            r = self.session.post(
                f"{self.base}/upload/image",
                files={"image": (name or os.path.basename(path), f)},
                data={"overwrite": "true"},
                timeout=120,
            )
        r.raise_for_status()
        return r.json()["name"]

    def submit(self, workflow: dict) -> str:
        r = self.session.post(f"{self.base}/prompt", json={"prompt": workflow}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"提交失败 {r.status_code}：{r.text[:500]}")
        return r.json()["prompt_id"]

    def queue(self) -> dict:
        response = self.session.get(f"{self.base}/queue", timeout=15)
        response.raise_for_status()
        q = response.json()
        if not isinstance(q.get('queue_running'), list) or not isinstance(q.get('queue_pending'), list):
            raise RuntimeError('Invalid remote queue response; refusing to assume idle')
        return q

    def queue_size(self) -> int:
        q = self.queue()
        return len(q['queue_running']) + len(q['queue_pending'])

    def wait(self, prompt_id: str, timeout_s: int = 1800, log_every_s: int = 30) -> dict:
        """轮询 /history 直到完成，返回条目；超时抛异常。

        公网链路可能瞬断：连接类错误自动重试（约 3 分钟容忍），不轻易升级为不确定态。
        """
        import requests as _requests

        start = time.time()
        consecutive_failures = 0
        while time.time() - start < timeout_s:
            try:
                hist = self.session.get(f"{self.base}/history/{prompt_id}", timeout=15).json()
                consecutive_failures = 0
            except (_requests.RequestException, OSError, ValueError):
                consecutive_failures += 1
                if consecutive_failures > 30:
                    raise  # 长时间不可达：按不确定态交给上层
                time.sleep(5)
                continue
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get('status', {})
                if status.get('status_str') in ('success', 'error'):
                    if status['status_str'] == 'error':
                        msgs = [str(m) for m in status.get('messages', [])]
                        raise RemoteEnded(f"执行失败：{json.dumps(msgs, ensure_ascii=False)[:800]}")
                    return entry
            if time.time() - start > 0 and int(time.time() - start) % log_every_s < 5:
                print(f"[comfy] 等待中… 已 {int(time.time() - start)}s，队列 {self.queue_size()}", flush=True)
            time.sleep(5)
        raise TimeoutError(f"等待 {timeout_s}s 超时")

    def download_outputs(self, entry: dict, save_dir: str) -> list[str]:
        os.makedirs(save_dir, exist_ok=True)
        saved = []
        for node_id, out in entry.get("outputs", {}).items():
            for vid in out.get("videos", []):
                saved.append(self._download(vid, save_dir))
            for img in out.get("images", []):
                if img.get("type") == "temp":  # 预览帧不收
                    continue
                saved.append(self._download(img, save_dir))
        return saved

    def _download(self, item: dict, save_dir: str) -> str:
        q = urllib.parse.urlencode({
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        r = self.session.get(f"{self.base}/view?{q}", timeout=300)
        r.raise_for_status()
        # Remote filenames are untrusted; never allow traversal outside the job directory.
        name = os.path.basename(item["filename"])
        if name in ("", ".", ".."):
            raise ValueError("Invalid output filename")
        path = os.path.join(save_dir, name)
        with open(path, "wb") as f:
            f.write(r.content)
        return path


def build_ref2va(prompt: str, ref_image_name: str, width: int = 768, height: int = 1344,
                 length: int = 124, seed: int = None, ref_image_size: str = "match",
                 template_path: str = None, ref_video_file: str = None,
                 ref_person_image_name: str = None) -> dict:
    """加载 ref2va.json 模板并注入参数（显式覆盖连线值，规避 widget 对齐风险）。

    prompt 中用 "<Picture 1>" 指代商品参考图、"<Picture 2>" 指代人物形象照（若提供）：
    tokenizer 自动按 ref_images 顺序前置插入（minimax.py tokenize_with_weights）。
    ref_video_file: 上一镜收尾片段（已上传）作参考视频，镜像人物/服饰/场景。
    ref_person_image_name: 人物形象照（已上传），生成人物容貌与之一致。
    """
    import random

    path = template_path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "workflows", "ref2va.json")
    wf = json.load(open(path, encoding="utf-8"))
    n3 = wf["3"]["inputs"]
    n3["prompt"] = prompt
    n3["width"] = width
    n3["height"] = height
    n3["length"] = length
    n3["ref_image_size"] = ref_image_size
    wf["18"]["inputs"]["image"] = ref_image_name  # 商品参考图 LoadImage
    if ref_person_image_name:  # 人物形象照：第二个参考图（node 28）
        wf["28"] = {"class_type": "LoadImage", "inputs": {"image": ref_person_image_name}}
        n3["ref_images.ref_image_1"] = ["28", 0]
    if ref_video_file:  # 接通模板中预留的 LoadVideo→GetVideoComponents→ref_video 链路
        wf["13"]["inputs"]["file"] = ref_video_file
        n3["ref_videos.ref_video_0"] = ["16", 0]  # GetVideoComponents 槽 0 = 帧序列 IMAGE
    wf["21"]["inputs"]["noise_seed"] = seed if seed is not None else random.randint(1, 2**48)
    return wf


def build_fl2va(prompt: str, first_frame_image_name: str, width: int = 448, height: int = 800,
                length: int = 124, seed: int = None, template_path: str = None) -> dict:
    """FL2VA 模板：上一镜最后一帧作为本镜第 0 帧（几何锚点），保证人物/服饰/场景无缝衔接。

    首帧由 extract_last_frame() 从上一镜成片抽出后经 /upload/image 上传。
    """
    import random

    path = template_path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "workflows", "fl2va.json")
    wf = json.load(open(path, encoding="utf-8"))
    n3 = wf["3"]["inputs"]
    n3["prompt"] = prompt
    n3["width"] = width
    n3["height"] = height
    n3["length"] = length
    wf["27"]["inputs"]["image"] = first_frame_image_name
    wf["21"]["inputs"]["noise_seed"] = seed if seed is not None else random.randint(1, 2**48)
    return wf


def extract_last_frame(video_path: str, out_path: str) -> str:
    """抽视频最后一帧：优先系统 ffmpeg，失败时回退 render.run（imageio-ffmpeg 二进制）。"""
    import subprocess

    from pipeline import render

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-sseof", "-0.3", "-i", str(video_path),
             "-update", "1", "-frames:v", "1", "-q:v", "2", str(out_path)],
            capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr[-300:])
    except (FileNotFoundError, RuntimeError):
        render.run(["-sseof", "-0.3", "-i", video_path, "-update", "1",
                    "-frames:v", "1", "-q:v", "2", out_path])
    return out_path
