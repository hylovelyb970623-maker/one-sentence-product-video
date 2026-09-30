"""LLM 不可用时的样例回放：保证管线在任何环境都能完整演示（pipeline-design.md §4）。"""
import sys

from .schemas import CreativePlan, CreativePlans, SellingPoint, ShotPrompt, Storyboard, StructuredProduct


def _note(msg: str) -> None:
    print(f"[replay] {msg}", file=sys.stderr)


def sample_product() -> StructuredProduct:
    return StructuredProduct(
        category="女装/阔腿裤",
        product_name="冰丝阔腿裤",
        selling_points=[
            SellingPoint(point="凉感面料", evidence="冰丝材质贴身不闷汗，夏日户外实拍不起热疹"),
            SellingPoint(point="显瘦", evidence="高腰垂坠版型，胯宽腿粗都能遮"),
            SellingPoint(point="通勤百搭", evidence="配衬衫是通勤风，配T恤是休闲风"),
        ],
        audience="25-40岁通勤女性",
        pain_points=["夏天穿裤子闷热黏腿", "腿型不自信不敢穿紧身裤"],
        usage_scenes=["通勤路上", "办公室", "周末逛街"],
        price_band="50-100",
        compliance_notes=["不可宣称医疗功效（如'治疗静脉曲张'）"],
    )


def sample_plans() -> CreativePlans:
    return CreativePlans(
        plans=[
            CreativePlan(
                hook_type="痛点型",
                hook_line="夏天穿裤子闷到怀疑人生？",
                angle="前2秒热到崩溃的通勤场景，反差引入冰丝凉感",
                selected_points=["凉感面料", "显瘦"],
                subtitle_script=(
                    "0-2s『夏天穿裤子闷到怀疑人生？』2-4s『冰丝面料，上身3秒降温』"
                    "4-6s『高腰垂坠，胯宽腿粗都能遮』6-9s『通勤逛街都好看，闺蜜追着要链接』9-10s『点击下方，59带回家』"
                ),
                bgm_tag="轻快节奏",
                sticker_brief="9s 处贴『限时59』价格贴片",
            ),
            CreativePlan(
                hook_type="场景型",
                hook_line="早八人的三分钟出门穿搭",
                angle="早晨赶时间的真实场景，一条裤子解决搭配焦虑",
                selected_points=["通勤百搭", "显瘦"],
                subtitle_script=(
                    "0-2s『早八人的三分钟出门穿搭』2-4s『一条阔腿裤配遍衣柜』"
                    "4-6s『高腰显腿长，坐下不勒肚子』6-9s『空调房不冷，太阳下不闷』9-10s『同款在下单页』"
                ),
                bgm_tag="清晨治愈",
                sticker_brief="9s 处贴『通勤必备』贴片",
            ),
            CreativePlan(
                hook_type="从众型",
                hook_line="办公室三个姐妹都买了这条裤子",
                angle="同事间的真实种草氛围，信任感拉满",
                selected_points=["凉感面料", "通勤百搭"],
                subtitle_script=(
                    "0-2s『办公室三个姐妹都买了这条裤子』2-4s『冰丝凉感，伏天也不闷』"
                    "4-6s『衬衫T恤随便配』6-9s『一人一条颜色还不重样』9-10s『链接给你要来了』"
                ),
                bgm_tag="活泼日常",
                sticker_brief="9s 处贴『同款在车车』贴片",
            ),
        ]
    )


def sample_storyboard() -> Storyboard:
    return Storyboard(
        shots=[
            ShotPrompt(
                shot_id=1, duration=2.0,
                subject="冰丝阔腿裤（商品）",
                action="通勤女性用手扇风、擦汗，裤子紧贴腿部显闷热",
                shot_size="中景", camera_move="手持轻晃", lighting="正午强光",
                scene="户外公交站", mood="燥热烦躁",
                refs=["cutout_asset"], first_frame_policy="none",
                subtitle="夏天穿裤子闷到怀疑人生？",
            ),
            ShotPrompt(
                shot_id=2, duration=2.5,
                subject="冰丝阔腿裤面料特写（商品）",
                action="手指轻抚面料，布料轻盈抖动展示垂坠冰凉质感",
                shot_size="特写", camera_move="缓推", lighting="明亮自然光",
                scene="纯色背景棚拍", mood="清爽",
                refs=["cutout_asset"], first_frame_policy="inherit_prev",
                subtitle="冰丝面料，上身3秒降温",
            ),
            ShotPrompt(
                shot_id=3, duration=2.5,
                subject="模特身穿冰丝阔腿裤（商品+模特）",
                action="模特转身走两步，高腰版型拉长腿部线条",
                shot_size="全景", camera_move="横移跟随", lighting="柔和顺光",
                scene="简约办公室", mood="自信干练",
                refs=["cutout_asset", "model_asset"], first_frame_policy="none",
                subtitle="高腰垂坠，胯宽腿粗都能遮",
            ),
            ShotPrompt(
                shot_id=4, duration=3.0,
                subject="冰丝阔腿裤（商品）+促销信息",
                action="商品正面定格，弹出价格贴片与购买引导",
                shot_size="近景", camera_move="固定", lighting="明亮棚光",
                scene="纯色背景棚拍", mood="促销热烈",
                refs=["cutout_asset"], first_frame_policy="none",
                subtitle="点击下方，59带回家",
            ),
        ]
    )


def replay(stage: str, err: Exception):
    """LLM 调用最终失败时按环节回放样例，演示不中断。"""
    _note(f"{stage} 阶段 LLM 调用失败，回放样例输出（原因：{err}）")
    if stage == "s1":
        return sample_product()
    if stage == "s2":
        return sample_plans()
    if stage == "s3":
        return sample_storyboard()
    raise err
