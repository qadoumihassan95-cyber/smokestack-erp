# PFS backend — local developer runner that mirrors the CI gate
# (.github/workflows/ci.yml). Reproducing `make ci` green locally should mean a
# green pipeline on push. See docs/ENGINEERING_PIPELINE.md.
#
# Usage: run from backend/  ->  make help
.DEFAULT_GOAL := help
SHELL := /bin/bash

# Keep this list identical to the --ignore-vuln flags in ci.yml (deps-audit job).
PIP_AUDIT_IGNORES := \
	--ignore-vuln PYSEC-2024-232 --ignore-vuln PYSEC-2024-233 --ignore-vuln PYSEC-2025-185 \
	--ignore-vuln PYSEC-2026-161 --ignore-vuln PYSEC-2026-248 --ignore-vuln PYSEC-2026-249 \
	--ignore-vuln PYSEC-2026-1942 --ignore-vuln PYSEC-2026-1941 --ignore-vuln PYSEC-2026-2281 \
	--ignore-vuln PYSEC-2026-2280 --ignore-vuln PYSEC-2026-1325

.PHONY: help install lint format-check security audit test heads migrate ci clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime + dev (test) dependencies
	pip install -r requirements.txt -r requirements-dev.txt
	pip install "ruff==0.16.*" "bandit==1.7.*" "pip-audit==2.*"

lint:  ## ruff bug-rules (blocking) + format check (advisory, matches CI)
	ruff check .
	ruff format --check --diff . || echo "::warning:: formatting drift (advisory)"

security:  ## bandit medium+ security scan (blocking, matches CI)
	bandit -q -r app -ll -c pyproject.toml

audit:  ## pip-audit with the tracked ignore-list (identical to CI)
	pip-audit -r requirements.txt --strict $(PIP_AUDIT_IGNORES)

test:  ## Full pytest suite (SQLite) — mirrors the unit-tests job
	pytest -q

heads:  ## Assert exactly one Alembic head (matches CI)
	@n=$$(alembic heads | grep -c '(head)'); echo "heads=$$n"; test "$$n" -eq 1

migrate:  ## alembic upgrade head against $$DATABASE_URL (prod preDeploy mirror)
	alembic upgrade head

ci: lint security audit test heads  ## Run the full local gate (reproduce CI before pushing)
	@echo "Local CI gate passed — safe to push."

clean:  ## Remove local test artifacts
	rm -rf .pytest_cache **/__pycache__ *.db /tmp/smokestack_*.db
