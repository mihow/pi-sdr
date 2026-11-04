# sdrscan image for the Raspberry Pi 5 (linux/arm64), built on an x86 host with
#   docker buildx build --platform linux/arm64 --target test -t sdrscan:arm64-test --load .
# Part A checkpoint version: enough to prove the package installs and its tests pass on
# aarch64 under qemu. Plan task B9 completes the runtime stage (healthcheck, entrypoint).

ARG DEBIAN=debian:trixie-slim

FROM ${DEBIAN} AS wheels
# webrtcvad ships no aarch64 wheel, so it is compiled here and only the wheel moves on.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential python3-dev python3-pip \
 && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-deps --wheel-dir /wheels webrtcvad==2.0.10 pyrtlsdr==0.3.0

FROM ${DEBIAN} AS base
# rtl-sdr pulls in the librtlsdr shared library (renamed librtlsdr2t64 in trixie).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-numpy python3-scipy python3-yaml \
      rtl-sdr opus-tools tini \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv --system-site-packages /opt/sdrscan
ENV PATH=/opt/sdrscan/bin:$PATH
COPY --from=wheels /wheels /tmp/wheels
RUN pip install --no-cache-dir --no-index /tmp/wheels/*.whl && rm -rf /tmp/wheels
WORKDIR /src
COPY pyproject.toml ./
COPY sdrscan ./sdrscan
# Dependencies come from Debian (numpy, scipy, yaml) and the wheels above.
RUN pip install --no-cache-dir --no-deps --no-build-isolation . 2>/dev/null \
 || (apt-get update && apt-get install -y --no-install-recommends python3-setuptools \
     && pip install --no-cache-dir --no-deps --no-build-isolation . \
     && apt-get purge -y python3-setuptools && rm -rf /var/lib/apt/lists/*)

FROM base AS test
RUN apt-get update && apt-get install -y --no-install-recommends python3-pytest \
 && rm -rf /var/lib/apt/lists/*
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q", "-m", "not slow and not browser and not fixtures and not hw"]

FROM base AS runtime
RUN useradd --system --home /var/lib/sdrscan --shell /usr/sbin/nologin sdrscan
USER sdrscan
ENTRYPOINT ["tini", "--", "python", "-c", "import sdrscan; print('sdrscan', sdrscan.__version__)"]
