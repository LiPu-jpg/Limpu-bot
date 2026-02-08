from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message
from nonebot.rule import to_me

from .moderation import moderate_toml
from .prserver_client import ensure_pr, get_course_structure, get_course_toml, submit_ops_dry_run
from .settings import settings

import tomlkit
from tomlkit.items import AoT, Table


@dataclass
class Pending:
    repo_name: Optional[str]
    course_code: str
    course_name: str
    repo_type: str

    mode: str

    # add/edit by section+index
    section_title: str = ""
    item_index: int = -1

    # modify by paragraph locating
    old_paragraph: str = ""
    new_paragraph: str = ""
    candidates: list[dict] | None = None
    target: dict | None = None

    # store the TOML we located against (avoid race / re-fetch)
    base_toml: str = ""

    # attribution
    want_attribution: bool | None = None
    author_name: str = ""
    author_link: str = ""

    # prepared payload
    patched_toml: str = ""


_PENDING: dict[tuple[int | None, int], Pending] = {}


def _key(event: MessageEvent) -> tuple[int | None, int]:
    return (getattr(event, "group_id", None), int(event.user_id))


def _allowed(event: MessageEvent) -> bool:
    if not settings.allowed_users:
        return True
    return str(event.user_id) in settings.allowed_users


def _text(event: MessageEvent) -> str:
    return str(event.get_message()).strip()


def _author_name(event: MessageEvent) -> str:
    sender = getattr(event, "sender", None)
    if sender:
        for key in ("card", "nickname"):
            v = getattr(sender, key, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return str(event.user_id)


def _today() -> str:
    return date.today().isoformat()


def _year_month() -> str:
    d = date.today()
    return f"{d.year:04d}-{d.month:02d}"


def make_node(bot: Bot, content: str, name: str = "hoa-pr bot") -> dict:
    return {
        "type": "node",
        "data": {
            "name": name,
            "uin": bot.self_id,
            "content": Message(content),
        },
    }


def _split_long_text(text: str, *, limit: int = 1800) -> list[str]:
    s = (text or "").strip()
    if not s:
        return [""]
    if len(s) <= limit:
        return [s]

    # Try split by blank line first
    parts = s.split("\n\n")
    if len(parts) > 1:
        out: list[str] = []
        buf = ""
        for p in parts:
            cand = (buf + "\n\n" + p).strip() if buf else p
            if len(cand) <= limit:
                buf = cand
            else:
                if buf:
                    out.extend(_split_long_text(buf, limit=limit))
                    buf = ""
                out.extend(_split_long_text(p, limit=limit))
        if buf:
            out.extend(_split_long_text(buf, limit=limit))
        return out

    # Fallback split by newline
    lines = s.split("\n")
    out2: list[str] = []
    buf2 = ""
    for ln in lines:
        cand = (buf2 + "\n" + ln).strip() if buf2 else ln
        if len(cand) <= limit:
            buf2 = cand
        else:
            if buf2:
                out2.append(buf2)
                buf2 = ""
            if len(ln) > limit:
                # hard cut
                out2.append(ln[:limit])
                rest = ln[limit:]
                if rest.strip():
                    out2.extend(_split_long_text(rest, limit=limit))
            else:
                buf2 = ln
    if buf2:
        out2.append(buf2)
    return out2


async def _send_forward(bot: Bot, event: MessageEvent, nodes: list[dict]) -> bool:
    try:
        if getattr(event, "group_id", None):
            await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
        else:
            await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)
        return True
    except Exception:
        return False


def _doc_table(doc: object) -> Table:
    if not isinstance(doc, Table):
        raise ValueError("invalid TOML doc")
    return doc


def _aot(v: object) -> AoT | None:
    return v if isinstance(v, AoT) else None


def _safe_str(v: object) -> str:
    if v is None:
        return ""
    return str(v)


def _norm_text(s: str) -> str:
    return (s or "").strip().replace("\r\n", "\n")


def _append_author_field(target: Table, author: dict) -> None:
    """Append/merge author into a TOML table.

    Keeps behavior compatible with prServer toml_ops:
    - missing -> inline table
    - inline table -> array of inline tables
    - array -> append
    """

    name = str(author.get("name") or "").strip()
    link = str(author.get("link") or "").strip()
    date_str = str(author.get("date") or "").strip()

    t = tomlkit.inline_table()
    t["name"] = name
    t["link"] = link
    t["date"] = date_str

    existing = target.get("author")
    if existing is None:
        target["author"] = t
        return
    if isinstance(existing, Array):
        existing.append(t)
        return
    if isinstance(existing, (InlineTable, dict)):
        arr = tomlkit.array()
        arr.multiline(True)
        arr.append(existing)
        arr.append(t)
        target["author"] = arr
        return
    target["author"] = t


def _extract_normal_segments(doc: Table) -> list[tuple[str, str]]:
    course_name = _safe_str(doc.get("course_name")).strip()
    course_code = _safe_str(doc.get("course_code")).strip()
    description = _safe_str(doc.get("description"))

    header = f"【{course_name or course_code}】\n代码：{course_code}\n\n{_norm_text(description)}".strip()
    segs: list[tuple[str, str]] = [("header", header)]

    sections = _aot(doc.get("sections"))
    if sections:
        for sec in sections:
            if not isinstance(sec, Table):
                continue
            title = _safe_str(sec.get("title")).strip() or "(未命名章节)"
            items = _aot(sec.get("items"))
            blocks: list[str] = []
            if items:
                for it in items:
                    if not isinstance(it, Table):
                        continue
                    c = _norm_text(_safe_str(it.get("content")))
                    if c:
                        blocks.append(c)
            body = "\n\n".join(blocks).strip()
            segs.append((title, f"【{title}】\n\n{body}".strip() if body else f"【{title}】\n\n（空）"))
    return segs


def _extract_multi_segments(doc: Table) -> list[tuple[str, str]]:
    course_name = _safe_str(doc.get("course_name")).strip()
    course_code = _safe_str(doc.get("course_code")).strip()
    description = _safe_str(doc.get("description"))

    header = f"【{course_name or course_code}】\n代码：{course_code}\n\n{_norm_text(description)}".strip()
    segs: list[tuple[str, str]] = [("header", header)]

    courses = _aot(doc.get("courses"))
    if not courses:
        return segs

    for c in courses:
        if not isinstance(c, Table):
            continue
        name = _safe_str(c.get("name")).strip() or "(未命名子课程)"
        code = _safe_str(c.get("code")).strip()
        lines: list[str] = [f"【子课程：{name}】", f"代码：{code}"]

        teachers = _aot(c.get("teachers"))
        if teachers:
            teacher_names = []
            teacher_reviews: list[str] = []
            for t in teachers:
                if not isinstance(t, Table):
                    continue
                tn = _safe_str(t.get("name")).strip()
                if tn:
                    teacher_names.append(tn)
                reviews = _aot(t.get("reviews"))
                if reviews:
                    for rv in reviews:
                        if not isinstance(rv, Table):
                            continue
                        rc = _norm_text(_safe_str(rv.get("content")))
                        if rc:
                            teacher_reviews.append(rc)
            if teacher_names:
                lines.append(f"教师：{', '.join(teacher_names)}")
            if teacher_reviews:
                lines.append("\n教师评价：\n" + "\n\n".join(teacher_reviews))

        sections = _aot(c.get("sections"))
        if sections:
            for sec in sections:
                if not isinstance(sec, Table):
                    continue
                title = _safe_str(sec.get("title")).strip() or "(未命名章节)"
                items = _aot(sec.get("items"))
                blocks: list[str] = []
                if items:
                    for it in items:
                        if not isinstance(it, Table):
                            continue
                        cc = _norm_text(_safe_str(it.get("content")))
                        if cc:
                            blocks.append(cc)
                if blocks:
                    lines.append(f"\n[{title}]\n" + "\n\n".join(blocks))

        segs.append((name, "\n".join(lines).strip()))

    return segs


def build_forward_nodes_from_toml(bot: Bot, toml_text: str) -> list[dict]:
    doc = _doc_table(tomlkit.parse(toml_text))
    repo_type = _safe_str(doc.get("repo_type")).strip() or "normal"

    segs = _extract_multi_segments(doc) if repo_type == "multi-project" else _extract_normal_segments(doc)
    nodes: list[dict] = []
    for title, body in segs:
        parts = _split_long_text(body, limit=1800)
        if len(parts) == 1:
            nodes.append(make_node(bot, parts[0]))
        else:
            for i, p in enumerate(parts, start=1):
                nodes.append(make_node(bot, f"{title}（{i}/{len(parts)}）\n\n{p}".strip()))
    return nodes


def _preview_line(content: str, *, limit: int = 60) -> str:
    pv = (content or "").strip().split("\n", 1)[0].strip()
    if len(pv) > limit:
        return pv[: limit - 1] + "…"
    return pv


def _find_paragraph_candidates(toml_text: str, snippet: str) -> list[dict]:
    """Locate snippet in multiple places.

    Returns candidates with a stable shape:
    - type=section_item: section, index
    - type=description
    - type=lecturer_review: lecturer, review_index
    - type=course_section_item: course_index, course_name, section, index
    - type=course_teacher_review: course_index, course_name, teacher, review_index
    """

    s = _norm_text(snippet)
    if not s:
        return []

    doc = _doc_table(tomlkit.parse(toml_text))
    repo_type = _safe_str(doc.get("repo_type")).strip() or "normal"

    out: list[dict] = []

    # description
    desc = _norm_text(_safe_str(doc.get("description")))
    if desc and s in desc:
        out.append({"type": "description", "preview": _preview_line(desc)})

    # lecturers.reviews (normal schema)
    lecturers = _aot(doc.get("lecturers"))
    if lecturers:
        for lec in lecturers:
            if not isinstance(lec, Table):
                continue
            ln = _safe_str(lec.get("name")).strip() or "(未命名教师)"
            reviews = _aot(lec.get("reviews"))
            if not reviews:
                continue
            for ridx0, rv in enumerate(reviews):
                if not isinstance(rv, Table):
                    continue
                rc = _norm_text(_safe_str(rv.get("content")))
                if rc and s in rc:
                    out.append(
                        {
                            "type": "lecturer_review",
                            "lecturer": ln,
                            "review_index": ridx0,
                            "preview": _preview_line(rc),
                        }
                    )

    # sections/items (normal)
    sections = _aot(doc.get("sections"))
    if sections:
        for sec in sections:
            if not isinstance(sec, Table):
                continue
            title = _safe_str(sec.get("title")).strip() or "(未命名章节)"
            items = _aot(sec.get("items"))
            if not items:
                continue
            for idx0, it in enumerate(items):
                if not isinstance(it, Table):
                    continue
                content = _norm_text(_safe_str(it.get("content")))
                if content and s in content:
                    out.append(
                        {
                            "type": "section_item",
                            "section": title,
                            "index": idx0,
                            "preview": _preview_line(content),
                        }
                    )

    # multi-project: courses[].teachers[].reviews + courses[].sections[].items
    if repo_type == "multi-project":
        courses = _aot(doc.get("courses"))
        if courses:
            for cidx0, c in enumerate(courses):
                if not isinstance(c, Table):
                    continue
                cname = _safe_str(c.get("name")).strip() or f"course#{cidx0+1}"

                teachers = _aot(c.get("teachers"))
                if teachers:
                    for t in teachers:
                        if not isinstance(t, Table):
                            continue
                        tn = _safe_str(t.get("name")).strip() or "(未命名教师)"
                        reviews = _aot(t.get("reviews"))
                        if reviews:
                            for ridx0, rv in enumerate(reviews):
                                if not isinstance(rv, Table):
                                    continue
                                rc = _norm_text(_safe_str(rv.get("content")))
                                if rc and s in rc:
                                    out.append(
                                        {
                                            "type": "course_teacher_review",
                                            "course_index": cidx0,
                                            "course_name": cname,
                                            "teacher": tn,
                                            "review_index": ridx0,
                                            "preview": _preview_line(rc),
                                        }
                                    )

                csecs = _aot(c.get("sections"))
                if csecs:
                    for sec in csecs:
                        if not isinstance(sec, Table):
                            continue
                        st = _safe_str(sec.get("title")).strip() or "(未命名章节)"
                        items = _aot(sec.get("items"))
                        if not items:
                            continue
                        for idx0, it in enumerate(items):
                            if not isinstance(it, Table):
                                continue
                            cc = _norm_text(_safe_str(it.get("content")))
                            if cc and s in cc:
                                out.append(
                                    {
                                        "type": "course_section_item",
                                        "course_index": cidx0,
                                        "course_name": cname,
                                        "section": st,
                                        "index": idx0,
                                        "preview": _preview_line(cc),
                                    }
                                )

    return out


def _patch_toml_by_target(
    base_toml: str,
    *,
    target: dict,
    old_paragraph: str,
    new_paragraph: str,
    author: dict | None,
) -> str:
    """Patch TOML locally when submit_ops doesn't cover the target."""

    doc = _doc_table(tomlkit.parse(base_toml))
    s_old = _norm_text(old_paragraph)
    s_new = _norm_text(new_paragraph)
    if not s_old:
        raise ValueError("old_paragraph is empty")

    t = str(target.get("type") or "")
    if t == "description":
        desc = _norm_text(_safe_str(doc.get("description")))
        if s_old not in desc:
            raise ValueError("原段落未在 description 中找到（内容已变化？）")
        doc["description"] = tomlkit.string(desc.replace(s_old, s_new, 1), multiline=True)
        return tomlkit.dumps(doc).rstrip() + "\n"

    if t == "lecturer_review":
        lecturers = _aot(doc.get("lecturers"))
        if not lecturers:
            raise ValueError("lecturers 不存在")
        lecturer_name = str(target.get("lecturer") or "").strip()
        ridx0 = int(target.get("review_index") or 0)
        for lec in lecturers:
            if not isinstance(lec, Table):
                continue
            if _safe_str(lec.get("name")).strip() != lecturer_name:
                continue
            reviews = _aot(lec.get("reviews"))
            if not reviews or ridx0 < 0 or ridx0 >= len(reviews):
                raise ValueError("reviews 索引越界")
            rv = reviews[ridx0]
            if not isinstance(rv, Table):
                raise ValueError("review 必须是 table")
            rc = _norm_text(_safe_str(rv.get("content")))
            if s_old not in rc:
                raise ValueError("原段落未在该教师评价中找到（内容已变化？）")
            rv["content"] = tomlkit.string(rc.replace(s_old, s_new, 1), multiline=True)
            if author:
                _append_author_field(rv, author)
            return tomlkit.dumps(doc).rstrip() + "\n"
        raise ValueError("未找到指定 lecturer")

    if t in {"course_teacher_review", "course_section_item"}:
        courses = _aot(doc.get("courses"))
        if not courses:
            raise ValueError("courses 不存在")
        cidx0 = int(target.get("course_index") or 0)
        if cidx0 < 0 or cidx0 >= len(courses):
            raise ValueError("course_index 越界")
        c = courses[cidx0]
        if not isinstance(c, Table):
            raise ValueError("course 必须是 table")

        if t == "course_teacher_review":
            teacher_name = str(target.get("teacher") or "").strip()
            ridx0 = int(target.get("review_index") or 0)
            teachers = _aot(c.get("teachers"))
            if not teachers:
                raise ValueError("teachers 不存在")
            for tt in teachers:
                if not isinstance(tt, Table):
                    continue
                if _safe_str(tt.get("name")).strip() != teacher_name:
                    continue
                reviews = _aot(tt.get("reviews"))
                if not reviews or ridx0 < 0 or ridx0 >= len(reviews):
                    raise ValueError("reviews 索引越界")
                rv = reviews[ridx0]
                if not isinstance(rv, Table):
                    raise ValueError("review 必须是 table")
                rc = _norm_text(_safe_str(rv.get("content")))
                if s_old not in rc:
                    raise ValueError("原段落未在该教师评价中找到（内容已变化？）")
                rv["content"] = tomlkit.string(rc.replace(s_old, s_new, 1), multiline=True)
                if author:
                    _append_author_field(rv, author)
                return tomlkit.dumps(doc).rstrip() + "\n"
            raise ValueError("未找到指定 teacher")

        # course_section_item
        section_title = str(target.get("section") or "").strip()
        idx0 = int(target.get("index") or 0)
        csecs = _aot(c.get("sections"))
        if not csecs:
            raise ValueError("sections 不存在")
        for sec in csecs:
            if not isinstance(sec, Table):
                continue
            if _safe_str(sec.get("title")).strip() != section_title:
                continue
            items = _aot(sec.get("items"))
            if not items or idx0 < 0 or idx0 >= len(items):
                raise ValueError("items 索引越界")
            it = items[idx0]
            if not isinstance(it, Table):
                raise ValueError("item 必须是 table")
            cc = _norm_text(_safe_str(it.get("content")))
            if s_old not in cc:
                raise ValueError("原段落未在该条目中找到（内容已变化？）")
            it["content"] = tomlkit.string(cc.replace(s_old, s_new, 1), multiline=True)
            if author:
                _append_author_field(it, author)
            return tomlkit.dumps(doc).rstrip() + "\n"
        raise ValueError("未找到指定 section")

    raise ValueError(f"unsupported target type: {t}")


def _format_structure(summary: dict) -> str:
    meta = summary.get("meta") or {}
    course_code = str(meta.get("course_code") or "")
    course_name = str(meta.get("course_name") or "")
    repo_type = str(meta.get("repo_type") or "")

    sections = (summary.get("sections") or {}).get("sections") or {}
    items = sections.get("items") or []

    lines: list[str] = []
    lines.append(f"结构摘要：{course_code} {course_name} ({repo_type})")
    if not items:
        lines.append("（没有 sections；你可以用 /pr add <章节标题> 来新增）")
        return "\n".join(lines)

    for sec in items:
        title = str(sec.get("label") or "").strip() or "(未命名章节)"
        lines.append(f"\n【{title}】")
        sec_items = sec.get("items") or []
        if not sec_items:
            lines.append("  （空）")
            continue
        for it in sec_items:
            idx0 = int(it.get("index") or 0)
            pv = str(it.get("preview") or "").strip()
            idx1 = idx0 + 1
            lines.append(f"  #{idx1} {pv}")

    lines.append("\n指令：/pr add <章节标题> 或 /pr edit <章节标题> <序号>")
    return "\n".join(lines)


matcher = on_message(rule=to_me(), priority=50)


@matcher.handle()
async def _(bot: Bot, event: MessageEvent):
    text = _text(event)

    # 命令：/pr help
    if text in {"/pr", "/pr help", "pr", "pr help"}:
        await matcher.finish(
            "PR 提交（最小闭环）\n"
            "1) /pr start AUTO2001 AUTO2001 自动化专业导论 normal\n"
            "2) /pr show 以合并转发方式展示全文（按主题分段，超长自动拆分）\n"
            "3) 添加：/pr add <章节标题>（可省略标题，按提示输入）\n"
            "4) 修改：/pr modify（按提示先发原段落，再发修改后的段落）\n"
            "5) Bot 会先做合规审核，通过后 ensure PR（已有 PR 会更新）\n\n"
            "取消：/pr cancel"
        )

    # 命令：/pr cancel
    if text in {"/pr cancel", "pr cancel"}:
        _PENDING.pop(_key(event), None)
        await matcher.finish("已取消本次 PR 提交流程")

    # 命令：/pr start <repo> <code> <name...> [repo_type]
    if text.startswith("/pr start ") or text.startswith("pr start "):
        if not _allowed(event):
            await matcher.finish("你没有权限发起 PR（管理员未授权）")

        parts = text.split()
        if len(parts) < 5:
            await matcher.finish("用法：/pr start <repo_name> <course_code> <course_name> <repo_type>")

        repo_name = parts[2]
        course_code = parts[3]
        repo_type = parts[-1]
        course_name = " ".join(parts[4:-1]).strip()
        if not course_name:
            await matcher.finish("course_name 不能为空")

        _PENDING[_key(event)] = Pending(
            repo_name=repo_name,
            course_code=course_code,
            course_name=course_name,
            repo_type=repo_type,
            mode="full_toml",
        )

        await matcher.finish(
            "已进入 PR 提交流程。\n"
            "你可以：\n"
            "- 用 /pr show 查看全文（合并转发）\n"
            "- 用 /pr add /pr modify 做结构化修改\n"
            "- 或直接下一条消息粘贴完整 readme.toml（整段提交）\n\n"
            "提示：会先做 LLM 合规审核，未通过将拒绝提交。"
        )

    # 命令：/pr show（查看结构）
    if text in {"/pr show", "pr show", "/pr view", "pr view"}:
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish("请先 /pr start 进入流程")

        await matcher.send("正在拉取内容并合并转发展示...")
        r = await get_course_toml(repo_name=pending.repo_name or pending.course_code)
        if not r.ok or not r.toml:
            await matcher.finish(f"拉取失败：{r.message}")

        try:
            nodes = build_forward_nodes_from_toml(bot, r.toml)
        except Exception as e:
            await matcher.finish(f"解析 TOML 失败：{e}")

        ok = await _send_forward(bot, event, nodes)
        if not ok:
            await matcher.finish("发送合并转发失败（可能风控/版本问题）。你可以改用直接粘贴整段 TOML 提交。")

        # Also provide a short summary for navigation
        s = await get_course_structure(repo_name=pending.repo_name or pending.course_code)
        if s.ok and s.data and isinstance(s.data.get("summary"), dict):
            await matcher.finish(_format_structure(s.data["summary"]))
        await matcher.finish("已展示。你可以 /pr add 或 /pr modify 继续。")

    # 命令：/pr add <章节标题>
    if text.startswith("/pr add ") or text.startswith("pr add "):
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish("请先 /pr start 进入流程")

        section_title = text.split(" ", 2)[2].strip() if len(text.split(" ", 2)) >= 3 else ""
        if section_title:
            _PENDING[_key(event)] = Pending(
                repo_name=pending.repo_name,
                course_code=pending.course_code,
                course_name=pending.course_name,
                repo_type=pending.repo_type,
                mode="add_content",
                section_title=section_title,
            )
            await matcher.finish(
                f"将向章节《{section_title}》追加一条内容。\n"
                "请下一条消息发送要添加的正文（不要带多余解释）。"
            )

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="add_section",
        )
        await matcher.finish("请发送要追加到的章节标题（已有标题或新建标题均可）。")

    # 命令：/pr modify（按原段落定位修改）
    if text in {"/pr modify", "pr modify", "/pr mod", "pr mod"}:
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish("请先 /pr start 进入流程")

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="modify_old",
        )
        await matcher.finish(
            "请下一条消息粘贴你要修改的“原段落”（尽量原样复制，越长越好，便于定位）。\n"
            "提示：支持 description、sections/items、lecturers.reviews，以及 multi-project 的子课程段落（sections/items、teachers.reviews）。"
        )

    # 命令：/pr edit <章节标题> <序号>（保留：按序号修改）
    if text.startswith("/pr edit ") or text.startswith("pr edit "):
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish("请先 /pr start 进入流程")

        parts = text.split()
        if len(parts) < 4:
            await matcher.finish("用法：/pr edit <章节标题> <序号>")

        try:
            idx1 = int(parts[-1])
        except Exception:
            await matcher.finish("序号必须是数字，例如：/pr edit 关于考试 1")

        section_title = " ".join(parts[2:-1]).strip()
        if not section_title:
            await matcher.finish("章节标题不能为空")
        if idx1 <= 0:
            await matcher.finish("序号从 1 开始")

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="edit_content",
            section_title=section_title,
            item_index=idx1 - 1,
        )
        await matcher.finish(
            "将修改章节《{section_title}》的第 {idx1} 条内容（按序号）。\n"
            "请下一条消息发送修改后的完整正文（不要带多余解释）。"
        )

    # 如果处于 pending，则把这条消息当 TOML 或正文
    pending = _PENDING.get(_key(event))
    if not pending:
        return

    default_author_name = _author_name(event)

    # full TOML flow
    if pending.mode == "full_toml":
        toml_text = text
        if not toml_text or len(toml_text) < 20:
            await matcher.finish("TOML 内容太短，请重新粘贴完整 readme.toml")

        await matcher.send("正在进行内容合规审核...")
        mod = await moderate_toml(toml_text)
        if not mod.approved:
            _PENDING.pop(_key(event), None)
            await matcher.finish(f"审核未通过：{mod.reason}")

        await matcher.send("审核通过，正在提交并确保 PR...")
        result = await ensure_pr(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            toml_text=toml_text,
        )
        _PENDING.pop(_key(event), None)
        if not result.ok:
            await matcher.finish(f"提交失败：{result.message}")
        if result.pr_url:
            await matcher.finish(f"已创建/更新 PR：{result.pr_url}")
        if result.request_id:
            await matcher.finish(f"仓库不存在，已进入 pending：request_id={result.request_id}")
        await matcher.finish(f"提交完成：{result.message}")

    # collect section title for add
    if pending.mode == "add_section":
        section_title = text.strip()
        if not section_title:
            await matcher.finish("章节标题不能为空，请重新发送")
        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="add_content",
            section_title=section_title,
        )
        await matcher.finish(f"将向章节《{section_title}》追加一条内容。请下一条消息发送正文。")

    # modify: receive old paragraph
    if pending.mode == "modify_old":
        old = text.strip()
        if len(old) < 10:
            await matcher.finish("原段落太短，建议复制更长一点的原文再试")

        await matcher.send("正在从仓库 TOML 中定位该段落...")
        r = await get_course_toml(repo_name=pending.repo_name or pending.course_code)
        if not r.ok or not r.toml:
            _PENDING.pop(_key(event), None)
            await matcher.finish(f"拉取 TOML 失败：{r.message}")

        candidates = _find_paragraph_candidates(r.toml, old)
        if not candidates:
            await matcher.finish("未定位到匹配条目。请确认复制的是仓库里的原文，或提供更长的片段。")

        if len(candidates) == 1:
            c = candidates[0]
            _PENDING[_key(event)] = Pending(
                repo_name=pending.repo_name,
                course_code=pending.course_code,
                course_name=pending.course_name,
                repo_type=pending.repo_type,
                mode="modify_new",
                section_title=str(c.get("section") or ""),
                item_index=int(c.get("index") or -1),
                old_paragraph=old,
                target=c,
                base_toml=r.toml,
            )
            if str(c.get("type")) == "section_item":
                await matcher.finish(
                    f"已定位到：章节《{c.get('section')}》第 {int(c.get('index') or 0)+1} 条：{c.get('preview')}\n"
                    "请下一条消息发送修改后的完整正文。"
                )
            if str(c.get("type")) == "description":
                await matcher.finish(
                    "已定位到：description\n"
                    f"预览：{c.get('preview')}\n"
                    "请下一条消息发送修改后的完整正文。"
                )
            if str(c.get("type")) == "lecturer_review":
                await matcher.finish(
                    f"已定位到：lecturers《{c.get('lecturer')}》评价#{int(c.get('review_index') or 0)+1}\n"
                    f"预览：{c.get('preview')}\n"
                    "请下一条消息发送修改后的完整正文。"
                )
            if str(c.get("type")) == "course_section_item":
                await matcher.finish(
                    f"已定位到：子课程《{c.get('course_name')}》章节《{c.get('section')}》第 {int(c.get('index') or 0)+1} 条\n"
                    f"预览：{c.get('preview')}\n"
                    "请下一条消息发送修改后的完整正文。"
                )
            if str(c.get("type")) == "course_teacher_review":
                await matcher.finish(
                    f"已定位到：子课程《{c.get('course_name')}》教师《{c.get('teacher')}》评价#{int(c.get('review_index') or 0)+1}\n"
                    f"预览：{c.get('preview')}\n"
                    "请下一条消息发送修改后的完整正文。"
                )
            await matcher.finish("已定位到目标。请下一条消息发送修改后的完整正文。")

        # multiple: ask choose
        lines = ["找到多个匹配，请回复序号选择："]
        for i, c in enumerate(candidates[:8], start=1):
            ctype = str(c.get("type") or "")
            if ctype == "section_item":
                lines.append(f"{i}) [sections] 《{c.get('section')}》#{int(c.get('index') or 0)+1} {c.get('preview')}")
            elif ctype == "description":
                lines.append(f"{i}) [description] {c.get('preview')}")
            elif ctype == "lecturer_review":
                lines.append(
                    f"{i}) [lecturers] 《{c.get('lecturer')}》评价#{int(c.get('review_index') or 0)+1} {c.get('preview')}"
                )
            elif ctype == "course_section_item":
                lines.append(
                    f"{i}) [courses.sections] 《{c.get('course_name')}》/《{c.get('section')}》#{int(c.get('index') or 0)+1} {c.get('preview')}"
                )
            elif ctype == "course_teacher_review":
                lines.append(
                    f"{i}) [courses.teachers] 《{c.get('course_name')}》/《{c.get('teacher')}》评价#{int(c.get('review_index') or 0)+1} {c.get('preview')}"
                )
            else:
                lines.append(f"{i}) {c.get('preview')}")
        if len(candidates) > 8:
            lines.append(f"（仅展示前 8 个，共 {len(candidates)} 个匹配；建议提供更长原文缩小范围）")

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="modify_choose",
            candidates=candidates[:8],
            old_paragraph=old,
            base_toml=r.toml,
        )
        await matcher.finish("\n".join(lines))

    if pending.mode == "modify_choose":
        if not pending.candidates:
            _PENDING.pop(_key(event), None)
            await matcher.finish("状态异常：请重新 /pr modify")
        try:
            pick = int(text.strip())
        except Exception:
            await matcher.finish("请回复数字序号（例如 1）")
        if pick <= 0 or pick > len(pending.candidates):
            await matcher.finish("序号超出范围")
        c = pending.candidates[pick - 1]
        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="modify_new",
            section_title=str(c.get("section") or ""),
            item_index=int(c.get("index") or -1),
            old_paragraph=pending.old_paragraph,
            target=c,
            base_toml=pending.base_toml,
        )
        ctype2 = str(c.get("type") or "")
        if ctype2 == "section_item":
            await matcher.finish(
                f"已选择：章节《{c.get('section')}》第 {int(c.get('index') or 0)+1} 条：{c.get('preview')}\n"
                "请下一条消息发送修改后的完整正文。"
            )
        if ctype2 == "description":
            await matcher.finish("已选择：description\n请下一条消息发送修改后的完整正文。")
        if ctype2 == "lecturer_review":
            await matcher.finish(
                f"已选择：lecturers《{c.get('lecturer')}》评价#{int(c.get('review_index') or 0)+1}\n"
                "请下一条消息发送修改后的完整正文。"
            )
        if ctype2 == "course_section_item":
            await matcher.finish(
                f"已选择：子课程《{c.get('course_name')}》章节《{c.get('section')}》第 {int(c.get('index') or 0)+1} 条\n"
                "请下一条消息发送修改后的完整正文。"
            )
        if ctype2 == "course_teacher_review":
            await matcher.finish(
                f"已选择：子课程《{c.get('course_name')}》教师《{c.get('teacher')}》评价#{int(c.get('review_index') or 0)+1}\n"
                "请下一条消息发送修改后的完整正文。"
            )
        await matcher.finish("已选择目标。请下一条消息发送修改后的完整正文。")

    if pending.mode == "modify_new":
        new = text.strip()
        if not new:
            await matcher.finish("修改后的正文不能为空")

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="attrib_ask",
            section_title=pending.section_title,
            item_index=pending.item_index,
            old_paragraph=pending.old_paragraph,
            new_paragraph=new,
            target=pending.target,
            base_toml=pending.base_toml,
        )
        await matcher.finish("是否在该条目 author 中留名？回复 y/n")

    # add/edit flow (by title/index): ask attribution after receiving content
    if pending.mode in {"add_content", "edit_content"}:
        content = text.strip()
        if not content:
            await matcher.finish("内容不能为空，请重新发送")
        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="attrib_ask",
            section_title=pending.section_title,
            item_index=pending.item_index,
            new_paragraph=content,
        )
        await matcher.finish("是否在该条目 author 中留名？回复 y/n")

    if pending.mode == "attrib_ask":
        ans = text.strip().lower()
        if ans in {"y", "yes", "是", "要", "留", "留名"}:
            _PENDING[_key(event)] = Pending(
                repo_name=pending.repo_name,
                course_code=pending.course_code,
                course_name=pending.course_name,
                repo_type=pending.repo_type,
                mode="attrib_name",
                section_title=pending.section_title,
                item_index=pending.item_index,
                old_paragraph=pending.old_paragraph,
                new_paragraph=pending.new_paragraph,
                target=pending.target,
                base_toml=pending.base_toml,
                want_attribution=True,
            )
            await matcher.finish(f"请输入显示名字（直接回车则用：{default_author_name}）")
        if ans in {"n", "no", "否", "不要", "不留"}:
            _PENDING[_key(event)] = Pending(
                repo_name=pending.repo_name,
                course_code=pending.course_code,
                course_name=pending.course_name,
                repo_type=pending.repo_type,
                mode="build_patch",
                section_title=pending.section_title,
                item_index=pending.item_index,
                old_paragraph=pending.old_paragraph,
                new_paragraph=pending.new_paragraph,
                target=pending.target,
                base_toml=pending.base_toml,
                want_attribution=False,
            )
            await matcher.send("好的，不留名。")
            # fallthrough to build_patch below
        else:
            await matcher.finish("请回复 y 或 n")

    if pending.mode == "attrib_name":
        name = text.strip() or default_author_name
        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="attrib_link",
            section_title=pending.section_title,
            item_index=pending.item_index,
            old_paragraph=pending.old_paragraph,
            new_paragraph=pending.new_paragraph,
            target=pending.target,
            base_toml=pending.base_toml,
            want_attribution=True,
            author_name=name,
        )
        await matcher.finish("可选：请输入你的主页链接（GitHub/博客等），留空则不填")

    if pending.mode == "attrib_link":
        link = text.strip()
        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="build_patch",
            section_title=pending.section_title,
            item_index=pending.item_index,
            old_paragraph=pending.old_paragraph,
            new_paragraph=pending.new_paragraph,
            target=pending.target,
            base_toml=pending.base_toml,
            want_attribution=True,
            author_name=pending.author_name,
            author_link=link,
        )
        await matcher.send("收到。")
        # fallthrough to build_patch below

    if pending.mode == "build_patch":
        author = None
        if pending.want_attribution:
            author = {
                "name": pending.author_name or default_author_name,
                "link": pending.author_link or "",
                "date": _year_month(),
            }

        # determine op based on what we have
        if pending.old_paragraph and pending.target:
            ttype = str(pending.target.get("type") or "")
            if ttype == "section_item":
                fields = {"content": pending.new_paragraph}
                if author:
                    fields["author"] = author
                ops = [
                    {
                        "op": "update_section_item",
                        "section": pending.section_title,
                        "index": pending.item_index,
                        "fields": fields,
                    }
                ]
            else:
                # local patch for targets not supported by submit_ops
                if not pending.base_toml:
                    _PENDING.pop(_key(event), None)
                    await matcher.finish("状态异常：缺少 base TOML，请重新 /pr modify")
                await matcher.send("正在生成修改后的 TOML...")
                try:
                    patched_toml = _patch_toml_by_target(
                        pending.base_toml,
                        target=pending.target,
                        old_paragraph=pending.old_paragraph,
                        new_paragraph=pending.new_paragraph,
                        author=author,
                    )
                except Exception as e:
                    await matcher.finish(f"生成失败：{e}")

                _PENDING[_key(event)] = Pending(
                    repo_name=pending.repo_name,
                    course_code=pending.course_code,
                    course_name=pending.course_name,
                    repo_type=pending.repo_type,
                    mode="confirm",
                    section_title=pending.section_title,
                    item_index=pending.item_index,
                    old_paragraph=pending.old_paragraph,
                    new_paragraph=pending.new_paragraph,
                    want_attribution=pending.want_attribution,
                    author_name=pending.author_name,
                    author_link=pending.author_link,
                    patched_toml=patched_toml,
                    target=pending.target,
                )

                old_preview = (pending.old_paragraph or "").strip()
                if old_preview and len(old_preview) > 200:
                    old_preview = old_preview[:199] + "…"
                new_preview = (pending.new_paragraph or "").strip()
                if new_preview and len(new_preview) > 200:
                    new_preview = new_preview[:199] + "…"

                msg = ["即将提交：定位修改".strip()]
                if old_preview:
                    msg.append(f"\n原段落（截断）：\n{old_preview}")
                msg.append(f"\n新段落（截断）：\n{new_preview}")
                msg.append("\n回复：确认 / 取消")
                await matcher.finish("\n".join(msg))
        elif pending.item_index >= 0:
            fields2 = {"content": pending.new_paragraph}
            if author:
                fields2["author"] = author
            ops = [
                {
                    "op": "update_section_item",
                    "section": pending.section_title,
                    "index": pending.item_index,
                    "fields": fields2,
                }
            ]
        else:
            item = {"content": pending.new_paragraph}
            if author:
                item["author"] = author
            ops = [
                {
                    "op": "append_section_item",
                    "section": pending.section_title,
                    "item": item,
                }
            ]

        await matcher.send("正在生成修改后的 TOML...")
        patched = await submit_ops_dry_run(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            ops=ops,
        )
        if not patched.ok or not patched.toml:
            _PENDING.pop(_key(event), None)
            await matcher.finish(f"生成失败：{patched.message}")

        info = ""
        if pending.section_title:
            info = f"章节《{pending.section_title}》"
        if pending.item_index >= 0:
            info += f" 第 {pending.item_index+1} 条"
        old_preview = (pending.old_paragraph or "").strip()
        if old_preview and len(old_preview) > 200:
            old_preview = old_preview[:199] + "…"
        new_preview = (pending.new_paragraph or "").strip()
        if new_preview and len(new_preview) > 200:
            new_preview = new_preview[:199] + "…"

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="confirm",
            section_title=pending.section_title,
            item_index=pending.item_index,
            old_paragraph=pending.old_paragraph,
            new_paragraph=pending.new_paragraph,
            want_attribution=pending.want_attribution,
            author_name=pending.author_name,
            author_link=pending.author_link,
            patched_toml=patched.toml,
            target=pending.target,
            base_toml=pending.base_toml,
        )

        msg = [f"即将提交：{info}".strip()]
        if old_preview:
            msg.append(f"\n原段落（截断）：\n{old_preview}")
        msg.append(f"\n新段落（截断）：\n{new_preview}")
        msg.append("\n回复：确认 / 取消")
        await matcher.finish("\n".join(msg))

    if pending.mode == "confirm":
        ans2 = text.strip().lower()
        if ans2 in {"取消", "cancel", "c", "n", "no"}:
            _PENDING.pop(_key(event), None)
            await matcher.finish("已取消本次修改")
        if ans2 not in {"确认", "confirm", "y", "yes", "是"}:
            await matcher.finish("请回复：确认 或 取消")

        if not pending.patched_toml:
            _PENDING.pop(_key(event), None)
            await matcher.finish("状态异常：缺少 patched TOML，请重新开始")

        await matcher.send("正在进行内容合规审核...")
        mod = await moderate_toml(pending.patched_toml)
        if not mod.approved:
            _PENDING.pop(_key(event), None)
            await matcher.finish(f"审核未通过：{mod.reason}")

        await matcher.send("审核通过，正在提交并确保 PR...")
        result = await ensure_pr(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            toml_text=pending.patched_toml,
        )
        _PENDING.pop(_key(event), None)
        if not result.ok:
            await matcher.finish(f"提交失败：{result.message}")
        if result.pr_url:
            await matcher.finish(f"已创建/更新 PR：{result.pr_url}")
        if result.request_id:
            await matcher.finish(f"仓库不存在，已进入 pending：request_id={result.request_id}")
        await matcher.finish(f"提交完成：{result.message}")

    # unknown mode
    _PENDING.pop(_key(event), None)
    await matcher.finish("状态异常：已重置会话，请重新 /pr start")

    if result.pr_url:
        await matcher.finish(f"✅ 已创建 PR：{result.pr_url}")

    if result.request_id:
        await matcher.finish(f"✅ 已进入 pending：request_id={result.request_id}")

    await matcher.finish(f"✅ 提交完成：{result.message}")
