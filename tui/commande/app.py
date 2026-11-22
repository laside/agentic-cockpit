#!/usr/bin/env python3
"""Onglet Commande : écran Textual au-dessus du modèle (model.py) et des actions (actions.py).

Lancement : python -m tui.commande.app <projet>
Textual n'est utilisé que par ce module ; le cœur du cockpit reste en bibliothèque standard.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_RACINE = Path(__file__).resolve().parents[2]
if str(_RACINE) not in sys.path:
    sys.path.insert(0, str(_RACINE))

from rich.text import Text  # noqa: E402
from textual import on, work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.css.query import NoMatches  # noqa: E402
from textual.screen import ModalScreen, Screen  # noqa: E402
from textual.widgets import Button, Footer, OptionList, RichLog, Select, Static  # noqa: E402
from textual.widgets.option_list import Option  # noqa: E402

from tui.commande import actions as actions_mod  # noqa: E402
from tui.commande.actions import Action  # noqa: E402
from tui.commande.model import FILTRES, CommandeModel  # noqa: E402

COULEURS_ETAT = {
    "DRAFT": "grey70", "READY": "cyan", "BLOCKED": "red", "ASSIGNED": "yellow",
    "GATE0": "yellow", "GATE1": "yellow", "GATE2": "magenta", "PR_OPEN": "blue",
    "MERGED": "green", "ABANDONED": "grey50",
}
NOMS_FILTRES = list(FILTRES.keys())
INTERVALLE_RAFRAICHISSEMENT = 3.0
LIGNES_JOURNAL = 25
DELAI_SORTIE = 120  # secondes max pour Brief, Coûts, Simulation

# Boutons du haut dont la sortie s'affiche dans une fenêtre modale.
ACTIONS_MODALES = {"brief", "costs", "auto_dry"}

INFOS_BULLES = {
    "sync_projet": "Enregistre les PR fusionnées sur GitHub, débloque les tickets dépendants, nettoie les worktrees.",
    "rafraichir": "Recharge tout de suite les tickets et le journal.",
    "brief": "Résumé de reprise en 30 lignes (branche, tickets, prochain ticket, coût du jour).",
    "costs": "Dépenses par voie et par ticket, sans mélanger les unités.",
    "auto_dry": "Montre ce que la boucle automatique lancerait, sans rien lancer.",
    # actions de ticket
    "promote": "Passe le brouillon à l'état prêt (READY).",
    "abandon": "Abandonne le ticket (confirmation demandée).",
    "run": "Lance un worker sur la voie choisie.",
    "claim": "Prend le ticket en main manuellement.",
    "events": "Affiche l'historique des événements du ticket.",
    "submit": "Soumet le travail du ticket à la relecture.",
    "diff": "Affiche le diff du worktree du ticket.",
    "approve": "Approuve le ticket et ouvre la PR (confirmation demandée).",
    "sync": "Enregistre la fusion de la PR et met à jour le ticket.",
}


def info_bulle(a: Action) -> str:
    """Info-bulle d'un bouton ; repli sur la commande pour les actions personnalisées."""
    return INFOS_BULLES.get(a.id) or f"Exécute : {' '.join(a.argv)}"[:200]


def normaliser_ticket(brut: dict[str, Any] | None) -> dict[str, Any] | None:
    """Unifie list_tickets (state/lane) et ticket_detail (etat/voie) en {id, state, etat, lane, voie}."""
    if not brut:
        return None
    etat = brut.get("state") or brut.get("etat") or ""
    voie = brut.get("lane") or brut.get("voie") or ""
    return {"id": brut["id"], "state": etat, "etat": etat, "lane": voie, "voie": voie}


def calculer_actions(
    model: CommandeModel,
    projet: str,
    ticket: dict[str, Any] | None,
    voie_choisie: str = "",
    worktree: str | None = None,
) -> tuple[list[Action], list[Action]]:
    """Renvoie (actions du projet pour l'en-tête, actions du ticket) sans doublon de Synchroniser."""
    branche = str(model.projets().get(projet, {}).get("default_branch") or "main")
    ticket = normaliser_ticket(ticket)
    toutes = actions_mod.actions_for(projet, ticket, voie_choisie, worktree, branche)
    perso = actions_mod.actions_personnalisees(projet, ticket, worktree, model.home)
    ids_projet = {a.id for a in actions_mod.actions_projet(projet)}
    # « État » (cockpit status) remplacé par « Rafraîchir » : rechargement local, aucune commande.
    entete = [Action("rafraichir", "Rafraîchir", False, False, []) if a.id == "status" else a
              for a in toutes if a.id in ids_projet]
    entete += [a for a in actions_mod.actions_personnalisees(projet, None, None, model.home)
               if a.id not in {x.id for x in entete}]
    deja = {a.id for a in entete}
    deja_libelles = {a.libelle for a in entete}
    du_ticket: list[Action] = []
    for a in [a for a in toutes if a.id not in ids_projet] + perso:
        if a.id in deja or a.libelle in deja_libelles:
            continue  # un seul Synchroniser (action PR_OPEN confondue avec celle du projet)
        deja.add(a.id)
        du_ticket.append(a)
    return entete, du_ticket


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "annuler", "Annuler")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialogue"):
            yield Static(self.message, id="dialogue-message")
            with Horizontal(id="dialogue-boutons"):
                yield Button("Confirmer", id="oui", variant="warning")
                yield Button("Annuler", id="non")

    @on(Button.Pressed, "#oui")
    def _oui(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#non")
    def _non(self) -> None:
        self.dismiss(False)

    def action_annuler(self) -> None:
        self.dismiss(False)


class DiffScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "fermer", "Fermer"), Binding("q", "fermer", "Fermer")]

    def __init__(self, contenu: str) -> None:
        super().__init__()
        self.contenu = contenu

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="diff-boite"):
            yield Static(Text(self.contenu or "(diff vide)"), id="diff-texte")

    def action_fermer(self) -> None:
        self.dismiss(None)


class SortieScreen(ModalScreen[None]):
    """Sortie d'une commande du haut (Brief, Coûts, Simulation), défilante."""

    BINDINGS = [Binding("escape", "fermer", "Fermer")]

    def __init__(self, titre: str, contenu: str) -> None:
        super().__init__()
        self.titre = titre
        self.contenu = contenu

    def compose(self) -> ComposeResult:
        with Vertical(id="sortie-boite"):
            yield Static(Text(self.titre, style="bold"), id="sortie-titre")
            with VerticalScroll(id="sortie-defil"):
                yield Static(Text(self.contenu or "(aucune sortie)"), id="sortie-texte")
            yield Button("Fermer", id="sortie-fermer")

    @on(Button.Pressed, "#sortie-fermer")
    def _fermer(self) -> None:
        self.dismiss(None)

    def action_fermer(self) -> None:
        self.dismiss(None)


class CommandeApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "Cockpit — Commande"
    BINDINGS = [
        Binding("q", "quit", "Quitter"),
        Binding("r", "rafraichir", "Rafraîchir"),
        Binding("f", "filtre_suivant", "Filtre suivant"),
        Binding("enter", "action_principale", "Action principale"),
    ]

    def __init__(
        self,
        projet: str,
        model: CommandeModel | None = None,
        log_dir: Path | None = None,
        intervalle: float = INTERVALLE_RAFRAICHISSEMENT,
    ) -> None:
        super().__init__()
        self.projet = projet
        self.model = model or CommandeModel()
        self.log_dir = log_dir
        self.intervalle = intervalle
        self.filtre = "Tous"
        self.ticket_id: str | None = None
        self.voie = ""
        self.tickets: list[dict[str, Any]] = []
        self.detail: dict[str, Any] | None = None
        self.worktree: str | None = None
        self.actions_entete: list[Action] = []
        self.actions_ticket: list[Action] = []
        self.procs: list[tuple[subprocess.Popen, Action, Path]] = []
        self.journal_actif: Path | None = None
        self.messages: list[str] = []
        self._verrou: asyncio.Lock | None = None

    # ---------- composition ----------
    def compose(self) -> ComposeResult:
        projets = sorted({self.projet, *self.model.projets().keys()})
        with Vertical(id="entete"):
            with Horizontal(id="barre-projet"):
                yield Select([(p, p) for p in projets], value=self.projet, allow_blank=False, id="projet")
            yield Horizontal(id="boutons-globaux")
        with Horizontal(id="corps"):
            with Vertical(id="gauche"):
                with Horizontal(id="filtres"):
                    for i, nom in enumerate(NOMS_FILTRES):
                        yield Button(nom, id=f"filtre-{i}")
                yield OptionList(id="tickets")
            with Vertical(id="droite"):
                with VerticalScroll(id="detail-zone"):
                    yield Static("Aucun ticket sélectionné.", id="detail")
                with Vertical(id="actions-zone"):
                    yield Select([(v, v) for v in self.model.voies()], prompt="Voie (pour Lancer)", id="voie")
                    yield Horizontal(id="boutons-ticket")
        yield RichLog(id="journal", wrap=True, markup=False)
        yield Footer()

    @property
    def ecran_principal(self) -> Screen:
        """Écran principal (bas de la pile) : cible de toutes les mises à jour d'arrière-plan,
        même quand une fenêtre modale est ouverte par-dessus."""
        return self.screen_stack[0]

    def _w(self, selecteur: str, type_: type | None = None) -> Any:
        ecran = self.ecran_principal
        return ecran.query_one(selecteur, type_) if type_ else ecran.query_one(selecteur)

    async def on_mount(self) -> None:
        self._verrou = asyncio.Lock()
        self.set_interval(self.intervalle, self._tic)
        self.action_rafraichir()

    # ---------- chargement (hors fil de l'interface) ----------
    def action_rafraichir(self) -> None:
        self._charger(self.projet, self.filtre, self.ticket_id)

    def _tic(self) -> None:
        try:
            self._sonder_processus()
            self.action_rafraichir()
        except Exception as exc:  # noqa: BLE001 - un rafraîchissement ne plante jamais l'écran
            self._noter(f"Rafraîchissement en échec : {exc!r}")

    @work(thread=True, exclusive=True, group="chargement")
    def _charger(self, projet: str, filtre: str, ticket_id: str | None) -> None:
        donnees: dict[str, Any] = {"projet": projet, "filtre": filtre, "ticket_id": ticket_id,
                                   "erreur": None, "tickets": [], "detail": None,
                                   "worktree": None, "evenements": []}
        try:
            donnees["tickets"] = self.model.list_tickets(projet, filtre)
            if ticket_id:
                donnees["detail"] = self.model.ticket_detail(projet, ticket_id)
                if donnees["detail"] and donnees["detail"]["etat"] == "GATE2":
                    donnees["worktree"] = self.model.worktree_of(projet, ticket_id)
            rows = self.model._query(
                "SELECT ticket_id, kind, detail FROM events WHERE project_id = ? ORDER BY id DESC LIMIT 8",
                (projet,),
            )
            donnees["evenements"] = [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001 - l'écran ne doit jamais planter
            donnees["erreur"] = f"Lecture impossible : {exc}"
        self.call_from_thread(self._appliquer, donnees)

    async def _appliquer(self, d: dict[str, Any]) -> None:
        if d["projet"] != self.projet or d["filtre"] != self.filtre:
            return  # résultat périmé
        if self._verrou is None:
            return
        async with self._verrou:
            try:
                if d["erreur"]:
                    self._noter(d["erreur"])
                else:
                    await self._maj_liste(d["tickets"])
                    if d["ticket_id"] == self.ticket_id:
                        self.detail = d["detail"]
                        self.worktree = d["worktree"]
                        self._w("#detail", Static).update(self._rendre_detail(self.detail))
                    await self._maj_boutons()
            except NoMatches as exc:
                self._noter(f"Rafraîchissement : élément introuvable ({exc})")
            except Exception as exc:  # noqa: BLE001 - un rafraîchissement ne plante jamais l'écran
                self._noter(f"Rafraîchissement en échec : {exc!r}")
            try:
                self._maj_journal(d["evenements"])
            except Exception as exc:  # noqa: BLE001
                self._noter(f"Journal non mis à jour : {exc!r}")

    async def _maj_liste(self, tickets: list[dict[str, Any]]) -> None:
        if [t["id"] + t["state"] for t in tickets] == [t["id"] + t["state"] for t in self.tickets]:
            self.tickets = tickets
            return
        self.tickets = tickets
        liste = self._w("#tickets", OptionList)
        liste.clear_options()
        options = []
        for t in tickets:
            ligne = Text.assemble(
                f"{t['id']}  ", (f"{t['state']:<9}", COULEURS_ETAT.get(t["state"], "white")),
                f" {t.get('priority') or '-':<3} {t.get('complexity') or '-':<4} {t.get('title') or ''}",
            )
            options.append(Option(ligne, id=t["id"]))
        liste.add_options(options)
        ids = [t["id"] for t in tickets]
        if self.ticket_id in ids:
            liste.highlighted = ids.index(self.ticket_id)
        elif ids:
            liste.highlighted = 0

    @staticmethod
    def _rendre_detail(d: dict[str, Any] | None) -> Text:
        if not d:
            return Text("Aucun ticket sélectionné.")
        t = Text()

        def titre(s: str) -> None:
            t.append(f"\n{s}\n", style="bold #81a1c1")

        t.append(f"{d['id']} — {d['titre']}\n", style="bold")
        titre("État")
        t.append(f"{d['etat']}  priorité {d['priorite']}  taille {d['complexite']}  voie {d['voie'] or '-'}"
                 f"{'  [sensible]' if d['sensible'] else ''}\n", style=COULEURS_ETAT.get(d["etat"], "white"))
        titre("Fichiers")
        t.append("\n".join(f"- {f}" for f in d["fichiers"]) + "\n" if d["fichiers"] else "(aucun)\n")
        titre("Critères")
        t.append("\n".join(f"- {c}" for c in d["criteres"]) + "\n" if d["criteres"] else "(aucun)\n")
        titre("Contrat")
        t.append((d["contrat"] or "(aucun)") + "\n")
        titre("Runs")
        for r in d["runs"]:
            t.append(f"- #{r.get('id')} tentative {r.get('attempt')} voie {r.get('lane')}\n")
        if not d["runs"]:
            t.append("(aucun)\n")
        titre("Événements")
        for e in d["evenements"]:
            t.append(f"- {e.get('kind')} {e.get('detail') or ''}\n")
        if not d["evenements"]:
            t.append("(aucun)\n")
        return t

    # ---------- boutons ----------
    def _ticket_courant(self) -> dict[str, Any] | None:
        if self.detail and self.detail["id"] == self.ticket_id:
            return normaliser_ticket(self.detail)
        for t in self.tickets:
            if t["id"] == self.ticket_id:
                return normaliser_ticket(t)
        return None

    def _longue_en_cours(self) -> bool:
        return any(p.poll() is None and a.longue for p, a, _ in self.procs)

    def _desactive(self, a: Action) -> bool:
        if a.longue and self._longue_en_cours():
            return True
        return a.id == "run" and not self.voie  # jamais --lane vide

    async def _maj_boutons(self) -> None:
        self.actions_entete, self.actions_ticket = calculer_actions(
            self.model, self.projet, self._ticket_courant(), self.voie, self.worktree)
        await self._remplacer("#boutons-globaux", "g", self.actions_entete)
        await self._remplacer("#boutons-ticket", "act", self.actions_ticket)

    async def _remplacer(self, selecteur: str, prefixe: str, acts: list[Action]) -> None:
        conteneur = self._w(selecteur)
        actuels = [b.id for b in conteneur.query(Button)]
        voulus = [f"{prefixe}-{a.id}" for a in acts]
        if actuels != voulus:
            await conteneur.remove_children()
            await conteneur.mount(*[Button(a.libelle, id=f"{prefixe}-{a.id}") for a in acts])
        for a in acts:
            b = conteneur.query_one(f"#{prefixe}-{a.id}", Button)
            b.tooltip = info_bulle(a)
            b.disabled = self._desactive(a)
            b.variant = "warning" if a.confirmation else "default"

    def _maj_desactivation(self) -> None:
        try:
            for prefixe, acts, sel in (("g", self.actions_entete, "#boutons-globaux"),
                                       ("act", self.actions_ticket, "#boutons-ticket")):
                for a in acts:
                    for b in self._w(sel).query(f"#{prefixe}-{a.id}"):
                        b.disabled = self._desactive(a)
        except Exception as exc:  # noqa: BLE001 - jamais de plantage de l'écran
            self._noter(f"Boutons non mis à jour : {exc!r}")

    # ---------- événements d'interface ----------
    @on(OptionList.OptionHighlighted, "#tickets")
    async def _ticket_surligne(self, ev: OptionList.OptionHighlighted) -> None:
        if ev.option.id is not None and ev.option.id != self.ticket_id:
            self.ticket_id = ev.option.id
            self.detail = None
            self.worktree = None
            self.action_rafraichir()
            if self._verrou:
                async with self._verrou:
                    await self._maj_boutons()

    @on(OptionList.OptionSelected, "#tickets")
    def _ticket_valide(self) -> None:
        self.action_action_principale()

    @on(Select.Changed, "#voie")
    async def _voie_changee(self, ev: Select.Changed) -> None:
        self.voie = "" if ev.value is Select.BLANK else str(ev.value)
        if self._verrou:
            async with self._verrou:
                await self._maj_boutons()

    @on(Select.Changed, "#projet")
    def _projet_change(self, ev: Select.Changed) -> None:
        if ev.value is not Select.BLANK and str(ev.value) != self.projet:
            self.projet = str(ev.value)
            self.ticket_id, self.detail, self.tickets = None, None, []
            self.action_rafraichir()

    @on(Button.Pressed)
    def _bouton(self, ev: Button.Pressed) -> None:
        bid = ev.button.id or ""
        if bid.startswith("filtre-"):
            self._choisir_filtre(NOMS_FILTRES[int(bid.split("-")[1])])
            return
        for acts, prefixe in ((self.actions_entete, "g-"), (self.actions_ticket, "act-")):
            for a in acts:
                if bid == prefixe + a.id:
                    self.demander(a)
                    return

    def _choisir_filtre(self, nom: str) -> None:
        self.filtre = nom
        self.tickets = []  # force la reconstruction de la liste
        self.action_rafraichir()

    def action_filtre_suivant(self) -> None:
        self._choisir_filtre(NOMS_FILTRES[(NOMS_FILTRES.index(self.filtre) + 1) % len(NOMS_FILTRES)])

    def action_action_principale(self) -> None:
        for a in self.actions_ticket:
            if not self._desactive(a):
                self.demander(a)
                return

    # ---------- exécution ----------
    def demander(self, a: Action) -> None:
        if self._desactive(a):
            self._noter(f"« {a.libelle} » indisponible pour le moment.")
            return
        if a.confirmation:
            def retour(ok: bool | None) -> None:
                if ok:
                    self.executer(a)
            self.push_screen(ConfirmScreen(f"Confirmer : {a.libelle} ?\n\n{' '.join(a.argv)}"), retour)
        else:
            self.executer(a)

    def executer(self, a: Action) -> None:
        if self._desactive(a):  # une seule action longue à la fois
            self._noter(f"« {a.libelle} » refusée : une action longue est déjà en cours.")
            return
        if a.id == "diff":
            self._afficher_diff(a)
            return
        if a.id == "rafraichir":
            self._noter("Rafraîchissement demandé.")
            self.action_rafraichir()
            return
        if a.id in ACTIONS_MODALES:
            self._noter(f"Lancé : {a.libelle}")
            self._afficher_sortie(a)
            return
        try:
            d = Path(self.log_dir) if self.log_dir else actions_mod.LOG_DIR
            d.mkdir(parents=True, exist_ok=True)
            journal = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**9:09d}-{a.id}.log"
            with open(journal, "wb") as sortie:
                proc = subprocess.Popen(a.argv, stdin=subprocess.DEVNULL, stdout=sortie,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        except FileNotFoundError:
            self._noter(f"Commande introuvable : {a.argv[0]}")
            return
        except Exception as exc:  # noqa: BLE001 - jamais de plantage de l'écran
            self._noter(f"Échec du lancement de « {a.libelle} » : {exc}")
            return
        self.procs.append((proc, a, journal))
        self.journal_actif = journal
        self._noter(f"Lancé : {a.libelle}")
        self._maj_desactivation()

    def _sonder_processus(self) -> None:
        """poll() de chaque processus lancé : évite les zombies et libère les boutons longs."""
        restants = []
        resync = False
        for proc, a, journal in self.procs:
            code = proc.poll()
            if code is None:
                restants.append((proc, a, journal))
            else:
                self._noter(f"Terminé : {a.libelle} (code {code})")
                resync = resync or a.id == "sync_projet"
        if len(restants) != len(self.procs):
            self.procs = restants
            self._maj_desactivation()
        if resync:
            self.action_rafraichir()  # Synchroniser : écran rechargé à la fin

    @work(thread=True, group="sortie")
    def _afficher_sortie(self, a: Action) -> None:
        """Exécute une commande courte, puis montre sa sortie dans une fenêtre modale."""
        try:
            res = subprocess.run(a.argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 errors="replace", timeout=DELAI_SORTIE, check=False)
            texte = res.stdout + res.stderr
            if res.returncode == 0:
                ligne = f"Terminé : {a.libelle} (code 0)"
            else:
                ligne = f"Erreur : {a.libelle} (code {res.returncode})"
                texte = f"Erreur (code {res.returncode}) :\n{texte}"
        except FileNotFoundError:
            ligne = texte = f"Commande introuvable : {a.argv[0]}"
        except subprocess.TimeoutExpired:
            ligne = texte = f"Erreur : {a.libelle} dépasse {DELAI_SORTIE} s, interrompue."
        except Exception as exc:  # noqa: BLE001 - jamais de plantage de l'écran
            ligne = texte = f"Échec de « {a.libelle} » : {exc}"
        self.call_from_thread(self._montrer_sortie, a.libelle, texte, ligne)

    def _montrer_sortie(self, titre: str, texte: str, ligne: str) -> None:
        self._noter(ligne)
        self.push_screen(SortieScreen(titre, texte))

    @work(thread=True, group="diff")
    def _afficher_diff(self, a: Action) -> None:
        try:
            res = subprocess.run(a.argv, capture_output=True, text=True, timeout=30, check=False)
            texte = res.stdout if res.returncode == 0 else f"Erreur git ({res.returncode}) :\n{res.stderr}"
        except Exception as exc:  # noqa: BLE001
            texte = f"Diff impossible : {exc}"
        self.call_from_thread(self.push_screen, DiffScreen(texte))

    # ---------- journal ----------
    def _noter(self, message: str) -> None:
        self.messages.append(f"{time.strftime('%H:%M:%S')} {message}")
        self.messages = self.messages[-50:]

    def _maj_journal(self, evenements: list[dict[str, Any]]) -> None:
        log = self._w("#journal", RichLog)
        log.clear()
        for m in self.messages[-10:]:
            log.write(m)
        for e in reversed(evenements):
            log.write(f"[événement] {e.get('ticket_id') or '-'} {e.get('kind')} {e.get('detail') or ''}"[:200])
        if self.journal_actif and self.journal_actif.exists():
            try:
                lignes = self.journal_actif.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lignes = []
            for ligne in lignes[-LIGNES_JOURNAL:]:
                log.write(f"> {ligne}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage : python -m tui.commande.app <projet>", file=sys.stderr)
        return 2
    CommandeApp(args[0]).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
