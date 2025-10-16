BRANCH ?= main

build:
	docker compose build \
		--build-arg BRANCH=$(BRANCH) \
		--build-arg COMMIT=unknown

up:
	docker compose up -d

dev: build up
	@COMMIT=$$(docker inspect pullr_$(BRANCH) --format='{{.Config.Env}}' | grep -oP 'COMMIT=\K[^,]*' || echo "unknown") && \
	echo "✓ Started pullr:$(BRANCH):$$COMMIT"

down:
	docker compose down

stop:
	docker compose down