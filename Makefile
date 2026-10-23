PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: setup run test doctor fixtures clean

setup:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e '.[dev]'

run:
	$(PY) -m bili_audio

test:
	$(PY) -m pytest -q

doctor:
	$(PY) -m bili_audio doctor

fixtures:
	$(PY) scripts/update_fixtures.py

clean:
	rm -rf output/.work .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
