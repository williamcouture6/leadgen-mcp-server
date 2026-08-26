"""Le parcours « il dit non, puis il change d'idée » — deux ruptures en aval
de la classification.

**Rupture B — l'ordre du lot.** `poll_and_process_replies` traitait les
messages dans l'ordre où Instantly les rend, sans jamais trier. Le prospect
écrit « on gère ça à l'interne » à 9h05, William lui répond de sa main à 9h12,
le prospect écrit « ah ok, montrez-moi » à 9h20 : le cron de 30 minutes ramène
les DEUX dans le même paquet. Si Instantly rend le plus récent d'abord, le
refus est traité EN DERNIER et gagne — le contact finit `disqualified` + `cold`
alors que sa dernière parole est un oui. C'est Instantly qui décidait, pas la
chronologie.

**Rupture C — le ping « reply orphelin ».** Quand l'adresse entrante ne
correspond à aucun contact (le prospect répond depuis son gmail perso), le ping
disait seulement « contact introuvable » : William devait aller fouiller
Outlook pour savoir si c'était un oui qu'il venait de rater. Et l'action
`slack_ping` était inscrite en dur, sans lire le retour de `notify`.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# =====================================================================
# Rupture B — la chronologie, pas l'ordre de l'API
# =====================================================================

def _item(uid: str, horodatage: str | None = None, **extra):
    """Item minimal du shape GET /api/v2/emails (type=received)."""
    it: dict = {
        "id": uid,
        "from_address_email_list": "jean@x.ca",
        "body": {"text": f"corps de {uid}"},
    }
    if horodatage is not None:
        it["timestamp_created"] = horodatage
    it.update(extra)
    return it


def _wire_poll(monkeypatch, items):
    """Remplace l'appel Instantly et `handle_reply`, et enregistre l'ORDRE
    dans lequel les messages sont effectivement traités."""
    from src.lib import instantly as instantly_lib
    from src.tools import reply

    ordre: list[str] = []

    async def fake_list_emails(**kw):
        return {"items": list(items)}

    async def fake_handle_reply(payload):
        ordre.append(payload.provider_message_id_inbound)
        return reply.HandleReplyOut(status="ok", category="interested")

    monkeypatch.setattr(instantly_lib, "list_emails", fake_list_emails)
    monkeypatch.setattr(reply, "handle_reply", fake_handle_reply)
    return ordre


# 9h05 : « on gère ça à l'interne ».  9h20 : « ah ok, montrez-moi ».
_REFUS = _item("refus-9h05", "2026-08-24T13:05:00.000Z")
_REVIREMENT = _item("revirement-9h20", "2026-08-24T13:20:00.000Z")


async def test_le_lot_se_traite_du_plus_vieux_au_plus_recent(monkeypatch):
    from src.tools import reply

    # Instantly rend le plus RÉCENT d'abord — le cas qui casse.
    ordre = _wire_poll(monkeypatch, [_REVIREMENT, _REFUS])
    out = await reply.poll_and_process_replies(reply.PollRepliesIn())

    assert ordre == ["refus-9h05", "revirement-9h20"]
    assert out.processed == 2


async def test_lordre_rendu_par_lapi_ne_decide_plus(monkeypatch):
    """Le même lot, rendu dans les deux sens, doit produire le même état final.

    C'est LE contrat : la dernière parole du prospect est la dernière écrite."""
    from src.tools import reply

    croissant = _wire_poll(monkeypatch, [_REFUS, _REVIREMENT])
    await reply.poll_and_process_replies(reply.PollRepliesIn())

    decroissant = _wire_poll(monkeypatch, [_REVIREMENT, _REFUS])
    await reply.poll_and_process_replies(reply.PollRepliesIn())

    assert croissant == decroissant == ["refus-9h05", "revirement-9h20"]


async def test_un_item_sans_horodatage_ne_fait_pas_planter_le_poll(monkeypatch):
    """Un tri qui lève sur une clé absente ou nulle casserait TOUT le poll —
    pas seulement l'item fautif. L'ordre retenu pour ces cas-là : ils passent
    en premier (un item non daté ne peut pas l'emporter sur un item daté), et
    entre eux ils gardent l'ordre de l'API (le tri est stable)."""
    from src.tools import reply

    ordre = _wire_poll(monkeypatch, [
        _item("date", "2026-08-24T13:20:00.000Z"),
        _item("cle-absente"),                          # pas de timestamp_created
        _item("cle-nulle", timestamp_created=None),    # clé présente, valeur nulle
    ])
    out = await reply.poll_and_process_replies(reply.PollRepliesIn())

    assert out.errors == 0
    assert out.processed == 3
    assert ordre == ["cle-absente", "cle-nulle", "date"]


async def test_le_repli_created_at_sert_aussi_de_cle_de_tri(monkeypatch):
    """`extract_from_instantly_email_list_item` accepte `created_at` en repli ;
    le tri doit lire exactement la même chose, sinon les deux divergent.

    ⚠️ L'item porté par `created_at` est volontairement le PLUS RÉCENT, donc
    celui qui doit finir DERNIER. Écrit dans l'autre sens, le test passait même
    sans le repli : la clé tombait à `""`, l'item partait en tête, et c'était
    justement la place attendue — il ne prouvait rien (vérifié par mutation)."""
    from src.tools import reply

    ordre = _wire_poll(monkeypatch, [
        _item("recent-autre-champ", created_at="2026-08-24T13:20:00.000Z"),
        _item("vieux", "2026-08-24T13:05:00.000Z"),
    ])
    await reply.poll_and_process_replies(reply.PollRepliesIn())

    assert ordre == ["vieux", "recent-autre-champ"]


def test_lextrait_et_le_tri_lisent_le_meme_horodatage():
    """Une seule définition de « quand ce message est arrivé » : `received_at`
    de l'extraction et la clé de tri sortent du même helper."""
    from src.tools import reply

    item = _item("x", "2026-08-24T13:20:00.000Z")
    extrait = reply.extract_from_instantly_email_list_item(item)

    assert extrait is not None
    assert extrait.received_at == "2026-08-24T13:20:00.000Z"
    assert reply._horodatage_item(item) == extrait.received_at


# =====================================================================
# Rupture C — le ping « reply orphelin » n'était pas lisible
# =====================================================================

def _wire_orphelin(monkeypatch, *, ping_ok=True):
    """Aucun message parent, aucun contact : `handle_reply` tombe dans la
    branche orpheline avant même d'appeler le classifieur."""
    from src import supabase_client
    from src.lib import slack as slack_mod

    vu: dict = {"notifies": [], "inserts": []}

    async def fake_select(table, *, params=None, schema=None):
        return []

    async def fake_insert(table, row, **kw):
        vu["inserts"].append((table, row))
        return [{"id": "orph-1"}]

    async def fake_notify(*, text, blocks=None, context=None, category=None):
        vu["notifies"].append({"text": text, "blocks": blocks,
                               "context": context, "category": category})
        return ping_ok

    def boom_classifier(*a, **kw):
        raise AssertionError("le classifieur ne doit pas tourner sans contact")

    monkeypatch.setattr(supabase_client, "select", fake_select)
    monkeypatch.setattr(supabase_client, "insert", fake_insert)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    from src.tools import reply
    monkeypatch.setattr(reply, "_call_classifier", boom_classifier)
    return vu


# Formulé avec des mots qu'on ne trouve NULLE PART dans le texte fixe du ping :
# sinon le test matcherait la phrase d'en-tête même avec un extrait vide.
_CRI_DU_PROSPECT = "Finalement oui — envoyez-moi votre proposition avant vendredi."


def _payload_orphelin(corps=_CRI_DU_PROSPECT):
    from src.tools import reply

    return reply.HandleReplyIn(
        lead_email="jean.perso@gmail.com",
        reply_subject="Re: votre site",
        reply_body_text=corps,
        provider_message_id_inbound="inb-orph",
    )


async def test_le_ping_orphelin_porte_lextrait(monkeypatch):
    """Sans l'extrait, William doit ouvrir Outlook pour savoir si c'est un oui."""
    from src.tools import reply

    vu = _wire_orphelin(monkeypatch)
    out = await reply.handle_reply(_payload_orphelin())

    assert out.status == "skipped_no_contact"
    pings = [n for n in vu["notifies"] if n["context"] == "wf7_orphan_reply"]
    assert len(pings) == 1
    texte = pings[0]["text"]
    assert "jean.perso@gmail.com" in texte
    assert "envoyez-moi votre proposition" in texte


async def test_le_journal_orphelin_dit_si_le_ping_est_passe(monkeypatch):
    """`slack_ping` était écrit en dur : un ping perdu était journalisé parti."""
    from src.tools import reply

    _wire_orphelin(monkeypatch, ping_ok=True)
    passe = await reply.handle_reply(_payload_orphelin())

    _wire_orphelin(monkeypatch, ping_ok=False)
    perdu = await reply.handle_reply(_payload_orphelin())

    assert passe.actions_taken == ["orphan_logged", "slack_ping"]
    assert perdu.actions_taken == ["orphan_logged", "slack_ping_failed"]


async def test_lextrait_orphelin_est_borne(monkeypatch):
    """Même borne que les extraits des pings voisins — #alertes ne reçoit pas
    un courriel de 2 000 caractères."""
    from src.tools import reply

    vu = _wire_orphelin(monkeypatch)
    await reply.handle_reply(
        _payload_orphelin("Je suis intéressé par votre offre. " * 60)
    )

    texte = vu["notifies"][0]["text"]
    assert "…" in texte
    assert len(texte) < 700, len(texte)
