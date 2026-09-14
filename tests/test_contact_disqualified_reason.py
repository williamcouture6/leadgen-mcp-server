"""Écarter un contact doit dire POURQUOI.

🔴 LE DÉFAUT (engagement nº 4b d'AC1c·A). `companies.disqualified_reason`
existe et est rempli à 100 % — c'est le système que William a bâti, et il
marche. Mais il n'a pas d'équivalent sur `contacts`, alors que c'est le niveau
où se prennent la plupart des décisions : une entreprise a souvent plusieurs
adresses, et en écarter une n'est pas fermer l'entreprise.

Mesuré le 2026-09-14 : **16 contacts disqualifiés**, dont **14 appartiennent à
une entreprise parfaitement valide** (`enriched`). Pour ces 14, il n'existait
aucun endroit où écrire la raison. Le même mot, `disqualified`, recouvrait au
moins sept causes :

    william@gmail.com                    l'adresse personnelle de William,
                                         ramassée par erreur
    impallari@gmail.com                  adresse parasite venue du code du site
    info@www.exterminationleblanc.com    adresse malformée
    manpreet.kaur@rentokil-terminix.com  multinationale + adresse nominative
    client@cvert.ca  (×2)                doublon entre deux fiches
    nicholas.a.jacques@gmail.com         nominative → Loi 25
    info@entreprisemroy.ca               personne ne sait

🔴 CE QUE ÇA COÛTE, et ce n'est pas de la propreté documentaire :
  · on ne peut pas revenir en arrière — rouvrir une fiche écartée par erreur
    demande de savoir pourquoi elle l'a été ;
  · on ne distingue pas une DÉCISION d'un AUTOMATISME. Sur les 16, 9 ont un
    jumeau actif ailleurs : ce sont des doublons fermés par la mécanique de la
    migration 0049, pas par un jugement. Une session future pourrait « corriger »
    une machine en croyant corriger un humain ;
  · on ne peut pas mesurer la qualité du scraping. Cinq de ces adresses sont
    des déchets d'extraction — c'est un signal fort sur WF-3, et il est
    invisible.

📏 UN SEUL CHEMIN DE CODE écarte un contact aujourd'hui : `reply.py`, quand le
prospect répond « pas intéressé ». C'est aussi la raison qu'il est le plus
grave de perdre — un refus explicite touche au consentement, donc à la LCAP.
C'est celui que ce fichier câble.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _capture(monkeypatch):
    """Capture les patches envoyés à `contacts`."""
    from src.tools import reply

    patches: list[dict] = []

    async def _update(table, patch, *, filters=None, **kw):
        if table == "contacts":
            patches.append(patch)
        return [{}]

    monkeypatch.setattr(reply.db, "update", _update)
    return reply, patches


async def test_un_refus_ecrit_sa_raison(monkeypatch) -> None:
    """🔴 LE CAS QUI COMPTE LE PLUS.

    « Le prospect a dit non » est un refus explicite : il touche au
    consentement. Le perdre, c'est ne plus pouvoir prouver pourquoi on ne
    réécrit pas — ou pire, réécrire.
    """
    reply, patches = _capture(monkeypatch)

    ok = await reply._update_contact_status(
        "ct-1", "disqualified", raison="reponse: pas interesse (WF-7)"
    )

    assert ok is True
    assert patches, "aucune écriture sur contacts"
    assert patches[0]["status"] == "disqualified"
    assert patches[0].get("disqualified_reason") == "reponse: pas interesse (WF-7)", (
        f"la raison n'est pas écrite : {patches[0]}"
    )


async def test_sans_raison_la_colonne_n_est_pas_touchee(monkeypatch) -> None:
    """🔴 NE PAS ÉCRASER UNE RAISON EXISTANTE PAR `None`.

    `_update_contact_status` sert aussi à des transitions qui ne sont pas des
    disqualifications (`replied`, `contacted`…). Si elle envoyait
    `disqualified_reason: None` à chaque appel, elle effacerait la raison d'un
    contact écarté plus tôt — un champ qui se vide tout seul est pire que pas
    de champ du tout.
    """
    reply, patches = _capture(monkeypatch)

    await reply._update_contact_status("ct-1", "replied")

    assert patches[0] == {"status": "replied"}, (
        f"la clé disqualified_reason ne doit PAS être envoyée : {patches[0]}"
    )


async def test_la_branche_pas_interesse_cable_bien_la_raison(monkeypatch) -> None:
    """Bout à bout : la vraie branche de `handle_reply`, pas juste l'aide.

    Sans ce test, quelqu'un pourrait ajouter le paramètre à la fonction et
    oublier de le passer à l'appel — la colonne existerait, vide pour toujours.
    C'est le défaut que ce fichier existe pour empêcher, rejoué une couche plus
    haut.
    """
    from src.tools import reply

    patches: list[dict] = []

    async def _update(table, patch, *, filters=None, **kw):
        if table == "contacts":
            patches.append(patch)
        return [{}]

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            p = params or {}
            # L'idempotence cherche un ENTRANT deja traite : il ne doit pas
            # exister, sinon handle_reply sort en `skipped_duplicate` et le test
            # ne mesure plus rien.
            if p.get("direction") == "eq.inbound":
                return []
            return [{"id": "m-1", "contact_id": "ct-1", "campaign_id": None,
                     "sequence_step_id": None, "subject": "S", "body_text": "B",
                     "to_email": "jean@x.ca", "provider_message_id": "pm-1",
                     "sent_at": None}]
        if table == "contacts":
            return [{"id": "ct-1", "company_id": "co-1", "first_name": "Jean",
                     "last_name": "Roy", "email": "jean@x.ca", "status": "new"}]
        if table == "companies":
            return [{"id": "co-1", "name": "X", "domain": "x.ca", "website": None,
                     "city": "Laval", "icp_segment": None, "industry": "plombier",
                     "research_json": {}, "track": "agence-ia"}]
        return []

    monkeypatch.setattr(reply.db, "update", _update)
    monkeypatch.setattr(reply.db, "select", _select)
    monkeypatch.setattr(reply.db, "insert", AsyncMock(return_value=[{"id": "m-2"}]))
    monkeypatch.setattr(reply.slack_lib, "notify", AsyncMock(return_value=True))
    # Le classifieur est un appel LLM synchrone lance dans un thread : on le
    # remplace par une fonction ordinaire qui rend le couple attendu.
    monkeypatch.setattr(
        reply, "_call_classifier",
        lambda *a, **k: ({"category": "not_interested", "confidence": 0.9}, {}),
    )

    out = await reply.handle_reply(reply.HandleReplyIn(
        lead_email="jean@x.ca",
        reply_subject="Re: allo",
        reply_body_text="Non merci, pas intéressé",
        provider_message_id_inbound="e-1",
        provider_message_id_parent="pm-1",
        eaccount="william@couture-ia.com",
    ))

    assert "contact_disqualified" in (out.actions_taken or []), out.actions_taken
    dq = [p for p in patches if p.get("status") == "disqualified"]
    assert dq, f"aucune disqualification écrite : {patches}"
    raison = dq[0].get("disqualified_reason") or ""
    assert raison, "le contact est disqualifié SANS raison — le défaut est rouvert"
    assert "interess" in raison.lower() or "refus" in raison.lower(), (
        f"la raison ne dit pas ce qui s'est passé : {raison!r}"
    )
