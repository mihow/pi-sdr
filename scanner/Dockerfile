# sdrscan image for the Raspberry Pi 5 (linux/arm64), built on an x86 host with
#   docker buildx build --platform linux/arm64 --target test -t sdrscan:arm64-test --load .
# Stages: wheels (compiles webrtcvad, which has no aarch64 wheel), base, test, runtime.
#   docker buildx build --platform linux/arm64 --target runtime -t sdrscan:arm64 --load .

ARG DEBIAN=debian:trixie-slim
# The runtime image comes with or without speech-to-text. The default carries whisper.cpp;
# a scanner-only image for a capped link is built with --build-arg RUNTIME_BASE=base.
ARG RUNTIME_BASE=base-stt

FROM ${DEBIAN} AS wheels
# webrtcvad ships no aarch64 wheel, so it is compiled here and only the wheel moves on.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential python3-dev python3-pip \
 && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-deps --wheel-dir /wheels webrtcvad==2.0.10 pyrtlsdr==0.3.0

FROM ${DEBIAN} AS whisper
# whisper.cpp is not packaged in Debian. Build whisper-cli statically for the Pi 5's
# Cortex-A76 (armv8.2-a with dotprod and fp16), without OpenMP, so the runtime needs no
# extra libraries. The model is pinned by checksum.
ARG WHISPER_VERSION=v1.7.4
ARG WHISPER_SHA256=9ce7b33028793fcbf62f81f1fd087af7778dace8772eaba8c43c66bf0c8a3eed
ARG MODEL=ggml-base.en-q5_1.bin
ARG MODEL_SHA256=4baf70dd0d7c4247ba2b81fafd9c01005ac77c2f9ef064e00dcf195d0e2fdd2f
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake \
 && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:${WHISPER_SHA256} \
    https://github.com/ggml-org/whisper.cpp/archive/refs/tags/${WHISPER_VERSION}.tar.gz /tmp/w.tgz
RUN mkdir /w && tar -xzf /tmp/w.tgz -C /w --strip-components 1 \
 && cmake -S /w -B /w/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
      -DGGML_NATIVE=OFF -DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16 -DGGML_OPENMP=OFF \
      -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF \
 && cmake --build /w/build -j"$(nproc)" --target whisper-cli \
 && install -D -m 0755 /w/build/bin/whisper-cli /out/bin/whisper-cli \
 && install -D -m 0644 /w/samples/jfk.wav /out/samples/jfk.wav
ADD --checksum=sha256:${MODEL_SHA256} \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${MODEL} /out/models/${MODEL}

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

# Speech-to-text: whisper-cli and the base.en model (about 60 MB), used by stt.engine
# whisper_cpp. config.template.yaml points at these paths.
FROM base AS base-stt
COPY --from=whisper --chmod=0755 /out/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=whisper --chmod=0644 /out/models/ /usr/share/sdrscan/models/

FROM base-stt AS test
RUN apt-get update && apt-get install -y --no-install-recommends python3-pytest \
 && rm -rf /var/lib/apt/lists/*
COPY tests ./tests
COPY deploy ./deploy
# A real speech sample for the one whisper-cli transcription test (SDRSCAN_WHISPER_BIN).
COPY --from=whisper --chmod=0644 /out/samples/jfk.wav /usr/share/sdrscan/samples/jfk.wav
ENV SDRSCAN_WHISPER_BIN=/usr/local/bin/whisper-cli \
    SDRSCAN_WHISPER_MODEL=/usr/share/sdrscan/models/ggml-base.en-q5_1.bin \
    SDRSCAN_WHISPER_SAMPLE=/usr/share/sdrscan/samples/jfk.wav
# Under qemu everything runs 20 to 40 times slower; wall-clock budget asserts are skipped.
ENV SDRSCAN_SLOW_HOST=1
CMD ["python", "-m", "pytest", "-q", "-m", "not slow and not browser and not fixtures and not hw"]

FROM ${RUNTIME_BASE} AS runtime
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
