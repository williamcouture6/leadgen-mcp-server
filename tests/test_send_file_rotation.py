"""Rotation de la file WF-6 (run_wf6) : sur-récolte + « jamais tenté d'abord ».

Contrat épinglé ici :
  - un draft sauté (peu importe la garde qui l'a sauté) reste 'draft', donc la
    requête FIFO le re-sélectionne — run_wf6 sur-récolte (> limit) pour que les
    envoyables derrière les bloqués partent quand même ;
  - la file tourne : jamais tenté d'abord (`last_send_attempt_at.asc.nullsfirst`),
    et TOUTE tentative qui n'aboutit pas (saut comme exception) est horodatée
    pour reculer derrière les frais ;
  - un rejet TERMINAL (suppression list) marque 'failed' et sort de la file.

`send_one_message` est stubbé (sauf pour le test de suppression, qui vérifie le
chemin réel de l'étape 3b) : la rotation de file ne dépend d'aucune garde
particulière — n'importe quel skip qui laisse le message 'draft' doit être
horodaté et céder son tour.
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
        "id": "m-1", "subject": "S", "body_text": "Allo, votre site est déjà refait.",
        "to_email": "jean@plomberiex.ca", "status": "draft", "direction": "outbound",
        "compliance_check_passed": True, "contact_id": "ct-1",
        "track": "agence-ia",
        "compliance_notes": None,
        # Depuis AC1b, un message `agence-ia` sans ses deux relances est refusé
        # au push (`skipped_followups_manquants`). Ces tests portent sur la
        # rotation de fichier et le corps verbatim, pas sur cette garde — qui a
        # ses propres tests dans test_transport_relances.py.
        "followups": {
            "relance_1": "corps r1",
            "relance_2": "corps r2",
            "relance_3": "corps r3",
        },
    }
    base.update(over)
    return base


async def test_supprime_est_marque_failed(monkeypatch) -> None:
    """Un désabonné doit sortir en skipped_suppressed et être marqué failed —
    sinon il reste draft à vie et squatte la tête de la file FIFO."""
    from src.tools import send

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            return [_msg()]
        if table == "contacts":
            return [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
                     "email": "jean@plomberiex.ca", "company_id": "co-1"}]
        if table == "companies":
            return [{"name": "Plomberie X", "domain": "plomberiex.ca"}]
        return []

    updates: list[tuple[str, dict]] = []

    async def _update(table, patch, **kw):
        updates.append((table, patch))
        return [{}]

    add_lead = AsyncMock(return_value={"id": "lead-1"})
    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", _update)
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", add_lead)
    monkeypatch.setattr(send, "_is_suppressed",
                        AsyncMock(return_value=(True, "email on suppression (optout)")))

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_suppressed"
    add_lead.assert_not_awaited()
    assert any(p.get("status") == "failed" for _, p in updates)


def _send_one_wire(monkeypatch, *, to_email: str | None = None,
                   suppressed: tuple[bool, str] = (False, "")):
    """Monte le VRAI `send_one_message` sur un draft. Par défaut le draft est
    envoyable (ni désabonné, ni domaine plateforme, warmup passé) ; `to_email`
    et `suppressed` permettent de le faire tomber sur l'une des deux gardes
    terminales. Retourne (updates, add_lead)."""
    from src.tools import send

    over = {"to_email": to_email} if to_email else {}

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            return [_msg(**over)]
        if table == "contacts":
            return [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
                     "email": "jean@plomberiex.ca", "company_id": "co-1",
                     "status": "new"}]
        if table == "companies":
            return [{"name": "Plomberie X", "domain": "plomberiex.ca"}]
        return []

    updates: list[tuple[str, dict]] = []

    async def _update(table, patch, **kw):
        updates.append((table, patch))
        return [{}]

    add_lead = AsyncMock(return_value={"id": "lead-1"})
    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", _update)
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", add_lead)
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=suppressed))
    return updates, add_lead


async def test_dry_run_chemin_propre_n_ecrit_rien(monkeypatch) -> None:
    """Un dry-run doit être une SIMULATION : l'appel Instantly était déjà
    court-circuité, mais les écritures qui suivaient (messages → 'queued',
    contacts → 'contacted') passaient quand même. Un dry-run mutait donc la
    base et sortait le draft de la file sans qu'aucun courriel ne parte.

    Ne couvre QUE le draft envoyable — les deux gardes terminales sont
    épinglées plus bas (section « les gardes terminales »)."""
    from src.tools import send

    updates, add_lead = _send_one_wire(monkeypatch)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1", dry_run=True))

    # statut 'ok' conservé : run_wf6 doit continuer à compter le candidat comme
    # poussable, un statut neuf tomberait dans la branche errors.
    assert out.status == "ok"
    assert (out.provider_message_id or "").startswith("dry_run_")
    add_lead.assert_not_awaited()
    assert updates == [], f"aucune écriture attendue en dry_run, vu : {updates}"


async def test_un_envoi_reel_pousse_le_corps_verbatim(monkeypatch) -> None:
    """Le pendant du dry-run : un envoi réel écrit bien, ET pousse à Instantly
    le `body_text` du draft TEL QUEL — rien n'est réécrit au moment du push
    (ce corps a passé la revue conformité, le modifier ici la contournerait)."""
    from src.tools import send

    updates, add_lead = _send_one_wire(monkeypatch)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "ok"
    add_lead.assert_awaited_once()
    kwargs = add_lead.await_args.kwargs
    assert kwargs["body_text"] == _msg()["body_text"]
    assert kwargs["subject"] == _msg()["subject"]
    tables = [t for t, _ in updates]
    assert "messages" in tables and "contacts" in tables


# =====================================================================
# « un dry-run ne modifie RIEN » — les gardes terminales aussi
#
# Le retour anticipé en dry_run est posé APRÈS les deux gardes qui marquent
# 'failed' (domaine de plateforme, liste de suppression) : une simulation
# tuait donc le draft pour de vrai. Le verdict doit être RAPPORTÉ (statut +
# skipped_reason inchangés) sans être gravé. Les paires ci-dessous épinglent
# les deux gardes × (dry-run / réel) ; le pendant réel de la suppression est
# `test_supprime_est_marque_failed` en tête de fichier.
# =====================================================================

async def test_dry_run_domaine_plateforme_rapporte_sans_graver(monkeypatch) -> None:
    from src.tools import send

    updates, add_lead = _send_one_wire(monkeypatch, to_email="ssingh@meta.com")

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1", dry_run=True))

    # le verdict est rapporté tel quel...
    assert out.status == "skipped_platform_domain"
    assert "meta.com" in (out.skipped_reason or "")
    add_lead.assert_not_awaited()
    # ...mais rien n'est gravé : la simulation ne tue pas le draft.
    assert updates == [], f"aucune écriture attendue en dry_run, vu : {updates}"


async def test_hors_dry_run_le_domaine_plateforme_est_marque_failed(monkeypatch) -> None:
    """Non-régression : en réel, le 'failed' DOIT être écrit — sinon le draft
    reste 'draft' à vie et squatte la tête de la file FIFO."""
    from src.tools import send

    updates, add_lead = _send_one_wire(monkeypatch, to_email="ssingh@meta.com")

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_platform_domain"
    add_lead.assert_not_awaited()
    assert any(t == "messages" and p.get("status") == "failed" for t, p in updates)


async def test_dry_run_supprime_rapporte_sans_graver(monkeypatch) -> None:
    from src.tools import send

    updates, add_lead = _send_one_wire(
        monkeypatch, suppressed=(True, "email on suppression (optout)"),
    )

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1", dry_run=True))

    assert out.status == "skipped_suppressed"
    assert out.skipped_reason == "email on suppression (optout)"
    add_lead.assert_not_awaited()
    assert updates == [], f"aucune écriture attendue en dry_run, vu : {updates}"


def _wf6_wire(monkeypatch, drafts: list[dict], *, ok_ids: set[str],
              boom_ids: set[str] | None = None):
    """Stub run_wf6 : renvoie `drafts` à la requête, fait réussir les
    message_id de `ok_ids`, fait LEVER ceux de `boom_ids` (le reste est sauté
    — skip transitoire quelconque, le message reste 'draft').
    Retourne (params de requête vus, écritures vues, message_id tentés).

    `db.update` est patché lui aussi : run_wf6 écrit l'horodatage de tentative,
    sans ce patch les tests taperaient le réseau."""
    from src.tools import send

    seen_params: list[dict] = []
    boom_ids = boom_ids or set()

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            seen_params.append(params or {})
            limit = int((params or {}).get("limit") or len(drafts))
            return drafts[:limit]
        return []

    calls: list[str] = []

    async def _fake_send_one(payload):
        calls.append(payload.message_id)
        if payload.message_id in boom_ids:
            raise RuntimeError(f"boom {payload.message_id}")
        if payload.message_id in ok_ids:
            return send.SendMessageOut(message_id=payload.message_id, status="ok",
                                       provider_message_id=f"lead-{payload.message_id}")
        return send.SendMessageOut(
            message_id=payload.message_id, status="skipped_warmup",
            skipped_reason="stub",
        )

    updates: list[tuple[str, dict, dict]] = []

    async def _update(table, patch, **kw):
        updates.append((table, patch, kw.get("filters") or {}))
        return [{}]

    monkeypatch.setenv("INSTANTLY_CAMPAIGN_ID_REACTI", "camp-agence")
    monkeypatch.setattr(send, "count_pushed_today", AsyncMock(return_value=0))
    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", _update)
    monkeypatch.setattr(send, "send_one_message", _fake_send_one)
    return seen_params, updates, calls


def _drafts(n: int) -> list[dict]:
    return [
        {"id": f"m-{i}", "to_email": f"a{i}@x.ca",
         "created_at": f"2026-08-14T00:{i:02d}:00Z", "track": "agence-ia"}
        for i in range(n)
    ]


async def test_les_bloques_ne_bloquent_pas_la_file(monkeypatch) -> None:
    """Quatre bloqués en tête de file ne doivent pas empêcher les deux
    envoyables derrière eux de partir. Sans sur-récolte, limit=2 ne verrait
    jamais plus loin que les deux premiers bloqués."""
    from src.tools import send

    drafts = _drafts(6)
    _, updates, calls = _wf6_wire(monkeypatch, drafts, ok_ids={"m-4", "m-5"})

    out = await send.run_wf6(send.RunWf6In(limit=2, track="agence-ia"))

    assert out.pushed == 2
    assert out.processed == 6
    assert calls == [f"m-{i}" for i in range(6)]  # tous tentés, dans l'ordre
    # les 4 non poussés sont horodatés (ils reculeront dans la file)
    stamped = {f["id"] for _, p, f in updates if "last_send_attempt_at" in p}
    assert stamped == {"eq.m-0", "eq.m-1", "eq.m-2", "eq.m-3"}


async def test_la_sur_recolte_sarrete_a_la_limite(monkeypatch) -> None:
    """La sur-récolte regarde plus loin, elle n'envoie pas plus : le daily cap
    reste la limite dure."""
    from src.tools import send

    drafts = _drafts(10)
    _, _, calls = _wf6_wire(monkeypatch, drafts, ok_ids={d["id"] for d in drafts})

    out = await send.run_wf6(send.RunWf6In(limit=2, track="agence-ia"))

    assert out.pushed == 2
    assert out.processed == 2  # on s'arrête dès que la limite est atteinte
    assert calls == ["m-0", "m-1"]


async def test_la_requete_demande_plus_que_la_limite(monkeypatch) -> None:
    from src.tools import send

    seen, _, _ = _wf6_wire(monkeypatch, _drafts(30), ok_ids=set())
    await send.run_wf6(send.RunWf6In(limit=2, track="agence-ia"))

    assert seen[0]["limit"] == "10"  # 2 × DRAFT_OVERFETCH_FACTOR


async def test_la_sur_recolte_est_plafonnee(monkeypatch) -> None:
    """Un cap absolu : une file de 5 000 drafts bloqués ne doit pas faire
    lire 5 000 lignes à chaque passe."""
    from src.tools import send

    seen, _, _ = _wf6_wire(monkeypatch, _drafts(50), ok_ids=set())
    await send.run_wf6(send.RunWf6In(limit=50, track="agence-ia", daily_cap=500))

    assert seen[0]["limit"] == str(send.DRAFT_OVERFETCH_MAX)


async def test_la_file_tourne_jamais_tentes_dabord(monkeypatch) -> None:
    """Sans rotation, les mêmes plus vieux reviennent à chaque passe et un
    bloqué définitif occupe un créneau pour toujours."""
    from src.tools import send

    seen, _, _ = _wf6_wire(monkeypatch, _drafts(3), ok_ids=set())
    await send.run_wf6(send.RunWf6In(limit=2, track="agence-ia"))

    assert seen[0]["order"] == "last_send_attempt_at.asc.nullsfirst,created_at.asc"


async def test_un_saut_horodate_la_tentative(monkeypatch) -> None:
    """C'est l'horodatage qui fait reculer le bloqué : sans lui, il reste en
    tête et la file se fige à nouveau."""
    from src.tools import send

    _, updates, _ = _wf6_wire(monkeypatch, _drafts(2), ok_ids=set())
    await send.run_wf6(send.RunWf6In(limit=2, track="agence-ia"))

    stamps = [(f, p) for t, p, f in updates if "last_send_attempt_at" in p]
    assert len(stamps) == 2
    assert {f["id"] for f, _ in stamps} == {"eq.m-0", "eq.m-1"}


async def test_un_envoi_reussi_nest_pas_horodate_par_la_boucle(monkeypatch) -> None:
    """Un message poussé quitte le statut draft, donc il sort de la requête :
    l'horodater serait une écriture pour rien."""
    from src.tools import send

    drafts = _drafts(2)
    _, updates, _ = _wf6_wire(monkeypatch, drafts, ok_ids={"m-0", "m-1"})
    await send.run_wf6(send.RunWf6In(limit=2, track="agence-ia"))

    assert not [p for _, p, _ in updates if "last_send_attempt_at" in p]


async def test_une_exception_horodate_aussi_la_tentative(monkeypatch) -> None:
    """Deuxième porte vers la même famine : un message qui LÈVE reste 'draft'
    tout comme un message sauté. Sans horodatage il garderait sa place en tête
    et re-consommerait un créneau à chaque passe — une ligne malformée, ou un
    contact qui 404 pour toujours, suffirait. La rotation couvre toute issue
    qui laisse le message en draft, pas seulement les sauts propres."""
    from src.tools import send

    _, updates, _ = _wf6_wire(monkeypatch, _drafts(2), ok_ids={"m-1"},
                              boom_ids={"m-0"})
    out = await send.run_wf6(send.RunWf6In(limit=2, track="agence-ia"))

    assert out.errors == 1
    assert out.pushed == 1
    # le levant est horodaté, le poussé ne l'est pas (il quitte 'draft')
    assert [f for _, p, f in updates if "last_send_attempt_at" in p] == [{"id": "eq.m-0"}]


async def test_une_passe_dry_run_nhorodate_rien(monkeypatch) -> None:
    """L'horodatage est une VRAIE écriture : posé pendant une simulation, il
    réordonne la file FIFO réelle. Un lot mixte (poussé / sauté / levant) en
    dry_run ne doit produire AUCUN `db.update` — ni horodatage, ni statut."""
    from src.tools import send

    _, updates, calls = _wf6_wire(monkeypatch, _drafts(4), ok_ids={"m-1"},
                                  boom_ids={"m-2"})

    out = await send.run_wf6(
        send.RunWf6In(limit=4, track="agence-ia", dry_run=True)
    )

    assert calls == ["m-0", "m-1", "m-2", "m-3"]  # le lot est bien parcouru
    assert out.pushed == 1 and out.errors == 1 and out.skipped_warmup == 2
    assert updates == [], f"aucune écriture attendue en dry_run, vu : {updates}"


async def test_un_horodatage_qui_echoue_ne_casse_pas_la_passe(monkeypatch) -> None:
    from src.tools import send

    _, _, calls = _wf6_wire(monkeypatch, _drafts(2), ok_ids=set())

    async def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(send.db, "update", _boom)

    out = await send.run_wf6(send.RunWf6In(limit=2, track="agence-ia"))
    assert out.errors == 0
    assert out.processed == 2
    assert calls == ["m-0", "m-1"]  # les deux tentés malgré l'horodatage cassé
