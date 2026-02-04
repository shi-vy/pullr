FROM python:3.14-slim

ARG BRANCH=main
ARG REPO_URL=https://github.com/shi-vy/pullr.git
ARG MEDIA_UID=1000
ARG MEDIA_GID=1000

WORKDIR /app

# Install git and 7zip for archive extraction
RUN apt-get update && \
    apt-get install -y git 7zip && \
    rm -rf /var/lib/apt/lists/*

# Create media group and user with configurable UID/GID
RUN groupadd -g ${MEDIA_GID} media && \
    useradd -u ${MEDIA_UID} -g media -m -s /bin/bash media

# Clone the branch
RUN git clone --depth 1 --branch ${BRANCH} ${REPO_URL} /app

# Calculate commit and save to file
RUN git rev-parse --short HEAD > /app/commit.txt && \
    echo ${BRANCH} > /app/branch.txt

RUN pip install --no-cache-dir -r requirements.txt

# Create directories and set ownership
RUN mkdir -p /downloads /media /logs && \
    chown -R media:media /app /downloads /media /logs

# Switch to media user
USER media

EXPOSE 8080
CMD ["python", "app/main.py"]