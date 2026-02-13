from nonebot import get_driver
from nonebot.plugin import PluginMetadata

# 1. 导入处理函数
# 这一步至关重要！如果不导入 handlers，里面定义的 @on_alconna 指令就不会生效。
from . import handlers

# 2. 导入数据管理器，用于启动时加载数据
from .data_loader import course_manager

# 3. 定义插件元数据 (可选，但在 nb plugin list 中好看)
__plugin_meta__ = PluginMetadata(
    name="HITSZ 校园助手",
    description="包含课程查询、评价检索及 RAG 校园问答功能",
    usage="""
    指令列表：
    /搜 自动控制 - 模糊搜索课程
    /查 AUTO1001 - 查看课程详情
    /查老师 裴文杰 或 /查老师 pwj - 查询教师评价
    /问 图书馆几点开门 - AI 问答
    /重构知识库 - 重新读取 rag_docs 并建立索引
    /刷 - 强制拉取最新课程数据
    """,
)

# 4. 注册启动钩子
# 当 NoneBot 启动完成后，立刻加载课程数据
driver = get_driver()

@driver.on_startup
async def _():
    # 加载课程 JSON/TOML
    course_manager.load_data()

    # 提示：RAG 引擎为懒加载，仅在首次使用 /问 或 /重构知识库 时初始化。
