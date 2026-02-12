from __future__ import annotations

import json
import re
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


_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 常见 key/token 前缀（尽量只脱敏 value，避免日志泄露）
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "sk-***"),
    (re.compile(r"\bghp_[A-Za-z0-9]{16,}\b"), "ghp_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"), "github_pat_***"),
    # 粗略兜底：形如 TOKEN=... / KEY=... 的行
    (re.compile(r"(?im)^(\s*(?:api_?key|token|secret|password)\s*=)\s*.+$"), r"\1 ***"),
]


def _redact(text: str) -> str:
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _try_parse_json(content: str) -> dict[str, Any] | None:
    s = (content or "").strip()
    if not s:
        return None

    # 1) 兼容 ```json ... ``` / ``` ... ```
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)
        s = s.strip()

    # 2) 直接解析
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except Exception:
        pass

    # 3) 兼容前后文：提取第一个 JSON object 块
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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

    data = _try_parse_json(content)
    if not data:
        if getattr(settings, "moderation_debug", False):
            max_chars = int(getattr(settings, "moderation_debug_max_chars", 2000) or 2000)
            preview = _redact(content)
            if len(preview) > max_chars:
                preview = preview[:max_chars] + "…(truncated)"
            print(f"[moderation] JSON 解析失败，模型原始输出（已脱敏）：\n{preview}")
        return ModerationResult(
            approved=False,
            reason="审核模型未返回可解析 JSON，请稍后重试或联系管理员",
            raw={"model_output": content},
        )

    approved = bool(data.get("approved", False))
    reason = str(data.get("reason", ""))[:200] or "未提供原因"
    return ModerationResult(approved=approved, reason=reason, raw=data)
