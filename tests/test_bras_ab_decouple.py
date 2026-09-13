"""🔴 Le gabarit A/B/C/D ne doit jamais être corrélé au potentiel du lead.

Deux conversations ont travaillé ce mécanisme en parallèle, chacune avec une
moitié du problème. La fusion du 2026-09-13 garde les deux.

**Le problème d'équilibre** (conversation B) : le rang repartait de zéro à
chaque lot. Un lot de 10 ne se divise pas par 4 — A=3, B=3, C=2, D=2 — donc C
et D accumulaient leur base 1,5 fois plus lentement. Correctif : `rang_du_bras`
est lu en base (`_bras_deja_servis`) et n'avance qu'à l'écriture d'un brouillon.

**Le problème de corrélation** (cette conversation) : depuis que le lot sort
trié par potentiel, appliquer un compteur dans l'ordre du parcours donne le
bras A aux meilleurs leads. Le compteur continu ne suffit PAS à le régler : à
10 brouillons par lot, le décalage vaut 2 modulo 4, donc le meilleur lead d'un
lot alternerait éternellement entre A et C, et B et D n'en verraient jamais un.

**La résolution** : le lot est SÉLECTIONNÉ par priorité, puis PARCOURU dans
l'ordre d'ARRIVÉE. Le compteur garde sa répartition égale, et le bras cesse de
dépendre du score. C'est ce que ce fichier verrouille.
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
    """Le lot tel que `_retenir` le rend : trié par priorité décroissante.

    `Ent0` est le meilleur potentiel (premier de la liste) mais le DERNIER
    arrivé (`rang_arrivee = 3`). Si le bras suivait la position dans la liste,
    Ent0 prendrait A. Il doit prendre D.
    """
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


def _socle(monkeypatch: pytest.MonkeyPatch, backlog: list[dict], vus: list):
    from src import http_api
    from src import supabase_client as sb
    from src.lib import calcom as calcom_mod
    from src.lib import slack as slack_mod

    async def faux_personalize(contact, company, *, template_choice, **kw):
        vus.append((company["name"], template_choice))
        return SimpleNamespace(
            status="ok", message_id="m", template_used=template_choice,
            duration_ms=1, error_text=None,
        )

    async def faux_backlog(**kw):
        return backlog

    async def faux_count(table, params=None, schema=None):
        return 0

    async def faux_notify(**kw):
        return True

    async def faux_deja_servis(track):
        return 0

    monkeypatch.setattr(http_api.db_tools, "list_contacts_to_personalize", faux_backlog)
    monkeypatch.setattr(http_api, "_personalize_one", faux_personalize)
    monkeypatch.setattr(http_api, "_bras_deja_servis", faux_deja_servis)
    monkeypatch.setattr(calcom_mod, "get_available_slots", lambda **kw: [])
    monkeypatch.setattr(http_api, "_load_client_references", lambda: [])
    monkeypatch.setattr(sb, "count", faux_count)
    monkeypatch.setattr(slack_mod, "notify", faux_notify)
    return http_api


async def test_le_bras_suit_l_ordre_d_arrivee_pas_le_potentiel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vus: list[tuple[str, str]] = []
    http_api = _socle(monkeypatch, _backlog_a_rang_inverse(), vus)
    from src.http_api import RunWf4In

    await http_api.run_wf4(RunWf4In(track="agence-ia", template_choice="ABCD"))

    # Parcouru dans l'ordre d'ARRIVÉE : Ent3 est arrivé le premier.
    assert [n for n, _ in vus] == ["Ent3", "Ent2", "Ent1", "Ent0"], (
        f"le lot est parcouru dans l'ordre de la file triée : {vus}"
    )
    # Et le compteur distribue les quatre bras dans cet ordre-là.
    assert [b for _, b in vus] == ["A", "B", "C", "D"], vus
    # Donc le MEILLEUR potentiel (Ent0, premier de la liste) prend D, pas A.
    assert dict(vus)["Ent0"] == "D"


async def test_le_compteur_continue_entre_les_lots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La moitié « équilibre » : un deuxième lot ne redémarre pas à A.

    `_bras_deja_servis` rend 2 — deux brouillons déjà écrits — donc le lot
    reprend au troisième bras.
    """
    vus: list[tuple[str, str]] = []
    http_api = _socle(monkeypatch, _backlog_a_rang_inverse(), vus)
    from src import http_api as api_mod
    from src.http_api import RunWf4In

    async def deja_deux(track):
        return 2

    monkeypatch.setattr(api_mod, "_bras_deja_servis", deja_deux)

    await http_api.run_wf4(RunWf4In(track="agence-ia", template_choice="ABCD"))

    assert [b for _, b in vus] == ["C", "D", "A", "B"], vus


async def test_la_prod_crie_si_l_etiquette_disparait(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Le journal, pour ce que les tests ne peuvent pas prévoir.

    Si un remaniement futur retirait `rang_arrivee` de `_retenir`, le lot
    serait parcouru dans l'ordre de la file triée — donc les bras
    redeviendraient corrélés au potentiel, silencieusement.
    """
    sans_etiquette = [
        {k: v for k, v in e.items() if k != "rang_arrivee"}
        for e in _backlog_a_rang_inverse()
    ]
    vus: list[tuple[str, str]] = []
    http_api = _socle(monkeypatch, sans_etiquette, vus)
    from src.http_api import RunWf4In

    with caplog.at_level("WARNING", logger="wf4"):
        await http_api.run_wf4(RunWf4In(track="agence-ia", template_choice="ABCD"))

    assert any("rang_arrivee" in m for m in caplog.messages), caplog.messages
