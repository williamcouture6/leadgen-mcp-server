"""Une alerte doit pouvoir NOMMER ce qui est en panne.

🔴 PANNE REELLE DU 2026-09-17, 12 h 33. L'alerte de WF-4 est arrivee dans
#alertes avec, a la place du nom de l'entreprise, un point d'interrogation :

    Detail : ? : JSONDecodeError('Extra data: line 18 column 1 (char 2443)')

Ce n'etait pas un hasard de ce soir-la. L'expression n8n demandait une case
`name`, puis `company_id` ; les items de `/wf4/run` portent `company_name` et
`contact_id`. Deux cases qui n'existent pas. **Toutes** les alertes de WF-4
auraient dit « ? », pour toujours.

En verifiant, la meme faute existait dans 5 des 8 alertes — dont WF-6, celui
qui envoie reellement les courriels. Trois seulement demandaient les bonnes
cases. Personne ne pouvait le voir : rien ne relie l'expression ecrite dans un
JSON n8n aux modeles Pydantic qui decrivent ce que l'API rend.

Ce fichier est ce lien. Il lit les workflows du depot voisin, extrait les cases
que chaque alerte reclame, et les confronte aux champs REELS du modele de
l'endpoint que ce meme workflow appelle.

⚠️ Une alerte qui ne nomme pas sa victime coute exactement ce qu'elle devait
faire economiser : il faut aller fouiller la base pour savoir de qui on parle.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.http_api import RunReactiWf2Item, RunWf3Item, RunWf4Item
from src.tools.reply import PollRepliesItem
from src.tools.send import RunWf6Item
from src.tools.send_status import SyncStatusItem

DOSSIER = Path(__file__).resolve().parents[2] / "n8n" / "workflows"

# Route appelee -> modele Pydantic de ses `items`.
MODELE_PAR_ROUTE: dict[str, type[BaseModel]] = {
    "wf2/run": RunReactiWf2Item,
    "wf3/run": RunWf3Item,
    "wf4/run": RunWf4Item,
    "wf6/run": RunWf6Item,
    "wf6/sync-status": SyncStatusItem,
    # Ajoutee le 2026-09-17. Son absence faisait sauter wf-7-poll-replies.json
    # EN SILENCE, et c'est precisement le fichier dont l'alerte ne nommait
    # personne : `.map(i => i.error_text)`, sans le moindre identifiant.
    "wf7/poll-replies": PollRepliesItem,
}

# ⚠️ `wf5/run` N'EST PAS ICI, et c'est voulu : `wf-5-compliance.json` n'a aucun
# `i.*` — son alerte part du serveur (`RunWf5Out.alerte_envoyee`), pas de n8n.
# L'entree existait dans la premiere version ; elle n'etait jamais exercee et
# donnait l'impression d'une couverture qui n'existait pas.

# `i.status` et `i.error_text` sont lus par le FILTRE de l'expression, pas par
# la partie qui nomme. Ils existent sur tous les modeles ; on les verifie comme
# les autres plutot que de les exclure — s'ils disparaissaient d'un modele,
# l'alerte cesserait de filtrer et enverrait les lignes saines.


def _workflows_avec_alerte() -> list[tuple[str, str, set[str]]]:
    """(fichier, route, cases reclamees) pour chaque workflow qui alerte."""
    trouves: list[tuple[str, str, set[str]]] = []
    for f in sorted(DOSSIER.glob("*.json")):
        texte = f.read_text(encoding="utf-8")
        # ⚠️ `[a-z_]+` tronquait EN SILENCE sur un chiffre ou une majuscule :
        # `i.error_text2` rendait `error_text`, un champ qui existe -> vert a
        # tort. `i.errorText` rendait `error`, un champ inexistant -> rouge en
        # accusant un nom qu'aucun fichier ne porte. Les deux sont pires que
        # pas de test du tout.
        cases = set(re.findall(r"\bi\.([A-Za-z_][A-Za-z0-9_]*)", texte))
        if not cases:
            continue
        routes = {r for r in re.findall(r"/(wf\d[a-z]*/[a-z-]+)", texte)}
        connues = routes & MODELE_PAR_ROUTE.keys()
        if len(connues) != 1:
            # 🔴 CE `continue` EST LE TROU DU TEST, et il a menti une journee.
            # Le commentaire d'origine disait « signale par son propre test,
            # plus bas ». Ce test N'EXISTAIT PAS. Un conseil de relecture l'a
            # trouve le 2026-09-17, et le fichier qui en payait le prix etait
            # `wf-7-poll-replies.json` — dont l'alerte ne nommait personne.
            # Le test existe maintenant : `test_aucune_alerte_n_echappe_au_
            # controle`, juste en dessous.
            continue
        trouves.append((f.name, connues.pop(), cases))
    return trouves


def test_le_depot_n8n_est_bien_la() -> None:
    """Temoin. Sans lui, un chemin casse rendrait toute la suite vide et VERTE."""
    assert DOSSIER.is_dir(), f"workflows introuvables : {DOSSIER}"
    assert len(list(DOSSIER.glob("*.json"))) >= 15


def test_au_moins_cinq_alertes_sont_examinees() -> None:
    """Deuxieme temoin : si l'extraction cassait, la liste se viderait et les
    tests du dessous passeraient sans rien regarder."""
    examines = _workflows_avec_alerte()
    assert len(examines) >= 5, (
        f"seulement {len(examines)} alerte(s) trouvee(s) : l'extraction est "
        "cassee, ce fichier ne prouve plus rien"
    )


@pytest.mark.parametrize(
    ("fichier", "route", "cases"),
    [pytest.param(*t, id=t[0]) for t in _workflows_avec_alerte()],
)
def test_une_alerte_ne_reclame_que_des_cases_qui_existent(
    fichier: str, route: str, cases: set[str]
) -> None:
    reels = set(MODELE_PAR_ROUTE[route].model_fields)
    fantomes = sorted(cases - reels)
    assert not fantomes, (
        f"{fichier} interroge /{route} et demande {fantomes}, "
        f"que le modele {MODELE_PAR_ROUTE[route].__name__} ne porte pas. "
        f"Ses champs reels : {sorted(reels)}. L'alerte affichera « ? » a la "
        "place, et personne ne saura de quelle fiche il s'agit."
    )


def test_aucune_alerte_n_echappe_au_controle() -> None:
    """🔴 LE TEST QUE MON COMMENTAIRE PROMETTAIT SANS L'ECRIRE.

    `_workflows_avec_alerte` saute tout fichier dont la route n'est pas dans
    `MODELE_PAR_ROUTE`. Le commentaire affirmait qu'un autre test le signalait.
    Il n'existait pas — et le fichier qui en payait le prix, trouve par un
    conseil de relecture le 2026-09-17, etait `wf-7-poll-replies.json` : son
    alerte faisait `.map(i => i.error_text)`, sans le moindre identifiant, alors
    que `PollRepliesItem.lead_email` existe. La maladie que ce fichier pretend
    avoir eradiquee vivait dans le seul fichier qu'il ne regardait pas.

    Un `continue` silencieux dans un test est toujours une promesse a tenir
    ailleurs ; celui-ci est la promesse.
    """
    examines = {f for f, _r, _c in _workflows_avec_alerte()}
    echappees = []
    for f in sorted(DOSSIER.glob("*.json")):
        if f.name in examines:
            continue
        texte = f.read_text(encoding="utf-8")
        if re.search(r"i\.[A-Za-z_]", texte):
            routes = sorted(set(re.findall(r"/(wf\d[a-z]*/[a-z-]+)", texte)))
            echappees.append((f.name, routes))
    assert not echappees, (
        "ces workflows lisent des champs d'items mais aucun modele ne leur est "
        f"associe, donc personne ne verifie leurs alertes : {echappees}. "
        "Ajoute la route a MODELE_PAR_ROUTE."
    )


# Ce qu'un humain reconnait sans avoir a interroger la base.
LISIBLES = {"name", "company_name", "to_email", "lead_email", "subject"}

# `(i.a || i.b || '?')` — on veut l'ORDRE, pas seulement la presence.
_REPLIS = re.compile(r"\(\s*i\.([A-Za-z_][A-Za-z0-9_]*)"
                     r"(?:\s*\|\|\s*i\.([A-Za-z_][A-Za-z0-9_]*))*")


def test_chaque_alerte_nomme_quelque_chose_de_LISIBLE_EN_PREMIER() -> None:
    """🔴 Le champ doit exister, mais il doit aussi DIRE quelque chose, et
    ARRIVER EN PREMIER.

    Un identifiant technique en repli est correct — c'est une cle, on peut la
    chercher. Mais une alerte qui affiche `contact_id` AVANT `company_name`
    passe le test des champs existants et reste illisible : il faut une requete
    pour savoir de qui on parle, ce que l'alerte devait justement eviter.

    ⚠️ Une premiere version de ce test faisait une simple intersection
    d'ensembles tout en affirmant dans sa description qu'elle verifiait l'ordre.
    Un conseil de relecture l'a releve le 2026-09-17 : `(i.contact_id ||
    i.company_name || '?')` l'aurait passe au vert. Une description qui promet
    plus que le code est pire qu'un test absent — on cesse de regarder.
    """
    fautifs = []
    for fichier, _route, _cases in _workflows_avec_alerte():
        texte = (DOSSIER / fichier).read_text(encoding="utf-8")
        premiers = [m.group(1) for m in _REPLIS.finditer(texte)]
        # Le filtre (`.filter(i => i.status ...)`) n'est pas un repli : il
        # n'utilise pas de `||` entre deux `i.`. On ne garde que les groupes
        # qui nomment.
        nommants = [p for p in premiers if p not in ("status", "error_text")]
        if not nommants:
            fautifs.append((fichier, "aucun champ ne nomme quoi que ce soit"))
        elif nommants[0] not in LISIBLES:
            fautifs.append((fichier, f"commence par « {nommants[0] } »"))
    assert not fautifs, (
        f"ces alertes ne nomment pas lisiblement en premier : {fautifs}. "
        f"Mets un champ de {sorted(LISIBLES)} en tete du repli."
    )
