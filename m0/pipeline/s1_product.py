"""S1 商品结构化（LLM #1）。契约与要求见 pipeline-design.md §1。"""
from .llm import call_json
from .schemas import StructuredProduct

SYSTEM = """你是资深电商运营。根据用户提供的一句话商品介绍，输出结构化商品分析 JSON。
字段：category, product_name, selling_points(数组，每项 {point, evidence}), audience, pain_points, usage_scenes, price_band, compliance_notes。
要求：
1. selling_points 提取 2-4 个，每个 evidence 必须是"能拍成画面的证据"，不是口号；
2. pain_points 从目标人群视角写，具体到生活场景；
3. compliance_notes 标注该类目的广告宣称红线（美妆/食品/医疗从严）；
4. 价格只引用用户原文，不要编造，没有则留空字符串；
5. 若用户没写商品名：从卖点推断品类，product_name 用品类通称（如"这款低卡提神饮品"），
   禁止输出"未提供"或带推断说明的括号——下游分镜会直接引用这个名字。
只输出 JSON 对象。"""


def run(sentence: str, replay: bool = False, fallback: bool = True) -> StructuredProduct:
    if replay:
        from . import mock_replay
        return mock_replay.sample_product()
    return call_json("s1", SYSTEM, f"商品一句话介绍：{sentence}", StructuredProduct, fallback=fallback)
