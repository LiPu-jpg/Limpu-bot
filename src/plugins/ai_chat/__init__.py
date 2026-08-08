"""AI 对话：/ai <内容> —— DeepSeek，群内按用户保留短期上下文。"""

from __future__ import annotations

import time
from collections import deque

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from limpu_core.llm import LLMError, chat
from limpu_core.settings import get_settings

ai_cmd = on_command("ai", aliases={"AI"}, priority=10, block=True)

SYSTEM_PROMPT = (
    "你是 Limpu，哈尔滨工业大学（深圳）课程资料群的群助手。"
    "回答简洁、准确、口语化，控制在 300 字以内，不要使用 markdown 表格。"
)

# (group_id, user_id) -> {"msgs": deque, "ts": float}
_sessions: dict[tuple[int, int], dict] = {}
MAX_TURNS = 6


def _session_key(event: MessageEvent) -> tuple[int, int]:
    group_id = getattr(event, "group_id", 0) or 0
    return (group_id, event.user_id)


def _get_history(key: tuple[int, int]) -> deque:
    ttl = get_settings().session_ttl
    sess = _sessions.get(key)
    now = time.time()
    if sess and now - sess["ts"] < ttl:
        return sess["msgs"]
    msgs: deque = deque(maxlen=MAX_TURNS * 2)
    _sessions[key] = {"msgs": msgs, "ts": now}
    return msgs


@ai_cmd.handle()
async def handle_ai(event: MessageEvent, args: Message = CommandArg()):
    content = args.extract_plain_text().strip()
    if not content:
        await ai_cmd.finish("用法：/ai <想问的内容>（支持连续追问）")

    key = _session_key(event)
    history = _get_history(key)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": content}]

    try:
        reply = await chat(messages)
    except LLMError as e:
        await ai_cmd.finish(f"AI 暂时不可用：{e}")

    history.append({"role": "user", "content": content})
    history.append({"role": "assistant", "content": reply})
    _sessions[key]["ts"] = time.time()

    await ai_cmd.finish(reply)
