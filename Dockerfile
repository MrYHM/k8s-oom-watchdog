# The base image is multi-arch. Releases are published for linux/amd64 and
# linux/arm64 by .github/workflows/publish-image.yml; a plain `docker build`
# here just produces an image for whatever platform you are on.
FROM python:3.12-slim

WORKDIR /app

# Dependencies come from pyproject.toml so the pinned version lives in exactly
# one place. Only the declared dependency is installed, not the project itself:
# watchdog.py is copied in below and run as a script, so nothing here needs a
# build backend. tomllib is standard library from 3.11.
#
# The pin itself matters: watchdog.py relies on the resize-subresource support
# that the kubernetes client generates from the K8s >= 1.33 OpenAPI spec.
COPY pyproject.toml ./
RUN pip install --no-cache-dir $(python3 -c \
      "import tomllib; print(' '.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))")

COPY watchdog.py /app/watchdog.py

# Host cgroup/meminfo files are world-readable; root is not needed.
RUN useradd --system --no-create-home --uid 10001 watchdog
USER 10001

ENTRYPOINT ["python3", "-u", "/app/watchdog.py"]
