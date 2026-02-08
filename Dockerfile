FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install runtime deps explicitly (project uses src/ layout and does not declare a build backend).
RUN pip install --no-cache-dir \
    nonebot2[fastapi]>=2.4.4 \
    nonebot-adapter-onebot>=2.4.6 \
    nonebot-plugin-status>=0.9.0 \
    nonebot-plugin-apscheduler>=0.5.0 \
    nonebot-plugin-alconna>=0.60.4 \
    httpx>=0.27.0 \
    GitPython>=3.1.0 \
    langchain>=0.1.0 \
    langchain-community>=0.0.10 \
    langchain-openai>=0.0.5 \
    langchain-huggingface>=0.0.1 \
    chromadb>=0.4.22 \
    sentence-transformers>=2.3.0 \
    thefuzz>=0.19.0 \
    tomlkit>=0.13.2

COPY bot.py /app/bot.py
COPY src /app/src
COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md

EXPOSE 8081

CMD ["python", "bot.py"]
