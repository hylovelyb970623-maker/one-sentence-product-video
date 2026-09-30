"""DeepSeek 调用封装：JSON mode + pydantic 校验 + 降档重试 + 样例回放兜底。"""
import json
import os
import re

from openai import OpenAI
from pydantic import BaseModel

from . import mock_replay

_client: OpenAI | None = None
_vision_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    return _client


def vision_ready() -> bool:
    """视觉模型可用：配置了 VISION_API_KEY 且指定了 VISION_MODEL。"""
    return bool(os.environ.get("VISION_API_KEY") and os.environ.get("VISION_MODEL"))


def _get_vision_client() -> OpenAI:
    global _vision_client
    if _vision_client is None:
        _vision_client = OpenAI(
            api_key=os.environ["VISION_API_KEY"],
            base_url=os.environ.get("VISION_BASE_URL", "https://openrouter.ai/api/v1"),
        )
    return _vision_client


def ask_yesno(image_data_url: str, question: str, extra_image: str | None = None, retries: int = 2) -> bool | None:
    """视觉模型判断是否类问题；extra_image 提供第二张图（如首尾帧对比）。不可用或答案含糊时返回 None。"""
    import re as _re

    if not vision_ready():
        return None
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            content = [{"type": "image_url", "image_url": {"url": image_data_url}}]
            if extra_image:
                content.append({"type": "image_url", "image_url": {"url": extra_image}})
            content.append({"type": "text", "text": f"{question}\n只回答\"是\"或\"否\"。"})
            r = _get_vision_client().chat.completions.create(
                model=os.environ["VISION_MODEL"],
                messages=[{"role": "user", "content": content}],
                temperature=0, max_tokens=8, timeout=60,
            )
            text = (r.choices[0].message.content or '').strip()
            if _re.search(r'是|yes|true', text, _re.I):
                return True
            if _re.search(r'否|no|false', text, _re.I):
                return False
            last = ValueError(f'ambiguous answer: {text[:40]}')
        except Exception as e:
            last = e
    return None


def _escape_embedded_quotes(segment: str) -> str:
    """Repair unescaped English quotes inside JSON string values（视觉模型常见输出毛病）。"""
    out = []
    in_string = False
    for i, ch in enumerate(segment):
        if ch == '"':
            if not in_string:
                in_string = True
                out.append(ch)
                continue
            nxt = next((c for c in segment[i + 1:] if not c.isspace()), '')
            if nxt in {',', '}', ']', ':'}:
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
        elif in_string and ch in '\n\r\t':
            out.append(' ')
        else:
            out.append(ch)
    return ''.join(out)


def _extract_json(text: str) -> dict:
    """容忍 markdown 围栏/前后缀/尾逗号/值内未转义引号：直接解析 → 首末花括号截取 → 修复重试。"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"响应中没有 JSON 对象：{text[:120]}")
    segment = text[start:end + 1]
    for repaired in (re.sub(r",\s*([}\]])", r"\1", segment),          # 去掉 } ] 前的尾逗号
                     _escape_embedded_quotes(re.sub(r",\s*([}\]])", r"\1", segment))):
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"响应无法修复为 JSON：{text[:120]}")


class LLMError(RuntimeError):
    """LLM 调用最终失败（重试耗尽）。fallback=False 时抛出，由上层决定如何呈现。"""


def call_json(stage: str, system: str, user: str, model_cls: type[BaseModel],
              retries: int = 2, fallback: bool = True, image_data_url: str | None = None,
              use_vision: bool = False):
    """调用 LLM 并解析为 pydantic 模型；失败降 temperature 重试。

    image_data_url 或 use_vision=True 时走视觉模型（VISION_* 配置），否则走 DeepSeek 文本模型。
    fallback=True：重试耗尽后回放内置样例（仅限 CLI 演示场景）。
    fallback=False：抛 LLMError——产品链路必须用这个，禁止静默替换内容。
    """
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if image_data_url or use_vision:
                if not vision_ready():
                    raise RuntimeError('视觉模型未配置（VISION_API_KEY / VISION_MODEL）')
                text = f"{system}\n\n{user}"
                content = ([{"type": "image_url", "image_url": {"url": image_data_url}},
                            {"type": "text", "text": text}] if image_data_url
                           else [{"type": "text", "text": text}])
                messages = [{"role": "user", "content": content}]
                model = os.environ["VISION_MODEL"]
                client = _get_vision_client()
            else:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
                model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
                client = _get_client()
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=max(0.0, 0.7 - 0.3 * attempt),
                # 免费视觉模型可能不支持 json_object，仅文本路径强制 JSON mode
                **({} if (image_data_url or use_vision) else {"response_format": {"type": "json_object"}}),
                timeout=120,
            )
            return model_cls.model_validate(_extract_json(resp.choices[0].message.content))
        except Exception as e:  # 解析/校验/网络统一进重试
            last_err = e
    if fallback:
        return mock_replay.replay(stage, last_err)
    raise LLMError(f"{stage} 阶段 LLM 调用失败：{last_err}") from last_err
