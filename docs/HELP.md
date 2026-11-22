# AGENTIC COCKPIT — AIDE

## L'onglet Commande
- Premier onglet de `cockpit open <projet>` : l'écran de pilotage des tickets, qui a le focus à l'ouverture.
- Il se lance seul avec `cockpit ui <projet>` (sans projet : le `default_project`).
- Premier lancement : `cockpit ui` crée l'environnement via `scripts/tui-setup.sh`.
- Actions personnalisées : `actions.toml` (README, section « Onglet Commande »).

## Le principe
Un ticket = une tâche bornée : fichiers autorisés (FILES_OWNED), critères, contrat.
Un worker (agent IA) le code dans un worktree isolé, sur la branche feat/T-xxx.
Les portes vérifient sans LLM : périmètre, secrets, lint, tests.
Tu relis, tu approuves, le cockpit ouvre la PR. Rien n'arrive sur main sans toi.

## Le cycle d'un ticket
DRAFT ──promote──> READY ──run──> ASSIGNED ──> GATE0 (lint+tests) ──> GATE2 (ta revue)
  ──merge --approve──> PR_OPEN ──(fusion GitHub) + sync──> MERGED
Échec répété : BLOCKED / ESCALATED.   Abandon : ABANDONED.

## Les commandes de base
  cockpit status <projet>                 où en est le projet (0 token)
  cockpit brief <projet>                  résumé de reprise en 30 lignes
  cockpit ticket list <projet>            tous les tickets
  cockpit ticket show <projet> --id T-xxx détail d'un ticket
  cockpit ticket add <projet> "Titre" --files a.py,tests/test_a.py --accept "..."
  cockpit ticket promote <projet> T-xxx   DRAFT -> READY
  cockpit run T-xxx <projet> --lane <voie> lancer un worker sur un ticket
  cockpit claim T-xxx <projet> --as claude   travailler soi-même (interactif)
  cockpit submit T-xxx <projet>           soumettre un travail interactif aux portes
  cockpit merge T-xxx <projet> --approve  approuver après relecture (ouvre la PR)
  cockpit sync <projet>                   enregistrer les PR fusionnées sur GitHub
  cockpit auto <projet> --slots 1 --dry-run   voir ce que la boucle lancerait
  cockpit costs <projet>                  coûts par voie et par ticket
  cockpit abandon T-xxx <projet> --reason "..."
  cockpit help                            toutes les commandes

## Les bons réflexes
- Un ticket = 8 fichiers au plus, avec un test dès qu'il y a du code.
- Pas de nom propre ni de donnée privée dans un titre ou un contrat : ils partent dans la PR.
- Données sensibles : --sensitive (revue obligatoire).
- Relire le diff avant --approve : git -C <worktree> diff origin/main
- Après chaque fusion sur GitHub : cockpit sync <projet>.
- Boucle automatique : toujours --dry-run d'abord, --slots 1 si les tests partagent une base.

## Zellij
  Ctrl+t puis ← / →     changer d'onglet
  Ctrl+p puis flèches   changer de panneau
  Ctrl+o puis d         se détacher (tout continue de tourner)
  cockpit open <projet> revenir
  Ctrl+q                quitter (arrête la session)

## En cas de pépin
- Refus « motif privé » : reformuler le titre ou le contrat du ticket.
- Ticket coincé après un Ctrl+C : cockpit ticket show <projet> --id T-xxx, puis release.
- Un worker coupé par le délai (code 124) : vérifier que le travail est complet avant d'approuver.
- Journal d'un ticket : cockpit events <projet>
