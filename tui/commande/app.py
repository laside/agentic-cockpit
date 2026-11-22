#!/usr/bin/env python3
"""Onglet Commande : écran Textual au-dessus du modèle (model.py) et des actions (actions.py).

Lancement : python -m tui.commande.app <projet>
Textual n'est utilisé que par ce module ; le cœur du cockpit reste en bibliothèque standard.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_RACINE = Path(__file__).resolve().parents[2]
if str(_RACINE) not in sys.path:
    sys.path.insert(0, str(_RACINE))

from rich.syntax import Syntax  # noqa: E402
from rich.text import Text  # noqa: E402
from textual import on, work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.css.query import NoMatches  # noqa: E402
from textual.screen import ModalScreen, Screen  # noqa: E402
from textual.widgets import Button, DataTable, Footer, OptionList, RichLog, Select, Static  # noqa: E402
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
ACTIONS_BASCULE = {"promote", "demote"}  # DRAFT <-> READY : courte, écran rechargé aussitôt

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


# ---------- fenêtres modales ----------
ICONES = {"brief": "📋", "costs": "💶", "auto_dry": "🔁", "sortie": "📄"}
GENRES_PAR_TITRE = {"Brief": "brief", "Coûts": "costs", "Simulation de la boucle": "auto_dry"}
CONSEQUENCES = {
    "approve": "Approuve le ticket et ouvre une PR sur GitHub.",
    "abandon": "Passe le ticket à l'état abandonné ; il ne sera plus lancé.",
    "promote": "Passe le brouillon à l'état prêt ; la boucle pourra le lancer.",
    "run": "Lance un worker sur la voie choisie.",
    "sync": "Enregistre la fusion de la PR et met à jour le ticket.",
    "sync_projet": "Enregistre les PR fusionnées et nettoie les worktrees.",
}
RE_VOIE = re.compile(
    r"^voie (?P<voie>\S+) : (?P<runs>\d+) run\(s\) · entrée (?P<in>\d+) · sortie (?P<out>\d+)"
    r" · cache (?P<cache>\d+) · (?P<cout>.*)$")
RE_TOTAL_TOK = re.compile(r"^TOTAL workers : entrée (\d+) · sortie (\d+) · cache (\d+)")
RE_TOTAL_EUR = re.compile(r"^TOTAL coût mesuré : ([\d,]+) €")
RE_EUR = re.compile(r"([\d,]+) € mesuré")
RE_USD = re.compile(r"([\d,]+) \$ équivalent API")
RE_LANCE = re.compile(r"^(?P<id>[A-Z]+-\d+)\s{2,}(?P<voie>\S+)(?:\s{2,}(?P<fichiers>.*))?$")
RE_FILE_BRIEF = re.compile(r"^[A-Z_0-9]+ \(\d+\) :")


def _montant(texte: str | None) -> float | None:
    return float(texte.replace(",", ".")) if texte else None


def analyser_couts(texte: str) -> dict[str, Any] | None:
    """Voies et totaux de « cockpit costs » ; None si la section WORKERS est illisible.
    Euros payés, équivalent API ($) et tokens restent dans des colonnes distinctes."""
    voies: list[dict[str, Any]] = []
    total_tok: tuple[int, ...] | None = None
    total_eur: float | None = None
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if m := RE_VOIE.match(ligne):
            eur, usd = RE_EUR.search(m["cout"]), RE_USD.search(m["cout"])
            voies.append({"voie": m["voie"], "runs": int(m["runs"]), "in": int(m["in"]),
                          "out": int(m["out"]), "cache": int(m["cache"]),
                          "eur": _montant(eur[1] if eur else None),
                          "usd": _montant(usd[1] if usd else None)})
        elif m := RE_TOTAL_TOK.match(ligne):
            total_tok = tuple(int(x) for x in m.groups())
        elif m := RE_TOTAL_EUR.match(ligne):
            total_eur = _montant(m[1])
    if total_tok is None or (not voies and "aucun run enregistré" not in texte):
        return None
    usd_l = [v["usd"] for v in voies if v["usd"] is not None]
    return {"voies": voies, "tokens": total_tok, "eur": total_eur or 0.0,
            "usd": sum(usd_l) if usd_l else None, "runs": sum(v["runs"] for v in voies)}


SECTIONS_BRIEF = [("Branche", "🌿"), ("Tickets", "🎫"), ("Prochain ticket", "➡"),
                  ("Événements", "🕑"), ("Coût du jour", "💶")]


def analyser_brief(texte: str) -> list[tuple[str, list[str]]] | None:
    """Range les lignes de « cockpit brief » par section ; None si rien n'est reconnu."""
    sections: dict[str, list[str]] = {nom: [] for nom, _ in SECTIONS_BRIEF}
    courante, reconnu = "Branche", False
    for brute in texte.splitlines():
        ligne = brute.strip()
        if not ligne:
            continue
        haut = ligne.upper()
        if haut.startswith(("PROJET ", "DERNIER COMMIT", "ÉCART")):
            courante, reconnu = "Branche", True
        elif haut.startswith("TICKETS") or RE_FILE_BRIEF.match(ligne):
            courante, reconnu = "Tickets", True
        elif haut.startswith("PROCHAIN"):
            courante, reconnu = "Prochain ticket", True
        elif haut.startswith("COÛT DU JOUR"):
            sections["Coût du jour"].append(ligne)
            reconnu = True
            continue
        elif haut.startswith("DERNIERS ÉVÉNEMENTS"):
            courante, reconnu = "Événements", True
            continue
        sections[courante].append(ligne)
    if not reconnu:
        return None
    return [(nom, sections[nom]) for nom, _ in SECTIONS_BRIEF if sections[nom]]


def analyser_simulation(texte: str) -> list[dict[str, str]]:
    """Tickets que la boucle lancerait (ordre, ticket, voie, fichiers)."""
    lances: list[dict[str, str]] = []
    for ligne in texte.splitlines():
        if m := RE_LANCE.match(ligne.strip()):
            lances.append({"ordre": str(len(lances) + 1), "id": m["id"], "voie": m["voie"],
                           "fichiers": m["fichiers"] or ""})
    return lances


def fichiers_du_diff(texte: str) -> list[str]:
    fichiers: list[str] = []
    for ligne in texte.splitlines():
        if ligne.startswith("diff --git "):
            nom = ligne.split(" b/", 1)[-1]
        elif ligne.startswith("+++ ") and not ligne.startswith("+++ /dev/null"):
            nom = ligne[4:].removeprefix("b/")
        else:
            continue
        if nom not in fichiers:
            fichiers.append(nom)
    return fichiers


def texte_titres(texte: str) -> Text:
    """Texte à espacement fixe ; les lignes en majuscules (titres) sont mises en valeur."""
    t = Text()
    for ligne in texte.splitlines():
        lettres = [c for c in ligne if c.isalpha()][:6]
        titre = bool(lettres) and all(c.isupper() for c in lettres)
        t.append(ligne + "\n", style="bold #88c0d0" if titre else "")
    return t


class FenetreModale(ModalScreen[Any]):
    """Cadre commun : en-tête (icône, titre, projet, heure), zone défilante, pied Copier / Fermer."""

    BINDINGS = [Binding("escape", "fermer", "Fermer")]
    ID_FERMER = "fenetre-fermer"
    ICONE = "📄"

    def __init__(self, titre: str, contenu: str, projet: str = "") -> None:
        super().__init__()
        self.titre = titre
        self.contenu = contenu
        self.projet = projet
        self.heure = time.strftime("%H:%M:%S")

    def compose(self) -> ComposeResult:
        with Vertical(classes="fenetre"):
            with Horizontal(classes="fenetre-entete"):
                yield Static(Text.assemble(f"{self.ICONE}  ", (self.titre, "bold #e5f0ff"),
                                           (f"  · {self.projet}" if self.projet else "", "#88c0d0")),
                             classes="fenetre-titre")
                yield Static(f"mis à jour {self.heure}", classes="fenetre-heure")
            with VerticalScroll(classes="fenetre-defil"):
                yield from self.corps()
            with Horizontal(classes="fenetre-pied"):
                yield from self.boutons()
                yield Static("Échap pour fermer", classes="fenetre-rappel")

    def corps(self) -> ComposeResult:
        yield Static(Text(self.contenu or "(aucune sortie)"), classes="fenetre-texte")

    def boutons(self) -> ComposeResult:
        yield Button("Copier", id="copier", classes="fenetre-copier")
        yield Button("Fermer", id=self.ID_FERMER, classes="fenetre-fermer")

    @on(Button.Pressed, "#copier")
    def _copier(self, ev: Button.Pressed) -> None:
        ev.stop()
        self.app.copy_to_clipboard(self.contenu)
        self.notify("Copié dans le presse-papiers.")

    @on(Button.Pressed, ".fenetre-fermer")
    def _fermer(self, ev: Button.Pressed) -> None:
        ev.stop()
        self.action_fermer()

    def action_fermer(self) -> None:
        self.dismiss(None)


class ConfirmScreen(FenetreModale):
    BINDINGS = [Binding("escape", "annuler", "Annuler")]
    ICONE = "⚠"

    def __init__(self, message: str, ticket: str = "", consequence: str = "",
                 projet: str = "", libelle: str = "Confirmer") -> None:
        super().__init__("Confirmation", message, projet)
        self.message = message
        self.ticket = ticket
        self.consequence = consequence
        self.libelle = libelle

    def corps(self) -> ComposeResult:
        question, _, reste = self.message.partition("\n")
        t = Text(question + "\n\n", style="bold #ebcb8b")
        if self.ticket:
            t.append("Ticket : ", style="#88c0d0")
            t.append(self.ticket + "\n", style="bold")
        if self.consequence:
            t.append("Conséquence : ", style="#88c0d0")
            t.append(self.consequence + "\n")
        if reste.strip():
            t.append("\n" + reste.strip() + "\n", style="#7b88a1")
        yield Static(t, classes="fenetre-texte")

    def boutons(self) -> ComposeResult:
        yield Button("Copier", id="copier", classes="fenetre-copier")
        yield Button(self.libelle, id="oui", variant="warning", classes="fenetre-oui")
        yield Button("Annuler", id="non", classes="fenetre-non")

    @on(Button.Pressed, "#oui")
    def _oui(self, ev: Button.Pressed) -> None:
        ev.stop()
        self.dismiss(True)

    @on(Button.Pressed, "#non")
    def _non(self, ev: Button.Pressed) -> None:
        ev.stop()
        self.dismiss(False)

    def action_annuler(self) -> None:
        self.dismiss(False)

    def action_fermer(self) -> None:
        self.dismiss(False)


class DiffScreen(FenetreModale):
    BINDINGS = [Binding("escape", "fermer", "Fermer"), Binding("q", "fermer", "Fermer")]
    ID_FERMER = "diff-fermer"
    ICONE = "🧾"

    def __init__(self, contenu: str, projet: str = "", titre: str = "Diff du worktree") -> None:
        super().__init__(titre, contenu, projet)

    def corps(self) -> ComposeResult:
        if not self.contenu.strip():
            yield Static("(diff vide)", classes="fenetre-texte")
            return
        fichiers = fichiers_du_diff(self.contenu)
        t = Text(f"Fichiers modifiés ({len(fichiers)})\n", style="bold #88c0d0")
        for f in fichiers or ["(aucun fichier repéré)"]:
            t.append(f"  • {f}\n")
        yield Static(t, id="diff-fichiers", classes="fenetre-section")
        yield Static(Syntax(self.contenu, "diff", theme="monokai", background_color="#0f1a2e"),
                     id="diff-texte", classes="fenetre-texte")


def _eur(v: float | None, gras: bool = False) -> Text:
    if v is None:
        return Text("—", style="grey50", justify="right")
    couleur = "#ebcb8b" if v > 0 else "#a3be8c"
    return Text(f"{v:.2f} €".replace(".", ","), justify="right", style=f"bold {couleur}" if gras else couleur)


def _usd(v: float | None, gras: bool = False) -> Text:
    if v is None:
        return Text("—", style="grey50", justify="right")
    return Text(f"{v:.2f} $".replace(".", ","), justify="right", style="bold #b48ead" if gras else "#b48ead")


def _tok(v: int, gras: bool = False) -> Text:
    return Text(f"{v:,}".replace(",", " "), justify="right", style="bold" if gras else "")


class SortieScreen(FenetreModale):
    """Sortie d'une commande du haut (Brief, Coûts, Simulation), mise en forme si possible."""

    ID_FERMER = "sortie-fermer"

    def __init__(self, titre: str, contenu: str, projet: str = "", genre: str = "") -> None:
        super().__init__(titre, contenu, projet)
        self.genre = genre or GENRES_PAR_TITRE.get(titre, "sortie")
        self.ICONE = ICONES.get(self.genre, "📄")
        self.erreur = contenu.startswith(("Erreur", "Commande introuvable", "Échec"))

    def corps(self) -> ComposeResult:
        if not self.erreur and self.genre == "costs":
            yield from self._corps_couts()
        elif not self.erreur and self.genre == "brief":
            yield from self._corps_brief()
        elif not self.erreur and self.genre == "auto_dry":
            yield from self._corps_simulation()
        else:
            classes = "fenetre-texte fenetre-erreur" if self.erreur else "fenetre-texte"
            yield Static(Text(self.contenu or "(aucune sortie)"), classes=classes)

    def _corps_couts(self) -> ComposeResult:
        res = analyser_couts(self.contenu)
        if res is None:
            yield Static(texte_titres(self.contenu or "(aucune sortie)"), classes="fenetre-texte fenetre-fixe")
            return
        yield Static(Text("Workers par voie — euros payés, équivalent API et tokens ne s'additionnent jamais",
                          style="bold #88c0d0"), classes="fenetre-section")
        table: DataTable = DataTable(id="couts-table", zebra_stripes=True, cursor_type="row")
        table.add_columns("Voie", "Runs", "Payé (€)", "Équiv. API ($)",
                          "Tokens entrée", "Tokens sortie", "Tokens cache")
        for v in res["voies"]:
            table.add_row(Text(v["voie"], style="#88c0d0"), _tok(v["runs"]), _eur(v["eur"]), _usd(v["usd"]),
                          _tok(v["in"]), _tok(v["out"]), _tok(v["cache"]))
        tin, tout, tcache = res["tokens"]
        table.add_row(Text("TOTAL", style="bold"), _tok(res["runs"], True), _eur(res["eur"], True),
                      _usd(res["usd"], True), _tok(tin, True), _tok(tout, True), _tok(tcache, True))
        yield table
        _, _, apres = self.contenu.partition("TOTAL coût mesuré")
        suite = apres.partition("\n")[2].strip("\n")
        if suite.strip():
            yield Static(texte_titres(suite), classes="fenetre-texte fenetre-fixe")

    def _corps_brief(self) -> ComposeResult:
        sections = analyser_brief(self.contenu)
        if sections is None:
            yield Static(Text(self.contenu or "(aucune sortie)"), classes="fenetre-texte")
            return
        icones = dict(SECTIONS_BRIEF)
        for nom, lignes in sections:
            t = Text(f"{icones[nom]}  {nom}\n", style="bold #88c0d0")
            t.append("\n".join(lignes))
            yield Static(t, classes="fenetre-bloc")

    def _corps_simulation(self) -> ComposeResult:
        lances = analyser_simulation(self.contenu)
        if not lances:
            yield Static(Text("✓ Rien à lancer : aucun ticket exécutable dans la file.", style="bold #a3be8c"),
                         id="simulation-vide", classes="fenetre-section")
            return
        yield Static(Text(f"{len(lances)} ticket(s) seraient lancés, dans cet ordre :", style="bold #88c0d0"),
                     classes="fenetre-section")
        table: DataTable = DataTable(id="simulation-table", zebra_stripes=True, cursor_type="row")
        table.add_columns("Ordre", "Ticket", "Voie", "Fichiers")
        for x in lances:
            table.add_row(Text(x["ordre"], justify="right"), Text(x["id"], style="bold"),
                          Text(x["voie"], style="#88c0d0"), x["fichiers"])
        yield table


class CommandeApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "Cockpit — Commande"
    BINDINGS = [
        Binding("q", "quit", "Quitter"),
        Binding("r", "rafraichir", "Rafraîchir"),
        Binding("f", "filtre_suivant", "Filtre suivant"),
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
        projets = sorted({self.projet, *self.model.projets_visibles().keys()})
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

    @on(Select.Changed, "#voie")
    async def _voie_changee(self, ev: Select.Changed) -> None:
        self.voie = "" if ev.value is Select.BLANK else str(ev.value)
        if self._verrou:
            async with self._verrou:
                await self._maj_boutons()

    @on(Select.Changed, "#projet")
    def _projet_change(self, ev: Select.Changed) -> None:
        if ev.value is Select.BLANK or str(ev.value) == self.projet:
            return  # projet déjà affiché : rien à faire
        cible = str(ev.value)
        if os.environ.get("ZELLIJ") is not None:
            # Dans Zellij : on amène la session du projet ; l'écran courant garde son projet.
            self._w("#projet", Select).value = self.projet
            self._noter(f"Bascule vers la session du projet {cible}…")
            self._changer_session(cible)
            return
        self.projet = cible
        self.ticket_id, self.detail, self.tickets = None, None, []
        self.action_rafraichir()

    @work(thread=True, group="session")
    def _changer_session(self, cible: str) -> None:
        """Lance « cockpit switch <projet> » ; toute erreur va au journal, jamais de plantage."""
        try:
            res = subprocess.run([actions_mod.CLI, "switch", cible], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, errors="replace",
                                 timeout=DELAI_SORTIE, check=False)
            ligne = (f"Session du projet {cible} : bascule demandée." if res.returncode == 0
                     else f"Erreur de bascule vers {cible} (code {res.returncode}) "
                          f"{(res.stdout + res.stderr).strip()}")
        except FileNotFoundError:
            ligne = f"Erreur de bascule vers {cible} : commande introuvable ({actions_mod.CLI})"
        except Exception as exc:  # noqa: BLE001 - jamais de plantage de l'écran
            ligne = f"Erreur de bascule vers {cible} : {exc}"
        self.call_from_thread(self._noter, ligne)

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

    # ---------- exécution ----------
    def demander(self, a: Action) -> None:
        if self._desactive(a):
            self._noter(f"« {a.libelle} » indisponible pour le moment.")
            return
        if a.confirmation:
            def retour(ok: bool | None) -> None:
                if ok:
                    self.executer(a)
            du_ticket = any(x.id == a.id for x in self.actions_ticket)
            self.push_screen(ConfirmScreen(f"Confirmer : {a.libelle} ?\n\n{' '.join(a.argv)}",
                                           ticket=(self.ticket_id or "") if du_ticket else "",
                                           consequence=CONSEQUENCES.get(a.id) or info_bulle(a),
                                           projet=self.projet, libelle=a.libelle), retour)
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
        if a.id in ACTIONS_BASCULE:
            self._noter(f"Lancé : {a.libelle}")
            self._basculer(a)
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

    @work(thread=True, group="bascule")
    def _basculer(self, a: Action) -> None:
        """Marquer prêt / Remettre en brouillon : exécution courte, puis rafraîchissement immédiat."""
        try:
            res = subprocess.run(a.argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 errors="replace", timeout=DELAI_SORTIE, check=False)
            sortie = (res.stdout + res.stderr).strip()
            ligne = (f"Terminé : {a.libelle} (code 0)" if res.returncode == 0
                     else f"Erreur : {a.libelle} (code {res.returncode}) {sortie}")
        except FileNotFoundError:
            ligne = f"Commande introuvable : {a.argv[0]}"
        except Exception as exc:  # noqa: BLE001 - jamais de plantage de l'écran
            ligne = f"Échec de « {a.libelle} » : {exc}"
        self.call_from_thread(self._noter, ligne)
        self.call_from_thread(self.action_rafraichir)

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
        self.call_from_thread(self._montrer_sortie, a.libelle, texte, ligne, a.id)

    def _montrer_sortie(self, titre: str, texte: str, ligne: str, genre: str = "") -> None:
        self._noter(ligne)
        self.push_screen(SortieScreen(titre, texte, projet=self.projet, genre=genre))

    @work(thread=True, group="diff")
    def _afficher_diff(self, a: Action) -> None:
        try:
            res = subprocess.run(a.argv, capture_output=True, text=True, timeout=30, check=False)
            texte = res.stdout if res.returncode == 0 else f"Erreur git ({res.returncode}) :\n{res.stderr}"
        except Exception as exc:  # noqa: BLE001
            texte = f"Diff impossible : {exc}"
        self.call_from_thread(self.push_screen, DiffScreen(texte, projet=self.projet))

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
