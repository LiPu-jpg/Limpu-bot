from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent, Bot, Message
from nonebot_plugin_alconna import Alconna, Args, on_alconna
from nonebot.rule import to_me

from .data_loader import course_manager
from .rag_engine import rag_engine

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
    msg = f"🔍 找到以下课程：\n" + "\n".join([f"• {m}" for m in matches])
    msg += "\n\n💡 请使用「/查 课程全名」或「/查 课程代码」获取详情"
    await matcher_search.finish(msg)


# =======================
# 功能 2: 课程详情查询
# =======================
# 触发：@bot 查 AUTO1001 或 @bot 查 自动化
matcher_query = on_alconna(Alconna("查", Args["target", str]), aliases={"info"}, use_cmd_start=True, rule=to_me(), priority=10)

@matcher_query.handle()
async def handle_query(bot: Bot, event: MessageEvent, target: str):
    course = course_manager.get_course_detail(target)
    
    if not course:
        await matcher_query.finish(f"❌ 未找到 '{target}'，请先尝试使用 /搜 确认名称。")

    # 构建合并转发消息
    nodes = []
    
    # 1. 头部信息
    header = f"📚 【{course['course_name']}】\n代码：{course['course_code']}\n══════════════════\n"
    if course.get("notices"):
        header += f"📢 注意事项：\n{course['notices'].strip()}\n"
    nodes.append(make_node(bot, header))

    # 2. 老师信息
    if course.get("lecturers"):
        for lec in course['lecturers']:
            txt = f"👨‍🏫 授课教师：{lec['name']}\n"
            for rev in lec.get("reviews", []):
                txt += f"\n「{rev['content'].strip()}」\n"
            nodes.append(make_node(bot, txt))

    # 3. 各板块评价
    sections = [
        ("course", "📖 课程评价"), ("exam", "📝 考试经验"), 
        ("lab", "🧪 实验经验"), ("advice", "💡 学习建议"),
        ("schedule", "📅 课程安排"), ("misc", "📦 其他杂项")
    ]
    for key, title in sections:
        if course.get(key):
            txt = f"{title}\n" + "\n".join([f"• {item['content'].strip()}" for item in course[key]])
            nodes.append(make_node(bot, txt))

    # 4. 底部链接
    footer = "🔗 相关资源\n👉 完整内容：https://hoa.moe"
    nodes.append(make_node(bot, footer))

    try:
        if event.group_id:
            await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
        else:
            await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=nodes)
    except Exception:
        await matcher_query.finish("⚠️ 发送合并转发消息失败，可能是风控或版本问题。")


# =======================
# 功能 3: 昵称设置
# =======================
# 触发：@bot 设置昵称 自动控制 AUTO1001
matcher_nick = on_alconna(Alconna("设置昵称", Args["nick", str], Args["code", str]), use_cmd_start=True, rule=to_me(), priority=5)

@matcher_nick.handle()
async def handle_nick(nick: str, code: str):
    success = course_manager.add_nickname(nick, code)
    if success:
        await matcher_nick.finish(f"✅ 成功将「{nick}」指向 {code.upper()}")
    else:
        await matcher_nick.finish(f"❌ 课程代码 {code.upper()} 不存在，请先确认代码。")


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
    # 可以加一个等待提示，因为 LLM 响应可能要几秒
    # await matcher_ask.send("🤔 思考中...") 
    res = await rag_engine.query(question)
    await matcher_ask.finish(res)
