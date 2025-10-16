BRANCH ?= main
COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")

build:
	BRANCH=$(BRANCH) COMMIT=$(COMMIT) docker compose build \
		--build-arg BRANCH=$(BRANCH) \
		--build-arg COMMIT=$(COMMIT)

up:
	docker compose up -d

down:
	docker compose down

# New: convenience target to build and start a branch
dev:
	$(MAKE) build BRANCH=$(BRANCH)
	$(MAKE) up