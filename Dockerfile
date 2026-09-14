# Test environment for the hardware-free suite. Run it through tests/docker.sh.
#
# Python and git, nothing else. Capture is standard library only, so this image shows the
# suite needs nothing the host happens to have installed. git is here because session
# manifests record commits.
# Pinned by digest (Python 3.12.14 when pinned), so a rebuild months from now is the same image.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /src
