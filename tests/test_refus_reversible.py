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


def _patch_de_verdict(
    verdict: str, tentatives_avant: int | None, refus_deja_subis: int = 0
):
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
    return getattr(http_api, cible)(verdict, tentatives_avant, refus_deja_subis)


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
    """⚠️ SANS PLAFOND, un contact que le juge refuse systématiquement tourne en
    rond et consomme une place du lot chaque jour."""
    from src.http_api import _MAX_REECRITURES

    patch = _patch_de_verdict("needs_revision", 0, _MAX_REECRITURES)
    assert "status" not in patch, (
        f"au-delà de {_MAX_REECRITURES} réécritures, le refus doit redevenir "
        "définitif — sinon le même contact revient indéfiniment"
    )


def test_le_plafond_NE_compte_PAS_les_tentatives_du_brouillon() -> None:
    """🔴 LE DÉFAUT QUE CE TEST AURAIT ATTRAPÉ, et qui a vécu trois heures.

    La première version du plafond lisait `messages.compliance_tentatives`.
    Elle ne pouvait PAS fonctionner : ce compteur porte sur UN brouillon, et
    chaque réécriture en crée un NEUF, dont le compteur repart à zéro.

    📏 Mesuré le 2026-09-18 : *Entretien V Boudreault*, TROIS brouillons en onze
    heures, tous refusés pour la même phrase (« tu fais de la tonte »), et
    `compliance_tentatives = 1` sur les trois. Le plafond n'était jamais
    atteint — il comptait un objet neuf à chaque tour.

    Ce qui boucle est le couple (contact, données) : le rédacteur relit les
    mêmes services et réécrit le même texte. Ce test fige la distinction.
    """
    from src.http_api import _MAX_REECRITURES

    # Un brouillon déjà passé DIX fois devant le juge, mais dont le CONTACT
    # n'a jamais été réécrit : il doit encore avoir droit à sa réécriture.
    patch = _patch_de_verdict("needs_revision", 10, 0)
    assert patch.get("status") == "failed", (
        "le plafond s'est remis à compter les passages du juge sur UN "
        "brouillon — il ne se déclenchera jamais, puisqu'une réécriture crée "
        "un message neuf"
    )

    # L'inverse : un brouillon neuf chez un contact déjà réécrit au plafond.
    patch = _patch_de_verdict("needs_revision", 0, _MAX_REECRITURES)
    assert "status" not in patch


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


def test_l_asphalte_arrive_au_juge_PAR_LA_LISTE() -> None:
    """🔴 CE TEST A CHANGE DE MOYEN LE 2026-09-18, pas d'objet.

    Le 2026-09-17, un brouillon avait ete refuse parce que l'entreprise fait
    « entretien/reparation d'asphalte, PAS pose neuve » — l'arbitrage
    pose-contre-reparation que la decision de William du 2026-09-16 ecarte.
    La correction d'alors : ecrire dans le prompt que l'asphalte est du pavage,
    et que poser vaut reparer.

    Le lendemain, cette phrase est PARTIE avec tout le dictionnaire recopie.
    Le juge ne l'apprend plus — il la LIT, dans la liste du bloc « Faits
    verifies », calculee par le code.

    Ce test suit le meme fait, par le chemin neuf : les services reels de
    *Scellant Deneigement XTRA* doivent rendre `pavage` dans la liste que le
    juge recoit. Si `asphalte` cessait d'etre une racine de `pavage`, il
    rougirait — comme avant, mais sur le mecanisme qui decide vraiment.
    """
    from src.lib.avis import bloc_faits_verifies
    from src.lib.metiers import RACINES, metiers_nommables

    services = [
        "Scellant d'asphalte au bitume",
        "Réparation de fissures d'asphalte à chaud",
        "Réparation de nids-de-poule",
        "Déneigement résidentiel",
    ]
    assert "asphalte" in RACINES["pavage"], (
        "l'asphalte n'est plus une racine de pavage : le refus du 2026-09-17 "
        "redeviendrait legitime"
    )
    nommables = metiers_nommables(services)
    assert "pavage" in nommables, nommables

    bloc = bloc_faits_verifies(4.7, 85, metiers_nommables=nommables)
    assert "pavage" in bloc, (
        "le juge ne voit pas `pavage` dans sa liste : il refera l'arbitrage "
        "pose-contre-reparation, et le refusera comme le 2026-09-17"
    )


def test_le_prompt_n_enseigne_PLUS_l_asphalte() -> None:
    """La contre-epreuve : la phrase ajoutee le 2026-09-17 doit avoir disparu.

    La garder ferait deux verites — le prompt dirait une chose, la liste une
    autre — et c'est exactement ce que le changement du 2026-09-18 ferme.
    """
    texte = PROMPT.read_text(encoding="utf-8")
    assert "asphalte" not in texte.lower(), (
        "le dictionnaire est revenu dans le prompt : il se sert, il ne se "
        "recopie pas"
    )
