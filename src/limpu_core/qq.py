"""OneBot v11 消息工具：长文本渲染为图片 → 合并转发。"""

from __future__ import annotations

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent


async def send_long_group(bot: Bot, event: GroupMessageEvent, text: str) -> None:
    """将 markdown 渲染为图片，打包进合并转发聊天记录。
    短文本（< 500字）直接发送；大于此值走图片 + 转发。
    """
    if len(text) < 500:
        await bot.send(event, text)
        return

    try:
        from limpu_core.render import md_to_forward_node
        node = await md_to_forward_node(text)
        await bot.call_api(
            "send_group_forward_msg",
            group_id=event.group_id,
            messages=[node],
        )
    except Exception as e:
        logger.warning(f"md_to_image failed: {e}, fallback to plain text")
        await bot.send(event, text)
