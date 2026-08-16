FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Credentials and the panel CA are mounted in, never baked into the image.
ENV SPAN_AUTH_FILE=/config/span-auth.json \
    SPAN_CA_CERT_DIR=/config/ca-certs \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["span-bridge"]
CMD ["run"]
