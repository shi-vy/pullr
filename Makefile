BRANCH ?= main

# Extract media UID/GID from config.yaml if it exists
CONFIG_FILE := config/config.yaml

ifneq (,$(wildcard $(CONFIG_FILE)))
    MEDIA_UID := $(shell grep '^media_uid:' $(CONFIG_FILE) 2>/dev/null | awk '{print $$2}' || echo 1000)
    MEDIA_GID := $(shell grep '^media_gid:' $(CONFIG_FILE) 2>/dev/null | awk '{print $$2}' || echo 1000)
else
    MEDIA_UID := 1000
    MEDIA_GID := 1000
endif

export MEDIA_UID
export MEDIA_GID

build:
	@echo "Building with MEDIA_UID=$(MEDIA_UID) MEDIA_GID=$(MEDIA_GID)"
	docker compose build --no-cache \
		--build-arg BRANCH=$(BRANCH) \
		--build-arg MEDIA_UID=$(MEDIA_UID) \
		--build-arg MEDIA_GID=$(MEDIA_GID)

up:
	@echo "Starting with user $(MEDIA_UID):$(MEDIA_GID)"
	docker compose up -d

dev: build up
	@echo "✓ Started pullr:$(BRANCH) as user $(MEDIA_UID):$(MEDIA_GID)"

down:
	docker compose down

stop:
	docker compose down

show-config:
	@echo "BRANCH=$(BRANCH)"
	@echo "MEDIA_UID=$(MEDIA_UID)"
	@echo "MEDIA_GID=$(MEDIA_GID)"