"""LLM 客户端（DeepSeek，OpenAI 兼容接口）。

用途：
  - /ai 群聊对话
  - PR 提交前的内容合规审核
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .settings import get_settings


class LLMError(Exception):
    pass


async def chat(messages: list[dict[str, str]], *, temperature: float = 0.7, max_tokens: int = 1024) -> str:
    s = get_settings()
    if not s.llm_api_key:
        raise LLMError("未配置 LIMPU_LLM_API_KEY")

    url = f"{s.llm_base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": s.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, json=payload, headers=headers)
    except Exception as e:
        raise LLMError(f"LLM 请求失败: {e}") from e
    if r.status_code >= 400:
        raise LLMError(f"LLM 返回 {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
        return str(data["choices"][0]["message"]["content"]).strip()
    except Exception as e:
        raise LLMError(f"LLM 响应解析失败: {e}") from e


async def moderate(text: str) -> tuple[bool, str]:
    """内容合规审核。返回 (是否通过, 原因)。"""
    system = (
        "你是内容合规审核员。审核用户提交到大学课程资料仓库的内容。"
        "仅当内容包含以下问题时判定不通过："
        "1) 违法违规、色情、政治敏感内容；2) 广告、引流、二维码推广；"
        "3) 明显的人身攻击/辱骂；4) 与课程完全无关的垃圾内容。"
        "观点尖锐、批评课程/教师（基于事实语气）属于正常评价，应通过。"
        '只回复 JSON：{"pass": true/false, "reason": "简短原因"}'
    )
    try:
        reply = await chat(
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=200,
        )
    except LLMError as e:
        # 审核服务故障时放行并提示（提交链路不应被审核故障卡死）
        return True, f"审核服务暂不可用，已跳过（{e}）"

    try:
        # 容错：提取回复中的 JSON 片段
        start, end = reply.find("{"), reply.rfind("}")
        data = json.loads(reply[start : end + 1])
        return bool(data.get("pass", True)), str(data.get("reason", ""))
    except Exception:
        return True, f"审核结果解析失败，人工复核（原文：{reply[:100]}）"
