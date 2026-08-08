"""帮助指令：/help 或 /帮助 —— 列出所有功能。"""

from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

help_cmd = on_command("help", aliases={"帮助", "菜单", "menu"}, priority=5, block=True)

HELP = """Limpu-bot 功能清单

【课程查询】
  /查 <关键词>                   搜课程，单结果直接出详情，多结果列出
  /设置昵称 <昵称> <课程代码>      设简称（例：/设置昵称 自控 AUTO1001）

【AI 对话】
  /ai <内容>                     DeepSeek 自由对话，可追问
  /问 <问题>                     知识库 RAG 问答
  /看                            查看上次 /问 的命中片段与来源

【教师】
  /教师 <姓名或拼音>              教师主页简介

【课程贡献 — /pr】
  /pr start <课程代码>             开始会话
  /pr add <章节标题>              追加章节内容（下条消息为正文）
  /pr review <教师名>             添加教师评价（下条消息为正文）
  /pr list                       查看
  /pr undo                       撤销
  /pr sign <名字> [链接]           署名
  /pr preview                     预览
  /pr submit                      审核后提交
  /pr cancel                      放弃

【群聊知识归档】
  /问题始                         开始记录群聊上下文字
  /问题终                         结束记录，LLM 总结并归档到知识库

/help  /帮助  /菜单                显示本帮助"""


@help_cmd.handle()
async def handle_help(event: MessageEvent):
    await help_cmd.finish(HELP)
