"""管线各环节的数据契约（pipeline-design.md §1）。

所有 LLM 环节的输出都约束在这些 pydantic 模型上，自由文本不允许流到下游。
"""
from typing import Literal

import re
from pydantic import BaseModel, field_validator


def _split_to_list(v):
    """LLM 偶发把数组字段写成顿号/逗号串，统一拆成 list。"""
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"[、，,；;\n]", v) if s.strip()]
    return v


class SellingPoint(BaseModel):
    point: str
    evidence: str


class StructuredProduct(BaseModel):
    """S1 输出：商品结构化信息。"""

    category: str = ""
    product_name: str = ""
    selling_points: list[SellingPoint] = []
    audience: str = ""
    pain_points: list[str] = []
    usage_scenes: list[str] = []
    price_band: str = ""
    compliance_notes: list[str] = []

    @field_validator("pain_points", "usage_scenes", "compliance_notes", mode="before")
    @classmethod
    def _coerce_lists(cls, v):
        return _split_to_list(v)


class CreativePlan(BaseModel):
    """S2 输出：创意方案卡（用户在界面上看到的"选项"）。"""

    hook_type: str
    hook_line: str
    angle: str
    selected_points: list[str] = []
    subtitle_script: str
    bgm_tag: str = ""
    sticker_brief: str = ""

    @field_validator("selected_points", mode="before")
    @classmethod
    def _coerce_points(cls, v):
        return _split_to_list(v)


class CreativePlans(BaseModel):
    plans: list[CreativePlan]


class ShotPrompt(BaseModel):
    """S3 输出：单镜结构化提示词，全管线核心中间表示。"""

    shot_id: int
    duration: float
    subject: str
    action: str
    shot_size: str
    camera_move: str
    lighting: str
    scene: str
    mood: str
    refs: list[str] = []
    first_frame_policy: Literal["none", "inherit_prev"] = "none"
    subtitle: str = ""

    @field_validator("refs", mode="before")
    @classmethod
    def _coerce_refs(cls, v):
        return _split_to_list(v)

    @field_validator("shot_id", mode="before")
    @classmethod
    def _coerce_shot_id(cls, v):
        """LLM 偶发写 "shot_1"/"镜1" 等变体，提取数字部分。"""
        if isinstance(v, str):
            digits = re.sub(r"\D", "", v)
            if digits:
                return int(digits)
        return v

    @field_validator("duration", mode="before")
    @classmethod
    def _coerce_duration(cls, v):
        """LLM 偶发写 "2s"/"约2秒" 等变体，提取数值。"""
        if isinstance(v, str):
            m = re.search(r"\d+(\.\d+)?", v)
            if m:
                return float(m.group())
        return v


class Storyboard(BaseModel):
    """S3 输出：一个创意方案展开后的完整分镜。"""

    shots: list[ShotPrompt]
