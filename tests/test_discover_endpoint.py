# mcp-server/tests/test_discover_endpoint.py
from __future__ import annotations

from typing import Any

import pytest

from src import http_api
from src.tools import reacti_discover as rd


class _LLMResult:
    def __init__(self, discovery: dict[str, Any], *, tronquee: bool = False) -> None:
        self.discovery = discovery
        self.tronquee = tronquee
        self.model = "claude-sonnet-4-6"
        self.usage = rd.DiscoveryUsage(input_tokens=1, output_tokens=1)


async def _noop(*a, **k):
    return None


@pytest.fixture
def patch_company(monkeypatch):
    """Stub db.select pour retourner UNE company agence-ia sans site."""
    async def _select(table, *, params=None):
        if table == "companies":
            return [{
                "id": "c1", "name": "Déneige X", "city": "Sherbrooke",
                "address": "1 rue X", "raw_payload": {"nationalPhoneNumber": "819-555"},
                "website": None, "track": "agence-ia",
            }]
        return []
    monkeypatch.setattr(http_api.db_tools.db, "select", _select)


@pytest.mark.asyncio
async def test_discover_found_inserts_and_backfills(monkeypatch, patch_company):
    discovery = {
        "found": True, "discovered_url": "https://facebook.com/x",
        "page_kind": "facebook",
        "emails": [{"email": "info@x.ca", "kind": "generic",
                    "source_url": "https://facebook.com/x/about",
                    "published_on_own_page": True}],
        "confidence": "high", "match_reasoning": "ok",
    }
    monkeypatch.setattr(rd, "_call_discovery_llm",
                        lambda **kw: _LLMResult(discovery))

    inserted: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []

    async def _insert_contact(payload):
        inserted.append(payload.model_dump())
        return http_api.db_tools.InsertContactOut(status="inserted", contact_id="ct1")

    async def _update(table, patch, *, filters):
        updated.append({"table": table, "patch": patch})
        return [{}]

    monkeypatch.setattr(http_api.db_tools, "insert_contact", _insert_contact)
    monkeypatch.setattr(http_api.db_tools.db, "update", _update)
    monkeypatch.setattr(http_api.db_tools, "record_agent_run", _noop)

    out = await http_api.reacti_discover_contact(
        http_api.ReactiDiscoverIn(company_id="c1")
    )

    assert out.status == "found"
    assert out.contacts_inserted == 1
    assert inserted[0]["email"] == "info@x.ca"
    assert inserted[0]["email_verification_source"] == "reacti_discovery_own_page"
    # website backfillé
    assert any(u["patch"].get("website") == "https://facebook.com/x" for u in updated)


@pytest.mark.asyncio
async def test_discover_not_found_marks_no_web_presence(monkeypatch, patch_company):
    monkeypatch.setattr(rd, "_call_discovery_llm",
                        lambda **kw: _LLMResult(dict(rd._EMPTY_DISCOVERY)))
    updated: list[dict[str, Any]] = []

    async def _update(table, patch, *, filters):
        updated.append(patch)
        return [{}]

    monkeypatch.setattr(http_api.db_tools.db, "update", _update)
    monkeypatch.setattr(http_api.db_tools, "record_agent_run", _noop)

    out = await http_api.reacti_discover_contact(
        http_api.ReactiDiscoverIn(company_id="c1")
    )
    assert out.status == "no_web_presence"
    assert any(p.get("status") == "no_web_presence" for p in updated)
    # La raison est persistée, pas seulement calculée : c'est tout l'objet de B2.
    assert any(p.get("disqualified_reason") == "discovery:pas_trouvee" for p in updated)


_COMPANY = {
    "id": "c1", "name": "Déneige X", "city": "Sherbrooke",
    "address": "1 rue X", "raw_payload": {"nationalPhoneNumber": "819-555"},
    "website": None, "track": "agence-ia",
}


def _patch_troncature(monkeypatch, *, passes_vides_deja_en_base: int):
    """Stub d'une passe tronquée, avec N troncatures déjà enregistrées.

    `db_tools.db` et `http_api.sb` sont le MÊME module (src.supabase_client) :
    patcher l'un patche l'autre.
    """
    updated: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []

    async def _select(table, *, params=None, schema=None):
        if table == "companies":
            return [dict(_COMPANY)]
        if table == "agent_runs":
            # Le comptage doit bien porter le filtre JSON sur la troncature.
            assert (params or {}).get("output_payload->>tronquee") == "eq.true"
            assert (params or {}).get("company_id") == "eq.c1"
            return [{"id": f"r{i}"} for i in range(passes_vides_deja_en_base)]
        return []

    async def _update(table, patch, *, filters):
        updated.append(patch)
        return [{}]

    async def _record(payload):
        runs.append(payload.model_dump())
        return {"agent_run_id": "ar1"}

    monkeypatch.setattr(http_api.db_tools.db, "select", _select)
    monkeypatch.setattr(http_api.db_tools.db, "update", _update)
    monkeypatch.setattr(http_api.db_tools, "record_agent_run", _record)
    monkeypatch.setattr(
        rd, "_call_discovery_llm",
        lambda **kw: _LLMResult(dict(rd._EMPTY_DISCOVERY), tronquee=True),
    )
    return updated, runs


@pytest.mark.asyncio
async def test_une_troncature_isolee_ne_condamne_pas_la_company(monkeypatch):
    """Le bout-en-bout du correctif : max_tokens ne doit RIEN écrire sur la
    company, sinon la panne de deux secondes redevient un verdict à vie."""
    updated, runs = _patch_troncature(monkeypatch, passes_vides_deja_en_base=0)

    out = await http_api.reacti_discover_contact(
        http_api.ReactiDiscoverIn(company_id="c1")
    )

    assert not any("status" in p for p in updated), updated
    # Sans cette clé dans le payload, le plafond ne compterait jamais rien.
    assert runs[0]["output_payload"]["tronquee"] is True
    # Et surtout : ce n'est pas un succès. Une passe qui n'a rien trouvé ne doit
    # pas répondre 'found' avec zéro contact.
    assert out.status == "a_reessayer"
    assert out.contacts_inserted == 0


@pytest.mark.asyncio
async def test_le_lot_ne_compte_pas_une_troncature_comme_trouvee(monkeypatch):
    """Le défaut que ce compteur ferme : un lot où le modèle a tronqué partout
    rapportait « trouvées : N ». L'opérateur en concluait que la découverte
    fonctionne, alors qu'elle n'avait rien trouvé du tout."""
    _patch_troncature(monkeypatch, passes_vides_deja_en_base=0)

    async def _backlog(limit=10, **kw):
        return [{"id": "c1", "name": "Déneige X"}, {"id": "c1", "name": "Déneige Y"}]

    monkeypatch.setattr(http_api.db_tools, "list_companies_to_discover", _backlog)

    out = await http_api.run_reacti_wf2(http_api.RunReactiWf2In(limit=2))

    assert out.processed == 2
    assert out.found == 0, "une troncature n'est pas une trouvaille"
    assert out.a_reessayer == 2
    assert out.no_web_presence == 0
    assert out.failed == 0
    assert {i.status for i in out.items} == {"a_reessayer"}


@pytest.mark.asyncio
async def test_la_troisieme_troncature_tranche_en_disant_technique(monkeypatch):
    """Au plafond, et seulement là, la troncature redevient un verdict — donc
    'no_web_presence' et non 'a_reessayer'."""
    updated, runs = _patch_troncature(monkeypatch, passes_vides_deja_en_base=2)

    out = await http_api.reacti_discover_contact(
        http_api.ReactiDiscoverIn(company_id="c1")
    )

    assert out.status == "no_web_presence"
    assert updated == [{
        "status": "no_web_presence",
        "disqualified_reason": "discovery:reponse_tronquee_x3",
    }]
    # La passe qui déclenche le plafond laisse quand même sa trace.
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_la_deuxieme_troncature_ne_tranche_pas_encore(monkeypatch):
    """Garde-fou d'ordre : si le comptage tournait APRÈS l'audit, la passe
    courante serait déjà en base et le plafond tomberait ici."""
    updated, _ = _patch_troncature(monkeypatch, passes_vides_deja_en_base=1)

    out = await http_api.reacti_discover_contact(
        http_api.ReactiDiscoverIn(company_id="c1")
    )

    assert not any("status" in p for p in updated), updated
    assert out.status == "a_reessayer"
