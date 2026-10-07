"""S5 引擎适配：ShotPrompt → 各引擎语法（模板层，无 LLM）。

双引擎语法模板是核心调优资产：H3 吃"主体→动作→景别→运镜→光影→氛围"的
描述式写法；Seedance（火山方舟）吃场景化叙事 + 结构化参数。
"""
from .schemas import ShotPrompt, StructuredProduct

NEGATIVE_H3 = "模糊, 变形, 多余肢体, 手部畸形, 水印, 文字, 低分辨率, 过曝, 商品结构错误"

# 按镜头类型的参数预设（调优资产：独立版本化）
PRESETS = {
    "商品特写": {"steps": 30, "cfg": 6.5, "sampler": "euler"},
    "模特展示": {"steps": 28, "cfg": 6.0, "sampler": "euler"},
    "场景演示": {"steps": 26, "cfg": 5.5, "sampler": "euler"},
    "促销定帧": {"steps": 24, "cfg": 6.0, "sampler": "euler"},
}


def _preset_for(shot: ShotPrompt) -> dict:
    if "模特" in shot.subject:
        return PRESETS["模特展示"]
    if "特写" in shot.shot_size:
        return PRESETS["商品特写"]
    if "定格" in shot.action or "促销" in shot.subject:
        return PRESETS["促销定帧"]
    return PRESETS["场景演示"]


def to_h3(shot: ShotPrompt, product: StructuredProduct) -> dict:
    """H3 语法（Ref2VA 分支自动携带商品/模特参考）。

    "<Picture 1>" 是 H3 参考图标签：tokenizer 会把上传的参考图作为
    "<Picture 1>: [图像]" 前置插入，提示词中用该标签指代商品主体。
    """
    prompt = (
        f"<Picture 1> 是商品参考图。{shot.subject}，{shot.action}。{shot.shot_size}，镜头{shot.camera_move}，"
        f"{shot.lighting}，{shot.scene}，{shot.mood}氛围，电商带货视频质感，"
        f"商品主体清晰完整、与 <Picture 1> 完全一致"
    )
    return {
        "engine": "h3",
        "prompt": prompt,
        "negative_prompt": NEGATIVE_H3,
        "refs": shot.refs,  # cutout_asset / model_asset → Ref2VA 参考输入
        "first_frame_policy": shot.first_frame_policy,  # inherit_prev → FL2VA 首帧
        "params": _preset_for(shot),
        "duration": shot.duration,
    }


def to_seedance(shot: ShotPrompt, product: StructuredProduct) -> dict:
    """Seedance（火山方舟）语法：叙事式 prompt + 结构化参数。"""
    prompt = (
        f"{shot.lighting}的{shot.scene}中，{shot.subject}正在{shot.action}，"
        f"{shot.shot_size}，镜头{shot.camera_move}，{shot.mood}氛围，电商广告质感，商品与参考图保持一致"
    )
    return {
        "engine": "seedance",
        "model": "doubao-seedance-2-0",  # 对接时以方舟控制台实际模型 ID 为准
        "prompt": prompt,
        "duration": shot.duration,
        "ratio": "9:16",
        "resolution": "1080p",
        "first_frame": shot.first_frame_policy,  # inherit_prev → 方舟首帧参数
    }
