"""RAG 原文补全：命中稳定来源时取全篇原文。

命中片段只有 280 字截断。知识库原文（txt/md 一般 < 10KB）托管在公开仓库
HIT-A/HITA_RagData，bot 将其浅克隆到本地 data/ 目录并由 repo_sync 插件
定时 `git pull` 更新。doc_id 即仓库内相对路径，优先读本地文件；
本地没有（未克隆/新文件未同步）时回退 GitHub API。
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .settings import get_settings

# doc_id 前缀黑名单：这些不是仓库路径，无法拉全文
_SKIP_PREFIXES = ("direct:", "crawl4ai/")

_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 600.0  # 本地文件缓存 10 分钟（repo_sync 30 分钟拉一次，足够新鲜）
MAX_FULLTEXT_BYTES = 64 * 1024  # 超过 64KB 的原文不用（避免超大文件拖慢）


def is_fetchable(doc_id: str) -> bool:
    if not doc_id or "\x00" in doc_id:
        return False
    # 防路径穿越
    if doc_id.startswith("/") or ".." in doc_id.split("/"):
        return False
    return not doc_id.startswith(_SKIP_PREFIXES)


def _local_path(doc_id: str) -> Path | None:
    repo_dir: Path = get_settings().rag_repo_dir
    if not repo_dir.is_dir():
        return None
    path = repo_dir / doc_id
    try:
        path.resolve().relative_to(repo_dir.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _read_local(doc_id: str) -> str | None:
    path = _local_path(doc_id)
    if path is None:
        return None
    try:
        if path.stat().st_size > MAX_FULLTEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


async def _read_remote(doc_id: str) -> str | None:
    s = get_settings()
    repo = s.rag_fulltext_repo
    if not repo:
        return None
    url = f"https://api.github.com/repos/{repo}/contents/{quote(doc_id)}?ref=main"
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "limpu-bot"}
    if s.github_token:
        headers["Authorization"] = f"Bearer {s.github_token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 200 and 0 < len(r.content) <= MAX_FULLTEXT_BYTES:
            return r.text
    except Exception:
        pass
    return None


async def fetch_fulltext(doc_id: str) -> str | None:
    """按 doc_id 取全文。本地仓库优先，GitHub API 兜底；失败返回 None。"""
    if not is_fetchable(doc_id):
        return None

    cached = _cache.get(doc_id)
    if cached and time.time() - cached[1] < CACHE_TTL:
        return cached[0] or None

    text = _read_local(doc_id)
    if text is None:
        text = await _read_remote(doc_id)

    _cache[doc_id] = (text or "", time.time())
    return text or None
