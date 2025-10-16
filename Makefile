BRANCH ?= main

build:
	docker compose build --no-cache \
		--build-arg BRANCH=$(BRANCH)

up:
	docker compose up -d

dev: build up
	@echo "✓ Started pullr:$(BRANCH)"

down:
	docker compose down

stop:
	docker compose down