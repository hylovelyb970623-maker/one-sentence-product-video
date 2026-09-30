"""S6 生成执行：统一任务协议网关（M0 为 mock 引擎，ComfyUI 待对接即插即换）。

统一协议（PRD §4.3）：submit(engine, mode, payload) → task 记录
（状态机 queued → running → succeeded/failed，含 cost_credits 与 result 资产指针）。
"""
import os
import time
import uuid

SEEDANCE_PER_SEC = 1.0  # 元/秒，定稿成本估算（火山方舟 Seedance 2.0 约 1 元/s）


class MockEngine:
    """mock 引擎：走完任务状态机，产出占位资产记录。"""

    name = "mock"

    def submit_and_run(self, shot_id: int, engine: str, mode: str, payload: dict) -> dict:
        task_id = f"g_{uuid.uuid4().hex[:8]}"
        if engine == "h3":
            cost = 0.0  # 本地打样，边际成本近零
        else:
            cost = round(float(payload.get("duration", 0)) * SEEDANCE_PER_SEC, 2)
        return {
            "task_id": task_id,
            "shot_card_id": shot_id,
            "engine": engine,
            "mode": mode,  # draft=打样 / final=定稿
            "status": "succeeded",
            "cost_credits": cost,
            "request": payload,
            "result": {
                "asset_id": f"a_{uuid.uuid4().hex[:8]}",
                "mock": True,
                "note": "mock 占位视频，对接真实引擎后此处为视频资产",
            },
        }


class ComfyUIEngine:
    """真实 H3 引擎：ComfyUI Ref2VA（商品参考图 → 视频）。

    对接链路：上传商品白底图 → 注入 ref2va.json 模板（{@图1} 占位符绑定参考图）
    → POST /prompt → 轮询 /history → /view 下载视频。
    Seedance 分支仍走 MockEngine（火山方舟 key 到位后同样即插即换）。
    """

    name = "comfyui"

    def __init__(self, product_image: str, ref_image_size: str = "max"):
        self.product_image = product_image  # 商品白底图本地路径
        self.ref_image_size = ref_image_size
        self._comfy = None
        self._ref_name = None

    def available(self) -> bool:
        return bool(os.environ.get("COMFYUI_HOST"))

    def _client(self):
        if self._comfy is None:
            from .comfy_client import ComfyUI

            self._comfy = ComfyUI.from_env()
            self._comfy.check()
        return self._comfy

    @staticmethod
    def _snap_frames(seconds: float) -> int:
        """H3 帧数网格：17k+5，训练范围 124–362（≈5.2–15s）。"""
        n = max(5, round(seconds * 24))
        return n + ((5 - (n % 17)) % 17)

    def submit_and_run(self, shot_id: int, engine: str, mode: str, payload: dict) -> dict:
        if engine != "h3":  # seedance 等其他引擎暂走 mock 协议
            return MockEngine().submit_and_run(shot_id, engine, mode, payload)

        from .comfy_client import build_ref2va

        comfy = self._client()
        if self._ref_name is None:
            self._ref_name = comfy.upload_image(self.product_image)

        frames = self._snap_frames(payload.get("duration", 5.2))
        wf = build_ref2va(
            prompt=payload["prompt"],
            ref_image_name=self._ref_name,
            width=768, height=1344,  # 9:16 竖屏
            length=frames,
            ref_image_size=self.ref_image_size,
        )
        t0 = time.time()
        prompt_id = comfy.submit(wf)
        entry = comfy.wait(prompt_id, timeout_s=2400)
        saved = comfy.download_outputs(entry, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out", "comfy"))
        video = next((p for p in saved if p.endswith((".mp4", ".webm"))), saved[0] if saved else None)
        return {
            "task_id": prompt_id,
            "shot_card_id": shot_id,
            "engine": "h3",
            "mode": mode,
            "status": "succeeded",
            "cost_credits": 0.0,
            "request": payload,
            "result": {
                "asset_id": os.path.basename(video) if video else None,
                "video_path": video,
                "frames": frames,
                "elapsed_s": round(time.time() - t0, 1),
            },
        }


def get_engine(product_image: str | None = None):
    """给了商品图且 ComfyUI 已配置 → 真实 H3 引擎；否则 mock。"""
    if product_image and os.environ.get("COMFYUI_HOST"):
        return ComfyUIEngine(product_image)
    return MockEngine()
