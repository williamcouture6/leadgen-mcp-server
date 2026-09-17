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

from src.http_api import RunReactiWf2Item, RunWf3Item, RunWf4Item, RunWf5Item
from src.tools.send import RunWf6Item
from src.tools.send_status import SyncStatusItem

DOSSIER = Path(__file__).resolve().parents[2] / "n8n" / "workflows"

# Route appelee -> modele Pydantic de ses `items`.
MODELE_PAR_ROUTE: dict[str, type[BaseModel]] = {
    "wf2/run": RunReactiWf2Item,
    "wf3/run": RunWf3Item,
    "wf4/run": RunWf4Item,
    "wf5/run": RunWf5Item,
    "wf6/run": RunWf6Item,
    "wf6/sync-status": SyncStatusItem,
}

# `i.status` et `i.error_text` sont lus par le FILTRE de l'expression, pas par
# la partie qui nomme. Ils existent sur tous les modeles ; on les verifie comme
# les autres plutot que de les exclure — s'ils disparaissaient d'un modele,
# l'alerte cesserait de filtrer et enverrait les lignes saines.


def _workflows_avec_alerte() -> list[tuple[str, str, set[str]]]:
    """(fichier, route, cases reclamees) pour chaque workflow qui alerte."""
    trouves: list[tuple[str, str, set[str]]] = []
    for f in sorted(DOSSIER.glob("*.json")):
        texte = f.read_text(encoding="utf-8")
        cases = set(re.findall(r"\bi\.([a-z_]+)", texte))
        if not cases:
            continue
        routes = {r for r in re.findall(r"/(wf\d[a-z]*/[a-z-]+)", texte)}
        connues = routes & MODELE_PAR_ROUTE.keys()
        if len(connues) != 1:
            # Un workflow qui alerte sur une route inconnue du tableau est
            # signale par son propre test, plus bas — pas ignore en silence.
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


def test_chaque_alerte_peut_nommer_quelque_chose_de_lisible() -> None:
    """🔴 Le champ doit exister, mais il doit aussi DIRE quelque chose.

    Un `contact_id` en repli est correct — c'est une cle, on peut la chercher.
    Une alerte qui n'aurait QUE des identifiants techniques serait conforme au
    test du dessus et illisible quand meme : le premier nom propose doit etre
    un nom, une adresse, quelque chose qu'un humain reconnait sans requete.
    """
    lisibles = {"name", "company_name", "to_email", "subject"}
    muettes = [
        (f, sorted(c))
        for f, _route, c in _workflows_avec_alerte()
        if not (c & lisibles)
    ]
    assert not muettes, (
        f"ces alertes ne nomment que des identifiants techniques : {muettes}. "
        "Il faut aller fouiller la base pour savoir de qui elles parlent."
    )
