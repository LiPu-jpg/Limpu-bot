import os
from typing import Optional

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
            self.llm = ChatOpenAI(
                openai_api_key=config.AI_API_KEY,
                openai_api_base=config.AI_BASE_URL,
                model_name=config.AI_MODEL,
                temperature=0.3,
            )

        if self.embeddings is None:
            print("🧠 正在加载 Embedding 模型 (CPU)...")
            self.embeddings = HuggingFaceEmbeddings(
                model_name=config.EMBEDDING_MODEL,
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )

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
            | self.llm
            | StrOutputParser()
        )

        try:
            return await chain.ainvoke(question)
        except Exception as e:
            return f"❌ AI 发生错误: {e}"

# 全局单例
rag_engine = RagEngine()
