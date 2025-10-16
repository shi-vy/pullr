BRANCH ?= main
COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")

build:
	BRANCH=$(BRANCH) COMMIT=$(COMMIT) docker compose build \
		--build-arg BRANCH=$(BRANCH) \
		--build-arg COMMIT=$(COMMIT)

up:
	BRANCH=$(BRANCH) docker compose up -d

down:
	docker compose down

# One command to build and start a specific branch
dev: build up
	@echo "✓ Started pullr:$(BRANCH)"

# Shortcut to stop everything
stop:
	docker compose down