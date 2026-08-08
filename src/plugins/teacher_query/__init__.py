"""教师查询：/教师 <姓名|拼音> —— 走 agent-backend teachers/search。

agent-backend 返回的是教师主页抓取的全文 markdown（约几万字，含大量导航噪音），
这里提取简介段落（教师名 + 逗号开头的句子）展示，避免刷屏。
"""

from __future__ import annotations

import re

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from limpu_core.agent_backend import AgentBackendError, search_teacher

teacher_cmd = on_command("教师", aliases={"teacher"}, priority=10, block=True)

BIO_MAX = 350


def _extract_bio(markdown: str, name: str) -> str:
    """从主页 markdown 中提取简介正文。

    策略：找「姓名，」开头的句子（教师主页简介的典型开头），截取到段落结束。
    """
    if not markdown or not name:
        return ""
    m = re.search(re.escape(name) + "，", markdown)
    if not m:
        return ""
    # 从匹配处截到空行或 BIO_MAX 长度
    rest = markdown[m.start():]
    end = rest.find("\n\n")
    bio = rest[: end if 0 < end < BIO_MAX else BIO_MAX]
    # 清理 markdown 标记，避免 QQ 显示一堆符号
    bio = re.sub(r"[#*\[\]()>`]", "", bio)
    bio = re.sub(r"\s+", " ", bio).strip()
    return bio


@teacher_cmd.handle()
async def handle_teacher(event: MessageEvent, args: Message = CommandArg()):
    name = args.extract_plain_text().strip()
    if not name:
        await teacher_cmd.finish("用法：/教师 <姓名 或 拼音>（如 /教师 裴文杰 或 /教师 peiwenjie）")

    try:
        data = await search_teacher(name)
    except AgentBackendError as e:
        msg = str(e)
        if "NOT_FOUND" in msg or "no homepage" in msg:
            await teacher_cmd.finish(f"没有找到「{name}」的主页信息")
        await teacher_cmd.finish(f"查询失败：{e}")

    result = data.get("result") or {}
    real_name = result.get("name") or name
    homepage = result.get("homepage") or ""
    bio = _extract_bio(result.get("markdown") or "", real_name)

    lines = [f"【{real_name}】"]
    if bio:
        lines.append(bio)
    if homepage:
        lines.append(f"主页：{homepage}")
    await teacher_cmd.finish("\n".join(lines))
