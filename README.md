# Agentic Cockpit V2

Cockpit de développement agentique multi-projets : une file de tickets (SQLite), un pipeline qui confie
chaque ticket à un agent en ligne de commande (headless) dans un worktree git isolé, des portes de
contrôle déterministes, puis une fusion contrôlée. L'objectif est de dépenser le moins de tokens possible :
tout ce qui peut être fait par un script (git, lint, tests, contrôle de périmètre, détection de secrets,
choix de la voie) est fait sans LLM.

## Principe
1. **Ticket** (`cockpit ticket add`) : titre, fichiers possédés, fichiers en lecture seule, contrat d'interface,
   critères d'acceptation, commande de validation.
2. **Run** (`cockpit run T-xxx <projet>`) : création du worktree et de la branche `feat/T-xxx`, compilation d'un
   brief minimal (`.nexus_brief.md`), lancement du worker de la voie choisie par le Gouverneur de quotas.
3. **Porte 0** (0 token) : périmètre des fichiers, secrets, lint, tests. Échec : le retour est renvoyé au worker
   (tentatives bornées), puis escalade.
4. **Porte 1** (optionnelle) : audit du diff par un modèle local (Ollama). Une Porte 1 statique
   (`gatekeeper.check_tests_present`) existe sous forme de fonction : un diff de code doit toucher un test.
5. **Revue puis fusion** (`cockpit merge T-xxx <projet> [--approve]`) : porte d'intégration (la branche principale
   est fusionnée dans le ticket et les tests rejoués), puis fusion `--no-ff`, nettoyage, export de la roadmap.
6. **`cockpit auto <projet>`** : boucle bornée qui exécute les tickets prêts sans conflit de fichiers
   (`--dry-run` pour voir le lot sans rien lancer).

## Travailler à plusieurs CLI
Plusieurs agents peuvent collaborer sur un même projet sans messagerie :
- **Architecte** : découpe les chantiers en tickets parents, `cockpit claim T-001 mon-projet --as claude`,
  puis suit l'avancement avec `cockpit status mon-projet`.
- **Chefs de chantier** : prennent leurs tickets avec `cockpit claim ... --as agy` (ou `deepseek`) et
  découpent en enfants : `cockpit ticket add mon-projet "Titre" --files src/a.py --parent T-001`.
- **Sous-agents headless** : exécutent les enfants avec `cockpit run T-002 mon-projet`, ou par lot avec
  `cockpit auto mon-projet --parent T-001 --auto-merge --slots 2 --max-tickets 6` (`--dry-run` pour simuler).
Cycle : `claim` -> `ticket add --parent` -> `run` / `auto --parent` -> `merge` des enfants -> `submit` du parent.
`cockpit status mon-projet` remplace une messagerie ; `cockpit note T-001 mon-projet "texte"` consigne une
information utile aux autres agents.
Règle : seul l'architecte fusionne un chantier dans la branche principale.

## Voies (config/runners.toml)
Une entrée par voie : `deepseek_coder` (opencode), `agy_standard` / `agy_argon` (Antigravity CLI),
`mistral_vibe`, `local_pc` (opencode sur Ollama local), `local_nas_coder`. Les voies `claude_haiku` et
`claude_sonnet` sont des agents Claude headless : le Gouverneur ne les choisit jamais seul, on les demande
explicitement avec `--lane` (`cockpit run T-001 mon-projet --lane claude_haiku`). Pour ajouter un LLM ou un
CLI : copier un bloc et faire pointer une voie dessus.

## Organisation du dépôt
- `bin/cockpit` : lanceur unique (`open`, `run`, `merge`, `auto`, `ticket`, `events`, `relaunch`, `new-project`, ...).
- `core/` : `pipeline.py` (pipeline), `pmo_db.py` (SQLite, Gouverneur de quotas, DAG de tickets),
  `context_compiler.py` (briefs), `gatekeeper.py` (portes), `scheduler.py` (lots sans conflit),
  `costs.py` (coûts réels via `opencode stats`), `fixer.py` (correcteur lint/typage par modèle local, non branché).
- `config/` : `projects.toml` (registre des projets, ports), `runners.toml` (voies), `lanes.yaml`.
- `scripts/` : lanceurs et utilitaires (`new-project.sh`, `relaunch.sh`, `dispatch-ticket.sh`, `git-relay.sh`).
- `tui/pmo_dashboard.py` : tableau de bord terminal. `layouts/` : disposition Zellij.
- `prompts/`, `templates/` : consignes des agents et modèles de nouveau projet.
- `tests/` : tests `unittest` (stdlib), sans réseau ni LLM.

## Démarrage rapide
```bash
git clone <ce dépôt> ~/agentic-cockpit
cd ~/agentic-cockpit
python3 -B -m unittest discover -s tests      # tous les tests
bin/cockpit new-project mon-projet            # crée un projet (dépôt, règles, registre des ports)
bin/cockpit ticket add mon-projet "Titre" --files src/a.py,tests/test_a.py --accept "..."
bin/cockpit run T-001 mon-projet --no-gate1
bin/cockpit merge T-001 mon-projet --approve
```
Prérequis : Python 3.11+ (stdlib seule), git, bash. Selon les voies utilisées : `opencode`, `agy`, `vibe`,
Ollama, Zellij. Le tableau de bord, le lanceur Zellij et le script `relaunch.sh` supposent Zellij.

## Points d'attention
- L'adresse du nœud Ollama « NAS » (`192.168.0.10`) est un **exemple** : à remplacer dans `config/runners.toml`
  et `config/projects.toml`.
- Les chemins de miroir `/mnt/c/Projets/...` supposent WSL ; adaptez-les.
- Ne jamais committer `.env`, `secrets/`, `*.key`, `*.pem` : le garde-fou de la Porte 0 et le relais git les refusent.
- Les petits modèles locaux (7 à 8 milliards de paramètres) ne sont **pas** fiables comme juges de Porte 1 :
  ils répondent « PASS » à tout. Mesurer sur un diff volontairement mauvais avant de s'y fier.
- `opencode stats` est un compteur global : le coût mesuré par ticket est faussé si d'autres runs opencode
  tournent en parallèle. La voie agy n'a pas de mesure de coût.
- Pas de licence pour l'instant : aucun droit de réutilisation n'est accordé tant qu'elle n'est pas ajoutée.

## État
Pipeline, portes, ordonnanceur, suivi des coûts et tableau de bord fonctionnent et sont couverts par des tests.
La Porte 1 statique est branchée dans `run_ticket` via l'option `require_tests` (un diff de code doit toucher
un test). Le Sprint Fixer n'est pas branché. La boucle `cockpit auto` n'a été éprouvée qu'avec des tests
unitaires (faux worker) et en essais supervisés.
