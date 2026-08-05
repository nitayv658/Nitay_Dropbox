FROM python:3.11-slim AS builder
WORKDIR /app
COPY block_server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY block_server/ ./block_server/
COPY schema/ ./schema/
COPY proto/ ./proto/

# Non-root user for security
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "block_server.app.main:app", \
     "--host", "0.0.0.0", "--port", "8001", \
     "--workers", "1", "--no-access-log"]
