"""S2 创意方案生成（LLM #2，产品灵魂）。钩子策略强制差异化。"""
from .llm import call_json
from .schemas import CreativePlans, StructuredProduct

SYSTEM = """你是短视频带货编导，擅长 10 秒高转化带货脚本。基于商品分析 JSON，生成 N 个差异化创意方案。
输出字段：{"plans": [...]}，每个方案 {hook_type, hook_line, angle, selected_points, subtitle_script, bgm_tag, sticker_brief}。
硬性要求：
1. 每个方案的 hook_type 必须不同，从这几种里选：痛点型 / 场景型 / 价格型 / 从众型 / 反差型；
2. selected_points 最多 3 个，宁少勿多，必须从商品分析的 selling_points 里选；
3. hook_line 必须口语化、有画面感，禁止书面语；
4. 全部文案禁止广告法极限词（最、第一、绝对、根治、国家级等）；
5. subtitle_script 按时间轴标注：0-2s 钩子 / 2-6s 卖点 / 6-9s 信任 / 9-10s CTA；
6. sticker_brief 说明第 9 秒左右促销贴片的内容。
只输出 JSON 对象。"""


def run(product: StructuredProduct, n: int = 3, replay: bool = False, fallback: bool = True) -> CreativePlans:
    if replay:
        from . import mock_replay
        return mock_replay.sample_plans()
    user = f"商品分析：{product.model_dump_json()}\n请生成 {n} 个创意方案。"
    return call_json("s2", SYSTEM, user, CreativePlans, fallback=fallback)
