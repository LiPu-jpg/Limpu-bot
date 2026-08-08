"""RAG 问答：/问 <问题>

链路：agent-backend /api/rag/query 向量检索（Qdrant）→ DeepSeek 基于命中片段生成回答。
agent-backend 的 embedding 依赖外部 LLM 服务，欠费/故障时优雅降级提示。

上下文策略：top_k=10 命中按 doc_id 聚合排列，前 2 个来源尝试拉 GitHub 全篇原文，
其余保留 280 字片段，所有来源平等喂给 LLM。
"""

from __future__ import annotations

import time
from collections import OrderedDict

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from limpu_core.agent_backend import AgentBackendError, rag_query
from limpu_core.llm import LLMError, chat
from limpu_core.rag_fulltext import fetch_fulltext, is_fetchable

ask_cmd = on_command("问", aliases={"ask"}, priority=10, block=True)
view_cmd = on_command("看", priority=10, block=True)

# (group_id, user_id) -> last query hits + question
_last_cache: dict[tuple[int, int], dict] = {}
CACHE_TTL = 600  # 10 分钟

RAG_SYSTEM = (
    "你是大学课程资料助手的问答模块。基于给定的知识库片段回答用户问题。\n"
    "要求：\n"
    "1. 只依据片段内容回答，片段没有的就说「知识库里没有相关信息」，不要编造；\n"
    "2. 片段中出现的具体信息（数字、条款、文件名、链接、分数线、比例等）必须原样引用，"
    "不要泛泛而谈、不要用「链接已提供」这类转述代替实际内容；\n"
    "3. 分点组织，覆盖片段里与问题相关的全部要点，上限 600 字，不要用 markdown 表格；\n"
    "4. 如果多个片段观点冲突，如实说明。"
)

MAX_CONTEXT_CHARS = 5000
MAX_FULLTEXT_CHARS = 4000
FULL_TEXT_TOP_N = 2  # 命中最多的前 N 个文档尝试拉全文


def _ranked_docs(hits: list[dict]) -> list[tuple[str, list[dict], float]]:
    """按命中次数 desc、最高分 desc 排列 doc_id。"""
    docs: OrderedDict[str, tuple[list[dict], float]] = OrderedDict()
    for hit in hits:
        doc = hit.get("doc_id") or ""
        if not doc:
            continue
        entry = docs.get(doc)
        if entry is None:
            docs[doc] = ([hit], float(hit.get("score") or 0.0))
        else:
            entry[0].append(hit)
            entry = (entry[0], max(entry[1], float(hit.get("score") or 0.0)))
            docs[doc] = entry
    ranked = sorted(docs.items(), key=lambda kv: (len(kv[1][0]), kv[1][1], kv[0]), reverse=True)
    return [(doc_id, entry[0], entry[1]) for doc_id, entry in ranked]


@ask_cmd.handle()
async def handle_ask(event: MessageEvent, args: Message = CommandArg()):
    question = args.extract_plain_text().strip()
    if not question:
        await ask_cmd.finish("用法：/问 <关于课程的问题>")

    try:
        data = await rag_query(question)
    except AgentBackendError as e:
        msg = str(e)
        if "余额不足" in msg or "429" in msg or "embedding" in msg.lower():
            await ask_cmd.finish("知识库暂时不可用（向量服务余额不足），请稍后再试或联系管理员充值")
        await ask_cmd.finish(f"知识库查询失败：{e}")

    hits = (data.get("result") or {}).get("hits") or []
    if not hits:
        await ask_cmd.finish("知识库里没有找到相关内容")

    # 缓存以供 /看 查看来源
    key = (getattr(event, "group_id", 0) or 0, event.user_id)
    _last_cache[key] = {"hits": hits, "question": question, "ts": time.time()}

    ranked = _ranked_docs(hits)
    # 前 N 个来源尝试拉全文，其余用片段
    fulltext_map: dict[str, str] = {}
    for i, (doc_id, _, _) in enumerate(ranked):
        if i < FULL_TEXT_TOP_N and is_fetchable(doc_id):
            ft = await fetch_fulltext(doc_id)
            if ft:
                fulltext_map[doc_id] = ft

    parts: list[str] = []
    sources: list[str] = []
    total = 0
    for doc_id, doc_hits, _ in ranked:
        ft = fulltext_map.get(doc_id)
        if ft:
            text = ft[:MAX_FULLTEXT_CHARS]
        else:
            text = "\n".join((h.get("snippet") or "").strip() for h in doc_hits if (h.get("snippet") or "").strip())
        if not text:
            continue
        if total + len(text) > MAX_CONTEXT_CHARS:
            space = MAX_CONTEXT_CHARS - total
            if space < 200:
                break
            text = text[:space]
        total += len(text)
        parts.append(text)
        sources.append(doc_id)
        if total >= MAX_CONTEXT_CHARS:
            break

    if not parts:
        await ask_cmd.finish("知识库里没有找到相关内容")

    context = "\n\n---\n\n".join(parts)
    used = sum(1 for s in sources if s in fulltext_map)
    label = f"知识库（{len(parts)} 个来源，{used} 份全文）"

    messages = [
        {"role": "system", "content": RAG_SYSTEM},
        {"role": "user", "content": f"{label}：\n{context}\n\n问题：{question}"},
    ]
    try:
        answer = await chat(messages, temperature=0.3, max_tokens=1000)
    except LLMError as e:
        await ask_cmd.finish(f"AI 生成回答失败：{e}")

    if sources:
        cleaned = []
        for s in sources[:6]:
            name = s.split("/")[-1]
            name = name.split(":")[0]
            if not name:
                name = s.split("/")[-2] if "/" in s else s[:40]
            marker = " *" if s in fulltext_map else ""
            cleaned.append(f"{name}{marker}")
        shown = "、".join(cleaned)
        answer += f"\n\n来源：{shown}"
        if any(s in fulltext_map for s in sources):
            answer += "\n（* 号标记的为全文模式）"
    await ask_cmd.finish(answer)


@view_cmd.handle()
async def handle_view(event: MessageEvent):
    key = (getattr(event, "group_id", 0) or 0, event.user_id)
    cached = _last_cache.get(key)
    if not cached or (time.time() - cached["ts"] > CACHE_TTL):
        await view_cmd.finish("尚未查询或缓存已过期，请先 /问 一个问题")

    hits: list[dict] = cached["hits"]
    lines = [f"上次查询「{cached['question'][:30]}」的命中来源（{len(hits)} 条）："]
    shown = 0
    for i, hit in enumerate(hits[:8]):
        snippet = (hit.get("snippet") or "").strip()
        doc = (hit.get("doc_id") or "").split("/")[-1].split(":")[0]
        score = float(hit.get("score") or 0.0)
        lines.append(f"\n[{i+1}] {doc}（相关度 {score:.2f}）")
        lines.append(snippet[:200])
        shown += 1
    if shown == 0:
        lines.append("（无命中）")
    if len(hits) > 8:
        lines.append(f"\n…其余 {len(hits) - 8} 条未展示")
    lines.append(f"\ntop_k={min(len(hits), 10)}")
    await view_cmd.finish("\n".join(lines))
