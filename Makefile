.PHONY: install lint format test run dry-run check test-destinations clean

install:
	python -m pip install -e ".[dev,qdrant]"

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	pytest

run:
	youtube-knowledge-base run

dry-run:
	youtube-knowledge-base run --dry-run

check:
	youtube-knowledge-base check

test-destinations:
	youtube-knowledge-base test-destinations

clean:
	rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
