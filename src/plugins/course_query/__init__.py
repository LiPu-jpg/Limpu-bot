"""课程查询：/查 —— 先搜索，单结果自动显详情，多结果列出候选。

别名 /搜 仍可用。
"""

from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message

from limpu_core import nicknames, courseserver
from limpu_core.qq import send_long_group

# /查 为主指令，/搜 为别名（行为完全一致）
info_cmd = on_command("查", aliases={"info", "搜", "search"}, priority=10, block=True)
nick_cmd = on_command("设置昵称", priority=10, block=True)


@info_cmd.handle()
async def handle_info(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    query = args.extract_plain_text().strip()
    if not query:
        await info_cmd.finish("用法：/查 <关键词 | 代码 | 昵称>")

    # 1) 昵称解析
    code = nicknames.get(query)

    # 2) 尝试直接按代码读
    try:
        detail = await courseserver.read_course(code)
    except courseserver.CourseServerError:
        detail = None

    if detail and not _is_bootstrap(detail.readme_md):
        await _show_detail(bot, event, code, detail)
        await info_cmd.finish()

    # 3) 直接读失败或空壳 → 搜索
    try:
        results = await courseserver.search_courses(query)
    except courseserver.CourseServerError:
        await info_cmd.finish(f"找不到与「{query}」相关的课程")

    if not results:
        await info_cmd.finish(f"找不到与「{query}」相关的课程")

    # 4) 搜索结果唯一 → 直接显详情
    if len(results) == 1:
        c = results[0]
        try:
            detail = await courseserver.read_course(c.code)
        except courseserver.CourseServerError:
            await _show_search_list(query, results)
            await info_cmd.finish()
        await _show_detail(bot, event, c.code, detail)
        await info_cmd.finish()

    # 5) 多个结果 → 列出
    await _show_search_list(query, results)


def _is_bootstrap(readme_md: str) -> bool:
    stripped = (readme_md or "").strip()
    return "Auto bootstrap" in stripped or len(stripped) < 400 or "## 授课教师" not in stripped


async def _show_detail(bot: Bot, event: MessageEvent, code: str, detail) -> None:
    header = f"{detail.repo}（{detail.repo_type}）\n\n"
    text = header + (detail.readme_md or "（暂无内容）")
    if isinstance(event, GroupMessageEvent):
        await send_long_group(bot, event, text)
    else:
        await info_cmd.send(text)


async def _show_search_list(query: str, results) -> None:
    lines = [f"「{query}」匹配到 {len(results)} 门课程："]
    for i, c in enumerate(results[:10], 1):
        teachers = "、".join(c.teachers[:3]) if c.teachers else "暂无"
        lines.append(f"{i}. {c.name}（{c.code}）\n   教师：{teachers}")
    if len(results) > 10:
        lines.append(f"…共 {len(results)} 条，仅显示前 10 条")
    lines.append("\n用 /查 <课程代码> 查看详情")
    await info_cmd.finish("\n".join(lines))


@nick_cmd.handle()
async def handle_nick(event: MessageEvent, args: Message = CommandArg()):
    parts = args.extract_plain_text().strip().split()
    if len(parts) != 2:
        await nick_cmd.finish("用法：/设置昵称 <昵称> <课程代码>\n例：/设置昵称 自控 AUTO1001")

    nickname, course_code = parts[0], parts[1].upper()
    try:
        await courseserver.read_course(course_code)
    except courseserver.CourseServerError:
        await nick_cmd.finish(f"课程 {course_code} 不存在，未设置昵称")

    nicknames.set_nick(nickname, course_code)
    await nick_cmd.finish(f"已设置：「{nickname}」→ {course_code}")
