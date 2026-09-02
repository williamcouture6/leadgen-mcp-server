"""Éligibilité WF-4 : un message ABANDONNÉ ne doit plus geler son contact.

Défaut corrigé : `list_contacts_to_personalize` excluait tout contact ayant un
message outbound, quel que soit son status. Un draft refusé par WF-5
(`compliance_check_passed = false`, jamais re-jugé car WF-5 ne juge que le NULL)
gelait donc son contact à vie ; la seule sortie était de DELETE le message, ce
qui détruit la trace du refus. `status = 'failed'` = « ne partira jamais » : la
ligne reste pour l'historique, le contact redevient éligible.

Les autres status doivent CONTINUER de bloquer (message vivant ou déjà remis).
"""
from __future__ import annotations

import pytest

from src import supabase_client as real_db
import src.tools.db as dbt

# Statuts qui doivent garder le contact hors du backlog WF-4.
STATUTS_BLOQUANTS = ["draft", "queued", "sent", "delivered", "bounced", "replied"]


def _fake_select_factory(messages: list[dict], captured: dict):
    """Émule PostgREST : applique le filtre `status=not.in.(...)` côté fake.

    Sans cette émulation le test ne prouverait rien — il faut que le fake
    RENDE le message si le code oublie de filtrer.
    """

    async def fake_select(table, params=None):
        params = params or {}
        if table == "contacts":
            return [
                {"id": "ct-1", "company_id": "co-1", "email": "a@ex.ca", "status": "new"},
            ]
        if table == "companies":
            return [
                # `website` renseigne : depuis AC1b, la garde sans-site ecarte les
                # entreprises sans site ET sans fiche Google exploitable.
                # Ce test porte sur l'eligibilite par STATUT de message, pas
                # sur cette garde.
                {"id": "co-1", "name": "Ex Co", "track": "OPT",
                 "website": "https://exco.ca", "research_json": {"x": 1}},
            ]
        if table == "messages":
            captured["params"] = params
            exclus: set[str] = set()
            filtre = params.get("status") or ""
            if filtre.startswith("not.in.("):
                exclus = {s.strip() for s in filtre[len("not.in.(") : -1].split(",")}
            return [
                {"contact_id": m["contact_id"]}
                for m in messages
                if m["status"] not in exclus
            ]
        return []

    return fake_select


@pytest.mark.asyncio
async def test_requete_messages_exclut_les_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(real_db, "select", _fake_select_factory([], captured))

    await dbt.list_contacts_to_personalize()

    assert captured["params"].get("status") == "not.in.(failed)"


@pytest.mark.asyncio
async def test_contact_dont_le_seul_message_est_failed_redevient_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cas des 23 drafts abandonnés au pivot 2026-06-07 (pitch réactivation)."""
    messages = [{"contact_id": "ct-1", "status": "failed"}]
    monkeypatch.setattr(real_db, "select", _fake_select_factory(messages, {}))

    out = await dbt.list_contacts_to_personalize()

    assert [o["contact"]["id"] for o in out] == ["ct-1"]


@pytest.mark.parametrize("status", STATUTS_BLOQUANTS)
@pytest.mark.asyncio
async def test_les_autres_status_bloquent_toujours(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Anti sur-correction : seul 'failed' est retiré du jeu de blocage.

    'bounced' inclus : l'adresse est morte, re-drafter ne ferait que re-bouncer
    (jetons brûlés + réputation d'envoi abîmée) — ça se règle au niveau contact.
    """
    messages = [{"contact_id": "ct-1", "status": status}]
    monkeypatch.setattr(real_db, "select", _fake_select_factory(messages, {}))

    out = await dbt.list_contacts_to_personalize()

    assert out == []
