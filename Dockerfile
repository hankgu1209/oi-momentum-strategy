FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

RUN useradd --create-home --uid 1000 appuser

COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs

RUN python -m pip install --retries 10 --timeout 60 \
    "aiohttp>=3.9" \
    "pandas>=2.2" \
    "PyYAML>=6.0" \
    "requests>=2.31" \
    "streamlit>=1.37" \
    "python-socks[asyncio]>=2.4" \
    "websockets>=12.0"

RUN mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

CMD ["streamlit", "run", "src/binance_oi_momentum/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
