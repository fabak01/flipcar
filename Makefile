# FlipCar — common commands
# Usage: make <target> [SERVER=user@host]

PYTHON := .venv/bin/python
APP    := src.main

# --- Local dev ---

.PHONY: smoke
smoke:
	PYTHONUNBUFFERED=1 $(PYTHON) -m $(APP) --smoke

.PHONY: dry-run
dry-run:
	PYTHONUNBUFFERED=1 $(PYTHON) -m $(APP) --dry-run

.PHONY: live
live:
	PYTHONUNBUFFERED=1 $(PYTHON) -m $(APP) --live

.PHONY: test
test:
	.venv/bin/python -m pytest tests/ -q

.PHONY: health
health:
	@source .env && $(PYTHON) -c "from src.main import run_health_check, print_health_check; from dotenv import load_dotenv; load_dotenv(); print_health_check(run_health_check())"

.PHONY: cookies
cookies:
	$(PYTHON) scripts/get_finn_cookies.py

.PHONY: logs
logs:
	tail -f logs/run_*.log 2>/dev/null || echo "No logs yet."

# --- Server deployment ---

SERVER ?= user@your-server
REMOTE := ~/flipcar-clean

.PHONY: deploy
deploy:
	@echo "Deploying to $(SERVER)..."
	ssh $(SERVER) "cd $(REMOTE) && git pull"
	ssh $(SERVER) "cd $(REMOTE) && .venv/bin/pip install -r requirements.txt -q"
	@echo "Deploy done. Run 'make push-cookies SERVER=$(SERVER)' if cookies expired."

.PHONY: push-cookies
push-cookies:
	bash scripts/push_cookies.sh $(SERVER) $(REMOTE)

.PHONY: push-env
push-env:
	@echo "Pushing .env to $(SERVER):$(REMOTE)/"
	scp .env $(SERVER):$(REMOTE)/.env
	@echo "Done."

.PHONY: server-setup
server-setup:
	ssh $(SERVER) "cd $(REMOTE) && bash scripts/server_setup.sh"

.PHONY: cron-install
cron-install:
	ssh $(SERVER) "cd $(REMOTE) && bash scripts/install_cron.sh"

.PHONY: server-logs
server-logs:
	ssh $(SERVER) "tail -f $(REMOTE)/logs/run_*.log"

.PHONY: server-smoke
server-smoke:
	ssh $(SERVER) "cd $(REMOTE) && PYTHONUNBUFFERED=1 .venv/bin/python -m src.main --smoke"
