FROM python:3.11-slim AS builder
WORKDIR /app
COPY metadata_service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY metadata_service/ ./metadata_service/
COPY schema/ ./schema/
COPY proto/ ./proto/

# Non-root user for security
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8002 50052

CMD ["python", "-m", "uvicorn", "metadata_service.app.main:app", \
     "--host", "0.0.0.0", "--port", "8002", \
     "--workers", "1", "--no-access-log"]
