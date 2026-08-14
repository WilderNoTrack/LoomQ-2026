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

# Vendor SDKs are optional accelerators. If a wheel is unavailable the image
# still builds and LoomQ runs on its reference simulator, reporting the reason
# in every result's `meta`. A submission that refuses to build because a
# third-party wheel moved is a submission that scores nothing.
#
# spinqit gets its own virtualenv because it pins antlr4-python3-runtime==4.9.2
# while amazon-braket's simulator pins ==4.13.2 — installing all three together
# fails to resolve and leaves none of them installed. Details in the two
# requirements files.
COPY requirements-backends.txt requirements-spinq.txt ./
RUN pip install --no-cache-dir -r requirements-backends.txt \
    || echo "[loomq] pyqpanda/braket unavailable; falling back to the reference simulator"
RUN python -m venv /opt/spinq \
    && /opt/spinq/bin/pip install --no-cache-dir -r requirements-spinq.txt \
    || echo "[loomq] spinqit unavailable; falling back to the reference simulator"

# Without this, `import spinqit` fails in LoomQ's own interpreter and the spinq
# target would silently fall back to the reference simulator *inside the image
# built to run it on spinqit*. loomq/backends/spinq_backend.py runs
# loomq/backends/spinq_runner.py here instead when the in-process import fails.
ENV LOOMQ_SPINQ_PYTHON=/opt/spinq/bin/python

COPY . .

# Public contract self-check. `python -m loomq selftest` runs the deeper suite:
# every circuit through every target IR and back, plus randomised L3 programs.
CMD ["python", "evaluator.py", "--json-out", "/tmp/loomq-public-report.json"]
