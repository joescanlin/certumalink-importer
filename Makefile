# Certuma Reach developer workflow (Phase 0 task C2).
# Two packages: certuma_core (pure, stdlib) and certuma (app: SQLAlchemy/Alembic).

VENV ?= .venv
PY := $(VENV)/bin/python
ALEMBIC := $(VENV)/bin/alembic
export CERTUMA_DATABASE_URL ?= postgresql+psycopg://certuma:certuma@localhost:55433/certuma

.PHONY: venv db-up db-wait db-down db-reset migrate downgrade db-shell test test-core test-db all-tests clean

venv:                ## create the app venv and install deps
	python3 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet "SQLAlchemy>=2.0,<2.1" "alembic>=1.13" "psycopg[binary]>=3.1"

db-up:               ## start the local Postgres container
	docker compose up -d
	$(MAKE) db-wait

db-wait:             ## block until Postgres is accepting connections
	@echo "waiting for postgres..."
	@until docker compose exec -T db pg_isready -U certuma -d certuma >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready"

db-down:             ## stop the container (keep data volume)
	docker compose down

db-reset:            ## drop the container AND its data volume, then recreate
	docker compose down -v
	$(MAKE) db-up

migrate:             ## apply all migrations
	$(ALEMBIC) upgrade head

downgrade:           ## roll all migrations back to base
	$(ALEMBIC) downgrade base

db-shell:            ## psql into the running container
	docker compose exec db psql -U certuma -d certuma

test-core:           ## pure-library + golden parity tests (no DB needed)
	PYTHONPATH=.:src python3 -m unittest discover -s tests/golden -p "test_*.py"

test:                ## existing + golden suites (no DB needed)
	PYTHONPATH=.:src python3 -m unittest discover -s tests -p "test_*.py"

test-db:             ## schema/migration tests (needs db-up + migrate)
	PYTHONPATH=.:src $(PY) -m unittest discover -s tests/db -p "test_*.py"

all-tests: db-up migrate test test-db  ## everything, against a live DB

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
