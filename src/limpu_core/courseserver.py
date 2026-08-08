"""course-server API 客户端（课程数据 + PR 提交）。

对应 course-server 路由：
  POST /v1/courses:search   {keyword, campus}
  POST /v1/course:read      {target:{campus, course_code}, include_toml?}
  POST /v1/course:preview   {target, ops}
  POST /v1/course:submit    {target, ops, pr?, idempotency_key}
  GET  /v1/pr:lookup        ?org=&repo=&number=
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .settings import get_settings


@dataclass
class CourseItem:
    code: str
    name: str
    repo: str
    repo_type: str
    teachers: list[str]
    aliases: list[str]


@dataclass
class CourseDetail:
    repo: str
    repo_type: str
    readme_md: str
    readme_toml: str


@dataclass
class PreviewResult:
    readme_toml: str
    readme_md: str
    changed_files: list[str]
    warnings: list[str]


@dataclass
class SubmitResult:
    ok: bool
    message: str
    pr_url: str = ""
    pr_number: int = 0


def _base() -> str:
    return get_settings().courseserver_base_url.rstrip("/")


def _campus() -> str:
    return get_settings().campus


class CourseServerError(Exception):
    pass


async def _post(path: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    url = f"{_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
    except Exception as e:
        raise CourseServerError(f"course-server 连接失败: {e}") from e
    if r.status_code >= 400:
        raise CourseServerError(f"course-server 返回 {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except Exception as e:
        raise CourseServerError(f"course-server 响应解析失败: {e}") from e
    if not data.get("ok"):
        err = data.get("error") or {}
        raise CourseServerError(f"{err.get('code', 'ERROR')}: {err.get('message', '未知错误')}")
    return data.get("data") or {}


async def search_courses(keyword: str) -> list[CourseItem]:
    data = await _post("/v1/courses:search", {"keyword": keyword, "campus": _campus()})
    results = []
    for item in data.get("results") or []:
        results.append(
            CourseItem(
                code=item.get("code", ""),
                name=item.get("name", ""),
                repo=item.get("repo", ""),
                repo_type=item.get("repo_type", ""),
                teachers=item.get("teachers") or [],
                aliases=item.get("aliases") or [],
            )
        )
    return results


async def read_course(course_code: str, include_toml: bool = False) -> CourseDetail:
    payload: dict[str, Any] = {
        "target": {"campus": _campus(), "course_code": course_code},
        "include_toml": include_toml,
    }
    data = await _post("/v1/course:read", payload)
    base = data.get("base") or {}
    result = data.get("result") or {}
    return CourseDetail(
        repo=base.get("repo", ""),
        repo_type=base.get("repo_type", ""),
        readme_md=result.get("readme_md", ""),
        readme_toml=result.get("readme_toml", ""),
    )


async def preview_ops(course_code: str, ops: list[dict[str, Any]], course_name: str = "") -> PreviewResult:
    target: dict[str, Any] = {"campus": _campus(), "course_code": course_code}
    if course_name:
        target["course_name"] = course_name
    data = await _post("/v1/course:preview", {"target": target, "ops": ops}, timeout=60.0)
    result = data.get("result") or {}
    summary = data.get("summary") or {}
    return PreviewResult(
        readme_toml=result.get("readme_toml", ""),
        readme_md=result.get("readme_md", ""),
        changed_files=summary.get("changed_files") or [],
        warnings=summary.get("warnings") or [],
    )


async def submit_ops(
    course_code: str,
    ops: list[dict[str, Any]],
    idempotency_key: str,
    course_name: str = "",
) -> SubmitResult:
    target: dict[str, Any] = {"campus": _campus(), "course_code": course_code}
    if course_name:
        target["course_name"] = course_name
    payload: dict[str, Any] = {
        "target": target,
        "ops": ops,
        "idempotency_key": idempotency_key,
    }
    data = await _post("/v1/course:submit", payload, timeout=120.0)
    pr = data.get("pr") or {}
    url = pr.get("url", "")
    return SubmitResult(
        ok=True,
        message="PR 已创建/更新",
        pr_url=url,
        pr_number=int(pr.get("number") or 0),
    )


async def lookup_pr(org: str, repo: str, number: int) -> dict[str, Any]:
    url = f"{_base()}/v1/pr:lookup"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params={"org": org, "repo": repo, "number": str(number)})
    except Exception as e:
        raise CourseServerError(f"course-server 连接失败: {e}") from e
    if r.status_code >= 400:
        raise CourseServerError(f"course-server 返回 {r.status_code}: {r.text[:200]}")
    data = r.json()
    if not data.get("ok"):
        err = data.get("error") or {}
        raise CourseServerError(f"{err.get('code', 'ERROR')}: {err.get('message', '未知错误')}")
    return data.get("data") or {}
