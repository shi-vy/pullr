FROM python:3.13-slim

ARG BRANCH=main
ARG REPO_URL=https://github.com/shi-vy/pullr.git

WORKDIR /app

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Clone the branch
RUN git clone --depth 1 --branch ${BRANCH} ${REPO_URL} /app

# Calculate commit and save to file
RUN git rev-parse --short HEAD > /app/commit.txt && \
    echo ${BRANCH} > /app/branch.txt

RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /downloads /media /logs

EXPOSE 8080
CMD ["python", "app/main.py"]