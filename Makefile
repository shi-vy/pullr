BRANCH ?= main

build:
	$(eval COMMIT := $(shell git rev-parse --short $(BRANCH) 2>/dev/null || echo "unknown"))
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