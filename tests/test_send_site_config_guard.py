"""Garde P4.10 dans send_one_message : pas de config produit, pas de courriel.

Contrat épinglé ici :
  - verdict 'ok' -> l'envoi continue.
  - absent / 'a-verifier' / 'refuse' -> skipped_no_site_config, PAS de push,
    message laissé en draft (jamais 'failed').
  - la garde tourne AVANT la frappe démo P3.
  - track OPT -> garde inactive.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    monkeypatch.setenv("INSTANTLY_API_KEY", "test")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID", "camp")
    monkeypatch.setenv("WARMUP_END_DATE", "2000-01-01")


def _msg(**over) -> dict:
    base = {
        "id": "m-1", "subject": "S", "body_text": "Allo https://couture-ia.com/demo/T",
        "to_email": "jean@plomberiex.ca", "status": "draft", "direction": "outbound",
        "compliance_check_passed": True, "contact_id": "ct-1",
        "demo_url": "https://couture-ia.com/demo/T", "track": "agence-ia",
        "compliance_notes": None,
    }
    base.update(over)
    return base


def _wire(monkeypatch, *, msg: dict, site_config_rows, read_boom: bool = False):
    """Branche db.select par TABLE (pas en séquence) — la garde ajoute un
    select, un side_effect séquentiel serait fragile.

    Retourne (updates, add_lead_mock) pour les assertions.
    """
    from src.tools import send

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            return [msg]
        if table == "contacts":
            if params and "status" in (params.get("select") or ""):
                return [{"status": "ready"}]
            return [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
                     "email": "jean@plomberiex.ca", "company_id": "co-1"}]
        if table == "companies":
            return [{"name": "Plomberie X", "domain": "plomberiex.ca"}]
        if table == "site_configs":
            if read_boom:
                raise RuntimeError("agence not exposed")
            return site_config_rows
        return []

    updates: list[tuple[str, dict]] = []

    async def _update(table, patch, **kw):
        updates.append((table, patch))
        return [{}]

    add_lead = AsyncMock(return_value={"id": "lead-1"})
    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", _update)
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", add_lead)
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(send.slack, "notify", AsyncMock(return_value=True))
    return updates, add_lead


async def test_verdict_ok_pousse(monkeypatch) -> None:
    from src.tools import send

    _, add_lead = _wire(monkeypatch, msg=_msg(), site_config_rows=[{"verdict": "ok"}])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "ok"
    add_lead.assert_awaited_once()


async def test_aucun_config_saute_sans_pousser(monkeypatch) -> None:
    from src.tools import send

    updates, add_lead = _wire(monkeypatch, msg=_msg(), site_config_rows=[])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_no_site_config"
    assert "absent" in (out.skipped_reason or "")
    add_lead.assert_not_awaited()
    # le message reste en draft : jamais marqué failed, il partira plus tard
    assert not any(p.get("status") for _, p in updates)


async def test_a_verifier_saute(monkeypatch) -> None:
    from src.tools import send

    _, add_lead = _wire(monkeypatch, msg=_msg(),
                        site_config_rows=[{"verdict": "a-verifier"}])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_site_config"
    assert "a-verifier" in (out.skipped_reason or "")
    add_lead.assert_not_awaited()


async def test_refuse_saute(monkeypatch) -> None:
    from src.tools import send

    _, add_lead = _wire(monkeypatch, msg=_msg(), site_config_rows=[{"verdict": "refuse"}])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_site_config"
    add_lead.assert_not_awaited()


async def test_track_opt_ignore_la_garde(monkeypatch) -> None:
    """OPT est legacy et n'a pas de démo : la garde ne doit pas le bloquer."""
    from src.tools import send

    _, add_lead = _wire(monkeypatch, msg=_msg(track="OPT", demo_url=None),
                        site_config_rows=[])
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "ok"
    add_lead.assert_awaited_once()


async def test_garde_tourne_avant_la_frappe_demo(monkeypatch) -> None:
    """Sans config, on ne crée pas de ligne agence.demo_sites pour rien."""
    from src.tools import send

    _wire(monkeypatch, msg=_msg(demo_url=None, body_text="Allo {{DEMO_URL}}"),
          site_config_rows=[])
    ensure = AsyncMock(return_value="https://couture-ia.com/demo/T")
    monkeypatch.setattr(send, "ensure_demo_site", ensure)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_site_config"
    ensure.assert_not_awaited()


_NOTE_MARKER = "site_config_bloque"
_ALERT_MARKER = "site_config_alert_sent"
_WAIT_MARKER = "site_config_attente"


async def test_note_posee_une_fois(monkeypatch) -> None:
    from src.tools import send

    updates, _ = _wire(monkeypatch, msg=_msg(), site_config_rows=[])
    await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    notes = [p["compliance_notes"] for t, p in updates
             if t == "messages" and "compliance_notes" in p]
    assert len(notes) == 1
    # Ligne absente = attente : c'est le marqueur d'attente qui est posé, et la
    # raison doit voyager avec lui.
    assert _WAIT_MARKER in notes[0]
    assert "absent" in notes[0]


async def test_note_pas_reecrite_a_la_passe_suivante(monkeypatch) -> None:
    """Le cron repasse tant que le verdict n'est pas débloqué — le champ ne
    doit pas grossir à chaque passe. Pendant du test d'attente : ici c'est le
    régime « à relire » qui doit être idempotent."""
    from src.tools import send

    updates, _ = _wire(
        monkeypatch,
        msg=_msg(compliance_notes=f"deja vu | {_NOTE_MARKER}: verdict='a-verifier'"),
        site_config_rows=[{"verdict": "a-verifier"}],
    )
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_no_site_config"
    assert not [p for t, p in updates if "compliance_notes" in p]


async def test_aucun_slack_quand_la_ligne_manque(monkeypatch) -> None:
    """Un config pas encore produit est un état d'attente normal tant que le
    lot nocturne P4.11 n'a pas tourné. Alerter là-dessus noierait le signal."""
    from src.tools import send

    _wire(monkeypatch, msg=_msg(), site_config_rows=[])
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(send.slack, "notify", notify)

    await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    notify.assert_not_awaited()


async def test_lecture_cassee_pingue_alertes_une_fois(monkeypatch) -> None:
    """Une garde cassée bloquerait TOUS les envois agence-ia en silence."""
    from src.tools import send

    updates, add_lead = _wire(monkeypatch, msg=_msg(), site_config_rows=[],
                              read_boom=True)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(send.slack, "notify", notify)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_no_site_config"
    assert "lecture_echouee" in (out.skipped_reason or "")
    add_lead.assert_not_awaited()
    notify.assert_awaited_once()
    assert notify.await_args.kwargs["category"] == "alerts"
    notes = [p["compliance_notes"] for t, p in updates if "compliance_notes" in p]
    assert notes and _ALERT_MARKER in notes[0]


async def test_lecture_cassee_pas_de_second_ping(monkeypatch) -> None:
    from src.tools import send

    _wire(monkeypatch,
          msg=_msg(compliance_notes=f"{_NOTE_MARKER}: lecture_echouee | {_ALERT_MARKER}"),
          site_config_rows=[], read_boom=True)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(send.slack, "notify", notify)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_site_config"
    notify.assert_not_awaited()


async def test_note_qui_echoue_ne_casse_pas_le_saut(monkeypatch) -> None:
    """Une note qui ne s'écrit pas ne doit pas transformer un skip propre en
    erreur — le skip est la décision qui compte."""
    from src.tools import send

    _wire(monkeypatch, msg=_msg(), site_config_rows=[])

    async def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(send.db, "update", _boom)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert out.status == "skipped_no_site_config"


async def test_ligne_absente_pose_le_marqueur_dattente(monkeypatch) -> None:
    from src.tools import send

    updates, _ = _wire(monkeypatch, msg=_msg(), site_config_rows=[])
    await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    notes = [p["compliance_notes"] for _, p in updates if "compliance_notes" in p]
    assert len(notes) == 1
    assert _WAIT_MARKER in notes[0]
    assert _NOTE_MARKER not in notes[0]


async def test_verdict_qui_refuse_pose_le_marqueur_de_relecture(monkeypatch) -> None:
    from src.tools import send

    updates, _ = _wire(monkeypatch, msg=_msg(),
                       site_config_rows=[{"verdict": "a-verifier"}])
    await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    notes = [p["compliance_notes"] for _, p in updates if "compliance_notes" in p]
    assert len(notes) == 1
    assert _NOTE_MARKER in notes[0]
    assert _WAIT_MARKER not in notes[0]


async def test_un_marqueur_dattente_deja_pose_ne_se_reecrit_pas(monkeypatch) -> None:
    from src.tools import send

    updates, _ = _wire(
        monkeypatch,
        msg=_msg(compliance_notes=f"{_WAIT_MARKER}: aucun config produit"),
        site_config_rows=[],
    )
    await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert not [p for _, p in updates if "compliance_notes" in p]


async def test_lattente_devient_une_relecture_quand_le_verdict_arrive(monkeypatch) -> None:
    """Le lot nocturne finit par produire le config — avec un verdict qui
    refuse. Le lead a cessé d'attendre : il faut qu'il cesse d'être compté
    comme tel, sinon « en attente de config N » dérive vers le haut sans
    jamais redescendre. Les deux marqueurs coexistent, l'histoire reste."""
    from src.tools import send

    updates, _ = _wire(
        monkeypatch,
        msg=_msg(compliance_notes=f"{_WAIT_MARKER}: aucun config produit"),
        site_config_rows=[{"verdict": "a-verifier"}],
    )
    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_no_site_config"
    notes = [p["compliance_notes"] for _, p in updates if "compliance_notes" in p]
    assert len(notes) == 1
    assert _WAIT_MARKER in notes[0]
    assert _NOTE_MARKER in notes[0]


async def test_les_notes_existantes_survivent(monkeypatch) -> None:
    """WF-5 écrit son verdict complet dans compliance_notes sur CHAQUE draft
    approuvé : la colonne n'est jamais vide en production. Une note P4.10 qui
    écrase effacerait la piste d'audit et le marqueur demo_alert_sent de P3."""
    from src.tools import send

    updates, _ = _wire(
        monkeypatch,
        msg=_msg(compliance_notes="COMPLIANCE OK - 0 violation(s) | demo_alert_sent"),
        site_config_rows=[],
    )
    await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    notes = [p["compliance_notes"] for _, p in updates if "compliance_notes" in p]
    assert len(notes) == 1
    assert "COMPLIANCE OK - 0 violation(s)" in notes[0]
    assert "demo_alert_sent" in notes[0]
    assert _WAIT_MARKER in notes[0]


async def test_la_note_vise_le_bon_message(monkeypatch) -> None:
    """Le mock ne regardait que (table, patch) : le filtre `id` n'était pinné
    par rien, une note pouvait viser une autre ligne sans casser un test."""
    from src.tools import send

    seen: list[dict] = []

    async def _update(table, patch, **kw):
        seen.append(kw)
        return [{}]

    _wire(monkeypatch, msg=_msg(), site_config_rows=[])
    monkeypatch.setattr(send.db, "update", _update)

    await send.send_one_message(send.SendMessageIn(message_id="m-1"))
    assert seen and seen[0]["filters"] == {"id": "eq.m-1"}


async def test_run_wf6_compte_les_sauts_sans_config(monkeypatch) -> None:
    """Un lot mixte : 1 envoyable, 2 sans config. Le rapport doit le dire."""
    from src.tools import send

    drafts = [
        {"id": "m-ok", "to_email": "a@x.ca", "created_at": "2026-08-14T00:00:00Z",
         "track": "agence-ia"},
        {"id": "m-1", "to_email": "b@x.ca", "created_at": "2026-08-14T00:01:00Z",
         "track": "agence-ia"},
        {"id": "m-2", "to_email": "c@x.ca", "created_at": "2026-08-14T00:02:00Z",
         "track": "agence-ia"},
    ]
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID_REACTI", "camp-agence")
    monkeypatch.setattr(send, "count_pushed_today", AsyncMock(return_value=0))

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            return drafts
        return []
    monkeypatch.setattr(send.db, "select", _select)

    async def _send_one(payload):
        if payload.message_id == "m-ok":
            return send.SendMessageOut(message_id="m-ok", status="ok",
                                       provider_message_id="lead-1")
        return send.SendMessageOut(
            message_id=payload.message_id, status="skipped_no_site_config",
            skipped_reason="aucun config produit (site_configs absent)",
        )
    monkeypatch.setattr(send, "send_one_message", _send_one)

    out = await send.run_wf6(send.RunWf6In(limit=3, track="agence-ia"))

    assert out.processed == 3
    assert out.pushed == 1
    assert out.skipped_no_site_config == 2
    assert out.skipped_other == 0
    assert out.errors == 0
