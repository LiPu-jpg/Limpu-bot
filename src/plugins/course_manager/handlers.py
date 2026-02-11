from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent, Bot, Message
from nonebot_plugin_alconna import Alconna, Args, on_alconna
from nonebot.rule import to_me

from .data_loader import course_manager

# --- 工具函数：构造合并转发节点 ---
def make_node(bot: Bot, content: str, name: str = "Hoa_Anon酱"):
    return {
        "type": "node",
        "data": {
            "name": name,
            "uin": bot.self_id,
            "content": Message(content)
        }
    }

# =======================
# 功能 1: 课程搜索 (模糊)
# =======================
# 触发：@bot 搜 自动控制
matcher_search = on_alconna(Alconna("搜", Args["keyword", str]), aliases={"search"}, use_cmd_start=True, rule=to_me(), priority=10)

@matcher_search.handle()
async def handle_search(keyword: str):
    matches = course_manager.search_fuzzy(keyword)
    
    if not matches:
        await matcher_search.finish(f"🧐 未找到包含 '{keyword}' 的课程。")
    
    # 如果只有一个结果，且匹配度很高，可以考虑直接展示（这里简单处理，还是列表展示）
    msg = "🔍 找到以下条目（可复制『代码』或『子课程名』去查询）：\n" + "\n".join(
        [f"• {m['code']} - {m['name']}" for m in matches]
    )
    msg += "\n\n💡 用法：/查 <课程代码或子课程名>"
    await matcher_search.finish(msg)


# =======================
# 功能 2: 课程详情查询
# =======================
# 触发：@bot 查 AUTO1001 或 @bot 查 自动化
matcher_query = on_alconna(Alconna("查", Args["target", str]), aliases={"info"}, use_cmd_start=True, rule=to_me(), priority=10)

@matcher_query.handle()
async def handle_query(bot: Bot, event: MessageEvent, target: str):
    course = course_manager.get_course_detail(target)

    # 若精确匹配失败：复用 /搜 的逻辑。
    # - 唯一候选：直接展示完整信息
    # - 多候选：提示用户先 /搜 或复制代码再 /查
    if not course:
        matches = course_manager.search_fuzzy(target)
        if len(matches) == 1:
            code = str(matches[0].get("code") or "").strip()
            if code:
                course = course_manager.get_course_detail(code)
        if not course:
            if matches:
                msg = "🧐 找到多个可能的课程，请复制课程代码再查询：\n" + "\n".join(
                    [f"• {m['code']} - {m['name']}" for m in matches]
                )
                msg += "\n\n用法：/查 课程代码  或  /搜 <关键词>"
                await matcher_query.finish(msg)
            await matcher_query.finish(f"❌ 未找到 '{target}'，请先尝试使用 /搜 确认名称。")

    def _norm_text(s: str) -> str:
        return (s or "").strip().replace("\r\n", "\n")

    def _safe_str(v) -> str:
        return "" if v is None else str(v)

    def _fmt_author(d) -> str:
        if not isinstance(d, dict):
            return ""
        name = _safe_str(d.get("name")).strip()
        link = _safe_str(d.get("link")).strip()
        date = _safe_str(d.get("date")).strip()
        tail = " ".join([x for x in [name, date] if x])
        if link:
            tail = (tail + " " + link).strip()
        return f"\n👤 {tail}" if tail else ""

    def _push_block(title: str, body: str):
        body = _norm_text(body)
        if not body:
            return
        nodes.append(make_node(bot, f"{title}\n{body}".strip()))

    # 构建合并转发消息（兼容新旧两套 schema）
    nodes = []

    async def _send_forward_or_fallback(nodes_to_send):
        def _as_text(nodes_subset) -> str:
            parts = []
            for n in nodes_subset:
                try:
                    parts.append(str(n.get("data", {}).get("content", "")))
                except Exception:
                    continue
            return "\n\n".join([p for p in parts if p]).strip()

        async def _send_text_chunks(text: str):
            text = (text or "").strip()
            if not text:
                await matcher_query.finish("⚠️ 无可发送内容。")
            # OneBot 单条消息过长容易失败；这里分段。
            chunk_size = 1500
            chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
            group_id = getattr(event, "group_id", None)
            if group_id:
                for c in chunks:
                    await bot.call_api("send_group_msg", group_id=group_id, message=Message(c))
            else:
                for c in chunks:
                    await bot.call_api("send_private_msg", user_id=event.user_id, message=Message(c))

        async def _send_forward(nodes_batch):
            group_id = getattr(event, "group_id", None)
            if group_id:
                await bot.call_api("send_group_forward_msg", group_id=group_id, messages=nodes_batch)
            else:
                await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes_batch)

        async def _send_in_batches(nodes_all, batch_size: int) -> bool:
            batches = [nodes_all[i : i + batch_size] for i in range(0, len(nodes_all), batch_size)]
            for b in batches:
                try:
                    await _send_forward(b)
                except Exception:
                    # batch 失败：尝试更小 batch；再不行就仅对该 batch 走文本降级
                    if batch_size > 5:
                        ok = await _send_in_batches(b, batch_size=max(5, batch_size // 2))
                        if ok:
                            continue
                    try:
                        await _send_forward([b[0]])
                        for n in b[1:]:
                            await _send_forward([n])
                    except Exception:
                        await _send_text_chunks(_as_text(b))
                    return False
            return True

        # 优先分批合并转发：避免单条 forward 因节点过多/内容过长触发失败。
        await _send_in_batches(nodes_to_send, batch_size=25)

    # multi-project 父仓库：输出该仓库下所有子课程的全量内容
    if isinstance(course, dict) and str(course.get("repo_type") or "").strip() == "multi-project" and isinstance(course.get("courses"), list):
        header = (
            f"📚 【{_safe_str(course.get('course_name'))}】\n"
            f"代码：{_safe_str(course.get('course_code'))}\n"
            f"══════════════════\n{_norm_text(_safe_str(course.get('description')))}"
        ).strip()
        nodes.append(make_node(bot, header))

        courses_list = course.get("courses")
        if not isinstance(courses_list, list):
            courses_list = []
        for idx, sub in enumerate(courses_list):
            if not isinstance(sub, dict):
                continue
            sub_name = _safe_str(sub.get("name") or f"(未命名子课程 {idx + 1})")
            sub_code = _safe_str(sub.get("code") or "").strip()
            title = f"🧩 {sub_name}" + (f"（{sub_code}）" if sub_code else "")

            parts: list[str] = []
            teachers = sub.get("teachers")
            if isinstance(teachers, list) and teachers:
                for t in teachers:
                    if not isinstance(t, dict):
                        continue
                    name = _safe_str(t.get("name") or "(未命名教师)")
                    txt = f"👨‍🏫 授课教师：{name}"
                    reviews = t.get("reviews")
                    if isinstance(reviews, list):
                        for rev in reviews:
                            if isinstance(rev, dict) and rev.get("content"):
                                txt += f"\n\n「{_norm_text(_safe_str(rev.get('content')))}」{_fmt_author(rev.get('author'))}"
                    parts.append(txt.strip())

            sections = sub.get("sections")
            if isinstance(sections, list) and sections:
                for sec in sections:
                    if not isinstance(sec, dict):
                        continue
                    st = _safe_str(sec.get("title") or "(未命名章节)")
                    items = sec.get("items")
                    blocks = []
                    if isinstance(items, list):
                        for it in items:
                            if isinstance(it, dict) and it.get("content"):
                                blocks.append(_norm_text(_safe_str(it.get("content"))) + _fmt_author(it.get("author")))
                    body = "\n\n".join([b for b in blocks if b])
                    if body:
                        parts.append(f"📌 {st}\n{body}")

            if parts:
                nodes.append(make_node(bot, f"{title}\n\n" + "\n\n".join(parts)))
            else:
                nodes.append(make_node(bot, f"{title}\n（暂无更多内容）"))

        nodes.append(make_node(bot, "🔗 相关资源\n👉 完整内容：https://hoa.moe"))
        await _send_forward_or_fallback(nodes)
        return

    # multi-project 子课程 wrapper
    if isinstance(course, dict) and course.get("_schema") == "multi-project-item":
        parent = course.get("_parent") or {}
        idx = int(course.get("_course_index") or 0)
        courses = parent.get("courses") if isinstance(parent, dict) else None
        sub = courses[idx] if isinstance(courses, list) and 0 <= idx < len(courses) else {}

        header = (
            f"📚 【{_safe_str(course.get('course_name'))}】\n"
            f"代码：{_safe_str(course.get('course_code'))}\n"
            f"══════════════════\n{_norm_text(_safe_str(parent.get('description')))}"
        ).strip()
        nodes.append(make_node(bot, header))

        # teachers + reviews
        teachers = sub.get("teachers") if isinstance(sub, dict) else None
        if isinstance(teachers, list):
            for t in teachers:
                if not isinstance(t, dict):
                    continue
                name = _safe_str(t.get("name") or "(未命名教师)")
                reviews = t.get("reviews")
                txt = f"👨‍🏫 授课教师：{name}\n"
                if isinstance(reviews, list):
                    for rev in reviews:
                        if isinstance(rev, dict) and rev.get("content"):
                            txt += f"\n「{_norm_text(_safe_str(rev.get('content')))}」{_fmt_author(rev.get('author'))}\n"
                nodes.append(make_node(bot, txt.strip()))

        # sections/items
        sections = sub.get("sections") if isinstance(sub, dict) else None
        if isinstance(sections, list):
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                title = _safe_str(sec.get("title") or "(未命名章节)")
                items = sec.get("items")
                blocks = []
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and it.get("content"):
                            blocks.append(_norm_text(_safe_str(it.get("content"))) + _fmt_author(it.get("author")))
                _push_block(f"📌 {title}", "\n\n".join([b for b in blocks if b]))

    # sections/lecturers
    elif isinstance(course, dict):
        header = (
            f"📚 【{_safe_str(course.get('course_name'))}】\n"
            f"代码：{_safe_str(course.get('course_code'))}\n"
            f"══════════════════\n{_norm_text(_safe_str(course.get('description')))}"
        ).strip()
        if course.get("notices"):
            header += f"\n\n📢 注意事项：\n{_norm_text(_safe_str(course.get('notices')))}"
        nodes.append(make_node(bot, header))

        lecturers = course.get("lecturers")
        if isinstance(lecturers, list):
            for lec in lecturers:
                if not isinstance(lec, dict):
                    continue
                txt = f"👨‍🏫 授课教师：{_safe_str(lec.get('name') or '(未命名教师)')}\n"
                reviews = lec.get("reviews")
                if isinstance(reviews, list):
                    for rev in reviews:
                        if isinstance(rev, dict) and rev.get("content"):
                            txt += f"\n「{_norm_text(_safe_str(rev.get('content')))}」{_fmt_author(rev.get('author'))}\n"
                nodes.append(make_node(bot, txt.strip()))

        sections2 = course.get("sections")
        if not isinstance(sections2, list):
            sections2 = []

        for sec in sections2:
            if not isinstance(sec, dict):
                continue
            title = _safe_str(sec.get("title") or "(未命名章节)")
            items = sec.get("items")
            blocks = []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("content"):
                            blocks.append(_norm_text(_safe_str(it.get("content"))) + _fmt_author(it.get("author")))
            _push_block(f"📌 {title}", "\n\n".join([b for b in blocks if b]))

    nodes.append(make_node(bot, "🔗 相关资源\n👉 完整内容：https://hoa.moe"))
    await _send_forward_or_fallback(nodes)


# =======================
# 功能 3: 昵称设置
# =======================
# 触发：@bot 设置昵称 自动控制 AUTO1001
matcher_nick = on_alconna(Alconna("设置昵称", Args["nick", str], Args["code", str]), use_cmd_start=True, rule=to_me(), priority=5)

@matcher_nick.handle()
async def handle_nick(nick: str, code: str):
    # 允许用户直接填课程名：如 “/设置昵称 大物 大学物理”
    raw = (code or "").strip()
    resolved = ""
    detail = course_manager.get_course_detail(raw)
    if isinstance(detail, dict):
        resolved = str(detail.get("course_code") or "").strip().upper()
    if not resolved:
        resolved = raw.strip().upper()

    success = course_manager.add_nickname(nick, resolved)
    if success:
        await matcher_nick.finish(f"✅ 成功将「{nick}」指向 {resolved}")
    else:
        await matcher_nick.finish(f"❌ 课程代码 {resolved} 不存在，请先确认代码。")


# =======================
# 功能 4: 数据刷新 (Git Pull)
# =======================
# 触发：@bot 刷
matcher_reload = on_alconna(Alconna("刷"), aliases={"update"}, use_cmd_start=True, rule=to_me(), priority=5)

@matcher_reload.handle()
async def handle_reload():
    await matcher_reload.send("⏳ 正在拉取最新数据...")
    res = await course_manager.update_repo()
    await matcher_reload.finish(res)


# =======================
# 功能 5: 知识库重构
# =======================
matcher_build_kb = on_alconna(Alconna("重构知识库"), use_cmd_start=True, rule=to_me(), priority=1)

@matcher_build_kb.handle()
async def handle_build_kb():
    from .rag_engine import rag_engine
    await matcher_build_kb.send("⏳ 正在重构知识库（CPU 占用较高，请稍候）...")
    res = await rag_engine.rebuild_index()
    await matcher_build_kb.finish(res)


# =======================
# 功能 6: AI 问答 (RAG)
# =======================
# 触发：@bot 问 怎么去图书馆
matcher_ask = on_alconna(Alconna("问", Args["question", str]), aliases={"ask"}, use_cmd_start=True, rule=to_me(), priority=20)

@matcher_ask.handle()
async def handle_ask(question: str):
    from .rag_engine import rag_engine
    # 可以加一个等待提示，因为 LLM 响应可能要几秒
    # await matcher_ask.send("🤔 思考中...") 
    res = await rag_engine.query(question)
    await matcher_ask.finish(res)
