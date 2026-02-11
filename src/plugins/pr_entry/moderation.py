from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from langchain_openai import ChatOpenAI

from .settings import settings


@dataclass(frozen=True)
class ModerationResult:
    approved: bool
    reason: str
    raw: dict[str, Any] | None = None


_PROMPT = """你是一个内容合规审核员。请审核用户提交的 readme.toml 内容是否适合发布到开源课程仓库的 README 中。

审核重点：
- 不含违法违规内容
- 不含仇恨/骚扰/人身攻击
- 不含色情露骨内容
- 不含暴力、血腥、极端主义
- 不泄露隐私信息（手机号、身份证号、住址、银行卡、账号密码等）
- 不包含任何 token/key/secret（例如以 sk- 开头的 key、GitHub token 等）
- 不包含引导违规或危险行为的内容

输入是 TOML 文本，你只输出严格 JSON（不要 Markdown，也不要代码块格式），结构如下：
{
  "approved": true/false,
  "reason": "一句话说明原因",
  "red_flags": ["..."]
}

如果无法确定，倾向于拒绝（approved=false）。
"""


def _client() -> ChatOpenAI:
    if not settings.llm_api_key:
        raise RuntimeError("未配置 HITSZ_MANAGER_AI_API_KEY，无法进行内容审核")

    # langchain_openai 不同版本的参数名不一致：有的用 openai_api_key/openai_api_base/model_name，
    # 有的用 api_key/base_url/model。这里根据可用字段动态映射，且用 **dict 避免类型检查误报。
    fields = getattr(ChatOpenAI, "model_fields", {}) or {}
    params: dict[str, Any] = {"temperature": 0}

    if "openai_api_key" in fields:
        params["openai_api_key"] = settings.llm_api_key
    elif "api_key" in fields:
        params["api_key"] = settings.llm_api_key

    if settings.llm_base_url:
        if "openai_api_base" in fields:
            params["openai_api_base"] = settings.llm_base_url
        elif "base_url" in fields:
            params["base_url"] = settings.llm_base_url

    if "model" in fields:
        params["model"] = settings.llm_model
    elif "model_name" in fields:
        params["model_name"] = settings.llm_model

    return ChatOpenAI(**params)


async def moderate_toml(toml_text: str) -> ModerationResult:
    try:
        llm = _client()
    except Exception as e:
        return ModerationResult(
            approved=False,
            reason=f"内容审核未配置或初始化失败：{e}",
            raw={"error": str(e)},
        )
    messages = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": toml_text},
    ]

    resp = await llm.ainvoke(messages)
    content = getattr(resp, "content", "") or ""

    try:
        data = json.loads(content)
    except Exception:
        return ModerationResult(
            approved=False,
            reason="审核模型未返回可解析 JSON，请稍后重试或联系管理员",
            raw={"model_output": content},
        )

    approved = bool(data.get("approved", False))
    reason = str(data.get("reason", ""))[:200] or "未提供原因"
    return ModerationResult(approved=approved, reason=reason, raw=data)
