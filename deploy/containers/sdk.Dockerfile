FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN python -m compileall -q anvil
USER nobody
CMD ["python", "-c", "import anvil; print(anvil.__version__)"]
