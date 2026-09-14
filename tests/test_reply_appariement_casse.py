"""L'appariement d'un courriel entrant ne doit pas dépendre de la casse.

🔴 LE DÉFAUT (moitié vérifiable de l'engagement nº 5 d'AC1c·A). `reply.py`
cherche le contact et le message parent en **égalité exacte** — `eq.{email}` en
PostgREST, qui est sensible à la casse. Un prospect qui répond depuis
`Jean.Tremblay@Gmail.com` alors que la base porte `jean.tremblay@gmail.com` ne
serait pas reconnu : sa réponse tomberait en « entrant orphelin », son contact
resterait `new`, et il pourrait recevoir un **deuxième** courriel froid après
avoir déjà répondu.

📏 POURQUOI CE DÉFAUT EST LATENT ET PAS VIVANT, mesuré le 2026-09-13 :
**0 des 417 adresses en base ne porte de majuscule**, et le chemin du *polling*
normalise déjà l'adresse entrante (`_first_str` fait `.strip().lower()`). Il ne
mordrait donc que par un autre chemin — le replay manuel de
`/wf7/handle-reply`, ou un scrape futur qui stockerait une majuscule.

✅ CE QUE CE FICHIER ÉPINGLE : les deux fonctions de recherche normalisent
**elles-mêmes**, au lieu de faire confiance à leur appelant. Une garde qui
dépend de la politesse de ses appelants n'est pas une garde.

🔴 CE QUE CE FICHIER NE FERME PAS — l'autre moitié de l'engagement nº 5, et
c'est la plus grave. `lead_email` est construit à partir du **From de la
réponse** (`from_address_email_list`, `from_address`, `from_email`, `from`),
donc de l'adresse depuis laquelle le prospect écrit — PAS de l'adresse de
campagne à qui on a écrit. Le cas que le code appelle lui-même « le plus
typique » reste entier : on écrit à `info@plomberie.ca`, le patron répond depuis
son gmail personnel, et **aucune normalisation de casse n'y change rien**. Il
faudrait relire le payload Instantly brut pour y trouver le champ qui porte le
lead de campagne — ce qui exige un payload réel sous les yeux, pas une
supposition sur des noms de champs.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


async def test_le_contact_est_trouve_malgre_les_majuscules(monkeypatch) -> None:
    from src.tools import reply

    vus: list[str] = []

    async def _select(table, *, params=None, schema=None):
        if table == "contacts":
            vus.append((params or {}).get("email", ""))
            return [{"id": "ct-1", "company_id": "co-1", "first_name": "Jean",
                     "last_name": "Roy", "email": "jean@plomberiex.ca",
                     "status": "new"}]
        return []

    monkeypatch.setattr(reply.db, "select", _select)

    out = await reply._find_contact_by_email("  Jean@PlomberieX.CA  ")

    assert out is not None and out["id"] == "ct-1"
    assert vus == ["eq.jean@plomberiex.ca"], (
        f"l'adresse doit être mise en minuscules et détourée avant la requête ; "
        f"envoyé : {vus!r}"
    )


async def test_le_parent_est_trouve_malgre_les_majuscules(monkeypatch) -> None:
    """Même garde sur `_find_parent_outbound`, qui filtre sur `to_email`.

    Sans elle, on retomberait sur `_find_contact_by_email` — ça marcherait par
    chance, mais on aurait perdu le lien vers le message parent, donc le fil de
    conversation.
    """
    from src.tools import reply

    vus: list[str] = []

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            p = params or {}
            if "to_email" in p:
                vus.append(p["to_email"])
                return [{"id": "m-1", "contact_id": "ct-1", "campaign_id": None,
                         "sequence_step_id": None, "subject": "S",
                         "body_text": "B", "to_email": "jean@plomberiex.ca",
                         "provider_message_id": "pm-1", "sent_at": None}]
        return []

    monkeypatch.setattr(reply.db, "select", _select)

    out = await reply._find_parent_outbound(
        parent_provider_id=None, lead_email="JEAN@PlomberieX.ca"
    )

    assert out is not None and out["contact_id"] == "ct-1"
    assert vus == ["eq.jean@plomberiex.ca"], f"envoyé : {vus!r}"


async def test_adresse_vide_ne_lance_aucune_requete(monkeypatch) -> None:
    """Une chaîne de blancs ne doit pas devenir `eq.` et ramener n'importe qui.

    🔴 Sans le détourage AVANT le test de vacuité, `"   "` est vrai, la requête
    part avec un filtre vide et PostgREST peut rendre une ligne arbitraire — on
    apparierait la réponse d'un inconnu au contact de quelqu'un d'autre.
    """
    from src.tools import reply

    appels: list[str] = []

    async def _select(table, *, params=None, schema=None):
        appels.append(table)
        return []

    monkeypatch.setattr(reply.db, "select", _select)

    assert await reply._find_contact_by_email("   ") is None
    assert await reply._find_contact_by_email("") is None
    assert appels == [], f"aucune requête ne devait partir ; vues : {appels!r}"
