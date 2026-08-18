"""Le résumé quotidien dit pourquoi rien n'est poussé (P4.10).

Sans ce compteur, une journée entièrement bloquée par la garde s'affiche
« drafts 23 · poussés 0 » — vrai, et incompréhensible.

Les drafts sont lus sans filtre de date, donc le lot peut dépasser le plafond de
1000 lignes de PostgREST : la lecture passe par `select_all()`. `sb.select` est
piégé pour lever, afin qu'un retour au primitif plafonné casse le test.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _socle(monkeypatch) -> None:
    """`select()` interdit (plafonné à 1000) + compteurs du jour à zéro."""
    from src import supabase_client as sb

    async def _boom(table, *, params=None, schema=None):
        raise AssertionError(
            f"select() plafonné à 1000 lignes utilisé sur {table!r} — "
            "utiliser count() ou select_all()"
        )

    async def _count(table, *, params=None, schema=None):
        return 0

    monkeypatch.setattr(sb, "select", _boom)
    monkeypatch.setattr(sb, "count", _count)


async def test_compte_separement_attente_et_relecture(monkeypatch) -> None:
    """Deux nombres, pas un. « En attente » se règle au prochain lot nocturne ;
    « à relire » attend une décision de William. Les additionner ferait dériver
    le premier vers le haut sans jamais redescendre.

    Le 3e draft porte LES DEUX marqueurs — il a attendu, puis a reçu un verdict
    qui refuse. Il compte dans l'état le plus actionnable, pas dans les deux."""
    from src import http_api
    from src import supabase_client as sb

    seen: list[dict] = []

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        params = params or {}
        if table == "messages" and "compliance_notes" in (params.get("select") or ""):
            seen.append({**params, "_order": order})
            return [
                {"id": "m-1", "compliance_notes": "COMPLIANCE OK | site_config_attente: absent"},
                {"id": "m-2", "compliance_notes": "site_config_attente: absent"},
                {"id": "m-3", "compliance_notes":
                    "site_config_attente: absent | site_config_bloque: verdict='refuse'"},
                {"id": "m-4", "compliance_notes": "site_config_bloque: verdict='a-verifier'"},
                {"id": "m-5", "compliance_notes": None},
                {"id": "m-6", "compliance_notes": "COMPLIANCE OK - 0 violation(s)"},
            ]
        return []

    _socle(monkeypatch)
    monkeypatch.setattr(sb, "select_all", _select_all)

    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )

    assert out["totals"]["agence-ia"]["waiting_config"] == 2   # m-1, m-2 (pas m-3)
    assert out["totals"]["agence-ia"]["to_review"] == 2        # m-3, m-4
    assert "en attente de config 2" in out["text"]
    assert "à relire 2" in out["text"]
    # sans filtre de date : un draft coincé depuis deux semaines compte encore
    assert "created_at" not in seen[0]
    assert seen[0]["status"] == "eq.draft"
    # ordre stable, sinon la pagination saute ou duplique des drafts
    assert seen[0]["_order"] == "id"


async def test_une_note_nulle_ne_fait_pas_planter_le_compte(monkeypatch) -> None:
    """compliance_notes est nullable : un `in` sur None lèverait."""
    from src import http_api
    from src import supabase_client as sb

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        params = params or {}
        if table == "messages" and "compliance_notes" in (params.get("select") or ""):
            return [{"id": "m-1", "compliance_notes": None}]
        return []

    _socle(monkeypatch)
    monkeypatch.setattr(sb, "select_all", _select_all)

    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["waiting_config"] == 0
    assert out["totals"]["agence-ia"]["to_review"] == 0


async def test_rien_de_bloque_rien_dans_le_texte(monkeypatch) -> None:
    """Ne pas polluer le résumé de deux zéros quotidiens."""
    from src import http_api
    from src import supabase_client as sb

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        return []

    _socle(monkeypatch)
    monkeypatch.setattr(sb, "select_all", _select_all)

    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )

    assert out["totals"]["agence-ia"]["waiting_config"] == 0
    assert out["totals"]["agence-ia"]["to_review"] == 0
    assert "en attente de config" not in out["text"]
    assert "à relire" not in out["text"]
