.PHONY: up migrate test down shell build

up:
	docker compose up -d

migrate:
	docker compose run --rm web python manage.py migrate

test:
	docker compose run --rm web python manage.py test

down:
	docker compose down

build:
	docker compose build

shell:
	docker compose run --rm web /bin/bash
