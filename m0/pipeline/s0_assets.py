"""S0 素材预处理（M0 简版）。

正式版按 pipeline-design.md：rembg 去背 + 主体裁切 + 多图择优；
M0 先提供测试商品图合成，保证管线可验证。
"""
import os


def make_test_product(path: str) -> str:
    """合成一张简易'橙色果汁瓶'白底图（仅管线联通测试用）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1024, 1024), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([400, 330, 624, 880], radius=48, fill=(255, 148, 40))   # 瓶身
    d.rectangle([438, 240, 586, 350], fill=(60, 60, 72))                         # 瓶盖
    d.rounded_rectangle([420, 470, 604, 560], radius=20, fill=(255, 222, 160))   # 标签区
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    return path
