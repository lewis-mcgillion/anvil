FROM python:3.12-slim
WORKDIR /app
COPY server /app/server
RUN pip install --no-cache-dir /app/server
USER nobody
EXPOSE 8080
CMD ["uvicorn", "anvil_server.app:app", "--app-dir", "/app/server", "--host", "0.0.0.0", "--port", "8080"]
