FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-numpy \
    python3-soapysdr soapysdr-module-rtlsdr \
    gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv --system-site-packages /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy as a proper Python package
RUN mkdir -p scanner
COPY *.py scanner/

EXPOSE 8080

ENTRYPOINT ["python3", "-m", "scanner"]
