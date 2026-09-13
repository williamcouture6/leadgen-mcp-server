"""Le métier figé sur le message n'est pas `MetiersResolus.scene`.

🔴 LE PIÈGE. Quand aucune fenêtre n'est ouverte, le courriel parle du DOMINANT
pendant que `r.scene` vaut None. Écrire `r.scene` sur le message enregistrerait
NULL exactement sur les cas hors saison — ceux pour lesquels la colonne existe.
"""
from __future__ import annotations

import datetime
from typing import Any

import pytest

from src.tools.personalize import metier_de_la_scene

SEPTEMBRE = datetime.date(2026, 9, 15)


def test_en_saison_la_scene_est_le_metier_ouvert():
    assert metier_de_la_scene(["Déneigement résidentiel"], SEPTEMBRE) == "déneigement"


def test_hors_saison_la_scene_est_le_DOMINANT_pas_None():
    """Un paysagiste pur en septembre : sa fenêtre est fermée, la copie parle
    quand même de paysagement. Le message doit l'enregistrer."""
    services = ["Aménagement paysager", "Plantations", "Haies"]
    assert metier_de_la_scene(services, SEPTEMBRE) == "paysagement"


def test_aucun_metier_reconnu_rend_None():
    assert metier_de_la_scene(["Consultation"], SEPTEMBRE) is None


# ---------------- Le câblage : le brouillon PORTE-T-IL la valeur ? ----------------
#
# 🔴 Le test qui compte. La règle peut être juste et n'être appelée par
# personne — ce dépôt a déjà payé ça trois fois (les `google_*` du dict
# `company`, le plancher d'avis, le décorateur @retry du juge). Patron repris
# de `tests/test_avis_cablage.py`, avec deux ajouts : le faux `_call_llm` (pour
# traverser le VRAI `personalize()`, seul endroit où la valeur est calculée) et
# `persist=True` (sans lui, `insert_message_draft` n'est jamais appelé).


class _FauxSeptembre(datetime.date):
    """Fige la date DU CÔTÉ DE LA COPIE. Sans ça le test dirait « vert » en
    septembre et rougirait en juin, alors que c'est justement le cas hors
    saison qu'il doit tenir."""

    @classmethod
    def today(cls) -> datetime.date:
        return SEPTEMBRE


@pytest.mark.asyncio
async def test_le_brouillon_porte_le_metier_hors_saison(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un paysagiste pur, en septembre : fenêtre fermée, `r.scene` vaut None.
    Le brouillon inséré doit quand même porter « paysagement » — sinon la
    colonne est NULL exactement sur les lignes pour lesquelles elle existe."""
    from src import http_api

    def faux_llm(user_message: str, model: str, max_tokens: int = 2500, track: str = "OPT"):
        return (
            {"template_used": "A", "subject": "s", "body_text": "corps", "warnings": []},
            http_api.personalize_tools.LLMUsage(),
        )

    monkeypatch.setattr(http_api.personalize_tools, "_call_llm", faux_llm)
    monkeypatch.setattr(http_api.personalize_tools, "date", _FauxSeptembre)

    async def faux_record_agent_run(payload):
        return {"agent_run_id": "ar-1"}

    capture: dict[str, Any] = {}

    async def faux_insert(payload):
        capture["payload"] = payload
        return {"message_id": "msg-1"}

    monkeypatch.setattr(http_api.db_tools, "record_agent_run", faux_record_agent_run)
    monkeypatch.setattr(http_api.db_tools, "insert_message_draft", faux_insert)

    await http_api._personalize_one(
        {"id": "ct-1", "email": "a@ex.ca"},
        {
            "id": "co-1",
            "name": "Paysagement Rivard",
            "website": "https://ex.ca",
            "city": "Lévis",
            "track": "agence-ia",
            "research_json": {
                "services_offered": ["Aménagement paysager", "Plantations", "Haies"]
            },
        },
        template_choice="A",
        model="m",
        persist=True,
        available_slots=[],
        social_proof=[],
    )

    payload = capture["payload"]
    assert payload.metier_scene, (
        "le brouillon ne porte AUCUN métier de scène : hors saison, "
        "`MetiersResolus.scene` vaut None et la valeur a été perdue en route"
    )
    assert payload.metier_scene == "paysagement"
    assert payload.metiers == ["paysagement"]


def test_la_date_est_lue_UNE_SEULE_FOIS_et_descend_au_prompt():
    """🔴 Mesure par mutation le 2026-09-12 : supprimer `aujourdhui=aujourdhui`
    dans l'appel a `_format_input_for_llm` passait les 1776 tests.

    Pourquoi c'est silencieux : le parametre a un defaut (`None`) qui retombe sur
    `date.today()`. La suppression ne casse rien, elle ramene simplement DEUX
    lectures d'horloge dans le meme appel — et une colonne pourrait alors nommer
    un metier dont la copie n'a pas parle, au tour de minuit, la veille d'une
    ouverture de fenetre.

    ⚠️ Le controle est SUR LA SOURCE, et c'est assume. Exercer la plomberie
    demanderait de simuler l'appel LLM complet ; ce qu'on veut tenir ici est
    exactement ce que la mutation retirait, et une assertion de source le tient
    sans faux-semblant. Elle dit ce qu'elle verifie : le kwarg est passe.
    """
    import inspect

    from src.tools import personalize as perso

    src = inspect.getsource(perso.personalize)
    assert "aujourdhui = date.today()" in src, (
        "personalize() ne lit plus l'horloge une fois pour toutes"
    )
    assert "aujourdhui=aujourdhui" in src, (
        "la date ne descend plus au prompt : `_format_input_for_llm` va retomber "
        "sur son propre date.today(), donc DEUX lectures d'horloge dans le meme "
        "appel"
    )
    # Et le defaut qui rend la suppression silencieuse existe bien : c'est LUI
    # qui justifie ce test plutot qu'une confiance dans la signature.
    assert inspect.signature(
        perso._format_input_for_llm
    ).parameters["aujourdhui"].default is None
