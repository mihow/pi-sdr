FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    python3-numpy python3-scipy \
    python3-soapysdr soapysdr-module-rtlsdr \
    libusb-1.0-0 \
    gcc python3-dev && \
    rm -rf /var/lib/apt/lists/* && \
    echo "/usr/local/lib" >> /etc/ld.so.conf.d/local.conf

# SDRplay API v3 — proprietary daemon + library + SoapySDR module.
# These files are copied from the host's SDRplay API installation.
# sdrplay_apiService runs inside the container (started by entrypoint.sh).
COPY sdrplay-api/sdrplay_apiService /opt/sdrplay/sdrplay_apiService
COPY sdrplay-api/libsdrplay_api.so.3.15 /usr/local/lib/libsdrplay_api.so.3.15
RUN ln -sf libsdrplay_api.so.3.15 /usr/local/lib/libsdrplay_api.so.3 && \
    ln -sf libsdrplay_api.so.3 /usr/local/lib/libsdrplay_api.so
COPY sdrplay-api/libsdrPlaySupport.so /usr/lib/x86_64-linux-gnu/SoapySDR/modules0.8/libsdrPlaySupport.so
RUN ldconfig

WORKDIR /app

RUN python3 -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy as a proper Python package
RUN mkdir -p scanner
COPY *.py scanner/
COPY entrypoint.sh /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
