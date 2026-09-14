"""La garde qui manquait : `send.py` ne lisait JAMAIS `contacts.status`.

🔴 LE DÉFAUT QUE CE FICHIER FERME (engagement nº 4a d'AC1c·A, audit du
2026-09-10). Mettre un contact en `disqualified` ne l'empêchait pas de recevoir
un courriel : `send_one_message` récupérait le contact pour ses métadonnées
Instantly (`select: id,first_name,last_name,email,company_id`) — **sans son
statut**. Mesuré à l'époque : un brouillon vers l'adresse personnelle de William
était resté armé alors que le contact était `disqualified` depuis une heure. Il
avait fallu passer par `suppression_list` pour le neutraliser vraiment.

📏 POURQUOI UNE LISTE BLANCHE, ET PAS UNE LISTE NOIRE.
La garde n'énumère pas les statuts qui bloquent, elle énumère les trois qui
laissent passer : `new`, `ready`, `researching`. Un push est une action
IRRÉVERSIBLE vers l'extérieur — sur une permission, on refuse par défaut. Une
onzième valeur ajoutée un jour à l'enum `contact_status` sera donc bloquée tant
que personne ne l'aura explicitement autorisée, au lieu de passer en silence.

✅ CE TRIPLET N'EST PAS ARBITRAIRE : c'est exactement celui que `send.py` utilise
déjà pour décider s'il bascule le contact en `contacted` après un push réussi
(`send.py`, « Side effect : flip contact.status »). La garde et le flip lisent
donc la même définition de « pas encore démarché ».

⚠️ ET ELLE NE PEUT PAS BLOQUER UNE REPRISE LÉGITIME. Le flip vers `contacted`
n'a lieu qu'APRÈS que le message soit passé de `draft` à poussé. Un contact en
`contacted` implique donc un message qui n'est plus `draft`, et l'étape 1 de
`send_one_message` l'a déjà écarté avant d'arriver ici.

🔴 LE REJET EST TERMINAL (message → `failed`), comme pour `suppression_list`.
Un skip qui laisserait le message en `draft` le ferait squatter la tête de la
file FIFO à chaque passe, pour toujours — c'est le défaut que
`test_send_file_rotation.py` documente déjà.
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
        "id": "m-1", "subject": "S", "body_text": "Allo.",
        "to_email": "jean@plomberiex.ca", "status": "draft", "direction": "outbound",
        "compliance_check_passed": True, "contact_id": "ct-1",
        "track": "agence-ia", "compliance_notes": None,
        "followups": {"relance_1": "r1", "relance_2": "r2", "relance_3": "r3"},
    }
    base.update(over)
    return base


def _monter(monkeypatch, *, contact_status: str):
    """Monte le VRAI `send_one_message` sur un draft par ailleurs parfaitement
    envoyable : ni désabonné, ni domaine plateforme, warmup passé, relances
    présentes. SEUL le statut du contact varie."""
    from src.tools import send

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            return [_msg()]
        if table == "contacts":
            return [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
                     "email": "jean@plomberiex.ca", "company_id": "co-1",
                     "status": contact_status}]
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
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=(False, "")))
    return send, updates, add_lead


# ── Ce qui doit être BLOQUÉ ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "statut",
    ["disqualified", "opted_out", "bounced", "replied", "qualified", "booked",
     "contacted"],
)
async def test_contact_non_demarchable_ne_part_pas(monkeypatch, statut: str) -> None:
    """Aucun de ces sept statuts ne doit atteindre Instantly.

    `disqualified` est le cas de l'audit. `replied` et `booked` sont pires
    encore : le prospect est DÉJÀ en conversation, et lui renvoyer un courriel
    froid après sa réponse est exactement ce qui fait dire « c'est du spam ».
    """
    send, updates, add_lead = _monter(monkeypatch, contact_status=statut)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_contact_inactif", (
        f"statut {statut!r} : attendu un refus, obtenu {out.status!r}"
    )
    add_lead.assert_not_awaited()
    assert statut in (out.skipped_reason or ""), (
        "le motif doit NOMMER le statut fautif — sinon on relit la base à la main"
    )


async def test_le_refus_est_terminal_le_message_passe_failed(monkeypatch) -> None:
    """Sinon le brouillon reste `draft` et squatte la tête de la file FIFO à
    chaque passe, pour toujours."""
    send, updates, _ = _monter(monkeypatch, contact_status="disqualified")

    await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert any(t == "messages" and p.get("status") == "failed" for t, p in updates), (
        f"aucun passage à 'failed' ; écritures vues : {updates}"
    )


async def test_dry_run_ne_grave_rien(monkeypatch) -> None:
    """La simulation RAPPORTE le verdict sans tuer le brouillon — même contrat
    que les gardes `suppression_list` et `platform_domain`."""
    send, updates, add_lead = _monter(monkeypatch, contact_status="disqualified")

    out = await send.send_one_message(
        send.SendMessageIn(message_id="m-1", dry_run=True)
    )

    assert out.status == "skipped_contact_inactif"
    add_lead.assert_not_awaited()
    assert not any(p.get("status") == "failed" for _, p in updates), (
        "un dry_run ne doit JAMAIS graver 'failed'"
    )


async def test_statut_inconnu_est_bloque_liste_blanche(monkeypatch) -> None:
    """Le cœur du choix : une valeur d'enum que personne n'a prévue est
    REFUSÉE, pas laissée passer. Si ce test tombe parce que quelqu'un a
    transformé la garde en liste noire, c'est la garde qu'il faut remettre —
    pas ce test qu'il faut ajuster."""
    send, _, add_lead = _monter(monkeypatch, contact_status="valeur_future_inconnue")

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_contact_inactif"
    add_lead.assert_not_awaited()


# ── Ce qui doit PASSER ─────────────────────────────────────────────────────

@pytest.mark.parametrize("statut", ["new", "ready", "researching"])
async def test_les_trois_statuts_demarchables_passent(monkeypatch, statut: str) -> None:
    """La garde ne doit pas casser le chemin normal."""
    send, _, add_lead = _monter(monkeypatch, contact_status=statut)

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "ok", f"statut {statut!r} bloqué à tort : {out.skipped_reason!r}"
    add_lead.assert_awaited_once()


async def test_statut_absent_du_select_ne_bloque_pas_tout(monkeypatch) -> None:
    """🔴 LE CAS QUI FERAIT LE PLUS DE DÉGÂTS : si un jour quelqu'un retire
    `status` de la projection du `select` contacts, la garde recevrait `None`
    partout. En liste blanche stricte, elle bloquerait alors **tous les
    envois** — panne totale et silencieuse.

    Le contrat retenu : `None` (colonne absente de la projection) est traité
    comme « la garde ne sait pas » et laisse passer, parce que les autres
    gardes (suppression_list, compliance, domaine) tiennent toujours. Une
    valeur PRÉSENTE mais inconnue, elle, bloque — c'est le test ci-dessus.
    """
    from src.tools import send

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            return [_msg()]
        if table == "contacts":
            # Pas de clé "status" du tout.
            return [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
                     "email": "jean@plomberiex.ca", "company_id": "co-1"}]
        if table == "companies":
            return [{"name": "Plomberie X", "domain": "plomberiex.ca"}]
        return []

    add_lead = AsyncMock(return_value={"id": "lead-1"})
    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", AsyncMock(return_value=[{}]))
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", add_lead)
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=(False, "")))

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "ok"
    add_lead.assert_awaited_once()


# ── La projection elle-même ────────────────────────────────────────────────

async def test_le_select_contacts_demande_bien_status(monkeypatch) -> None:
    """Tient la PROJECTION, pas seulement le comportement.

    Sans cette assertion, retirer `status` du `select` rendrait la garde
    inerte — elle lirait `None` partout et laisserait tout passer (voir le test
    ci-dessus) — et aucun autre test ne le verrait, puisque les stubs rendent
    le champ quoi qu'on demande.
    """
    from src.tools import send

    projections: list[str] = []

    async def _select(table, *, params=None, schema=None):
        if table == "contacts":
            projections.append((params or {}).get("select", ""))
            return [{"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
                     "email": "jean@plomberiex.ca", "company_id": "co-1",
                     "status": "new"}]
        if table == "messages":
            return [_msg()]
        if table == "companies":
            return [{"name": "Plomberie X", "domain": "plomberiex.ca"}]
        return []

    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", AsyncMock(return_value=[{}]))
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign",
                        AsyncMock(return_value={"id": "lead-1"}))
    monkeypatch.setattr(send, "_is_suppressed", AsyncMock(return_value=(False, "")))

    await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert projections, "aucun select sur `contacts` — le test ne mesure plus rien"
    assert "status" in projections[0], (
        f"`status` absent de la projection contacts : {projections[0]!r}"
    )
