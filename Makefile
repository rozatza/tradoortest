# Bootstrap (venv + npm ci) is part of `make test`, so a clean checkout
# and CI both run the same command.

PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
STAMP_PY := $(VENV)/.install-stamp
STAMP_WEB := web/node_modules/.install-stamp

.PHONY: help test lint test-service test-web install

help:
	@echo "make test    install deps if needed, then ruff, pytest, and the web check"
	@echo "make install create .venv and install web dependencies"

install: $(STAMP_PY) $(STAMP_WEB)

$(STAMP_PY): service/pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e "./service[dev]"
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
	npm run build --prefix web

test: lint test-service test-web
