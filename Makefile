BRANCH ?= main
COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")

build:
	docker compose build \
		--build-arg BRANCH=$(BRANCH) \
		--build-arg COMMIT=$(COMMIT)

up:
	docker compose up -d

dev: build up
	@echo "✓ Started pullr:$(BRANCH):$(COMMIT)"

down:
	docker compose down

stop:
	docker compose down