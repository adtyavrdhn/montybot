# A Linux machine for the tests that need bwrap (the CPython tier, #6, and the jailed Servo, #18), as the server has
# it: Ubuntu 24.04 with bubblewrap, socat and a system Python with pandas and pypdf, plus the libraries servoshell
# 0.7.0 loads (GStreamer, EGL with Mesa's software renderer, fonts). `tests/linux/run.sh` builds it and runs pytest.
FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates python3 python3-pandas python3-pypdf \
        socat libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libgstreamer-plugins-bad1.0-0 \
        libegl1 libegl-mesa0 libgl1-mesa-dri fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

# Not root, as on the server, so the jail is an unprivileged user's.
RUN useradd --create-home tester
USER tester
# The uv cache is a volume; made here so the volume belongs to `tester`.
RUN mkdir -p /home/tester/.cache/uv /home/tester/montybot
ENV UV_PROJECT_ENVIRONMENT=/home/tester/venv \
    UV_CACHE_DIR=/home/tester/.cache/uv \
    UV_LINK_MODE=copy
WORKDIR /home/tester/montybot
