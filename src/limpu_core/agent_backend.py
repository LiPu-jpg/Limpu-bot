"""agent-backend API 客户端（RAG / 教师 / 搜索 / 健康检查）。"""

from __future__ import annotations

from typing import Any

import httpx

from .settings import get_settings


class AgentBackendError(Exception):
    pass


def _base() -> str:
    return get_settings().agent_base_url.rstrip("/")


async def _post(path: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    url = f"{_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
    except Exception as e:
        raise AgentBackendError(f"agent-backend 连接失败: {e}") from e
    if r.status_code >= 400:
        raise AgentBackendError(f"agent-backend 返回 {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        raise AgentBackendError(f"agent-backend 响应解析失败: {e}") from e
    if not data.get("ok", True):
        err = data.get("error") or {}
        raise AgentBackendError(f"{err.get('code', 'ERROR')}: {err.get('message', '未知错误')}")
    return data


async def rag_query(question: str, top_k: int = 10) -> dict[str, Any]:
    """返回 agent-backend /api/rag/query 的原始 data（result.hits 等）。"""
    return await _post("/api/rag/query", {"query": question, "top_k": top_k}, timeout=60.0)


async def search_teacher(name_or_pinyin: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    # 纯字母视为拼音，否则按姓名查
    if name_or_pinyin.isascii() and name_or_pinyin.replace(" ", "").isalpha():
        payload["pinyin"] = name_or_pinyin
    else:
        payload["name"] = name_or_pinyin
    return await _post("/api/teachers/search", payload, timeout=30.0)


async def health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{_base()}/health")
        return r.status_code == 200
    except Exception:
        return False
