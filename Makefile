BRANCH ?= main
COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")

build:
	docker compose build --build-arg BRANCH=$(BRANCH) --build-arg COMMIT=$(COMMIT)

up:
	docker compose up -d

down:
	docker compose down
