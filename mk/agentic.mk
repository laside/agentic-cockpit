# Agentic Cockpit V2 — Noyau Makefile Générique Multi-Projets (mk/agentic.mk)
SHELL := /bin/bash
COCKPIT_HOME ?= $(HOME)/agentic-cockpit
PROJECT ?= mon-app

.PHONY: cockpit roadmap worktree commit land

cockpit: ## Ouvre ou bascule sur la session Zellij du projet (cockpit-<project>)
	@$(COCKPIT_HOME)/bin/cockpit open $(PROJECT)

roadmap: ## Affiche la Roadmap PMO, le Chemin Critique et les Quotas (SQLite, 0 token)
	@$(COCKPIT_HOME)/bin/cockpit roadmap $(PROJECT)

worktree: ## Crée un worktree isolé pour le ticket T=T-xxx avec son lien .agents propre au projet
	@test -n "$$T" || { echo "Usage: make worktree T=T-001"; exit 1; }
	@source $(COCKPIT_HOME)/scripts/cockpit-env.sh && \
	  wt="$$WT_TICKETS_DIR/$$T" && \
	  git worktree add "$$wt" -b "feat/$$T" && \
	  ln -s "$$PROJECT_ROOT/.agents" "$$wt/.agents" && \
	  echo "OK worktree : $$wt (lié à $$PROJECT_ROOT/.agents)"

commit: ## Indexe et commite en vérifiant l'absence de secrets (.env, secrets/)
	@$(COCKPIT_HOME)/scripts/git-relay.sh commit M="$$M" F="$$F"

land: ## Nettoie le worktree fusionné T=T-xxx et débloque les tickets dépendants dans SQLite
	@$(COCKPIT_HOME)/scripts/git-relay.sh land T="$$T"
