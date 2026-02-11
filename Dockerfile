ARG PYTHON_BASE=python:3.12-slim
FROM ${PYTHON_BASE}

# Default to CN-friendly mirrors (override via build args if needed)
ARG APT_MIRROR=https://mirrors.aliyun.com/debian
ARG APT_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_DEFAULT_TIMEOUT=1000

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL}

RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries \
        && if [ -n "$APT_MIRROR" ] || [ -n "$APT_SECURITY_MIRROR" ]; then \
                 if [ -f /etc/apt/sources.list ]; then \
                     if [ -n "$APT_MIRROR" ]; then sed -i "s|http://deb.debian.org/debian|$APT_MIRROR|g; s|https://deb.debian.org/debian|$APT_MIRROR|g" /etc/apt/sources.list; fi; \
                     if [ -n "$APT_SECURITY_MIRROR" ]; then sed -i "s|http://security.debian.org/debian-security|$APT_SECURITY_MIRROR|g; s|https://security.debian.org/debian-security|$APT_SECURITY_MIRROR|g" /etc/apt/sources.list; fi; \
                 elif [ -f /etc/apt/sources.list.d/debian.sources ]; then \
                     if [ -n "$APT_MIRROR" ]; then sed -i "s|http://deb.debian.org/debian|$APT_MIRROR|g; s|https://deb.debian.org/debian|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi; \
                     if [ -n "$APT_SECURITY_MIRROR" ]; then sed -i "s|http://security.debian.org/debian-security|$APT_SECURITY_MIRROR|g; s|https://security.debian.org/debian-security|$APT_SECURITY_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi; \
                 else \
                     echo "WARN: no apt sources file found"; \
                 fi; \
             fi \
    && apt-get update \
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
