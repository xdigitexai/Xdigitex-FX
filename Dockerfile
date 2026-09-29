FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 DATA_DIR=/var/lib/xdigitex-trade
WORKDIR /app
COPY server.py xpay.py marketdata.py index.html styles.css app.js README.md ./
RUN useradd --system --home-dir /app --shell /usr/sbin/nologin appuser \
    && mkdir -p /var/lib/xdigitex-trade \
    && chown -R appuser:appuser /app /var/lib/xdigitex-trade
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=2)"
CMD ["python", "server.py"]
