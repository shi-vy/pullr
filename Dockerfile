# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install curl for debugging (optional)
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy dependencies and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source and config
COPY app ./app
COPY config ./config

# Create runtime dirs (will be mounted at runtime)
RUN mkdir -p /downloads /media /logs

EXPOSE 8080

CMD ["python", "app/main.py"]
