.PHONY: install lint type test check run seed demo

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check returnproof tests

type:
	mypy returnproof

test:
	pytest

check: lint type test

run:
	uvicorn returnproof.app:app --reload --port 8000

seed:
	python -m returnproof.seed

demo:
	python -m returnproof.demo --reset

api-key:
	python -m returnproof.apikey_cli --email owner@nirvaanretail.example --name local-dev
