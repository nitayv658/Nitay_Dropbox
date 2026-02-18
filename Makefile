PYTHON       ?= python3
PYTEST        = $(PYTHON) -m pytest
TEST_DB_URL  ?= postgresql://postgres:password@localhost:5432/dropboxsim_test
COV_MODULES   = --cov=block_server --cov=metadata_service --cov=client_utils

.PHONY: install install-test-deps test coverage \
        test-chunker test-dedup test-conflict test-resume test-stress \
        docker-up docker-down docker-logs docker-build docker-clean \
        lint format

## ── Dependencies ─────────────────────────────────────────────────────────────

install:
	pip install -r block_server/requirements.txt
	pip install -r metadata_service/requirements.txt
	pip install -r tests/requirements.txt

install-test-deps:
	pip install -r tests/requirements.txt

## ── Tests ────────────────────────────────────────────────────────────────────

test:
	TEST_DATABASE_URL=$(TEST_DB_URL) $(PYTEST) tests/ -v

coverage:
	TEST_DATABASE_URL=$(TEST_DB_URL) $(PYTEST) tests/ $(COV_MODULES) \
		--cov-report=term-missing \
		--cov-report=html:htmlcov
	@echo "HTML report → htmlcov/index.html"

test-chunker:
	$(PYTEST) tests/test_chunker.py -v

test-dedup:
	TEST_DATABASE_URL=$(TEST_DB_URL) $(PYTEST) tests/test_deduplication.py -v

test-conflict:
	TEST_DATABASE_URL=$(TEST_DB_URL) $(PYTEST) tests/test_sync_conflict.py -v

test-resume:
	TEST_DATABASE_URL=$(TEST_DB_URL) $(PYTEST) tests/test_resumability.py -v

test-stress:
	TEST_DATABASE_URL=$(TEST_DB_URL) $(PYTEST) tests/test_sync_stress.py -v -s

## ── Docker ───────────────────────────────────────────────────────────────────

docker-build:
	docker compose build

docker-up:
	@test -f .env || (echo "ERROR: .env not found. Run: cp .env.example .env" && exit 1)
	docker compose up -d
	@echo "Block Server:     http://localhost:8001/docs"
	@echo "Metadata Service: http://localhost:8002/docs"
	@echo "Metrics:          http://localhost:8001/metrics"

docker-down:
	docker compose down

docker-clean:
	docker compose down -v

docker-logs:
	docker compose logs -f

## ── Linting ──────────────────────────────────────────────────────────────────

lint:
	$(PYTHON) -m ruff check block_server/ metadata_service/ client_utils/ tests/

format:
	$(PYTHON) -m ruff format block_server/ metadata_service/ client_utils/ tests/
