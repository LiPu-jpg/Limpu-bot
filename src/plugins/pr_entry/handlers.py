from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional
import re

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message, MessageSegment
from nonebot.rule import to_me

from .moderation import moderate_toml
from .prserver_client import ensure_pr, get_course_structure, get_course_toml, submit_ops_dry_run
from .settings import settings
from ..course_manager.data_loader import course_manager

import tomlkit
from tomlkit.container import Container
from tomlkit.items import AoT, Array, InlineTable, Table, Trivia
from tomlkit.toml_document import TOMLDocument


@dataclass
class Pending:
    repo_name: Optional[str] = None
    course_code: Optional[str] = None
    course_name: Optional[str] = None
    repo_type: Optional[str] = None

    mode: str = ""

    # add/edit by section+index
    section_title: str = ""
    item_index: int = -1

    # modify by paragraph locating
    old_paragraph: str = ""
    new_paragraph: str = ""
    candidates: Optional[list[dict]] = None
    target: Optional[dict] = None

    # store the TOML we located against (avoid race / re-fetch)
    base_toml: Optional[str] = None

    # attribution
    want_attribution: bool | None = None
    author_name: str = ""
    author_link: str = ""

    # prepared payload
    patched_toml: str = ""


def _normal_template(*, course_name: str, course_code: str) -> str:
    return (
        f'course_name = "{course_name}"\n'
        'repo_type = "normal"\n'
        f'course_code = "{course_code}"\n\n'
        'description = """\n"""\n'
    )


def _multiproject_template(*, course_name: str, course_code: str) -> str:
    return (
        f'course_name = "{course_name}"\n'
        'repo_type = "multi-project"\n'
        f'course_code = "{course_code}"\n\n'
        '[[courses]]\n'
        f'course_code = "{course_code}"\n'
        f'course_name = "{course_name}"\n'
        'description = """\n"""\n'
    )


def _normal_draft_ready_text(*, course_code: str, course_name: str) -> str:
    return (
        f"已建立临时仓库草稿：{course_code} / {course_name}（normal）\n"
        "现在可以多次使用 /pr add、/pr addreview、/pr adddesc、/pr modify 累计修改，最后用 /pr submit 统一提交。"
    )


_PENDING: dict[tuple[int | None, int], Pending] = {}


def _key(event: MessageEvent) -> tuple[int | None, int]:
    group_id = getattr(event, "group_id", None)
    user_id = int(getattr(event, "user_id", 0))
    return (group_id, user_id)


def _allowed(event: MessageEvent) -> bool:
    if not settings.allowed_users:
        return True
    return str(event.user_id) in settings.allowed_users


def _text(event: MessageEvent) -> str:
    # 群聊里多数交互都需要 @bot（to_me）。
    # 但用户想“留空/直接回车”时，实际发送的是“@bot”且不带文本。
    # OneBot 的 get_plaintext() 往往会把 @ 显示成“@xxx”，导致误判为非空。
    # 这里优先从 message 段里仅提取 text 段，忽略 at 段，从而把“只 @”视为真正的空输入。
    try:
        msg = getattr(event, "message", None) or event.get_message()
        if isinstance(msg, Message):
            parts: list[str] = []
            for seg in msg:
                if getattr(seg, "type", None) == "text":
                    parts.append(str((getattr(seg, "data", None) or {}).get("text", "")))
            s = "".join(parts).strip()
            if s or s == "":
                return s
    except Exception:
        pass

    # 兜底：尽量去掉开头的 @xxx
    s2 = (event.get_plaintext() or "").strip()
    s2 = re.sub(r"^\s*[@＠][^\s]+\s*", "", s2)
    return s2.strip()


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


def _submit_result_text(result) -> str:
    if result.pr_url:
        if getattr(result, "status", None) == "cache_pr_created":
            return f"课程仓库不存在，已创建 hoa-cache 建议目录 PR：{result.pr_url}"
        return f"已创建/更新 PR：{result.pr_url}"
    if result.request_id:
        if getattr(result, "status", None) in {"queued", "running"}:
            return f"已进入后台处理：request_id={result.request_id}。请稍后查看结果。"
        return f"仓库不存在，已进入 pending：request_id={result.request_id}"
    return f"提交完成：{result.message}"


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
            await bot.call_api("send_group_forward_msg", group_id=getattr(event, "group_id", None), messages=nodes)
        else:
            await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)
        return True
    except Exception:
        return False


def _doc_table(doc: object) -> Table:
    if isinstance(doc, TOMLDocument):
        table = Table(Container(), Trivia(), is_aot_element=False)
        for key, value in doc.items():
            table[key] = value
        return table
    if isinstance(doc, Table):
        return doc
    raise ValueError("invalid TOML doc")


def _aot(v: object) -> AoT | None:
    return v if isinstance(v, AoT) else None


def _safe_str(v: object) -> str:
    if v is None:
        return ""
    return str(v)


def _norm_text(s: str) -> str:
    return (s or "").strip().replace("\r\n", "\n")


def _toml_multiline(s: str):
    s2 = _norm_text(s)
    return tomlkit.string(s2, multiline=True)


def _append_toml_by_target(
    base_toml: str,
    *,
    target: dict,
    content: str = "",
    author: dict | None = None,
) -> str:
    """对 multi-project TOML 做追加类修改（本地 patch，不依赖 prServer submit_ops）。"""

    doc = _doc_table(tomlkit.parse(base_toml))

    t = str((target or {}).get("type") or "").strip()
    if t not in {"append_course", "append_course_section_item", "append_course_teacher_review"}:
        raise ValueError(f"unsupported append target type: {t}")

    courses = _aot(doc.get("courses"))
    if courses is None:
        raise ValueError("multi-project: courses 不存在")

    if t == "append_course":
        name = str(target.get("course_name") or "").strip()
        if not name:
            raise ValueError("course_name 不能为空")
        for c in courses:
            if isinstance(c, Table) and _safe_str(c.get("name")).strip() == name:
                raise ValueError("已存在同名子课程")

        c = tomlkit.table()
        c.add("name", name)
        c.add("code", str(target.get("code") or ""))
        courses.append(c)
        return tomlkit.dumps(doc).rstrip() + "\n"

    course_name = str(target.get("course_name") or "").strip()
    if not course_name:
        raise ValueError("course_name 不能为空")

    course_tbl: Table | None = None
    for c in courses:
        if isinstance(c, Table) and _safe_str(c.get("name")).strip() == course_name:
            course_tbl = c
            break
    if course_tbl is None:
        raise ValueError("未找到指定子课程")

    if t == "append_course_section_item":
        section_title = str(target.get("section") or "").strip()
        if not section_title:
            raise ValueError("section 不能为空")

        secs = _aot(course_tbl.get("sections"))
        if secs is None:
            secs = AoT([])
            course_tbl["sections"] = secs

        sec_tbl: Table | None = None
        for sec in secs:
            if isinstance(sec, Table) and _safe_str(sec.get("title")).strip() == section_title:
                sec_tbl = sec
                break
        if sec_tbl is None:
            sec_tbl = tomlkit.table()
            sec_tbl.add("title", section_title)
            sec_tbl.add("items", AoT([]))
            secs.append(sec_tbl)

        items = _aot(sec_tbl.get("items"))
        if items is None:
            items = AoT([])
            sec_tbl["items"] = items

        it = tomlkit.table()
        it.add("content", _toml_multiline(content))
        if author:
            _append_author_field(it, author)
        items.append(it)
        return tomlkit.dumps(doc).rstrip() + "\n"

    # append_course_teacher_review
    teacher = str(target.get("teacher") or "").strip()
    if not teacher:
        raise ValueError("teacher 不能为空")

    teachers = _aot(course_tbl.get("teachers"))
    if teachers is None:
        teachers = AoT([])
        course_tbl["teachers"] = teachers

    t_tbl: Table | None = None
    for tt in teachers:
        if isinstance(tt, Table) and _safe_str(tt.get("name")).strip() == teacher:
            t_tbl = tt
            break
    if t_tbl is None:
        t_tbl = tomlkit.table()
        t_tbl.add("name", teacher)
        t_tbl.add("reviews", AoT([]))
        teachers.append(t_tbl)

    reviews = _aot(t_tbl.get("reviews"))
    if reviews is None:
        reviews = AoT([])
        t_tbl["reviews"] = reviews

    rv = tomlkit.table()
    rv.add("content", _toml_multiline(content))
    if author:
        _append_author_field(rv, author)
    reviews.append(rv)
    return tomlkit.dumps(doc).rstrip() + "\n"


def _append_normal_lecturer_review(
    base_toml: str,
    *,
    lecturer: str,
    content: str,
    author: dict | None,
) -> str:
    doc = _doc_table(tomlkit.parse(base_toml))

    name = (lecturer or "").strip()
    if not name:
        raise ValueError("lecturer 不能为空")

    lecturers = _aot(doc.get("lecturers"))
    if lecturers is None:
        lecturers = AoT([])
        doc["lecturers"] = lecturers

    lec_tbl: Table | None = None
    for lec in lecturers:
        if isinstance(lec, Table) and _safe_str(lec.get("name")).strip() == name:
            lec_tbl = lec
            break
    if lec_tbl is None:
        lec_tbl = tomlkit.table()
        lec_tbl.add("name", name)
        lec_tbl.add("reviews", AoT([]))
        lecturers.append(lec_tbl)

    reviews = _aot(lec_tbl.get("reviews"))
    if reviews is None:
        reviews = AoT([])
        lec_tbl["reviews"] = reviews

    rv = tomlkit.table()
    rv.add("content", _toml_multiline(content))
    if author:
        _append_author_field(rv, author)
    reviews.append(rv)
    return tomlkit.dumps(doc).rstrip() + "\n"


def _set_description_local(base_toml: str, *, content: str) -> str:
    doc = _doc_table(tomlkit.parse(base_toml))
    doc["description"] = tomlkit.string(_norm_text(content), multiline=True)
    return tomlkit.dumps(doc).rstrip() + "\n"


def _append_normal_section_item(
    base_toml: str,
    *,
    section_title: str,
    content: str,
    author: dict | None,
) -> str:
    doc = _doc_table(tomlkit.parse(base_toml))
    title = (section_title or "").strip()
    if not title:
        raise ValueError("section_title 不能为空")

    sections = _aot(doc.get("sections"))
    if sections is None:
        sections = AoT([])
        doc["sections"] = sections

    sec_tbl: Table | None = None
    for sec in sections:
        if isinstance(sec, Table) and _safe_str(sec.get("title")).strip() == title:
            sec_tbl = sec
            break
    if sec_tbl is None:
        sec_tbl = tomlkit.table()
        sec_tbl.add("title", title)
        sec_tbl.add("items", AoT([]))
        sections.append(sec_tbl)

    items = _aot(sec_tbl.get("items"))
    if items is None:
        items = AoT([])
        sec_tbl["items"] = items

    it = tomlkit.table()
    it.add("content", _toml_multiline(content))
    if author:
        _append_author_field(it, author)
    items.append(it)
    return tomlkit.dumps(doc).rstrip() + "\n"


def _update_normal_section_item(
    base_toml: str,
    *,
    section_title: str,
    item_index: int,
    content: str,
    author: dict | None,
) -> str:
    doc = _doc_table(tomlkit.parse(base_toml))
    sections = _aot(doc.get("sections"))
    if not sections:
        raise ValueError("sections 不存在")

    target_title = (section_title or "").strip()
    sec_tbl: Table | None = None
    for sec in sections:
        if isinstance(sec, Table) and _safe_str(sec.get("title")).strip() == target_title:
            sec_tbl = sec
            break
    if sec_tbl is None:
        raise ValueError("未找到指定章节")

    items = _aot(sec_tbl.get("items"))
    if not items or item_index < 0 or item_index >= len(items):
        raise ValueError("章节条目索引越界")
    it = items[item_index]
    if not isinstance(it, Table):
        raise ValueError("章节条目必须是 table")
    it["content"] = _toml_multiline(content)
    if author:
        _append_author_field(it, author)
    return tomlkit.dumps(doc).rstrip() + "\n"


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


def _extract_segments(doc):
    repo_type = getattr(doc, "repo_type", "")  # 从doc中获取repo_type
    if isinstance(doc, Table):
        return _extract_multi_segments(doc) if repo_type == "multi-project" else _extract_normal_segments(doc)
    elif isinstance(doc, TOMLDocument):
        table = _doc_table(doc)  # 使用_doc_table将TOMLDocument转换为Table
        return _extract_multi_segments(table) if repo_type == "multi-project" else _extract_normal_segments(table)
    else:
        raise TypeError("doc must be of type Table or TOMLDocument")


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
    repo_type = getattr(doc, "repo_type", "")

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
    repo_type = getattr(doc, "repo_type", "")

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
    if repo_type == "multi-project":
        lines.append("（multi-project：请先用 /pr target <子课程名或序号> 选中子课程，再对该子课程的 sections 进行操作）")
        return "\n".join(lines)
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


def _list_multi_courses_from_toml(toml_text: str) -> list[dict]:
    """Return list of {index,name,code} for multi-project courses."""
    doc = _doc_table(tomlkit.parse(toml_text))
    repo_type = str(getattr(doc, "repo_type", "") or "").strip()
    if repo_type != "multi-project":
        return []
    courses = _aot(doc.get("courses"))
    out: list[dict] = []
    if not courses:
        return out
    for i, c in enumerate(courses):
        if not isinstance(c, Table):
            continue
        name = _safe_str(c.get("name")).strip()
        code = _safe_str(c.get("code")).strip()
        if not name:
            continue
        out.append({"index": i + 1, "name": name, "code": code})
    return out


def _pick_course_name(*, toml_text: str, pick: str) -> str | None:
    """Pick course name by exact name or 1-based index (string)."""
    items = _list_multi_courses_from_toml(toml_text)
    if not items:
        return None
    raw = (pick or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw)
        for it in items:
            if int(it.get("index") or 0) == idx:
                return str(it.get("name") or "").strip() or None
        return None
    # exact match
    for it in items:
        if str(it.get("name") or "").strip() == raw:
            return raw
    return None


def _format_multi_course_structure(*, toml_text: str, course_name: str) -> str:
    doc = _doc_table(tomlkit.parse(toml_text))
    lines: list[str] = []
    lines.append(f"当前子课程：{course_name}")

    courses = _aot(doc.get("courses"))
    picked: Table | None = None
    if courses:
        for c in courses:
            if not isinstance(c, Table):
                continue
            if _safe_str(c.get("name")).strip() == course_name:
                picked = c
                break
    if not picked:
        return "\n".join(lines + ["（未在 courses 中找到该子课程；请用 /pr target 重新选择）"])

    teachers = _aot(picked.get("teachers"))
    if teachers:
        names: list[str] = []
        for t in teachers:
            if not isinstance(t, Table):
                continue
            n = _safe_str(t.get("name")).strip()
            if n:
                names.append(n)
        if names:
            lines.append(f"教师：{', '.join(names)}")

    secs = _aot(picked.get("sections"))
    if not secs:
        lines.append("（该子课程暂无 sections；你可以用 /pr add <章节标题> 新增）")
        return "\n".join(lines)

    lines.append("sections：")
    for sec in secs:
        if not isinstance(sec, Table):
            continue
        title = _safe_str(sec.get("title")).strip() or "(未命名章节)"
        items = _aot(sec.get("items"))
        n_items = len(items) if items else 0
        lines.append(f"- {title}（{n_items} 条）")

    lines.append("\n指令：/pr add <章节标题>  或  /pr modify（按原段落定位修改）")
    return "\n".join(lines)


def _build_forward_nodes_for_multi_course(bot: Bot, toml_text: str, course_name: str) -> list[dict]:
    doc = _doc_table(tomlkit.parse(toml_text))
    segs = _extract_multi_segments(doc)
    picked = None
    for title, body in segs:
        if title == course_name:
            picked = (title, body)
            break
    if not picked:
        return [make_node(bot, f"未找到子课程《{course_name}》；请用 /pr target 重新选择。")]

    title, body = picked
    nodes: list[dict] = []
    parts = _split_long_text(body, limit=1800)
    if len(parts) == 1:
        nodes.append(make_node(bot, parts[0]))
    else:
        for i, p in enumerate(parts, start=1):
            nodes.append(make_node(bot, f"{title}（{i}/{len(parts)}）\n\n{p}".strip()))
    return nodes


def _chunk_lines(lines: list[str], *, limit: int = 1800) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        s = (line or "")
        add = len(s) + (1 if buf else 0)  # newline
        if buf and size + add > limit:
            out.append("\n".join(buf).rstrip())
            buf = [s]
            size = len(s)
            continue
        buf.append(s)
        size += add
    if buf:
        out.append("\n".join(buf).rstrip())
    return out


async def _prompt_pick_multi_course(*, matcher, event: MessageEvent, repo_name: str, hint: str = "") -> None:
    r = await get_course_toml(repo_name=repo_name)
    if not r.ok or not r.toml:
        await matcher.finish(_reply_msg(event, f"拉取失败：{r.message}"))

    toml_text = str(r.toml)
    items = _list_multi_courses_from_toml(toml_text)
    if not items:
        await matcher.finish(_reply_msg(event, "该仓库为 multi-project，但 readme.toml 中没有 courses 列表，无法选择子课程。"))

    header: list[str] = []
    if hint:
        header.append(hint)
    header.append(f"请先选择要编辑的子课程（共 {len(items)} 门）：")

    body: list[str] = []
    for it in items:
        idx = int(it.get("index") or 0)
        name = str(it.get("name") or "").strip()
        code = str(it.get("code") or "").strip()
        code_part = f"（{code}）" if code else ""
        body.append(f"{idx}. {name}{code_part}")

    footer = "用法：/pr target <子课程名>  或  /pr target <序号>"

    # 分多条消息发送，避免超长度限制
    chunks = _chunk_lines(body, limit=1500)
    if not chunks:
        await matcher.finish(_reply_msg(event, "该仓库没有可选子课程。"))

    if len(chunks) == 1:
        await matcher.finish(_reply_msg(event, "\n".join(header + chunks + ["", footer]).rstrip()))

    for i, chunk in enumerate(chunks, start=1):
        if i == 1:
            text = "\n".join(header + [f"（第 {i}/{len(chunks)} 段）"] + [chunk]).rstrip()
        else:
            text = "\n".join([f"（第 {i}/{len(chunks)} 段）", chunk]).rstrip()

        if i < len(chunks):
            await matcher.send(_reply_msg(event, text))
        else:
            await matcher.finish(_reply_msg(event, "\n".join([text, "", footer]).rstrip()))


matcher = on_message(rule=to_me(), priority=100)


def _is_repo_type(s: str) -> bool:
    v = (s or "").strip().lower()
    return v in {"normal", "multi-project"}


def _extract_meta_from_summary(summary: dict | None) -> tuple[str, str, str]:
    meta = (summary or {}).get("meta") or {}
    course_code = str(meta.get("course_code") or "").strip()
    course_name = str(meta.get("course_name") or "").strip()
    repo_type = str(meta.get("repo_type") or "").strip()
    return course_code, course_name, repo_type


def _reply_msg(event: MessageEvent, text: str) -> Message | str:
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        return text
    user_id = int(getattr(event, "user_id", 0) or 0)
    return Message([MessageSegment.at(user_id), MessageSegment.text(" "), MessageSegment.text(text)])


@matcher.handle()
async def _(bot: Bot, event: MessageEvent):
    text = _text(event)
    reply = lambda s: _reply_msg(event, s)
    # 容错：有些客户端会输入“/ pr show”（/ 后多空格）或多空格。
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = re.sub(r"^/\s+", "/", text)

    # 检测是否仅仅是 @ 机器人
    if not text.strip():
        if not _PENDING.get(_key(event)):
            await matcher.finish(
                "🎓 你好！我是 HITSZ 课程助理 Hoa_Anon酱\n"
                "找课程请记得 @我 并使用以下指令：\n"
                "━━━━━━━━━━━━━━\n"
                "【课程查询】\n"
                "- /搜 <关键词>：模糊搜索课程\n"
                "- /查 <课程代码|全名|昵称>：查看详细评价（合并转发）\n"
                "- /查老师 <全名|首字母>\n"
                "- /设置昵称 <昵称> <课程代码>：绑定昵称\n"
                "- /刷：拉取课程仓库并更新数据\n"
                "━━━━━━━━━━━━━━\n"
                "【问答（RAG）】\n"
                "- /重构知识库：重建向量库（较耗资源，建议低峰使用）\n"
                "- /问 <问题>：基于知识库问答\n"
                "━━━━━━━━━━━━━━\n"
                "【PR 提交】\n"
                "- /pr help：查看流程说明\n"
                "- /pr start <repo> <code> <name...> <repo_type>：进入会话\n"
                "- /pr show：展示 readme.toml（分段合并转发）\n"
                "- /pr add：追加内容；/pr edit：按序号修改；/pr modify：按原段落定位修改\n"
                "- /pr cancel：取消当前会话\n"
                "━━━━━━━━━━━━━━\n"
                "🌐 网页版：https://v3.hoa.moe"
            )

    # 命令：/pr help
    if text in {"/pr", "/pr help", "pr", "pr help"}:
        await matcher.finish(
            reply(
                "PR 提交（流程）\n"
                "1) /pr start <repo 或 课程代码/全名/昵称>\n"
                "   如果仓库不存在，会提示你建立临时仓库草稿（QQ 侧默认按 normal 处理）\n"
                "2) /pr show 查看当前草稿/当前内容（合并转发）\n"
                "3) 追加：/pr add [章节标题]；教师：/pr addreview ...；说明：/pr adddesc\n"
                "   multi-project：先 /pr target <子课程>，或在命令里带 <子课程名>\n"
                "   multi-project 新增子课程：/pr addcourse <子课程名> [课程代码]\n"
                "4) 修改：/pr modify（按原段落定位）；或 /pr edit <章节> <序号>\n"
                "5) 每次按提示回复“确认”只是加入当前草稿\n"
                "6) 全部改完后用 /pr submit 统一提交一个 PR；/pr cancel 取消"
            )
        )

    if text in {"/pr submit", "pr submit"}:
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))
        staged = (pending.base_toml or "").strip()
        if not staged:
            await matcher.finish("当前没有待提交草稿，请先用 /pr add、/pr addreview、/pr adddesc 或 /pr modify")

        await matcher.send(reply("正在进行内容合规审核..."))
        mod = await moderate_toml(staged)
        if not mod.approved:
            await matcher.finish(f"审核未通过：{mod.reason}")

        await matcher.send(reply("审核通过，正在提交并确保 PR..."))
        result = await ensure_pr(
            repo_name=(pending.repo_name or ""),
            course_code=(pending.course_code or ""),
            course_name=(pending.course_name or ""),
            repo_type=(pending.repo_type or ""),
            toml_text=staged,
        )
        if not result.ok:
            await matcher.finish(f"提交失败：{result.message}")
        _PENDING.pop(_key(event), None)
        await matcher.finish(_submit_result_text(result))

    # 命令：/pr cancel
    if text in {"/pr cancel", "pr cancel"}:
        _PENDING.pop(_key(event), None)
        await matcher.finish(reply("已取消本次 PR 提交流程"))

    # 命令：/pr start
    # 新版（简化）：
    # - /pr start <repo_name>
    # - /pr start <课程代码|全名|昵称>
    # 兼容老版：/pr start <repo_name> <course_code> <course_name...> <repo_type>
    if text.startswith("/pr start ") or text.startswith("pr start "):
        if not _allowed(event):
            await matcher.finish("你没有权限发起 PR（管理员未授权）")

        parts = text.split()
        args = parts[2:]
        if not args:
            await matcher.finish(
                reply(
                    "用法：\n"
                    "- /pr start <repo_name>\n"
                    "- /pr start <课程代码|全名|昵称>\n"
                    "- 兼容旧写法：/pr start <repo_name> <course_code> <course_name...> <repo_type>"
                )
            )

        repo_name = ""
        course_code = ""
        course_name = ""
        repo_type = ""

        # legacy: /pr start <repo_name> <course_code> <course_name...> <repo_type>
        if len(args) >= 4:
            repo_name = args[0].strip()
            course_code = args[1].strip()
            course_name = " ".join(args[2:-1]).strip()
            repo_type = args[-1].strip()
        else:
            key = args[0].strip()
            maybe_type = args[1].strip() if len(args) >= 2 else ""
            if maybe_type and not _is_repo_type(maybe_type):
                await matcher.finish(
                    "用法：/pr start <repo_name>\n"
                    "或：/pr start <课程代码|全名|昵称>（课程全名/昵称请不要带空格）"
                )

            # (A) 优先把 key 当 repo_name：从 prServer 自动补齐 code/name/type
            repo_name = key
            if maybe_type:
                repo_type = maybe_type

            s = await get_course_structure(repo_name=repo_name)
            if s.ok and s.data and isinstance(s.data.get("summary"), dict):
                cc, cn, rt = _extract_meta_from_summary(s.data.get("summary"))
                course_code = cc or course_code
                course_name = cn or course_name
                repo_type = repo_type or rt
            else:
                # (B) 再把 key 当“课程代码/全名/昵称”：从本地 course_manager 解析到 code/name
                course = course_manager.get_course_detail(key)
                if course:
                    schema = str(course.get("_schema") or "")
                    if schema == "multi-project-item":
                        parent = course.get("_parent") if isinstance(course, dict) else None
                        if not isinstance(parent, dict):
                            await matcher.finish(
                                "该课程属于 multi-project 仓库，但缺少父仓库信息，无法自动定位 repo_name。\n"
                                "请用完整写法：/pr start <repo_name> <course_code> <course_name...> multi-project"
                            )

                        # 父仓库的 course_code 通常就是 repo_name
                        p_code = str(parent.get("course_code") or "").strip()
                        p_name = str(parent.get("course_name") or "").strip()
                        p_type = str(parent.get("repo_type") or "").strip() or "multi-project"
                        repo_name = p_code or repo_name or key
                        course_code = p_code or course_code
                        course_name = p_name or course_name
                        repo_type = repo_type or p_type

                        # 记住选中的子课程，后续 /pr add 可省略子课程名
                        sub_name = str(course.get("course_name") or "").strip() or key
                        _PENDING[_key(event)] = Pending(
                            repo_name=repo_name,
                            course_code=course_code,
                            course_name=course_name,
                            repo_type=repo_type,
                            mode="idle",
                            target={"type": "multi-project-course", "course_name": sub_name},
                        )
                        await matcher.finish(
                            "已进入 PR 提交流程（multi-project）。\n"
                            f"当前选中子课程：{sub_name}\n"
                            "- 用 /pr show 查看当前仓库内容\n"
                            "- 用 /pr target 切换子课程\n"
                            "- 用 /pr add 或 /pr modify 编辑\n"
                            "- 编辑完成后按提示确认提交"
                        )

                    course_code = str(course.get("course_code") or "").strip()
                    course_name = str(course.get("course_name") or "").strip()
                    repo_type = repo_type or str(course.get("repo_type") or "").strip() or "normal"
                    if not repo_name:
                        repo_name = course_code
                    # 约定：大多数仓库名与 course_code 一致；若你的实际 repo_name 不一致，请用 /pr start <repo_name>
                    if repo_name == key:
                        repo_name = course_code or key

                if not course_code or not course_name:
                    _PENDING[_key(event)] = Pending(
                        repo_name="",
                        course_code="",
                        course_name="",
                        repo_type="normal",
                        mode="temp_repo_confirm",
                    )
                    await matcher.finish(
                        "查询不到现有仓库/课程。\n"
                        "是否建立临时仓库草稿？回复 y/n\n"
                        "确认后请按格式发送：<课程代码> <课程全名>"
                    )

        repo_name = (repo_name or "").strip()
        course_code = (course_code or "").strip()
        course_name = (course_name or "").strip()
        repo_type = (repo_type or "").strip() or "normal"

        if not repo_name:
            await matcher.finish("缺少 repo_name，请重试：/pr start <repo_name>")
        if not course_code or not course_name:
            await matcher.finish(
                "无法自动补齐 course_code/course_name。\n"
                "如果是新仓库或仓库名不等于课程代码，请用完整写法：\n"
                "/pr start <repo_name> <course_code> <course_name...> <repo_type>"
            )

        base_toml = None
        r_start = await get_course_toml(repo_name=repo_name)
        if r_start.ok and r_start.toml:
            base_toml = r_start.toml

        start_source = str(((r_start.data or {}) if r_start.ok and isinstance(r_start.data, dict) else {}).get("source") or "").strip()
        start_repo_meta = ((r_start.data or {}) if r_start.ok and isinstance(r_start.data, dict) else {}).get("repo")
        if start_source == "template" and not start_repo_meta:
            _PENDING[_key(event)] = Pending(
                repo_name=repo_name,
                course_code=course_code,
                course_name=course_name,
                repo_type="normal",
                mode="temp_repo_confirm",
            )
            await matcher.finish(
                "当前没有找到已存在的课程仓库。\n"
                f"已识别到课程信息：{course_code} / {course_name}\n"
                "是否按 normal 临时仓库草稿继续？回复 y/n"
            )

        _PENDING[_key(event)] = Pending(
            repo_name=repo_name,
            course_code=course_code,
            course_name=course_name,
            repo_type=repo_type,
            mode="idle",
            base_toml=base_toml,
        )

        if repo_type == "multi-project":
            # multi-project 必须先选子课程再操作
            await _prompt_pick_multi_course(
                matcher=matcher,
                event=event,
                repo_name=repo_name,
                hint="已进入 PR 提交流程（multi-project）。",
            )

        await matcher.finish(
            "已进入 PR 提交流程。\n"
            "- 用 /pr show 查看当前仓库内容\n"
            "- 用 /pr add 或 /pr modify 编辑\n"
            "- 编辑完成后按提示确认提交"
        )

    # 命令：/pr show（查看结构）
    if text in {"/pr show", "pr show", "/pr view", "pr view"}:
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))

        await matcher.send("正在拉取内容并合并转发展示...")
        repo_key = (pending.repo_name or pending.course_code or "").strip()
        if not repo_key:
            await matcher.finish("缺少仓库标识（repo_name/course_code），请重新 /pr start")

        staged_toml = (pending.base_toml or "").strip()
        if staged_toml:
            r = None
            toml_text_show = staged_toml
        else:
            r = await get_course_toml(repo_name=repo_key)
            if not r.ok or not r.toml:
                await matcher.finish(f"拉取失败：{r.message}")
            toml_text_show = r.toml

        # multi-project：必须选定子课程；show 只展示该子课程
        if (pending.repo_type or "").strip() == "multi-project":
            t = pending.target or {}
            if not (isinstance(t, dict) and str(t.get("type") or "") == "multi-project-course"):
                await _prompt_pick_multi_course(matcher=matcher, event=event, repo_name=repo_key)
            course_name = str((t or {}).get("course_name") or "").strip()
            if not course_name:
                await _prompt_pick_multi_course(matcher=matcher, event=event, repo_name=repo_key)

            nodes = _build_forward_nodes_for_multi_course(bot, toml_text_show, course_name)
            ok = await _send_forward(bot, event, nodes)
            if not ok:
                await matcher.finish("发送合并转发失败（可能风控/版本问题）。你可以改用直接粘贴整段 TOML 提交。")

            await matcher.finish(_format_multi_course_structure(toml_text=toml_text_show, course_name=course_name))

        try:
            nodes = build_forward_nodes_from_toml(bot, toml_text_show)
        except Exception as e:
            await matcher.finish(f"解析 TOML 失败：{e}")

        ok = await _send_forward(bot, event, nodes)
        if not ok:
            await matcher.finish("发送合并转发失败（可能风控/版本问题）。你可以改用直接粘贴整段 TOML 提交。")

        # Also provide a short summary for navigation
        if staged_toml:
            await matcher.finish("已展示当前会话草稿。继续用 /pr add、/pr addreview、/pr adddesc、/pr modify 编辑，最后 /pr submit 统一提交。")
        s = await get_course_structure(repo_name=repo_key)
        if s.ok and s.data and isinstance(s.data.get("summary"), dict):
            await matcher.finish(_format_structure(s.data["summary"]))
        await matcher.finish("已展示。你可以 /pr add 或 /pr modify 继续。")

    if text in {"/pr adddesc", "pr adddesc"}:
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))
        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="add_content",
            target={"type": "description"},
            base_toml=pending.base_toml,
        )
        await matcher.finish("将修改 description。请下一条消息发送完整的课程说明正文。")

    # 命令：/pr target <子课程名>（multi-project 选择子课程）
    if text.startswith("/pr target ") or text.startswith("pr target "):
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))
        if (pending.repo_type or "").strip() != "multi-project":
            await matcher.finish("该命令仅适用于 multi-project 仓库")

        raw_pick = text.split(" ", 2)[2].strip() if len(text.split(" ", 2)) >= 3 else ""
        repo_key = (pending.repo_name or pending.course_code or "").strip()
        if not repo_key:
            await matcher.finish("缺少仓库标识（repo_name/course_code），请重新 /pr start")
        if not raw_pick:
            await _prompt_pick_multi_course(matcher=matcher, event=event, repo_name=repo_key)

        toml_text_target = (pending.base_toml or "").strip()
        if not toml_text_target:
            r = await get_course_toml(repo_name=repo_key)
            if not r.ok or not r.toml:
                await matcher.finish(f"拉取失败：{r.message}")
            toml_text_target = r.toml
        picked_name = _pick_course_name(toml_text=toml_text_target, pick=raw_pick)
        if not picked_name:
            await _prompt_pick_multi_course(matcher=matcher, event=event, repo_name=repo_key, hint=f"未找到子课程：{raw_pick}")

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="idle",
            target={"type": "multi-project-course", "course_name": picked_name},
            base_toml=pending.base_toml,
        )
        await matcher.finish(f"已切换当前子课程：{picked_name}\n提示：/pr show 查看该子课程；/pr add 追加 sections；/pr addreview 追加教师评价")

    # 命令：/pr addcourse <子课程名>（multi-project 新增一门子课程）
    if text.startswith("/pr addcourse ") or text.startswith("pr addcourse "):
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))
        if (pending.repo_type or "").strip() != "multi-project":
            await matcher.finish("该命令仅适用于 multi-project 仓库")

        parts = text.split()
        if len(parts) < 3:
            await matcher.finish("用法：/pr addcourse <子课程名> [课程代码]")
        course_name = parts[2].strip() if len(parts) >= 3 else ""
        code = parts[3].strip() if len(parts) >= 4 else ""
        if not course_name:
            await matcher.finish("用法：/pr addcourse <子课程名> [课程代码]")

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="build_patch",
            target={"type": "append_course", "course_name": course_name, "code": code},
            want_attribution=False,
            new_paragraph="",
            base_toml=pending.base_toml,
        )
        await matcher.send("正在生成修改后的 TOML...")
        # fallthrough to build_patch below

    # 命令：/pr addreview
    # - multi-project：/pr addreview <子课程名> <教师名>
    # - normal：/pr addreview <教师名>
    if text.startswith("/pr addreview ") or text.startswith("pr addreview "):
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))

        parts = text.split()
        repo_type = (pending.repo_type or "").strip()
        if repo_type == "multi-project":
            # 允许省略子课程名：若已 /pr target，则 /pr addreview <教师名>
            course_name = ""
            teacher = ""
            if len(parts) >= 4:
                course_name = parts[2].strip()
                teacher = " ".join(parts[3:]).strip()
            elif len(parts) >= 3:
                t = pending.target or {}
                if isinstance(t, dict) and str(t.get("type") or "") == "multi-project-course":
                    course_name = str(t.get("course_name") or "").strip()
                    teacher = " ".join(parts[2:]).strip()

            if not course_name or not teacher:
                await matcher.finish("用法：/pr addreview <子课程名> <教师名>（或先 /pr target 后：/pr addreview <教师名>）")

            _PENDING[_key(event)] = Pending(
                repo_name=pending.repo_name,
                course_code=pending.course_code,
                course_name=pending.course_name,
                repo_type=pending.repo_type,
                mode="add_content",
                target={
                    "type": "append_course_teacher_review",
                    "course_name": course_name,
                    "teacher": teacher,
                },
                base_toml=pending.base_toml,
            )
            await matcher.finish(
                f"将向子课程《{course_name}》教师《{teacher}》追加一条评价。\n"
                "请下一条消息发送要添加的正文（不要带多余解释）。"
            )

        # normal
        if len(parts) < 3:
            await matcher.finish("用法：/pr addreview <教师名>")
        lecturer = " ".join(parts[2:]).strip()
        if not lecturer:
            await matcher.finish("用法：/pr addreview <教师名>")

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="add_content",
            target={"type": "append_lecturer_review", "lecturer": lecturer},
            base_toml=pending.base_toml,
        )
        await matcher.finish(
            f"将向教师《{lecturer}》追加一条评价。\n"
            "请下一条消息发送要添加的正文（不要带多余解释）。"
        )

    # 命令：/pr add <章节标题>
    if text.startswith("/pr add ") or text.startswith("pr add "):
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))

        # multi-project：支持
        # - /pr add <子课程名> <章节标题>
        # - /pr add <章节标题>（需先 /pr target 或 /pr start 已选中子课程）
        if (pending.repo_type or "").strip() == "multi-project":
            parts = text.split()
            args = parts[2:]
            if not args:
                t = pending.target or {}
                if isinstance(t, dict) and str(t.get("type") or "") == "multi-project-course":
                    _PENDING[_key(event)] = Pending(
                        repo_name=pending.repo_name,
                        course_code=pending.course_code,
                        course_name=pending.course_name,
                        repo_type=pending.repo_type,
                        mode="add_section",
                        target=t,
                        base_toml=pending.base_toml,
                    )
                    await matcher.finish(
                        f"当前子课程：{str(t.get('course_name') or '').strip()}\n"
                        "请发送要追加到的章节标题（已有标题或新建标题均可）。"
                    )
                await matcher.finish(
                    "multi-project 需要先指定子课程：\n"
                    "- /pr target <子课程名或序号>\n"
                    "- 然后 /pr add 进入交互，或 /pr add <章节标题>\n"
                    "- 也可直接：/pr add <子课程名> <章节标题>"
                )

            course_name = ""
            section_title = ""
            if len(args) >= 2:
                course_name = args[0].strip()
                section_title = " ".join(args[1:]).strip()
            else:
                # len(args) == 1
                t = pending.target or {}
                if isinstance(t, dict) and str(t.get("type") or "") == "multi-project-course":
                    course_name = str(t.get("course_name") or "").strip()
                    section_title = " ".join(args).strip()
                else:
                    await matcher.finish(
                        "multi-project 需要先指定子课程：\n"
                        "- /pr add <子课程名> <章节标题>\n"
                        "- 或 /pr target <子课程名> 后再 /pr add <章节标题>"
                    )

            if not course_name or not section_title:
                await matcher.finish(
                    "用法：\n"
                    "- /pr add <子课程名> <章节标题>\n"
                    "- 或先 /pr target <子课程名>，再 /pr add <章节标题>"
                )

            _PENDING[_key(event)] = Pending(
                repo_name=pending.repo_name,
                course_code=pending.course_code,
                course_name=pending.course_name,
                repo_type=pending.repo_type,
                mode="add_content",
                target={
                    "type": "append_course_section_item",
                    "course_name": course_name,
                    "section": section_title,
                },
                base_toml=pending.base_toml,
            )
            await matcher.finish(
                f"将向子课程《{course_name}》章节《{section_title}》追加一条内容。\n"
                "请下一条消息发送要添加的正文（不要带多余解释）。"
            )

        section_title = text.split(" ", 2)[2].strip() if len(text.split(" ", 2)) >= 3 else ""
        if section_title:
            _PENDING[_key(event)] = Pending(
                repo_name=pending.repo_name,
                course_code=pending.course_code,
                course_name=pending.course_name,
                repo_type=pending.repo_type,
                mode="add_content",
                section_title=section_title,
                base_toml=pending.base_toml,
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
            section_title=pending.section_title or "",
            item_index=pending.item_index or -1,
            old_paragraph=pending.old_paragraph or "",
            new_paragraph=pending.new_paragraph or "",
            target=pending.target or None,
            base_toml=pending.base_toml or None,
        )
        await matcher.finish("请发送要追加到的章节标题（已有标题或新建标题均可）。")

    # 命令：/pr modify（按原段落定位修改）
    if text in {"/pr modify", "pr modify", "/pr mod", "pr mod"}:
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="modify_old",
            base_toml=pending.base_toml,
        )
        await matcher.finish(
            "请下一条消息粘贴你要修改的“原段落”（尽量原样复制，越长越好，便于定位）。\n"
            "提示：支持 description、sections/items、lecturers.reviews，以及 multi-project 的子课程段落（sections/items、teachers.reviews）。"
        )

    # 命令：/pr edit <章节标题> <序号>（保留：按序号修改）
    if text.startswith("/pr edit ") or text.startswith("pr edit "):
        pending = _PENDING.get(_key(event))
        if not pending:
            await matcher.finish(reply("请先 /pr start 进入流程"))

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
            base_toml=pending.base_toml,
        )
        await matcher.finish(
            f"将修改章节《{section_title}》的第 {idx1} 条内容（按序号）。\n"
            "请下一条消息发送修改后的完整正文（不要带多余解释）。"
        )

    # 如果处于 pending，则把这条消息当 TOML 或正文
    pending = _PENDING.get(_key(event))
    if not pending:
        return

    default_author_name = _author_name(event)

    if getattr(pending, "mode", None) == "temp_repo_confirm":
        ans = text.strip().lower()
        if ans in {"y", "yes", "是", "要", "确认"}:
            course_code = (pending.course_code or "").strip()
            course_name = (pending.course_name or "").strip()
            _PENDING[_key(event)] = Pending(
                repo_name=(pending.repo_name or course_code).strip(),
                course_code=course_code,
                course_name=course_name,
                repo_type="normal",
                mode="temp_repo_meta",
            )
            if course_code:
                await matcher.finish(
                    f"请补全课程信息。\n已识别课程代码：{course_code}\n"
                    "请发送：<课程代码> <课程全名>\n"
                    "也可以只发送课程全名，我会沿用上面的课程代码。"
                )
            await matcher.finish("请按格式发送：<课程代码> <课程全名>\n例如：CS1001 程序设计")
        if ans in {"n", "no", "否", "不要", "取消"}:
            _PENDING.pop(_key(event), None)
            await matcher.finish("已取消临时仓库创建")
        await matcher.finish("请回复 y 或 n")

    if getattr(pending, "mode", None) == "temp_repo_meta":
        parts = text.strip().split()
        if len(parts) < 2 and not (len(parts) == 1 and (pending.course_code or "").strip()):
            await matcher.finish("格式不对，请发送：<课程代码> <课程全名>；如果上一步已识别课程代码，也可以只发送课程全名")
        repo_type = "normal"
        if len(parts) == 1 and (pending.course_code or "").strip():
            course_code = (pending.course_code or "").strip()
            course_name = parts[0].strip()
        else:
            course_code = parts[0].strip()
            course_name = " ".join(parts[1:]).strip()
        if not course_code or not course_name:
            await matcher.finish("课程代码和课程全名都不能为空，请重试")
        base_toml = _normal_template(course_name=course_name, course_code=course_code)
        _PENDING[_key(event)] = Pending(
            repo_name=(pending.repo_name or course_code).strip() or course_code,
            course_code=course_code,
            course_name=course_name,
            repo_type=repo_type,
            mode="idle",
            base_toml=base_toml,
        )
        await matcher.finish(_normal_draft_ready_text(course_code=course_code, course_name=course_name))

    # full TOML flow
    if getattr(pending, "mode", None) == "full_toml":
        toml_text = text
        if not toml_text or len(toml_text) < 20:
            await matcher.finish("TOML 内容太短，请重新粘贴完整 readme.toml")

        await matcher.send(reply("正在进行内容合规审核..."))
        mod = await moderate_toml(toml_text)
        if not mod.approved:
            _PENDING.pop(_key(event), None)
            await matcher.finish(f"审核未通过：{mod.reason}")

        await matcher.send(reply("审核通过，正在提交并确保 PR..."))
        repo_name: str | None = (pending.repo_name or "").strip() or (pending.course_code or "").strip() or None
        course_code = (pending.course_code or "").strip()
        course_name = (pending.course_name or "").strip()
        repo_type = (pending.repo_type or "").strip()

        if not course_code or not course_name or not repo_type:
            await matcher.finish("缺少必要的仓库或课程信息，无法继续。")

        r = await ensure_pr(
            repo_name=repo_name,
            course_code=course_code,
            course_name=course_name,
            repo_type=repo_type,
            toml_text=toml_text,
        )
        _PENDING.pop(_key(event), None)
        if not r.ok:
            await matcher.finish(f"提交失败：{r.message}")
        await matcher.finish(_submit_result_text(r))

    # collect section title for add
    if getattr(pending, "mode", None) == "add_section":
        section_title = text.strip()
        if not section_title:
            await matcher.finish("章节标题不能为空，请重新发送")
        # multi-project：把“章节标题”转成 append_course_section_item target
        if (pending.repo_type or "").strip() == "multi-project":
            t = pending.target or {}
            if not (isinstance(t, dict) and str(t.get("type") or "") == "multi-project-course"):
                await matcher.finish("multi-project 请先 /pr target 选中子课程")
            cname = str(t.get("course_name") or "").strip()
            if not cname:
                await matcher.finish("multi-project 请先 /pr target 选中子课程")
            _PENDING[_key(event)] = Pending(
                repo_name=pending.repo_name,
                course_code=pending.course_code,
                course_name=pending.course_name,
                repo_type=pending.repo_type,
                mode="add_content",
                target={"type": "append_course_section_item", "course_name": cname, "section": section_title},
                base_toml=pending.base_toml,
            )
            await matcher.finish(
                reply(
                    f"将向子课程《{cname}》章节《{section_title}》追加一条内容。\n"
                    "请下一条消息发送正文（不要带多余解释）。"
                )
            )

        _PENDING[_key(event)] = Pending(
            repo_name=pending.repo_name,
            course_code=pending.course_code,
            course_name=pending.course_name,
            repo_type=pending.repo_type,
            mode="add_content",
            section_title=section_title,
            base_toml=pending.base_toml,
        )
        await matcher.finish(reply(f"将向章节《{section_title}》追加一条内容。请下一条消息发送正文。"))

    # modify: receive old paragraph
    if getattr(pending, "mode", None) == "modify_old":
        old = text.strip()
        if len(old) < 10:
            await matcher.finish("原段落太短，建议复制更长一点的原文再试")

        await matcher.send("正在从仓库 TOML 中定位该段落...")
        repo_key2 = (pending.repo_name or pending.course_code or "").strip()
        if not repo_key2:
            _PENDING.pop(_key(event), None)
            await matcher.finish("缺少仓库标识（repo_name/course_code），请重新 /pr start")

        toml_text_modify = (pending.base_toml or "").strip()
        if not toml_text_modify:
            r = await get_course_toml(repo_name=repo_key2)
            if not r.ok or not r.toml:
                _PENDING.pop(_key(event), None)
                await matcher.finish(f"拉取 TOML 失败：{r.message}")
            toml_text_modify = r.toml

        candidates = _find_paragraph_candidates(toml_text_modify, old)
        # multi-project：只允许修改“当前选中子课程”的条目
        if (pending.repo_type or "").strip() == "multi-project":
            t = pending.target or {}
            cname = ""
            if isinstance(t, dict) and str(t.get("type") or "") == "multi-project-course":
                cname = str(t.get("course_name") or "").strip()
            if not cname:
                await _prompt_pick_multi_course(matcher=matcher, event=event, repo_name=repo_key2)
            candidates = [
                c
                for c in candidates
                if str(c.get("type") or "") in {"course_section_item", "course_teacher_review"}
                and str(c.get("course_name") or "").strip() == cname
            ]
        if not candidates:
            await matcher.finish(
                "未定位到匹配条目（multi-project 只会在当前选中子课程内查找）。\n"
                "- 请确认复制的是该子课程的原文\n"
                "- 或用 /pr target 切换子课程后重试"
            )

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
                base_toml=toml_text_modify,
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
            await matcher.finish(reply("已定位到目标。请下一条消息发送修改后的完整正文。"))

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
            base_toml=toml_text_modify,
        )
        await matcher.finish("\n".join(lines))

    if getattr(pending, "mode", None) == "modify_choose":
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
            await matcher.finish(reply("已选择：description\n请下一条消息发送修改后的完整正文。"))
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
        await matcher.finish(reply("已选择目标。请下一条消息发送修改后的完整正文。"))

    if getattr(pending, "mode", None) == "modify_new":
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
    if getattr(pending, "mode", None) in {"add_content", "edit_content"}:
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
            target=pending.target,
            base_toml=pending.base_toml,
        )
        await matcher.finish("是否在该条目 author 中留名？回复 y/n")

    if getattr(pending, "mode", None) == "attrib_ask":
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
            await matcher.finish(reply(f"请输入显示名字（直接回车或只 @我 不带文字则用：{default_author_name}）"))
        elif ans in {"n", "no", "否", "不要", "不留"}:
            pending = Pending(
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
            _PENDING[_key(event)] = pending
            await matcher.send("好的，不留名。")
            # fallthrough to build_patch below
        else:
            await matcher.finish(reply("请回复 y 或 n"))

    if getattr(pending, "mode", None) == "attrib_name":
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
        await matcher.finish(reply("可选：请输入你的主页链接（GitHub/博客等），留空（或只 @我 不带文字）则不填"))

    if getattr(pending, "mode", None) == "attrib_link":
        link = text.strip()
        pending = Pending(
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
        _PENDING[_key(event)] = pending
        await matcher.send("收到。")
        # fallthrough to build_patch below

    if getattr(pending, "mode", None) == "build_patch":
        author = None
        if getattr(pending, "want_attribution", False):
            author = {
                "name": getattr(pending, "author_name", default_author_name),
                "link": getattr(pending, "author_link", ""),
                "date": _year_month(),
            }

        base_toml = (getattr(pending, "base_toml", "") or "").strip()
        if not base_toml:
            repo_key = (getattr(pending, "repo_name", "") or getattr(pending, "course_code", "") or "").strip()
            if repo_key:
                r0 = await get_course_toml(repo_name=repo_key)
                if r0.ok and r0.toml:
                    base_toml = r0.toml
        if not base_toml:
            course_code = (getattr(pending, "course_code", "") or "").strip()
            course_name = (getattr(pending, "course_name", "") or course_code).strip()
            repo_type = (getattr(pending, "repo_type", "") or "normal").strip()
            base_toml = _multiproject_template(course_name=course_name, course_code=course_code) if repo_type == "multi-project" else _normal_template(course_name=course_name, course_code=course_code)

        # append operations are patched locally (prServer submit_ops 不支持这些追加类操作)
        ttype0 = str(((pending.target or {}) if isinstance(pending.target, dict) else {}).get("type") or "")
        if ttype0 in {"append_course", "append_course_section_item", "append_course_teacher_review", "append_lecturer_review", "description"}:
            try:
                if ttype0 == "description":
                    patched_toml = _set_description_local(base_toml, content=getattr(pending, "new_paragraph", "") or "")
                elif ttype0 == "append_lecturer_review":
                    patched_toml = _append_normal_lecturer_review(
                        base_toml,
                        lecturer=str((getattr(pending, "target", {}) or {}).get("lecturer") or "").strip(),
                        content=getattr(pending, "new_paragraph", "") or "",
                        author=author,
                    )
                else:
                    patched_toml = _append_toml_by_target(
                        base_toml,
                        target=getattr(pending, "target", {}) or {},
                        content=getattr(pending, "new_paragraph", "") or "",
                        author=author,
                    )
            except Exception as e:
                _PENDING.pop(_key(event), None)
                await matcher.finish(f"生成失败：{e}")

            new_preview = (getattr(pending, "new_paragraph", "") or "").strip()
            if new_preview and len(new_preview) > 200:
                new_preview = new_preview[:199] + "…"

            _PENDING[_key(event)] = Pending(
                repo_name=getattr(pending, "repo_name", ""),
                course_code=getattr(pending, "course_code", ""),
                course_name=getattr(pending, "course_name", ""),
                repo_type=getattr(pending, "repo_type", ""),
                mode="confirm",
                new_paragraph=getattr(pending, "new_paragraph", ""),
                want_attribution=getattr(pending, "want_attribution", False),
                author_name=getattr(pending, "author_name", ""),
                author_link=getattr(pending, "author_link", ""),
                patched_toml=patched_toml,
                base_toml=base_toml,
                target=getattr(pending, "target", {}) or {},
            )

            msg = ["即将加入当前草稿".strip()]
            if new_preview:
                msg.append(f"\n新增内容（截断）：\n{new_preview}")
            msg.append("\n回复：确认 / 取消")
            await matcher.finish(reply("\n".join(msg)))

        if getattr(pending, "old_paragraph", None) and getattr(pending, "target", None):
            ttype = str((pending.target or {}).get("type") or "")
            if ttype == "section_item":
                try:
                    patched_toml = _update_normal_section_item(
                        base_toml,
                        section_title=getattr(pending, "section_title", ""),
                        item_index=getattr(pending, "item_index", -1),
                        content=getattr(pending, "new_paragraph", ""),
                        author=author,
                    )
                except Exception as e:
                    _PENDING.pop(_key(event), None)
                    await matcher.finish(f"生成失败：{e}")
            else:
                # local patch for targets not supported by submit_ops
                if not getattr(pending, "base_toml", None):
                    _PENDING.pop(_key(event), None)
                    await matcher.finish("状态异常：缺少 base TOML，请重新 /pr modify")
                await matcher.send("正在生成修改后的 TOML...")
                try:
                    patched_toml = _patch_toml_by_target(
                        getattr(pending, "base_toml", ""),
                        target=getattr(pending, "target", {}),
                        old_paragraph=getattr(pending, "old_paragraph", ""),
                        new_paragraph=getattr(pending, "new_paragraph", ""),
                        author=author,
                    )
                except Exception as e:
                    await matcher.finish(f"生成失败：{e}")

                _PENDING[_key(event)] = Pending(
                    repo_name=getattr(pending, "repo_name", ""),
                    course_code=getattr(pending, "course_code", ""),
                    course_name=getattr(pending, "course_name", ""),
                    repo_type=getattr(pending, "repo_type", ""),
                    mode="confirm",
                    section_title=getattr(pending, "section_title", ""),
                    item_index=getattr(pending, "item_index", -1),
                    old_paragraph=getattr(pending, "old_paragraph", ""),
                    new_paragraph=getattr(pending, "new_paragraph", ""),
                    target=getattr(pending, "target", {}),
                    base_toml=base_toml,
                    want_attribution=getattr(pending, "want_attribution", False),
                    author_name=getattr(pending, "author_name", ""),
                    author_link=getattr(pending, "author_link", ""),
                    patched_toml=patched_toml,
                )

                old_preview = (getattr(pending, "old_paragraph", "") or "").strip()
                if old_preview and len(old_preview) > 200:
                    old_preview = old_preview[:199] + "…"
                new_preview = (getattr(pending, "new_paragraph", "") or "").strip()
                if new_preview and len(new_preview) > 200:
                    new_preview = new_preview[:199] + "…"

                msg = ["即将加入当前草稿：定位修改".strip()]
                if old_preview:
                    msg.append(f"\n原段落（截断）：\n{old_preview}")
                msg.append(f"\n新段落（截断）：\n{new_preview}")
                msg.append("\n回复：确认 / 取消")
                await matcher.finish(reply("\n".join(msg)))
        elif getattr(pending, "item_index", -1) >= 0:
            try:
                patched_toml = _update_normal_section_item(
                    base_toml,
                    section_title=getattr(pending, "section_title", ""),
                    item_index=getattr(pending, "item_index", -1),
                    content=getattr(pending, "new_paragraph", ""),
                    author=author,
                )
            except Exception as e:
                _PENDING.pop(_key(event), None)
                await matcher.finish(f"生成失败：{e}")
        else:
            try:
                patched_toml = _append_normal_section_item(
                    base_toml,
                    section_title=getattr(pending, "section_title", ""),
                    content=getattr(pending, "new_paragraph", ""),
                    author=author,
                )
            except Exception as e:
                _PENDING.pop(_key(event), None)
                await matcher.finish(f"生成失败：{e}")

        info = ""
        if getattr(pending, "section_title", ""):
            info = f"章节《{getattr(pending, "section_title", "")}》"
        if getattr(pending, "item_index", -1) >= 0:
            info += f" 第 {getattr(pending, "item_index", -1)+1} 条"
        old_preview = (getattr(pending, "old_paragraph", "") or "").strip()
        if old_preview and len(old_preview) > 200:
            old_preview = old_preview[:199] + "…"
        new_preview = (getattr(pending, "new_paragraph", "") or "").strip()
        if new_preview and len(new_preview) > 200:
            new_preview = new_preview[:199] + "…"

        _PENDING[_key(event)] = Pending(
            repo_name=getattr(pending, "repo_name", ""),
            course_code=getattr(pending, "course_code", ""),
            course_name=getattr(pending, "course_name", ""),
            repo_type=getattr(pending, "repo_type", ""),
            mode="confirm",
            section_title=getattr(pending, "section_title", ""),
            item_index=getattr(pending, "item_index", -1),
            old_paragraph=getattr(pending, "old_paragraph", ""),
            new_paragraph=getattr(pending, "new_paragraph", ""),
            want_attribution=getattr(pending, "want_attribution", False),
            author_name=getattr(pending, "author_name", ""),
            author_link=getattr(pending, "author_link", ""),
            patched_toml=patched_toml,
            target=pending.target,
            base_toml=base_toml,
        )

        msg = [f"即将加入当前草稿：{info}".strip()]
        if old_preview:
            msg.append(f"\n原段落（截断）：\n{old_preview}")
        msg.append(f"\n新段落（截断）：\n{new_preview}")
        msg.append("\n回复：确认 / 取消")
        await matcher.finish(reply("\n".join(msg)))

    if getattr(pending, "mode", None) == "confirm":
        ans2 = text.strip().lower()
        if ans2 in {"取消", "cancel", "c", "n", "no"}:
            _PENDING[_key(event)] = Pending(
                repo_name=getattr(pending, "repo_name", ""),
                course_code=getattr(pending, "course_code", ""),
                course_name=getattr(pending, "course_name", ""),
                repo_type=getattr(pending, "repo_type", ""),
                mode="idle",
                base_toml=getattr(pending, "base_toml", ""),
                target=getattr(pending, "target", None),
            )
            await matcher.finish("已取消当前这一步修改，现有草稿已保留。")
        if ans2 not in {"确认", "confirm", "y", "yes", "是"}:
            await matcher.finish(reply("请回复：确认 或 取消"))

        if not getattr(pending, "patched_toml", None):
            _PENDING.pop(_key(event), None)
            await matcher.finish("状态异常：缺少 patched TOML，请重新开始")

        _PENDING[_key(event)] = Pending(
            repo_name=getattr(pending, "repo_name", ""),
            course_code=getattr(pending, "course_code", ""),
            course_name=getattr(pending, "course_name", ""),
            repo_type=getattr(pending, "repo_type", ""),
            mode="idle",
            base_toml=getattr(pending, "patched_toml", ""),
            target=getattr(pending, "target", None),
        )
        await matcher.finish("已加入当前草稿。继续用 /pr add、/pr addreview、/pr adddesc、/pr modify 编辑；全部完成后用 /pr submit 统一提交。")

    # unknown mode
    _PENDING.pop(_key(event), None)
    await matcher.finish("状态异常：已重置会话，请重新 /pr start")

    await matcher.finish(_submit_result_text(result))
