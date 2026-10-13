# Runs the text/voice API: FastAPI calls Groq via Pydantic AI and ElevenLabs
# for STT/TTS over HTTP. LLM_MODEL must use the groq/ prefix.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1

# uv for installing the api package.
COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /bin/

WORKDIR /app

# Install both workspace packages with the locked production dependencies.
COPY pyproject.toml uv.lock ./
COPY agent/ ./agent/
COPY api/ ./api/
# Who the rep represents: config/owner.md, else the example (or set OWNER_FILE).
COPY config/ ./config/
RUN uv export --frozen --no-dev --package hireme-api --no-emit-workspace -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt ./agent ./api

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
