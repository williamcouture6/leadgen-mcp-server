"""🔴 Le gabarit A/B/C/D se tire du rang d'ARRIVÉE, jamais de la position d'envoi.

Le garde-fou qui manquait, relevé par le second conseil du 2026-09-09.

`bras_du_lot` alterne sur un rang, et son invariant dit « jamais une propriété
du contact ». Depuis que le lot sort trié par potentiel, la POSITION dans le lot
est une fonction du score : si le rang venait de là, le bras A prendrait les
rangs 0, 4, 8… donc toujours les meilleurs leads, D les plus faibles, à chaque
envoi et dans le même sens — et `v_perf_par_bras` mesurerait la qualité des
leads au lieu de la copie.

`db._poser_rang_arrivee` pose donc `rang_arrivee` (l'ordre d'arrivée) sur chaque
retenu, et `/wf4/run` lit CETTE étiquette. Rien ne le vérifiait : un test
contrôlait que la clé est posée, un autre que le tri trie, aucun que le pipeline
consomme la bonne des deux. Le repli `entry.get("rang_arrivee", rang)` aurait
laissé un remaniement futur retomber en silence sur la position triée.

Ici le backlog est fabriqué avec un rang d'arrivée EXACTEMENT INVERSE de la
position : si le code lisait la position, les bras sortiraient A, B, C, D.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test")


def _backlog_a_rang_inverse() -> list[dict]:
    """Quatre contacts dont `rang_arrivee` est l'inverse de la position."""
    return [
        {
            "contact": {"id": f"ct-{i}", "email": f"a{i}@x.ca", "first_name": "A"},
            "company": {
                "id": f"co-{i}", "name": f"Ent{i}", "website": "https://x.ca",
                "google_rating": 4.8, "google_reviews_count": 40,
                "research_json": {"services_offered": ["Déneigement résidentiel"]},
            },
            "rang_arrivee": 3 - i,
        }
        for i in range(4)
    ]


async def test_le_bras_suit_le_rang_d_arrivee_pas_la_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import http_api
    from src import supabase_client as sb
    from src.lib import calcom as calcom_mod
    from src.lib import slack as slack_mod
    from src.http_api import RunWf4In

    vus: list[tuple[str, str]] = []

    async def faux_personalize(contact, company, *, template_choice, **kw):
        vus.append((company["name"], template_choice))
        return SimpleNamespace(
            status="ok", message_id="m", template_used=template_choice,
            duration_ms=1, error_text=None,
        )

    async def faux_backlog(**kw):
        return _backlog_a_rang_inverse()

    async def faux_count(table, params=None, schema=None):
        return 0

    async def faux_notify(**kw):
        return True

    monkeypatch.setattr(http_api.db_tools, "list_contacts_to_personalize", faux_backlog)
    monkeypatch.setattr(http_api, "_personalize_one", faux_personalize)
    monkeypatch.setattr(calcom_mod, "get_available_slots", lambda **kw: [])
    monkeypatch.setattr(http_api, "_load_client_references", lambda: [])
    monkeypatch.setattr(sb, "count", faux_count)
    monkeypatch.setattr(slack_mod, "notify", faux_notify)

    await http_api.run_wf4(RunWf4In(track="agence-ia", template_choice="ABCD"))

    bras = [b for _, b in vus]
    assert bras == ["D", "C", "B", "A"], (
        f"les bras suivent la position triée, pas le rang d'arrivée : {vus}"
    )
