FROM python:3.12-slim

WORKDIR /app

COPY mcp/cron-mcp/ /app/mcp/cron-mcp/

WORKDIR /app/mcp/cron-mcp
RUN pip install --no-cache-dir -e .

CMD ["python", "server.py"]
