# meertex: convenience targets.
#
# `make up` runs the whole stack in Docker: Postgres and the server. `make dev`
# runs the server natively instead, with --reload, against the same Postgres.
# That is the faster loop for working on it. meerpad's Makefile, trimmed.

COMPOSE ?= docker compose
VENV    ?= .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
VERSION := $(shell cat VERSION)

# The ports docker-compose.yml publishes. Read from the environment (or the
# command line) so `MEERTEX_PORT=9000 make up` and `make dev` agree.
MEERTEX_PORT    ?= 8060
MEERTEX_DB_PORT ?= 5436

.PHONY: help config up down logs build infra psql dev compile-dev venv test-db test lint images \
        cli install-skill editor

help:
	@echo "meertex $(VERSION):"
	@echo "  make up            - build + run the stack (postgres + server) -> http://127.0.0.1:$(MEERTEX_PORT)"
	@echo "  make down          - stop it"
	@echo "  make logs          - tail the server (sign-in links land here when SMTP_HOST is empty)"
	@echo "  make psql          - a shell on the database"
	@echo "  make infra         - run only postgres (for native development)"
	@echo "  make dev           - run the server natively with --reload on :$(MEERTEX_PORT) (needs venv; stop 'make up' first)"
	@echo "  make compile-dev   - run the compile service natively on :8070 with this machine's TeX Live"
	@echo "  make venv          - create $(VENV) with the server's and the CLI's dependencies, pytest and ruff"
	@echo "  make test          - run the test suite (make test-db first)"
	@echo "  make lint          - ruff over app, cli and tests"
	@echo "  make images        - build meertex-server:$(VERSION)"
	@echo "  make editor        - rebuild app/static/vendor/editor/editor.js from tools/editor (needs node)"
	@echo "  make cli           - install the meertex CLI with pipx (from ./cli)"
	@echo "  make install-skill - link the new-paper skill into ~/.claude/skills"

config:
	@test -f .env || { cp .env.example .env && chmod 600 .env \
	  && echo "wrote .env from .env.example; SMTP and the rest are optional on a laptop"; }

up: config
	MEERTEX_VERSION=$(VERSION) $(COMPOSE) up --build -d
	@echo "meertex on http://127.0.0.1:$(MEERTEX_PORT)"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f web

build:
	MEERTEX_VERSION=$(VERSION) $(COMPOSE) build

# --wait: until the healthcheck passes, so a `test-db` or `dev` straight after
# does not race a database that is still initialising.
infra:
	$(COMPOSE) up -d --wait db

psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-meertex} -d $${POSTGRES_DB:-meertex}

# --- native development ------------------------------------------------------

venv:
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt pytest httpx ruff
	$(PIP) install -q -e ./cli
	@echo "ready: $(VENV)"

dev: infra
	$(VENV)/bin/uvicorn app.main:app --reload --port $(MEERTEX_PORT) --ws-max-size 100000000

# The compile service with the system's TeX Live (latexmk, synctex, latexdiff),
# for `make dev`. The token is .env's COMPILE_TOKEN, which `make dev` sends.
compile-dev:
	@token=$$(sed -n 's/^COMPILE_TOKEN=//p' .env 2>/dev/null | tail -n 1); \
	COMPILE_PORT=8070 COMPILE_TOKEN=$${COMPILE_TOKEN:-$${token:-dev-compile-token}} \
	  python3 -I compile/service.py

# A database of its own, dropped and rebuilt by the tests. Never the one your
# papers are in: every test starts by emptying every table.
TEST_DB ?= postgresql+psycopg://meertex:meertex@127.0.0.1:$(MEERTEX_DB_PORT)/meertex_test

test-db: infra
	$(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-meertex} -d postgres \
	  -c "DROP DATABASE IF EXISTS meertex_test" \
	  -c "CREATE DATABASE meertex_test"

# The server's tests and the CLI's (whose e2e test starts a server on :8062).
test:
	MEERTEX_TEST_DB=$(TEST_DB) $(VENV)/bin/pytest -q -ra tests cli/tests

lint:
	$(VENV)/bin/ruff check app cli tests

images:
	docker build --build-arg MEERTEX_VERSION=$(VERSION) -t meertex-server:$(VERSION) .

# The browser editor's one vendored module (yjs, y-websocket, CodeMirror). The
# result is committed, so the server and its image never need node.
editor:
	cd tools/editor && npm ci && node build.mjs

# --- the CLI and the skill ---------------------------------------------------

cli:
	pipx install --force ./cli

# A symlink, so the skill follows this checkout. Remove it with
#   rm ~/.claude/skills/meertex-new-paper
install-skill:
	mkdir -p $(HOME)/.claude/skills
	ln -sfn $(CURDIR)/skills/meertex-new-paper $(HOME)/.claude/skills/meertex-new-paper
	@echo "linked ~/.claude/skills/meertex-new-paper -> $(CURDIR)/skills/meertex-new-paper"
