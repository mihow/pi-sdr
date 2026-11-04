# sdrscan image for the Raspberry Pi 5 (linux/arm64), built on an x86 host with
#   docker buildx build --platform linux/arm64 --target test -t sdrscan:arm64-test --load .
# Stages: wheels (compiles webrtcvad, which has no aarch64 wheel), base, test, runtime.
#   docker buildx build --platform linux/arm64 --target runtime -t sdrscan:arm64 --load .

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
COPY deploy ./deploy
# Under qemu everything runs 20 to 40 times slower; wall-clock budget asserts are skipped.
ENV SDRSCAN_SLOW_HOST=1
CMD ["python", "-m", "pytest", "-q", "-m", "not slow and not browser and not fixtures and not hw"]

FROM base AS runtime
# Runs as a system user. The host passes the dongle with --device and adds the group that
# owns it (plugdev on Raspberry Pi OS) with --group-add; see deploy/sdrscan.service.
RUN useradd --system --home /var/lib/sdrscan --shell /usr/sbin/nologin sdrscan \
 && mkdir -p /var/lib/sdrscan /run/sdrscan /etc/sdrscan \
 && chown sdrscan:sdrscan /var/lib/sdrscan /run/sdrscan \
 && rm -rf /src
WORKDIR /var/lib/sdrscan
USER sdrscan
VOLUME ["/var/lib/sdrscan"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD ["sdrscan", "health", "--max-age-s", "180"]
ENTRYPOINT ["tini", "--", "sdrscan"]
CMD ["run", "--config", "/etc/sdrscan/config.yaml", "--source", "rtl"]
