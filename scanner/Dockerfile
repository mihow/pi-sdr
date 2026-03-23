FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir "setuptools<82" && pip install --no-cache-dir -r requirements.txt

# Copy as a proper Python package
RUN mkdir -p scanner
COPY *.py scanner/

EXPOSE 8080

ENTRYPOINT ["python", "-m", "scanner"]
