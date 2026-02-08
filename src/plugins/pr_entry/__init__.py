from nonebot.plugin import PluginMetadata

from . import handlers

__plugin_meta__ = PluginMetadata(
    name="PR 入口（审核+提交）",
    description="QQ 内发起 readme.toml 提交；先经 LLM 合规审核，再调用 prServer 自动开 PR",
    usage=(
        "/pr help\n"
        "/pr start AUTO2001 AUTO2001 自动化专业导论 normal\n"
        "/pr show\n"
        "/pr add <章节标题>\n"
        "/pr modify\n"
        "（按提示发送原段落/新段落，或直接粘贴整段 TOML）\n"
        "/pr cancel"
    ),
)
