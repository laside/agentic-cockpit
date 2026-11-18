# Agentic Cockpit

Cockpit de développement agentique multi-projets : une file de tickets (SQLite), un pipeline qui
confie chaque ticket à un agent en ligne de commande (headless) dans un worktree git isolé, des
portes de contrôle déterministes, puis une fusion contrôlée. L'objectif est de dépenser le moins de
tokens possible : tout ce qui peut être fait par un script (git, lint, tests, contrôle de périmètre,
détection de secrets, choix de la voie) est fait sans LLM.

## Principe

1. **Ticket** (`cockpit ticket add`) : titre, fichiers possédés (`FILES_OWNED`), fichiers en lecture
   seule, contrat d'interface, critères d'acceptation, commande de validation.
2. **Run** (`cockpit run T-xxx <projet>`) : création du worktree et de la branche `feat/T-xxx`,
   compilation d'un brief minimal (`.nexus_brief.md`), lancement du worker de la voie choisie par le
   Gouverneur de quotas.
3. **Porte 0** (0 token) : périmètre des fichiers, secrets, lint, tests. Échec : le retour est renvoyé
   au worker (tentatives bornées), puis escalade.
4. **Porte 1** (optionnelle) : audit du diff par un modèle local. Une Porte 1 statique
   (`gatekeeper.check_tests_present`) vérifie qu'un diff de code touche aussi un test.
5. **Revue humaine** : un ticket peut être laissé à l'état `GATE2` ; `cockpit merge T-xxx <projet>`
   exige alors `--approve` après relecture du diff.
6. **Porte d'intégration puis fusion** (`cockpit merge T-xxx <projet> [--approve]`) : la branche
   principale est ramenée dans la branche du ticket et lint/tests sont rejoués, puis la fusion est
   effectuée (`--no-ff` en local, squash merge via GitHub en mode `github`), suivie du nettoyage et
   de l'export de la roadmap.
7. **`cockpit auto <projet>`** : boucle bornée qui exécute les tickets prêts sans conflit de
   fichiers (`--dry-run` pour voir le lot sans rien lancer ; `--auto-merge` est refusé tant que le juge de
   revue n'a pas une calibration récente et réussie, voir `cockpit judge-calibrate`).

## Installation

Prérequis :

- `git` et `bash` ;
- Python 3.11 ou plus récent (stdlib uniquement, compatible 3.12) ;
- Zellij **optionnel** : nécessaire seulement au tableau de bord et au lanceur multi-sessions ;
- les CLI des workers que vous activez dans `config/runners.toml` (par exemple `opencode`, `agy`,
  `vibe`, ou un serveur Ollama local).

```bash
git clone <ce dépôt> ~/agentic-cockpit
cd ~/agentic-cockpit
python3 -B -m unittest discover -s tests      # tous les tests, sans réseau ni LLM
bin/cockpit new-project demo                  # crée un projet et l'enregistre
bin/cockpit ticket add demo "Titre" --files src/a.py,tests/test_a.py --accept "..."
bin/cockpit run T-001 demo --no-gate1
bin/cockpit merge T-001 demo --approve
```

Le code lit `COCKPIT_HOME` (défaut : `~/agentic-cockpit`).

## Configuration

La configuration vit **hors du dépôt**, dans une surcouche :

- `$COCKPIT_CONFIG_DIR` s'il est défini, sinon `~/.config/agentic-cockpit` ;
- à défaut, `<COCKPIT_HOME>/config/` (compatibilité) ;
- à défaut, l'exemple livré `<stem>.example<suffix>`.

Pour démarrer, copiez les exemples fournis :

```bash
mkdir -p ~/.config/agentic-cockpit
cp config/projects.example.toml ~/.config/agentic-cockpit/projects.toml
cp config/runners.example.toml  ~/.config/agentic-cockpit/runners.toml
```

- `projects.toml` : registre des projets, chemins de worktrees, branche par défaut, ports, et
  `merge_mode` (`"local"` par défaut, `"github"`, ou `"github_pr"`).
- `runners.toml` : une entrée par voie (commande `argv`, quotas, délais, Porte 1 statique).
- `config/lanes.yaml` : budgets et seuils du Gouverneur de quotas.
- `forbidden-words.txt` : liste privée de motifs (une expression régulière par ligne, `#` pour les
  commentaires) lue par `cockpit privacy-check`. Placez-la dans la surcouche
  (`~/.config/agentic-cockpit/forbidden-words.txt` ou `$COCKPIT_CONFIG_DIR/forbidden-words.txt`) :
  elle ne doit jamais être commitée.

Les secrets se saisissent avec `cockpit secret <NOM_DE_CLE>` ; ils sont stockés hors dépôt et
ignorés par git.

## Commandes

Toutes les commandes passent par le lanceur unique `bin/cockpit` :

- `cockpit ticket add <projet> "Titre" --files a.py,tests/test_a.py --accept "..."` : crée un ticket.
- `cockpit ticket draft <projet> "Objectif" [--lane L]` : fait rédiger un ticket `DRAFT` par un worker.
- `cockpit plan <projet> "Objectif" [--lane L] [--max-tickets N]` : fait rédiger un plan de 2 à 4
  tickets `DRAFT` (N borné 2..4, défaut 4) avec dépendances mappées.
- `cockpit ticket promote <projet> T-001 T-002 ...` : passe plusieurs `DRAFT` à `READY` (ou
  `BLOCKED` si une dépendance n'est pas `MERGED`), en validant tous les ids avant toute promotion.
- `cockpit ticket promote T-xxx <projet>` : forme historique, conservée.
- `cockpit run T-xxx <projet> [--lane <voie>] [--no-gate1]` : exécute un ticket (worker, Porte 0,
  Porte 1, commit).
- `cockpit auto <projet> [--dry-run] [--auto-merge] [--parent T-xxx] [--slots N] [--max-tickets N]` :
  boucle autonome sur la file.
- `cockpit merge T-xxx <projet> [--approve]` : porte d'intégration puis fusion et nettoyage.
- `cockpit claim T-xxx <projet> --as claude|agy|deepseek` : réserve un ticket pour un chantier
  interactif.
- `cockpit submit T-xxx <projet>` / `cockpit release T-xxx <projet>` : soumet le worktree aux portes
  ou relâche le ticket vers `READY`.
- `cockpit sync <projet>` : clôt les tickets `PR_OPEN` dont la PR est fusionnée (0 token).
- `cockpit status <projet>` : état synthétique du projet (0 token) ; lance `sync` au préalable.
- `cockpit brief <projet>` : brief de reprise en 30 lignes maximum (branche, dernier commit, tickets, prochain ticket, événements, coût du jour), lu uniquement dans la base et le git local, sans réseau.
- `cockpit note T-xxx <projet> "texte"` : consigne une note utile aux autres agents.
- `cockpit costs <projet> [--since AAAA-MM-JJ]` : rapport des coûts workers et architecte.
- `cockpit judge-calibrate [--runs N] [--json chemin]` : calibre le juge de revue sur un jeu de cas.
- `cockpit abandon T-xxx <projet> [--reason "..."]` : abandonne proprement un ticket ou un chantier.
- `cockpit privacy-check [--repo <chemin>] [--range A..B] [--generic]` : garde de confidentialité.
- `cockpit install-hooks` : installe le hook git `pre-push` de confidentialité.
- `cockpit lessons <projet> [--apply] [--db chemin] [--rules chemin] [--min N]` : propose des règles
  courtes à partir des échecs récurrents, sans token.

`cockpit help` affiche la liste complète.

### Leçons apprises (`cockpit lessons`)

`cockpit lessons <projet> [--apply] [--db chemin] [--rules chemin] [--min N]` analyse les événements
`GATE0_FAIL`, `GATE1_FAIL`, `INTEGRATION_FAIL` et `PR_PRIVACY_FAIL` de la base (`--db`) et classe les
échecs dans 5 catégories pré-rédigées : `test_contrat`, `perimetre_files_owned`,
`aucun_fichier_modifie`, `donnee_privee` et `porte_integration`. La classification se fait par mot-clé
(recherche insensible à la casse et aux accents) ; les échecs sans mot-clé et de kind non mappé sont
ignorés. Les catégories revenant au moins 3 fois (seuil réglable par `--min N`) proposent leur règle
pré-rédigée, sans interpolation du motif ni du détail. Sans
`--apply`, la commande se contente d'afficher les propositions : elle n'écrit rien. `--apply` insère
les règles de façon idempotente dans la section bornée à 20 règles maximum, sans doublon, du fichier
cible (`templates/core.md` par défaut, ou `--rules chemin`), délimitée exactement par les marqueurs
`<!-- BEGIN COCKPIT LESSONS -->` et `<!-- END COCKPIT LESSONS -->`. La commande ne consomme aucun
token et n'écrit rien sans `--apply`.

## Rédaction assistée de tickets

`cockpit ticket draft <projet> "Objectif" [--lane L]` crée un worktree jetable détaché de
`origin/<branche par défaut>` et laisse le worker de la voie explorer le dépôt. Le worker écrit
`draft.json` (`title`, `size`, `prio`, `files`, `contract`, `accept`) ; la validation est mécanique
(chemins, 8 fichiers maximum, taille, priorité, contrat de 1500 caractères maximum, acceptation non
vide, confidentialité). En cas de refus, une seule relance reçoit l'erreur dans son prompt, puis
aucun ticket n'est créé. Un brouillon valide devient un ticket `DRAFT` : `cockpit auto` et
`cockpit run` l'ignorent jusqu'à `cockpit ticket promote T-xxx <projet>`.

`cockpit plan <projet> "Objectif" [--lane L] [--max-tickets N]` réutilise le même worktree jetable
et demande au worker un `plan.json` (`{"tickets": [...]}`). Chaque ticket contient `title`, `size`,
`prio`, `files`, `contract`, `accept` et `depends_on` (indices 0-based). Le plan doit contenir 2 à 4
tickets ; chaque ticket passe les règles du mode `draft`, les indices de dépendance doivent être
valides et sans cycle. Un seul ticket invalide refuse tout le plan (au plus une relance), sans créer
aucun ticket ni laisser de worktree `_draft-*`. Les tickets valides sont créés atomiquement en
`DRAFT`, avec des ids consécutifs, `owner="drafter"`, les `depends_on` mappés vers les ids réels et
un événement `PLAN_CREATED`. Le résumé affiche une ligne compacte par ticket
(`T-xxx [S/P2] titre (dépend de T-yyy)`) puis le total, suivi des chevauchements de fichiers
éventuels (signalés sans bloquer la création).

## Multi-projets

`cockpit new-project <projet> [chemin] [port]` initialise un dépôt, ses règles, son `Makefile`, ses
worktrees et son entrée dans `projects.toml` (avec contrôle des collisions de ports). Chaque projet a
sa propre session Zellij `cockpit-<projet>` : `cockpit open <projet>` (ou `cockpit switch <projet>`)
bascule d'un projet à l'autre sans fermer les sessions en arrière-plan. Un même projet peut être
travaillé par plusieurs agents via `claim` / `submit` / `release`.

## Flux GitHub

Pour un projet dont `projects.toml` contient `merge_mode = "github"` :

1. le worktree du ticket est poussé sur la branche `feat/T-xxx` ;
2. une pull request est créée (ou une PR ouverte est réutilisée) vers la branche par défaut ;
3. les checks CI sont attendus (`gh pr checks`) ;
4. la PR est fusionnée en **squash merge** avec suppression de la branche, puis la branche locale
   est mise à jour en fast-forward.

La branche `main` reste protégée : le pipeline ne pousse jamais directement dessus et la roadmap est
régénérée sans être commitée. Côté GitHub, activez la protection de `main` et exigez les checks CI
avant fusion. En mode `local` (défaut), la fusion se fait en `--no-ff` depuis le dépôt racine.

### Notification CI des pull requests

Lorsqu'une pull request fait passer le job `tests`, un job `notify` poste ou met à jour un commentaire
sur la PR (notification mobile). Le commentaire porte le marqueur `<!-- cockpit-ci-tests-passed -->`
et est édité en place (`gh pr comment --edit-last --create-if-none`), donc une seule notification
subsiste par PR. Le job ne s'exécute que pour `pull_request` et si `tests` réussit ; il utilise
uniquement le `GITHUB_TOKEN` avec la permission `pull-requests: write`.

Limite des PR venant d'un fork : le `GITHUB_TOKEN` y est en lecture seule et ne peut pas écrire de
commentaire. Le job `notify` déclare `continue-on-error: true` : cet échec est toléré et ne fait pas
échouer la CI.

Pour un projet dont `projects.toml` contient `merge_mode = "github_pr"` (PR seule), le push et la
création (ou réutilisation) de la PR sont identiques au mode `github`, puis le pipeline s'arrête :
aucun `gh pr checks`, aucun `gh pr merge`. Le ticket passe à l'état `PR_OPEN` (événement `PR_OPENED`
avec l'URL). Le corps de PR est généré sans LLM (contrat, fichiers modifiés, diffstat, résultat des
Portes 0/1, coût du dernier run lu dans la table `runs`) ; le titre et le corps passent par la liste
privée avant tout push (`PR_PRIVACY_FAIL` sinon). `PR_OPEN` est hors des états actifs : le ticket
n'est ni relancé ni compté en échec par `auto`, ses dépendants restent bloqués, et `status`
l'affiche dans la section « PR OUVERTES ». `cockpit sync <projet>` interroge `gh pr view` pour chaque
ticket `PR_OPEN` : si la PR est fusionnée, `origin/<branche par défaut>` est récupérée en fast-forward
dans la racine, le worktree et la branche locale sont retirés, le ticket passe `MERGED` et ses
dépendants sont débloqués. Une PR fermée sans fusion journalise `PR_CLOSED` sans changer l'état ; une
erreur réseau ou un `gh` absent laisse tout inchangé. `cockpit status` appelle `sync` d'abord.

## Économie de tokens

Tout ce qui peut être décidé sans LLM l'est : périmètre `FILES_OWNED`, détection de secrets, lint,
tests, Porte 1 statique, choix de la voie, intégration et fusion. Les commandes `ticket`, `status`,
`costs`, `events`, `lessons`, `privacy-check` et `install-hooks` ne consomment aucun token. Chaque run
enregistre les tokens (entrée, sortie, cache) lus dans la sortie JSON du worker. `cockpit costs
<projet> [--since ...] [--transcript <journal.jsonl>]` sépare le coût des workers de celui de
l'architecte. Les unités ne sont jamais mélangées : euros mesurés (API payante), équivalent API
(abonnement), tokens seuls (voie sans coût exposé). La ligne `MOYENNE` divise la sortie attribuée
par le nombre de tickets fusionnés ayant réellement reçu des tokens de sortie du journal ; un ticket
fusionné sans données est exclu du dénominateur (N=0 donne une moyenne à 0).

## Sécurité

- **R7** : ne jamais lire ni committer `.env`, `secrets/`, `*.key` ou `*.pem`. La Porte 0, le relais
  git et `cockpit privacy-check` refusent ces fichiers.
- **Périmètre `FILES_OWNED`** : un ticket ne peut créer ou modifier que les fichiers déclarés ; tout
  débordement est signalé et renvoyé au worker.
- **Hook `pre-push`** : `cockpit install-hooks` installe un hook qui exécute `cockpit privacy-check`
  sur la plage de commits poussée (fichiers suivis, adresses e-mail et IP privées, mots interdits).
  Installez-le dans chaque dépôt avant de pousser.

## Organisation du dépôt

- `bin/cockpit` : lanceur unique (`open`, `run`, `merge`, `auto`, `ticket`, `claim`, `submit`,
  `release`, `status`, `costs`, `judge-calibrate`, `abandon`, `privacy-check`, `install-hooks`,
  `new-project`, `events`, `note`, `relaunch`, ...).
- `core/` : `pipeline.py` (pipeline), `pmo_db.py` (SQLite, Gouverneur de quotas, DAG de tickets),
  `context_compiler.py` (briefs), `gatekeeper.py` (portes), `scheduler.py` (lots sans conflit),
  `costs.py` et `costs_report.py` (coûts), `judge.py` et `judge_calibrate.py` (revue),
  `config_paths.py` (résolution de la surcouche), `fixer.py` (correcteur lint/typage, non branché).
- `config/` : exemples `projects.example.toml` et `runners.example.toml`, `lanes.yaml`.
- `scripts/` : `new-project.sh`, `relaunch.sh`, `dispatch-ticket.sh`, `git-relay.sh`,
  `privacy-check.py`, `install-hooks.sh`, `cockpit-env.sh`.
- `tui/pmo_dashboard.py` : tableau de bord terminal ; `layouts/` : disposition Zellij.
- `prompts/`, `templates/` : consignes des agents et modèles de nouveau projet.
- `tests/` : tests `unittest` (stdlib), sans réseau ni LLM.

## Points d'attention

- Les chemins de worktrees des exemples supposent `~/agentic-cockpit-worktrees` ; adaptez-les dans
  `projects.toml`.
- Les petits modèles locaux (7 à 8 milliards de paramètres) ne sont pas fiables comme juges de
  Porte 1 : ils répondent souvent « PASS » à tout. Mesurez avec `cockpit judge-calibrate` avant de
  vous y fier.
- La boucle `cockpit auto` est couverte par des tests unitaires (faux worker) et des essais
  supervisés ; commencez par `--dry-run`.
- Pas de licence pour l'instant : aucun droit de réutilisation n'est accordé tant qu'elle n'est pas
  ajoutée.

## État

Pipeline, portes, ordonnanceur, suivi des coûts, confidentialité et tableau de bord fonctionnent et
sont couverts par des tests. La Porte 1 statique est branchée dans `run_ticket` via l'option
`require_tests` (un diff de code doit toucher un test). Le Sprint Fixer n'est pas branché.
