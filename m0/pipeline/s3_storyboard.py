"""S3 分镜展开（LLM #3）：创意方案 → 3-4 个 ShotPrompt。"""
from .llm import call_json
from .schemas import CreativePlan, StructuredProduct, Storyboard

SYSTEM = """你是带货视频分镜师。把选定的创意方案展开为 10 秒的 3-4 个分镜。
输出字段：{"shots": [...]}，每个分镜 {shot_id, duration, subject, action, shot_size, camera_move, lighting, scene, mood, refs, first_frame_policy, subtitle}。
格式硬约束：shot_id 必须是纯整数（1、2、3…，不要写 "shot_1" 或 "镜1"）；duration 是数字秒数（如 2.5，不要写 "2.5s"）。
硬性要求：
1. duration 为秒，合计约 10 秒；
2. 相邻镜头 shot_size 必须不同，避免连续同景别；
3. subject 必须包含商品名（商品每镜必现）；有模特的镜头 subject 写"模特+商品"；
4. 第 1 镜就是钩子画面，直接呈现 hook_line 描述的场景；
5. first_frame_policy：与上一镜画面连贯的镜头用 "inherit_prev"，否则 "none"；
6. subtitle 按方案的 subtitle_script 拆分到各镜，禁止极限词；
7. refs 统一填 ["cutout_asset"]，模特出镜的镜头追加 "model_asset"。
只输出 JSON 对象。"""


def run(plan: CreativePlan, product: StructuredProduct, replay: bool = False, fallback: bool = True) -> Storyboard:
    if replay:
        from . import mock_replay
        return mock_replay.sample_storyboard()
    user = f"创意方案：{plan.model_dump_json()}\n商品分析：{product.model_dump_json()}"
    return call_json("s3", SYSTEM, user, Storyboard, fallback=fallback)
