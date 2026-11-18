<!-- BEGIN AGENTIC-KIT -->
# Conventions Agentic Cockpit V2 — Projet `__PROJECT_ID__`

1. **Zéro exploration inutile** : lis uniquement `.nexus_brief.md` dans ton worktree de ticket.
2. **Périmètre strict** : n'écris que dans les fichiers déclarés dans `FILES_OWNED` (vérifié par Porte 0).
3. **Protection des secrets (R7)** : ne lis jamais `.env`, `secrets/` ni `*.key`.
4. **Isolation Git** : ne commite jamais sur `main`. Travaille uniquement dans `<WT_TICKETS_DIR>/T-xxx`.

## Travail à plusieurs CLI
- Aucune messagerie entre agents : lis l'état partagé avec `cockpit status __PROJECT_ID__`.
- Chef de chantier (agy, DeepSeek) : `cockpit claim T-xxx __PROJECT_ID__ --as <agy|deepseek>`, travaille dans le WORKTREE affiché, puis `cockpit submit T-xxx __PROJECT_ID__`.
- Délègue une partie : commite le chantier, puis `cockpit ticket add __PROJECT_ID__ "titre" --parent T-xxx --files ... --accept ...` (fichiers pris dans ceux du chantier, 4 sous-tickets ouverts au plus), puis `cockpit run T-yyy __PROJECT_ID__ [--lane <voie>]` ou `cockpit auto __PROJECT_ID__ --parent T-xxx --auto-merge` ; choisis la voie la moins chère qui suffit.
- Intègre un sous-ticket : `cockpit merge T-yyy __PROJECT_ID__`.
- Signale un changement d'interface : `cockpit note T-xxx __PROJECT_ID__ "..."`.
- Seul l'architecte (Claude) fusionne un chantier dans la branche principale.
<!-- END AGENTIC-KIT -->

## Section spécifique au projet `__PROJECT_ID__`
- Racine du dépôt : `__PROJECT_ROOT__`
- Commandes de validation : `make lint` et `make test`
