"""群聊问答捕获：引用消息 + /问题始 → get_msg 回溯 → LLM 总结。

用法：
  回复起始消息发 /问题始 → 从该消息（含）开始捕获
  回复终止消息发 /问题终 → 截至该消息（含），所有消息送 LLM 总结归档
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

from nonebot import get_bot, logger, on_startswith
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from limpu_core.llm import LLMError, chat
from limpu_core.settings import get_settings

qa_start_cmd = on_startswith("/问题始", priority=5, block=True)
qa_end_cmd = on_startswith("/问题终", priority=5, block=True)

SESSION_TTL = 1800

_sessions: dict[int, dict] = {}
_pending_fetches: dict[int, set[int]] = {}  # group_id -> set of msg_ids being fetched

SUMMARY_SYSTEM = (
    "你是一个知识库编辑，从QQ群聊天记录中提炼问答知识条目。\n"
    "要求：\n"
    "1. 过滤纯闲聊（打招呼、表情包、无信息量的对话），只保留有技术/课业/生活技能价值的内容；\n"
    "2. 归纳为一个或多个 Q&A 条目，每个条目格式：\n"
    "   ## 问题\n"
    "   （简洁概括群友问的是什么）\n"
    "   ## 解答\n"
    "   （综合群友的回答，保留关键信息如链接、命令、数字、步骤）\n"
    "   条目之间用 --- 分隔；\n"
    "3. 不要编造聊天记录里没有的信息；\n"
    "4. 输出纯文本 Markdown（不要代码块包裹）。"
)


def _get_session(event: GroupMessageEvent) -> dict | None:
    sess = _sessions.get(event.group_id)
    if sess and time.time() - sess["ts"] < SESSION_TTL:
        sess["ts"] = time.time()
        return sess
    _sessions.pop(event.group_id, None)
    return None


def _extract_reply_id(event: GroupMessageEvent) -> int | None:
    if event.reply:
        mid = int(event.reply.message_id)
        return mid if mid > 0 else None
    return None


def _extract_text(info: dict) -> str:
    msg = info.get("message")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        return "".join(
            seg.get("data", {}).get("text", "")
            for seg in msg if seg.get("type") == "text"
        )
    return str(info.get("raw_message") or "")


async def _fetch_msg(bot, msg_id: int, group_id: int) -> dict | None:
    try:
        info = await bot.call_api("get_msg", message_id=msg_id)
        nick = (info.get("sender", {}) or {}).get("nickname", "") or str(info.get("user_id", ""))
        text = _extract_text(info)
        if not text.strip():
            logger.info(f"get_msg({msg_id}) returned empty text, raw: {info.get('raw_message','?')[:100]}")
            return None
        return {
            "text": text,
            "nick": nick,
            "ts": info.get("time", time.time()),
            "msg_id": msg_id,
        }
    except Exception as e:
        logger.warning(f"get_msg({msg_id}) failed: {e}")
        return None


async def _fetch_range(bot, start_id: int, end_id: int, group_id: int) -> list[dict]:
    """批量回溯 [start_id, end_id] 之间的消息。"""
    msgs: list[dict] = []
    for msg_id in range(start_id, end_id + 1):
        m = await _fetch_msg(bot, msg_id, group_id)
        if m:
            msgs.append(m)
    return msgs


@qa_start_cmd.handle()
async def handle_start(event: GroupMessageEvent):
    assert isinstance(event, GroupMessageEvent)
    if _get_session(event) is not None:
        await qa_start_cmd.finish("当前已有进行中的问题记录，请先 /问题终 结束")

    reply_id = _extract_reply_id(event)
    if not reply_id:
        await qa_start_cmd.finish("请 **回复一条消息** 再发送 /问题始，以此标记起始位置。")

    _sessions[event.group_id] = {
        "ts": time.time(),
        "status": "active",
        "start_msg_id": reply_id,
        "end_msg_id": None,
        "parts": [],
    }
    await qa_start_cmd.finish(f"已标记起始消息（id={reply_id}）。请**回复最后一条相关消息**发送 /问题终。")


@qa_end_cmd.handle()
async def handle_end(event: GroupMessageEvent):
    assert isinstance(event, GroupMessageEvent)
    sess = _get_session(event)
    if not sess or sess.get("status") != "active":
        await qa_end_cmd.finish("当前没有进行中的问题记录，请先 /问题始 开始")

    reply_id = _extract_reply_id(event)
    if not reply_id:
        await qa_end_cmd.finish("请 **回复一条消息** 再发送 /问题终，以此标记终止位置。")

    start_id = sess["start_msg_id"]
    end_id = reply_id

    if end_id < start_id:
        await qa_end_cmd.finish(f"终止消息（{end_id}）在起始（{start_id}）之前，请重新选择。")

    _sessions.pop(event.group_id, None)

    # 批量回溯
    await qa_end_cmd.send(f"正在回溯消息 {start_id} → {end_id}，共 {end_id - start_id + 1} 条…")
    bot = get_bot()
    msgs = await _fetch_range(bot, start_id, end_id, event.group_id)

    if not msgs:
        await qa_end_cmd.finish("回溯失败——未能获取到任何消息（消息可能已过期或被清理）")

    logger.info(f"fetched {len(msgs)} messages from {start_id} to {end_id}")

    formatted = _format_messages(msgs)
    try:
        summary = await chat(
            [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": f"群聊记录：\n```\n{formatted[:8000]}\n```\n请提炼知识条目："},
            ],
            temperature=0.3,
            max_tokens=1500,
        )
    except LLMError as e:
        await qa_end_cmd.finish(f"AI 总结失败：{e}")

    if not summary.strip():
        await qa_end_cmd.finish("AI 未生成有效总结，已取消")

    s = get_settings()
    qa_dir = s.qa_dir()
    qa_dir.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("%Y-%m-%d_%H%M.md")
    filepath = qa_dir / filename
    filepath.write_text(summary, encoding="utf-8")

    repo_dir = s.rag_repo_dir
    ok = await _run_git(["add", str(filepath.relative_to(repo_dir))], cwd=str(repo_dir))
    if ok:
        await _run_git(["commit", "-m", f"qq-qa: {datetime.now().strftime('%Y-%m-%d %H:%M')}"], cwd=str(repo_dir))
    asyncio.create_task(_push_background(repo_dir, s.rag_github_token, filename))

    preview = summary[:300] + ("…" if len(summary) > 300 else "")
    await qa_end_cmd.finish(f"已归档（{len(msgs)} 条消息）：{filename}\n\n{preview}")


def _format_messages(msgs: list[dict]) -> str:
    lines = []
    for m in sorted(msgs, key=lambda x: x["msg_id"]):
        ts = datetime.fromtimestamp(m["ts"]).strftime("%H:%M")
        lines.append(f"[{ts}] {m['nick']}: {m['text']}")
    return "\n".join(lines)


# ---------------- git helpers ----------------


GIT_TIMEOUT = 45
PUSH_MAX_RETRIES = 5
RETRY_DELAY = 60
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


async def _run_git(args: list[str], cwd: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**dict(os.environ), **_GIT_ENV},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
        logger.info(f"git {' '.join(args)}: {(out or b'').decode(errors='replace')[-200:]}")
        return proc.returncode == 0
    except Exception as e:
        logger.warning(f"git {' '.join(args)} failed: {e}")
        return False


async def _run_git_long(args: list[str], cwd: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**dict(os.environ), **_GIT_ENV},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        logger.info(f"git {' '.join(args)}: {(out or b'').decode(errors='replace')[-200:]}")
        return proc.returncode == 0
    except Exception as e:
        logger.warning(f"git {' '.join(args)} failed: {e}")
        return False


async def _push_background(repo_dir: Path, token: str, filename: str) -> None:
    if not token:
        return
    await asyncio.sleep(3)
    remote_url = f"https://{token}@github.com/HIT-A/HITA_RagData.git"
    for attempt in range(1, PUSH_MAX_RETRIES + 1):
        ok = await _run_git_long(["push", remote_url, "main"], cwd=str(repo_dir))
        if ok:
            logger.info(f"qa-qa/{filename}: git push OK ({attempt})")
            return
        await _run_git_long(["pull", "--ff-only"], cwd=str(repo_dir))
        if attempt < PUSH_MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
