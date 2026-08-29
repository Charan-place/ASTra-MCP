# syntax=docker/dockerfile:1

## ---- Builder stage -------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed to build tree-sitter language wheels etc.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY astra ./astra

RUN pip install --no-cache-dir --prefix=/install .

# Pre-download the sentence-transformers embedding model used by
# astra/indexer/embedder.py so runtime cold-start is fast.
ENV PYTHONPATH=/install/lib/python3.11/site-packages
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

## ---- Runtime stage --------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy installed site-packages (astra + all dependencies) from the builder.
COPY --from=builder /install /usr/local
# Copy the pre-downloaded embedding model cache so it's baked into the image.
COPY --from=builder /root/.cache /root/.cache

COPY astra ./astra
COPY pyproject.toml README.md ./

ENV ASTRA_DATA_DIR=/app/.astra
VOLUME ["/app/.astra"]
EXPOSE 7865

ENTRYPOINT ["astra"]
CMD ["dashboard", "--host", "0.0.0.0", "--no-open"]
