.PHONY: install lint format format-check typecheck test check run clean

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .

format-check:
	$(PYTHON) -m ruff format --check .

typecheck:
	$(PYTHON) -m mypy src tests

test:
	$(PYTHON) -m pytest

check: lint format-check typecheck test

run:
	@echo "CLI not yet wired up — coming in Phase 3."

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
