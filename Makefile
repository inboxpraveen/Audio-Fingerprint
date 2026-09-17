# Convenience targets (Linux/macOS; on Windows run the commands directly).
.PHONY: install dev test test-all lint format serve doctor docker clean

install:
	pip install -e .

dev:
	pip install -e ".[dev]"
	pre-commit install || true

test:
	pytest -q

test-all:
	pytest -q -m "slow or not slow"

lint:
	ruff check fingerprint tests run.py
	ruff format --check fingerprint tests run.py

format:
	ruff format fingerprint tests run.py
	ruff check --fix fingerprint tests run.py

serve:
	audiofp serve

doctor:
	audiofp doctor

docker:
	docker build -t audiofp -f docker/Dockerfile .

clean:
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
