# Imagem enxuta, usuário não-root, sem segredos. Python 3.12 (versão alvo do projeto).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 bot
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY config/tokens.example.json ./config/tokens.example.json
RUN mkdir -p /app/data /app/reports /app/logs && chown -R bot:bot /app
USER bot

# Nenhuma porta é publicada. O health check não opera: valida config, banco, dados e kill switch.
HEALTHCHECK --interval=60s --timeout=30s --start-period=60s --retries=3 \
  CMD python -m app.main --health-check || exit 1

STOPSIGNAL SIGTERM
# Padrão SEGURO: paper trading. LIVE exige BOT_MODE=live + BOT_LIVE_CONFIRM + --confirm-live.
ENTRYPOINT ["python", "-m", "app.main"]
CMD ["--mode", "paper"]
