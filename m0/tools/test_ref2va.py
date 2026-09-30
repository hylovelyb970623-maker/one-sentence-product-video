"""真实生成测试：合成商品白底图 → 上传 → Ref2VA 生成 5 秒竖屏视频 → 下载。

用法：.venv/bin/python tools/test_ref2va.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cli import load_env  # noqa: E402

load_env()

from PIL import Image, ImageDraw  # noqa: E402

from pipeline.comfy_client import ComfyUI, build_ref2va  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out")


def make_test_product(path: str) -> None:
    """合成一张简易'橙色果汁瓶'白底图（仅管线联通测试，不求画质）。"""
    img = Image.new("RGB", (1024, 1024), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([400, 330, 624, 880], radius=48, fill=(255, 148, 40))      # 瓶身
    d.rectangle([438, 240, 586, 350], fill=(60, 60, 72))                            # 瓶盖
    d.rounded_rectangle([420, 470, 604, 560], radius=20, fill=(255, 222, 160))      # 标签区
    img.save(path)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    img_path = os.path.join(OUT, "test_product.png")
    make_test_product(img_path)
    print(f"测试商品图：{img_path}")

    comfy = ComfyUI.from_env()
    info = comfy.check()
    print(f"ComfyUI 已连接：v{info.get('system', {}).get('comfyui_version')}，队列 {comfy.queue_size()}")

    name = comfy.upload_image(img_path)
    print(f"参考图已上传：{name}")

    prompt = (
        "电商带货视频：{@图1}一瓶橙色果汁饮品（商品主体，外观与参考图完全一致，"
        "瓶身标签清晰可辨），从画面外轻轻落在原木桌面上，旁边摆放新鲜橙子切片。"
        "近景，镜头缓慢推近商品，明亮清新的自然光，现代简约厨房背景，"
        "清爽愉悦氛围，商品主体清晰完整"
    )
    wf = build_ref2va(prompt=prompt, ref_image_name=name, width=768, height=1344, length=124)
    print("提交生成任务（768x1344，124帧≈5.2s，Turbo 8步）…")
    t0 = time.time()
    prompt_id = comfy.submit(wf)
    print(f"prompt_id: {prompt_id}")

    entry = comfy.wait(prompt_id, timeout_s=1800)
    dt = time.time() - t0
    print(f"生成完成，耗时 {dt:.0f}s")

    saved = comfy.download_outputs(entry, os.path.join(OUT, "comfy_test"))
    for p in saved:
        print(f"✔ 产物：{p}（{os.path.getsize(p) / 1e6:.1f} MB）")


if __name__ == "__main__":
    main()
