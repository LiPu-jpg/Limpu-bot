"""PR 提交流程（简化版）：对话式攒结构化 ops → 预览 → 审核 → 提交。

指令一览（群里需 @bot）：
  /pr                        帮助
  /pr start <课程代码|昵称>   开始会话
  /pr add <章节标题>          往章节追加内容（下一条消息为正文）
  /pr review <教师名>         添加教师评价（下一条消息为正文）
  /pr list                   查看当前待提交的操作
  /pr undo                   撤销上一个操作
  /pr sign <名字> [链接]      署名（可选）
  /pr preview                预览合并后的文档与警告
  /pr submit                 合规审核 → 确认 → 提交 PR
  /pr cancel                 放弃当前会话

多轮交互实现：/pr add 等只记录 pending 状态，下一条普通消息由
本模块的低优先级监听器消费，避免依赖 Matcher 的 got/pause 魔法。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from limpu_core import nicknames, courseserver
from limpu_core.llm import moderate
from limpu_core.settings import get_settings

PENDING_TTL = 120  # 等待下一条消息的超时（秒）

pr_cmd = on_command("pr", priority=10, block=True)
follow_up = on_message(priority=99, block=False)


@dataclass
class Pending:
    kind: str  # "add" | "review" | "confirm"
    meta: str = ""  # add=章节标题, review=教师名
    ts: float = field(default_factory=time.time)


@dataclass
class Session:
    course_code: str
    course_name: str
    ops: list[dict] = field(default_factory=list)
    author: dict | None = None
    pending: Pending | None = None
    ts: float = field(default_factory=time.time)


_sessions: dict[tuple[int, int], Session] = {}


def _key(event: MessageEvent) -> tuple[int, int]:
    return (getattr(event, "group_id", 0) or 0, event.user_id)


def _get_session(event: MessageEvent) -> Session | None:
    sess = _sessions.get(_key(event))
    if sess and time.time() - sess.ts < get_settings().session_ttl:
        sess.ts = time.time()
        return sess
    _sessions.pop(_key(event), None)
    return None


HELP = """PR 提交流程（向课程仓库贡献内容）：
1. /pr start <课程代码|昵称> —— 开始
2. /pr add <章节标题> —— 追加章节内容
   /pr review <教师名> —— 添加教师评价
3. /pr list / /pr undo —— 查看 / 撤销
4. /pr sign <名字> [链接] —— 署名（可选）
5. /pr preview —— 预览效果
6. /pr submit —— 审核并提交
/pr cancel —— 放弃"""


@pr_cmd.handle()
async def handle_pr(event: MessageEvent, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    sub, _, rest = text.partition(" ")
    sub, rest = sub.strip(), rest.strip()

    if not sub or sub == "help":
        await pr_cmd.finish(HELP)

    handler = {
        "start": _do_start,
        "add": _do_add,
        "review": _do_review,
        "list": _do_list,
        "undo": _do_undo,
        "sign": _do_sign,
        "preview": _do_preview,
        "show": _do_preview,
        "submit": _do_submit,
        "cancel": _do_cancel,
    }.get(sub)

    if handler is None:
        await pr_cmd.finish(f"未知子指令「{sub}」\n\n{HELP}")

    if not get_settings().pr_allowed(event.user_id):
        await pr_cmd.finish("你不在 PR 提交白名单中")
    await handler(event, rest)  # type: ignore[misc]


# ---------------- 子指令 ----------------


async def _do_start(event: MessageEvent, rest: str):
    if not rest:
        await pr_cmd.finish("用法：/pr start <课程代码|昵称>\n例：/pr start COMP2030")
    code = nicknames.get(rest).upper()
    try:
        detail = await courseserver.read_course(code)
    except courseserver.CourseServerError as e:
        await pr_cmd.finish(f"找不到课程：{e}")

    name = ""
    try:
        hits = await courseserver.search_courses(code)
        for h in hits:
            if h.code == code:
                name = h.name
                break
    except courseserver.CourseServerError:
        pass

    _sessions[_key(event)] = Session(course_code=code, course_name=name)
    await pr_cmd.finish(
        f"已开始 PR 会话：{name or code}（{code}，{detail.repo_type}）\n"
        "接下来：\n/pr add <章节标题> —— 追加章节内容\n/pr review <教师名> —— 添加教师评价\n/pr cancel —— 放弃"
    )


async def _do_add(event: MessageEvent, rest: str):
    sess = _get_session(event)
    if not sess:
        await pr_cmd.finish("请先 /pr start <课程代码>")
    if not rest:
        await pr_cmd.finish("用法：/pr add <章节标题>，然后按提示发送正文")
    sess.pending = Pending(kind="add", meta=rest)
    await pr_cmd.finish(f"请直接发送要追加到「{rest}」的正文（{PENDING_TTL} 秒内有效）")


async def _do_review(event: MessageEvent, rest: str):
    sess = _get_session(event)
    if not sess:
        await pr_cmd.finish("请先 /pr start <课程代码>")
    if not rest:
        await pr_cmd.finish("用法：/pr review <教师名>，然后按提示发送评价正文")
    sess.pending = Pending(kind="review", meta=rest)
    await pr_cmd.finish(f"请直接发送对「{rest}」老师的评价（{PENDING_TTL} 秒内有效）")


async def _do_list(event: MessageEvent, _rest: str):
    sess = _get_session(event)
    if not sess:
        await pr_cmd.finish("当前没有进行中的 PR 会话")
    if not sess.ops:
        await pr_cmd.finish("还没有记录任何操作，用 /pr add 或 /pr review 添加")

    lines = [f"待提交操作（{len(sess.ops)} 项）—— {sess.course_name or sess.course_code}："]
    for i, op in enumerate(sess.ops, 1):
        label = op.get("title") or op.get("lecturer_name") or op["op"]
        kind = "章节" if op["op"] == "add_section_item" else "评价"
        preview = op["content"][:60] + ("…" if len(op["content"]) > 60 else "")
        lines.append(f"{i}. [{kind}] {label}：{preview}")
    if sess.author:
        lines.append(f"署名：{sess.author.get('name', '')} {sess.author.get('link', '')}")
    await pr_cmd.finish("\n".join(lines))


async def _do_undo(event: MessageEvent, _rest: str):
    sess = _get_session(event)
    if not sess or not sess.ops:
        await pr_cmd.finish("没有可撤销的操作")
    op = sess.ops.pop()
    label = op.get("title") or op.get("lecturer_name") or op["op"]
    await pr_cmd.finish(f"已撤销：{label}（剩余 {len(sess.ops)} 项）")


async def _do_sign(event: MessageEvent, rest: str):
    sess = _get_session(event)
    if not sess:
        await pr_cmd.finish("请先 /pr start <课程代码>")
    parts = rest.split()
    if not parts:
        await pr_cmd.finish("用法：/pr sign <名字> [主页链接]\n不署名可跳过此步")
    sess.author = {"name": parts[0], "date": time.strftime("%Y-%m")}
    if len(parts) > 1:
        sess.author["link"] = parts[1]
    await pr_cmd.finish(f"已署名：{parts[0]}（{sess.author['date']}）")


def _ops_with_author(sess: Session) -> list[dict]:
    if not sess.author:
        return [dict(op) for op in sess.ops]
    return [{**op, "author": sess.author} for op in sess.ops]


async def _do_preview(event: MessageEvent, _rest: str):
    sess = _get_session(event)
    if not sess or not sess.ops:
        await pr_cmd.finish("没有可预览的内容（先 /pr add 或 /pr review）")

    await pr_cmd.send("正在生成预览…")
    try:
        result = await courseserver.preview_ops(sess.course_code, _ops_with_author(sess), sess.course_name)
    except courseserver.CourseServerError as e:
        await pr_cmd.finish(f"预览失败：{e}")

    lines = ["预览生成成功：", f"变更文件：{', '.join(result.changed_files) or '无'}"]
    if result.warnings:
        lines.append("警告：")
        lines.extend(f"- {w}" for w in result.warnings[:5])
    else:
        lines.append("无警告")
    if result.readme_md:
        md = result.readme_md
        lines.append(f"\n合并后文档前 500 字：\n{md[:500]}{'…' if len(md) > 500 else ''}")
    await pr_cmd.finish("\n".join(lines))


async def _do_submit(event: MessageEvent, _rest: str):
    sess = _get_session(event)
    if not sess or not sess.ops:
        await pr_cmd.finish("没有可提交的内容")

    await pr_cmd.send("正在进行内容合规审核…")
    for i, op in enumerate(sess.ops, 1):
        ok, reason = await moderate(op.get("content", ""))
        if not ok:
            await pr_cmd.finish(
                f"第 {i} 条内容未通过审核：{reason}\n可用 /pr undo 撤销后重新添加"
            )

    sess.pending = Pending(kind="confirm")
    await pr_cmd.finish(
        f"审核通过。将向 {sess.course_name or sess.course_code}（{sess.course_code}）提交 {len(sess.ops)} 项变更。\n"
        f"回复「确认」提交，回复其他内容取消（{PENDING_TTL} 秒内有效）"
    )


async def _do_cancel(event: MessageEvent, _rest: str):
    if _sessions.pop(_key(event), None):
        await pr_cmd.finish("已放弃当前 PR 会话")
    await pr_cmd.finish("当前没有进行中的 PR 会话")


# ---------------- 多轮交互监听器 ----------------


@follow_up.handle()
async def handle_follow_up(event: MessageEvent):
    # 指令消息不拦截（已由各 command matcher 处理）
    text = event.message.extract_plain_text().strip()
    if text.startswith("/"):
        return

    sess = _get_session(event)
    if not sess or not sess.pending:
        return
    if time.time() - sess.pending.ts > PENDING_TTL:
        sess.pending = None
        return

    pending, sess.pending = sess.pending, None

    if pending.kind == "add":
        if not text:
            await follow_up.finish("正文为空，本次追加已取消")
        sess.ops.append({"op": "add_section_item", "title": pending.meta, "content": text})
        await follow_up.finish(f"已记录（共 {len(sess.ops)} 项）。/pr list 查看，/pr submit 提交")

    if pending.kind == "review":
        if not text:
            await follow_up.finish("正文为空，本次评价已取消")
        sess.ops.append({"op": "add_lecturer_review", "lecturer_name": pending.meta, "content": text})
        await follow_up.finish(f"已记录（共 {len(sess.ops)} 项）。/pr list 查看，/pr submit 提交")

    if pending.kind == "confirm":
        if text != "确认":
            await follow_up.finish("已取消提交（会话保留，可继续编辑或 /pr cancel 放弃）")
        key_src = json.dumps(_ops_with_author(sess), ensure_ascii=False, sort_keys=True)
        idem = f"qq-{event.user_id}-{sess.course_code}-{hashlib.sha1(key_src.encode()).hexdigest()[:12]}"
        try:
            result = await courseserver.submit_ops(sess.course_code, _ops_with_author(sess), idem, sess.course_name)
        except courseserver.CourseServerError as e:
            await follow_up.finish(f"提交失败：{e}\n（会话保留，可重试 /pr submit）")
        _sessions.pop(_key(event), None)
        await follow_up.finish(f"提交成功！PR：{result.pr_url}" if result.pr_url else "提交成功！")
