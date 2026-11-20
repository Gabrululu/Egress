PYTHON ?= python3.11
VENV   := .venv
BIN    := $(VENV)/bin
SCAN_PATH ?= sample_app
IMAGE  ?= egress:local

.PHONY: install run scan test clean docker-build docker-run demo-check demo-warm demo-reset

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

# ---- demo rehearsal ---------------------------------------------------------------------
demo-check:
	$(BIN)/python -m copilot.demo check

demo-warm:
	$(BIN)/python -m copilot.demo warm

# Destructive (stack volumes + output/). Usage: make demo-reset CONFIRM=yes
demo-reset:
	$(BIN)/python -m copilot.demo reset $(if $(filter yes,$(CONFIRM)),--yes,)

# ---- containerised Egress (host needs only Docker) ---------------------------------------
docker-build:
	docker build -t $(IMAGE) .

# Host network: UI on :8501 and the smoke test reaches the stack on localhost.
# output/ is mounted at the SAME path so compose bind mounts resolve on the host daemon.
# Mounting the Docker socket grants the container control of the host's Docker: local use only.
docker-run:
	mkdir -p output
	docker run --rm -it --name egress --network host \
	  --user $$(id -u):$$(id -g) --group-add $$(stat -c %g /var/run/docker.sock) \
	  -v /var/run/docker.sock:/var/run/docker.sock \
	  -v $(CURDIR)/output:$(CURDIR)/output -e EGRESS_OUTPUT_DIR=$(CURDIR)/output \
	  $$( [ -f .env ] && echo --env-file .env ) \
	  $(IMAGE)
