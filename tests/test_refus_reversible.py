"""Un refus « à revoir » renvoie à la réécriture ; seul « bloqué » est définitif.

🔴 DÉCISION WILLIAM, 2026-09-18 : « il faut faire en sorte de mieux tuner le
juge, car je commence à manquer de patience pour ce problème. »

LE DÉFAUT, MESURÉ. `needs_revision` écrivait EXACTEMENT la même chose que
`blocked` — `compliance_check_passed = false` — et la sélection de WF-5 ne
reprend que `is.null`. Le brouillon quittait donc le lot POUR TOUJOURS et son
contact restait gelé à vie.

Au 2026-09-18 : **12 entreprises gelées sur 93 jugées (13 %)**, et la relecture
des motifs a montré que la majorité étaient des FAUX POSITIFS. Le cas qui a
rendu la chose intenable : *Haute voltige déneigement*, gelée alors que le juge
écrit dans son propre verdict « aucune fabrication ni violation bloquante
détectée » — il la refusait pour du STYLE.

Trois jours de correctifs du juge, un par un, chacun rouvrant un trou ailleurs.
Ce fichier garde le changement qui casse le cycle : **une erreur du juge coûte
désormais une réécriture, pas un prospect.**
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

API = Path(__file__).resolve().parent.parent / "src/http_api.py"
PROMPT = Path(__file__).resolve().parent.parent / "src/prompts/compliance.md"


def _patch_de_verdict(verdict: str, tentatives_avant: int | None):
    """Rejoue la fonction de persistance sans monter toute la route."""
    from src import http_api

    for nom in dir(http_api):
        fn = getattr(http_api, nom)
        if callable(fn) and getattr(fn, "__doc__", None) and "verdict" in nom.lower():
            pass
    # La fonction est privée et son nom peut changer : on la retrouve par son
    # contenu plutôt que par un nom codé en dur.
    source = API.read_text(encoding="utf-8")
    arbre = ast.parse(source)
    cible = None
    for n in ast.walk(arbre):
        if isinstance(n, ast.FunctionDef):
            corps = ast.get_source_segment(source, n) or ""
            if '"compliance_verdict": verdict' in corps and "_MAX_REECRITURES" in corps:
                cible = n.name
    assert cible, "la fonction qui persiste le verdict est introuvable"
    return getattr(http_api, cible)(verdict, tentatives_avant)


@pytest.mark.parametrize("tentatives", [None, 0, 1])
def test_a_revoir_renvoie_a_la_reecriture(tentatives: int | None) -> None:
    """🔴 LE CŒUR DU CHANGEMENT.

    `failed` et pas un retour à `null` : `null` ferait rejuger le MÊME texte par
    le MÊME juge, indéfiniment. `failed` libère l'entreprise — `db._retenir`
    exclut `not.in.(failed)` — donc WF-4 en écrit un NEUF. Ce qui a été refusé
    est réécrit, pas relu.
    """
    patch = _patch_de_verdict("needs_revision", tentatives)
    assert patch.get("status") == "failed", (
        "un brouillon « à revoir » reste bloqué : son contact est gelé à vie"
    )
    assert patch["compliance_check_passed"] is False


def test_bloque_reste_definitif() -> None:
    """Un `blocked` dit « mensonge vérifiable ». Un mensonge ne se corrige pas
    en réécrivant : il vient de la donnée, pas de la plume."""
    patch = _patch_de_verdict("blocked", 0)
    assert "status" not in patch, (
        "un blocage renverrait à la réécriture : on redemanderait au rédacteur "
        "de mentir autrement"
    )
    assert patch["compliance_check_passed"] is False


def test_le_plafond_rend_le_refus_definitif() -> None:
    """⚠️ SANS PLAFOND, un brouillon que le juge refuse systématiquement tourne
    en rond et consomme une place du lot chaque jour."""
    from src.http_api import _MAX_REECRITURES

    patch = _patch_de_verdict("needs_revision", _MAX_REECRITURES)
    assert "status" not in patch, (
        f"au-delà de {_MAX_REECRITURES} réécritures, le refus doit redevenir "
        "définitif — sinon la même fiche revient indéfiniment"
    )


def test_approuve_ne_touche_pas_au_statut() -> None:
    patch = _patch_de_verdict("approved", 0)
    assert "status" not in patch
    assert patch["compliance_check_passed"] is True


def test_une_config_absente_ne_compte_pas_comme_une_tentative() -> None:
    """La garde du 2026-09-11, qu'il ne faut pas casser en passant : un `error`
    n'écrit ni verdict ni compteur. Une variable d'environnement manquante ne
    doit pas atteindre le plafond en trois passes."""
    patch = _patch_de_verdict("error", 0)
    assert patch == {"compliance_verdict": "error"}


# ── Les trois phrases qui ont produit les faux positifs du 2026-09-18 ───────


def test_le_juge_a_interdiction_de_noter_la_copie() -> None:
    """📏 *Haute voltige déneigement*, refusée avec « aucune fabrication ni
    violation bloquante détectée, mais le courriel n'exploite pas les hooks de
    personnalisation les plus forts ». Le juge disait lui-même qu'il n'y avait
    pas de faute."""
    texte = PROMPT.read_text(encoding="utf-8")
    assert "TU NE NOTES PAS LA COPIE" in texte
    assert "Un courriel améliorable n'est pas un courriel fautif" in texte


def test_avoir_une_equipe_n_est_pas_un_motif_de_refus() -> None:
    """📏 Un brouillon refusé pour « une PME de 5-10 employés avec équipe ».

    🔴 Toute la cible est faite de PME de 5 à 25 employés qui ont des équipes
    sur le terrain — c'est justement POURQUOI on leur écrit : les gars sont
    dehors, personne ne décroche. Sans cette borne, presque toute la liste
    devient refusable.
    """
    texte = PROMPT.read_text(encoding="utf-8")
    assert "AVOIR UNE ÉQUIPE NE SUFFIT PAS" in texte
    for metier in ("répartiteur", "réceptionniste", "centre d'appels"):
        assert metier in texte, (
            f"« {metier} » a disparu : la permission ne nomme plus ce qui "
            "distingue une vraie panne de ce profil normal"
        )


def test_l_asphalte_est_nomme_comme_racine_de_pavage() -> None:
    """📏 Un brouillon refusé parce que l'entreprise fait « entretien/réparation
    d'asphalte, pas pose neuve » — l'arbitrage pose-contre-réparation que la
    décision du 2026-09-16 écarte.

    🔴 Ce test lie le prompt au DICTIONNAIRE plutôt qu'à une chaîne : si
    `asphalte` cessait d'être une racine de `pavage`, la consigne deviendrait
    fausse et ce test le dirait.
    """
    from src.lib.metiers import RACINES

    assert "asphalte" in RACINES["pavage"], (
        "l'asphalte n'est plus une racine de pavage : la consigne du prompt "
        "est devenue fausse"
    )
    texte = PROMPT.read_text(encoding="utf-8")
    assert "asphalte" in texte.lower()
    assert re.search(r"POSER, RÉPARER, SCELLER, NIVELER", texte), (
        "la consigne ne dit plus explicitement que réparer vaut poser"
    )
