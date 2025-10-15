# syntax=docker/dockerfile:1
FROM python:3.13-slim

ARG BRANCH=main
ARG COMMIT=unknown
ENV BRANCH=${BRANCH}
ENV COMMIT=${COMMIT}
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install git (to switch branches)
RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*

# Copy your whole repo into the image (so it can checkout branches)
COPY . /src
WORKDIR /src

# Switch to the desired branch if it exists
RUN git rev-parse --is-inside-work-tree >/dev/null 2>&1 && \
    git fetch --all && \
    git checkout ${BRANCH} || echo "Branch ${BRANCH} not found, using current branch."

# Move the desired code into /app
RUN mkdir -p /app && cp -r app config requirements.txt /app/ && mkdir -p /app/logs /downloads /media
WORKDIR /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080
CMD ["python", "app/main.py"]
