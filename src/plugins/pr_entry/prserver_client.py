from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .settings import settings


@dataclass(frozen=True)
class SubmitResult:
    ok: bool
    message: str
    pr_url: str | None = None
    request_id: str | None = None
    toml: str | None = None
    data: dict[str, Any] | None = None


def _headers() -> dict[str, str]:
    if settings.prserver_api_key:
        return {"X-Api-Key": settings.prserver_api_key}
    return {}


async def submit_course(
    *,
    repo_name: str | None,
    course_code: str,
    course_name: str,
    repo_type: str,
    toml_text: str,
) -> SubmitResult:
    base = settings.prserver_base_url.rstrip("/")
    if not base:
        return SubmitResult(ok=False, message="未配置 HITSZ_MANAGER_PRSERVER_BASE_URL")

    payload: dict[str, object] = {
        "course_code": course_code,
        "course_name": course_name,
        "repo_type": repo_type,
        "toml": toml_text,
    }
    if repo_name:
        payload["repo_name"] = repo_name

    url = f"{base}/v1/courses/submit"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code >= 400:
            return SubmitResult(ok=False, message=f"prServer 返回 {r.status_code}: {r.text}")
        data = r.json()
    except Exception as e:
        return SubmitResult(ok=False, message=f"请求 prServer 失败: {e}")

    pr_url = data.get("pr_url")
    request_id = data.get("request_id")
    if pr_url:
        return SubmitResult(ok=True, message="PR 已创建", pr_url=str(pr_url))
    if request_id:
        return SubmitResult(ok=True, message="仓库不存在，已进入 pending", request_id=str(request_id))
    return SubmitResult(ok=True, message=f"提交成功，但返回未知字段: {data}")


async def get_course_structure(*, repo_name: str) -> SubmitResult:
    base = settings.prserver_base_url.rstrip("/")
    if not base:
        return SubmitResult(ok=False, message="未配置 HITSZ_MANAGER_PRSERVER_BASE_URL")

    url = f"{base}/v1/courses/structure"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=_headers(), params={"repo_name": repo_name})
        if r.status_code >= 400:
            return SubmitResult(ok=False, message=f"prServer 返回 {r.status_code}: {r.text}")
        data = r.json()
    except Exception as e:
        return SubmitResult(ok=False, message=f"请求 prServer 失败: {e}")

    summary = data.get("summary")
    if isinstance(summary, dict):
        return SubmitResult(ok=True, message="ok", data={"summary": summary})
    return SubmitResult(ok=False, message=f"structure 未返回 summary: {data}")


async def get_course_toml(*, repo_name: str) -> SubmitResult:
    base = settings.prserver_base_url.rstrip("/")
    if not base:
        return SubmitResult(ok=False, message="未配置 HITSZ_MANAGER_PRSERVER_BASE_URL")

    url = f"{base}/v1/courses/toml"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=_headers(), params={"repo_name": repo_name})
        if r.status_code >= 400:
            return SubmitResult(ok=False, message=f"prServer 返回 {r.status_code}: {r.text}")
        data = r.json()
    except Exception as e:
        return SubmitResult(ok=False, message=f"请求 prServer 失败: {e}")

    toml = data.get("toml")
    if toml:
        return SubmitResult(ok=True, message=str(data.get("source") or "ok"), toml=str(toml), data=data)
    return SubmitResult(ok=False, message=f"toml 未返回 toml: {data}")


async def submit_ops_dry_run(
    *,
    repo_name: str | None,
    course_code: str,
    course_name: str,
    repo_type: str,
    ops: list[dict[str, Any]],
) -> SubmitResult:
    base = settings.prserver_base_url.rstrip("/")
    if not base:
        return SubmitResult(ok=False, message="未配置 HITSZ_MANAGER_PRSERVER_BASE_URL")

    payload: dict[str, object] = {
        "course_code": course_code,
        "course_name": course_name,
        "repo_type": repo_type,
        "ops": ops,
        "dry_run": True,
    }
    if repo_name:
        payload["repo_name"] = repo_name

    url = f"{base}/v1/courses/submit_ops"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code >= 400:
            return SubmitResult(ok=False, message=f"prServer 返回 {r.status_code}: {r.text}")
        data = r.json()
    except Exception as e:
        return SubmitResult(ok=False, message=f"请求 prServer 失败: {e}")

    toml = data.get("toml")
    if toml:
        return SubmitResult(ok=True, message="patched", toml=str(toml))
    return SubmitResult(ok=False, message=f"dry_run 未返回 toml: {data}")


async def ensure_pr(
    *,
    repo_name: str | None,
    course_code: str,
    course_name: str,
    repo_type: str,
    toml_text: str,
) -> SubmitResult:
    base = settings.prserver_base_url.rstrip("/")
    if not base:
        return SubmitResult(ok=False, message="未配置 HITSZ_MANAGER_PRSERVER_BASE_URL")

    payload: dict[str, object] = {
        "course_code": course_code,
        "course_name": course_name,
        "repo_type": repo_type,
        "toml": toml_text,
    }
    if repo_name:
        payload["repo_name"] = repo_name

    url = f"{base}/v1/pr/ensure"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code >= 400:
            return SubmitResult(ok=False, message=f"prServer 返回 {r.status_code}: {r.text}")
        data = r.json()
    except Exception as e:
        return SubmitResult(ok=False, message=f"请求 prServer 失败: {e}")

    pr_url = data.get("pr_url")
    request_id = data.get("request_id")
    status = str(data.get("status") or "")
    if pr_url:
        return SubmitResult(ok=True, message=f"{status or 'ok'}", pr_url=str(pr_url))
    if request_id:
        return SubmitResult(ok=True, message=f"{status or 'waiting_repo'}", request_id=str(request_id))
    return SubmitResult(ok=True, message=f"{status or 'ok'}: {data}")
