PYTHON ?= python3.11
VENV   := .venv
BIN    := $(VENV)/bin
SCAN_PATH ?= sample_app

.PHONY: install run scan test clean

install:
	$(PYTHON) -m venv $(VENV)
	PIP_NO_CACHE_DIR=1 $(BIN)/pip install -q --upgrade pip
	PIP_NO_CACHE_DIR=1 $(BIN)/pip install -q -r requirements.txt
	@echo "OK: $$($(BIN)/python --version) in $(VENV)"

run:
	$(BIN)/streamlit run app.py --server.port 8501 --server.headless true

scan:
	$(BIN)/python -m scanner $(SCAN_PATH)

test:
	$(BIN)/python -m pytest -q

clean:
	rm -rf $(VENV) .pytest_cache
	find . -path ./$(VENV) -prune -o -name __pycache__ -type d -prune -exec rm -rf {} +
	find output -mindepth 1 ! -name .gitkeep -delete
