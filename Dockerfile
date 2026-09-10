# The base image is multi-arch. Releases are published for linux/amd64 and
# linux/arm64 by .github/workflows/publish-image.yml; a plain `docker build`
# here just produces an image for whatever platform you are on.
FROM python:3.12-slim

WORKDIR /app

# Pinned: watchdog.py relies on the resize subresource support that the
# kubernetes client generates from the K8s >= 1.33 OpenAPI spec.
RUN pip install --no-cache-dir kubernetes==33.1.0

COPY watchdog.py /app/watchdog.py

# Host cgroup/meminfo files are world-readable; root is not needed.
RUN useradd --system --no-create-home --uid 10001 watchdog
USER 10001

ENTRYPOINT ["python3", "-u", "/app/watchdog.py"]
