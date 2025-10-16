# syntax=docker/dockerfile:1
FROM python:3.13-slim

ARG BRANCH=main
ARG COMMIT=unknown
ENV BRANCH=${BRANCH}
ENV COMMIT=${COMMIT}
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install required tools
RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*

# Copy everything (including .git if available)
COPY . /app

# If .git exists, checkout branch and get the commit hash
RUN if [ -d .git ]; then \
      echo "Checking out branch ${BRANCH}" && \
      git fetch --all && \
      git checkout ${BRANCH} || echo "Branch ${BRANCH} not found, using current branch"; \
      COMMIT=$(git rev-parse --short HEAD) && \
      echo "COMMIT=${COMMIT}" > /tmp/commit.txt; \
    else \
      echo "No .git directory found — skipping checkout."; \
    fi

RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /downloads /media /logs

EXPOSE 8080
CMD ["python", "app/main.py"]