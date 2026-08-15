"""Garde P4.10 — décision seule : un prospect n'est envoyable que s'il a une
ligne agence.site_configs au verdict 'ok'.

Ce module ne fait que DÉCIDER. La note, l'alerte et le statut sont testés
dans test_send_site_config_guard.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


async def test_verdict_ok_passe(monkeypatch) -> None:
    from src.lib import site_config_gate as gate

    monkeypatch.setattr(gate.db, "select",
                        AsyncMock(return_value=[{"verdict": "ok", "gele": False}]))
    d = await gate.check_site_config("co-1")
    assert d.allowed is True
    assert d.read_failed is False


async def test_lecture_cible_le_schema_agence(monkeypatch) -> None:
    """Régression : site_configs n'est PAS dans public. Sans schema='agence',
    PostgREST cherche public.site_configs et retourne 404."""
    from src.lib import site_config_gate as gate

    sel = AsyncMock(return_value=[{"verdict": "ok", "gele": False}])
    monkeypatch.setattr(gate.db, "select", sel)
    await gate.check_site_config("co-1")

    assert sel.await_args.args[0] == "site_configs"
    assert sel.await_args.kwargs["schema"] == "agence"
    assert sel.await_args.kwargs["params"]["company_id"] == "eq.co-1"


async def test_aucune_ligne_bloque(monkeypatch) -> None:
    from src.lib import site_config_gate as gate

    monkeypatch.setattr(gate.db, "select", AsyncMock(return_value=[]))
    d = await gate.check_site_config("co-1")
    assert d.allowed is False
    assert "absent" in (d.reason or "")
    assert d.read_failed is False


async def test_a_verifier_bloque(monkeypatch) -> None:
    from src.lib import site_config_gate as gate

    monkeypatch.setattr(gate.db, "select",
                        AsyncMock(return_value=[{"verdict": "a-verifier", "gele": False}]))
    d = await gate.check_site_config("co-1")
    assert d.allowed is False
    assert "a-verifier" in (d.reason or "")


async def test_refuse_bloque(monkeypatch) -> None:
    from src.lib import site_config_gate as gate

    monkeypatch.setattr(gate.db, "select",
                        AsyncMock(return_value=[{"verdict": "refuse", "gele": False}]))
    d = await gate.check_site_config("co-1")
    assert d.allowed is False
    assert "refuse" in (d.reason or "")


async def test_verdict_inconnu_bloque(monkeypatch) -> None:
    """Défensif : le CHECK de la migration 0026 l'empêche, mais la garde ne
    suppose pas que la contrainte existe encore."""
    from src.lib import site_config_gate as gate

    monkeypatch.setattr(gate.db, "select",
                        AsyncMock(return_value=[{"verdict": "peut-etre", "gele": False}]))
    d = await gate.check_site_config("co-1")
    assert d.allowed is False
    assert "inconnu" in (d.reason or "")


async def test_verdict_vide_bloque(monkeypatch) -> None:
    from src.lib import site_config_gate as gate

    monkeypatch.setattr(gate.db, "select",
                        AsyncMock(return_value=[{"verdict": None, "gele": False}]))
    d = await gate.check_site_config("co-1")
    assert d.allowed is False


async def test_sans_company_id_bloque_sans_lire(monkeypatch) -> None:
    from src.lib import site_config_gate as gate

    sel = AsyncMock(return_value=[])
    monkeypatch.setattr(gate.db, "select", sel)
    d = await gate.check_site_config(None)
    assert d.allowed is False
    assert "company_id" in (d.reason or "")
    assert d.read_failed is False
    sel.assert_not_awaited()


async def test_lecture_qui_leve_bloque_et_se_signale(monkeypatch) -> None:
    """Fail closed : dans le doute on n'envoie pas, et read_failed=True dit à
    l'appelant que c'est une PANNE (donc Slack), pas une attente normale."""
    from src.lib import site_config_gate as gate

    async def _boom(*a, **k):
        raise RuntimeError("agence not exposed")
    monkeypatch.setattr(gate.db, "select", _boom)

    d = await gate.check_site_config("co-1")
    assert d.allowed is False
    assert d.read_failed is True
    assert "lecture_echouee" in (d.reason or "")


async def test_gele_napparait_que_dans_la_raison_dun_refus(monkeypatch) -> None:
    """gele protège l'ÉCRITURE, pas l'envoi : il ne change jamais allowed.
    On le mentionne seulement pour aider au diagnostic d'un refus."""
    from src.lib import site_config_gate as gate

    monkeypatch.setattr(gate.db, "select",
                        AsyncMock(return_value=[{"verdict": "ok", "gele": True}]))
    assert (await gate.check_site_config("co-1")).allowed is True

    monkeypatch.setattr(gate.db, "select",
                        AsyncMock(return_value=[{"verdict": "refuse", "gele": True}]))
    d = await gate.check_site_config("co-1")
    assert d.allowed is False
    assert "gele" in (d.reason or "")
