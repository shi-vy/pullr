BRANCH ?= main

build:
	@COMMIT=$$(git rev-parse --short $(BRANCH) 2>/dev/null || echo "unknown"); \
	echo "Building branch $(BRANCH) at commit $$COMMIT"; \
	docker compose build \
		--build-arg BRANCH=$(BRANCH) \
		--build-arg COMMIT=$$COMMIT

up:
	docker compose up -d

dev: build up
	@echo "✓ Started pullr:$(BRANCH)"

down:
	docker compose down

stop:
	docker compose down