FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    JOBBOT_HOST=0.0.0.0 \
    JOBBOT_PORT=8000 \
    JOBBOT_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[all]" \
    && python -m playwright install --with-deps chromium \
    && useradd --create-home --uid 1000 jobbot \
    && mkdir -p /data && chown jobbot:jobbot /data

USER jobbot
VOLUME /data
EXPOSE 8000
CMD ["jobbot", "serve"]
