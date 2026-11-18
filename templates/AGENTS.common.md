<!-- BEGIN AGENTIC-KIT -->
# Conventions Agentic Cockpit V2 — Projet `__PROJECT_ID__`

1. **Zéro exploration inutile** : lis uniquement `.nexus_brief.md` dans ton worktree de ticket.
2. **Périmètre strict** : n'écris que dans les fichiers déclarés dans `FILES_OWNED` (vérifié par Porte 0).
3. **Protection des secrets (R7)** : ne lis jamais `.env`, `secrets/` ni `*.key`.
4. **Isolation Git** : ne commite jamais sur `main`. Travaille uniquement dans `<WT_TICKETS_DIR>/T-xxx`.
<!-- END AGENTIC-KIT -->

## Section spécifique au projet `__PROJECT_ID__`
- Racine du dépôt : `__PROJECT_ROOT__`
- Commandes de validation : `make lint` et `make test`
