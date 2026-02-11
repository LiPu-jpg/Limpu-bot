import os
from typing import Any, Optional, cast

from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from .config import config


class RagEngine:
    def __init__(self):
        self.vector_db: Optional[Chroma] = None
        self.retriever = None
        self.llm: Optional[ChatOpenAI] = None
        self.embeddings: Optional[HuggingFaceEmbeddings] = None

        # 延迟初始化：避免 bot 启动时就加载 embedding/LLM
        # 只有在 /问 或 /重构知识库 时才初始化。

    def _ensure_initialized(self) -> None:
        if config.HF_ENDPOINT:
            os.environ["HF_ENDPOINT"] = config.HF_ENDPOINT

        if self.llm is None:
            if not config.AI_API_KEY:
                raise RuntimeError("未配置 HITSZ_MANAGER_AI_API_KEY")

            # langchain_openai 不同版本参数名不一致，动态映射避免运行时报错
            fields = getattr(ChatOpenAI, "model_fields", {}) or {}
            # 这里用 dict + **params 是为了兼容不同版本的 ChatOpenAI 参数名。
            # 但若不显式标注类型，Pylance 会把 params 推断成 dict[str, float]，
            # 从而在后续写入 str/bool 等值时报一堆误报。
            params: dict[str, Any] = {"temperature": 0.3}

            if "openai_api_key" in fields:
                params["openai_api_key"] = config.AI_API_KEY
            elif "api_key" in fields:
                params["api_key"] = config.AI_API_KEY

            if config.AI_BASE_URL:
                if "openai_api_base" in fields:
                    params["openai_api_base"] = config.AI_BASE_URL
                elif "base_url" in fields:
                    params["base_url"] = config.AI_BASE_URL

            if "model" in fields:
                params["model"] = config.AI_MODEL
            elif "model_name" in fields:
                params["model_name"] = config.AI_MODEL

            self.llm = ChatOpenAI(**cast(Any, params))

        if self.embeddings is None:
            # 为了避免每次容器重启都重新下载模型，这里把缓存固定到 data 目录下。
            # 若 data 挂载为 volume，则模型下载一次后可复用。
            cache_root = str((config.DATA_ROOT / "hf_cache").resolve())
            os.environ.setdefault("HF_HOME", cache_root)
            os.environ.setdefault("HUGGINGFACE_HUB_CACHE", os.path.join(cache_root, "hub"))
            os.environ.setdefault("TRANSFORMERS_CACHE", os.path.join(cache_root, "transformers"))

            print("🧠 正在加载 Embedding 模型 (CPU)...")
            try:
                self.embeddings = HuggingFaceEmbeddings(
                    model_name=config.EMBEDDING_MODEL,
                    cache_folder=cache_root,
                    model_kwargs={"device": "cpu"},
                    encode_kwargs={"normalize_embeddings": True},
                )
            except Exception as e:
                hint = (
                    "Embedding 模型下载/加载失败。通常是服务器无法访问 huggingface.co，且本地缓存不存在。\n"
                    f"- 当前模型：{config.EMBEDDING_MODEL}\n"
                    f"- 缓存目录：{cache_root}\n"
                    "可选修复：\n"
                    "1) 设置 HuggingFace 镜像：HITSZ_MANAGER_HF_ENDPOINT=https://hf-mirror.com （或你可用的镜像）并重启；\n"
                    "2) 或在有网络的机器上预下载该模型到上述缓存目录，再拷贝/挂载到服务器。"
                )
                raise RuntimeError(hint) from e

        self._load_existing_db()

    def _load_existing_db(self) -> None:
        if self.embeddings is None:
            return
        if config.VECTOR_DB_DIR.exists() and any(config.VECTOR_DB_DIR.iterdir()):
            try:
                self.vector_db = Chroma(
                    persist_directory=str(config.VECTOR_DB_DIR), 
                    embedding_function=self.embeddings
                )
                self.retriever = self.vector_db.as_retriever(search_kwargs={"k": 3})
                print("📚 本地向量知识库加载成功")
            except Exception as e:
                print(f"⚠️ 向量库加载失败 (可能是首次运行): {e}")

    async def rebuild_index(self) -> str:
        """重建知识库索引 (耗时操作)"""
        try:
            self._ensure_initialized()
        except Exception as e:
            return f"❌ 初始化失败: {e}"

        if not config.RAG_DOCS_DIR.exists():
            return "❌ 目录 data/rag_docs 不存在"
        
        # 1. 读取文件
        loader = DirectoryLoader(str(config.RAG_DOCS_DIR), glob="**/*.txt", loader_cls=TextLoader)
        docs = loader.load()
        if not docs:
            return "⚠️ data/rag_docs 目录下没有 .txt 文件"

        # 2. 切分文本
        splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100)
        splits = splitter.split_documents(docs)

        # 3. 写入 Chroma
        # 注意：这里会重新生成整个库
        try:
            if self.vector_db:
                # 尝试清理旧数据，或者直接覆盖目录
                self.vector_db = None
            
            # 创建新的 DB
            self.vector_db = Chroma.from_documents(
                documents=splits,
                embedding=self.embeddings,
                persist_directory=str(config.VECTOR_DB_DIR)
            )
            self.retriever = self.vector_db.as_retriever(search_kwargs={"k": 3})
            return f"✅ 知识库构建完成！共索引 {len(splits)} 个文本片段。"
        except Exception as e:
            return f"❌ 构建失败: {e}"

    async def query(self, question: str) -> str:
        """RAG 问答流程"""
        try:
            self._ensure_initialized()
        except Exception as e:
            return f"❌ 初始化失败: {e}"

        if not self.retriever:
            return "⚠️ 知识库尚未初始化，请先使用指令构建知识库。"

        llm = self.llm
        if llm is None:
            return "❌ LLM 未初始化"

        template = """你是一个哈工大深圳(HITSZ)的校园助手。请根据以下已知信息回答用户的问题。
        
        严格遵守以下规则：
        1. 仅根据[已知信息]回答，不要编造内容。
        2. 如果已知信息中没有答案，请直接回答“抱歉，知识库中暂时没有相关信息”。
        3. 回答要简洁明了。

        [已知信息]:
        {context}

        [用户问题]: {question}
        """
        prompt = ChatPromptTemplate.from_template(template)

        chain = (
            {"context": self.retriever, "question": RunnablePassthrough()}
            | prompt
            | llm
            | StrOutputParser()
        )

        try:
            return await chain.ainvoke(question)
        except Exception as e:
            return f"❌ AI 发生错误: {e}"

# 全局单例
rag_engine = RagEngine()
