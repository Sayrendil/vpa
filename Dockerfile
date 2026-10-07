FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY si01 ./si01
RUN pip install --no-cache-dir .
COPY config ./config
COPY prompts ./prompts
COPY evals ./evals
COPY scripts ./scripts
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "si01"]
