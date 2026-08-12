FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/submission

# pyqpanda links against libcurl.so.4
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/submission

# LoomQ's core has no third-party dependencies, so this layer cannot fail.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Vendor SDKs are optional accelerators. If a wheel is unavailable for this
# platform the image still builds and LoomQ runs on its reference simulator,
# reporting the reason in every result's `meta`. A submission that refuses to
# build because a third-party wheel moved is a submission that scores nothing.
COPY requirements-backends.txt .
RUN pip install --no-cache-dir -r requirements-backends.txt \
    || echo "[loomq] vendor SDKs unavailable; using the built-in reference simulator"

COPY . .

# Public contract self-check. `python -m loomq selftest` runs the deeper suite:
# every circuit through every target IR and back, plus randomised L3 programs.
CMD ["python", "evaluator.py", "--json-out", "/tmp/loomq-public-report.json"]
