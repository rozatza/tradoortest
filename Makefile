# Bootstrap (venv + npm ci) is part of `make test`, so a clean checkout
# and CI both run the same command.

PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
STAMP_PY := $(VENV)/.install-stamp
STAMP_WEB := web/node_modules/.install-stamp

.PHONY: help test lint test-service test-web install

help:
	@echo "make test    install deps if needed, then ruff, pytest, and the web checks"
	@echo "make install create .venv and install web dependencies"

install: $(STAMP_PY) $(STAMP_WEB)

# service/requirements.lock is a hashed pin of service/pyproject.toml.
# Regenerate with:
#   pip-compile --generate-hashes --allow-unsafe --strip-extras --extra=dev \
#     --output-file=service/requirements.lock service/pyproject.toml
$(STAMP_PY): service/pyproject.toml service/requirements.lock
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -U pip
	$(PY) -m pip install --require-hashes -r service/requirements.lock
	$(PY) -m pip install --no-deps --no-build-isolation -e "./service"
	touch $(STAMP_PY)

$(STAMP_WEB): web/package-lock.json
	npm ci --prefix web
	touch $(STAMP_WEB)

lint: $(STAMP_PY)
	cd service && ../$(PY) -m ruff check .

test-service: $(STAMP_PY)
	cd service && ../$(PY) -m pytest -q --tb=short

test-web: $(STAMP_WEB)
	npm run lint --prefix web
	npm test --prefix web
	npm run build --prefix web

test: lint test-service test-web
