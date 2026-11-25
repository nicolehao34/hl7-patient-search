FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
EXPOSE 2575 8000
CMD ["hl7-search", "serve", "--http-host", "0.0.0.0", "--database", "/data/hl7.db"]

