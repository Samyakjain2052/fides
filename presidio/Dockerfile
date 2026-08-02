FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Long timeout + retries so slow/unstable connections don't kill the build
# mid-download (pip's default 15s read timeout is too short for large files).
ENV PIP_DEFAULT_TIMEOUT=1000 \
    PIP_RETRIES=10

RUN pip install --no-cache-dir -r requirements.txt

# The connection is slow/unstable and drops mid-transfer, which makes pip's
# retry (a full restart) impractical for a 587MB file. Use curl with resume
# support (-C -) in a retry loop so each reconnect continues where it left off.
RUN url="https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.7.1/en_core_web_lg-3.7.1-py3-none-any.whl"; \
    out="/tmp/en_core_web_lg-3.7.1-py3-none-any.whl"; \
    for i in $(seq 1 50); do \
        curl -fL --retry 5 --retry-delay 5 -C - -o "$out" "$url" && break; \
        echo "download attempt $i failed, resuming..."; \
    done \
    && pip install --no-cache-dir "$out" \
    && rm -f "$out"

COPY recognizers.py .
COPY main.py .

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
