"""Le message brut d'Instantly doit être gardé sur chaque entrant.

🔴 POURQUOI, ET C'EST TRÈS CONCRET. Le rattachement d'une réponse à son
prospect est **cassé par construction**, et ce fichier est ce qui permettra de
le réparer sans rien deviner.

La chaîne, telle qu'elle est écrite aujourd'hui :

    send.py  →  messages.provider_message_id = <ID DU LEAD Instantly>
    réponse  →  in_reply_to_uuid              = <ID DU COURRIEL>
    reply.py →  cherche provider_message_id = in_reply_to_uuid

Deux sortes d'identifiants comparées avec `=`. Ça ne peut jamais correspondre.
La recherche par le fil est donc **morte**, et chaque réponse retombe sur le
seul repli qui reste : l'appariement par adresse d'expéditeur. D'où le cas que
`reply.py` appelle lui-même « le plus typique » — le prospect répond depuis son
gmail personnel, on ne le reconnaît pas, et il peut recevoir une relance après
avoir déjà répondu.

📏 POURQUOI ON NE PEUT PAS LE RÉPARER AUJOURD'HUI, mesuré le 2026-09-14 :
**0 des 156 messages porte un `provider_message_id`** — rien n'a jamais été
poussé chez Instantly, donc **aucune réponse n'existe**. Personne ne peut dire
quel champ du message brut porte le lead de campagne, et deviner un nom de
champ qui échoue en silence serait pire que de ne rien faire.

📏 ET L'APPARIEMENT PAR SUJET NE SAUVE RIEN : mesuré le même jour, **3 sujets
distincts pour 156 messages**, dont un partagé par **86**. Un « Re: » ne
désigne personne.

✅ D'OÙ CE FICHIER. En gardant le message brut, la **première vraie réponse**
livre elle-même les noms de champs. Sans lui, elle passerait et on n'aurait
toujours rien — on attendrait la deuxième, puis la troisième, pendant que des
prospects reçoivent des relances de trop.

⚠️ LES DEUX CHEMINS COMPTENT, et l'orphelin plus que l'autre : c'est
précisément celui qu'on ne sait pas rattacher, donc celui dont on a le plus
besoin du brut.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    monkeypatch.setenv("INSTANTLY_SENDER_EMAIL", "william@couture-ia.com")


BRUT = {
    "uuid": "e-123",
    "from_address_email_list": "jean.perso@gmail.com",
    "in_reply_to_uuid": "e-parent-999",
    "thread_id": "t-42",
    # Le champ qu'on cherche vit peut-être ici, sous un nom qu'on ignore :
    "lead": "info@plomberiex.ca",
    "campaign_id": "camp-7",
}


def _payload(**over):
    from src.tools.reply import HandleReplyIn
    base = dict(
        lead_email="jean.perso@gmail.com",
        reply_subject="Re: une question",
        reply_body_text="Oui ça m'intéresse, rappelez-moi",
        provider_message_id_inbound="e-123",
        provider_message_id_parent="e-parent-999",
        eaccount="william@couture-ia.com",
        raw_payload=BRUT,
    )
    base.update(over)
    return HandleReplyIn(**base)


def _monter(monkeypatch, *, contact_trouve: bool):
    """Monte `handle_reply`. `contact_trouve=False` force le chemin ORPHELIN."""
    from src.tools import reply

    inserts: list[tuple[str, dict]] = []

    async def _insert(table, row, **kw):
        inserts.append((table, row))
        return [{"id": "m-new"}]

    async def _select(table, *, params=None, schema=None):
        if table == "contacts" and contact_trouve:
            return [{"id": "ct-1", "company_id": "co-1", "first_name": "Jean",
                     "last_name": "Roy", "email": "jean.perso@gmail.com",
                     "status": "new"}]
        if table == "companies" and contact_trouve:
            return [{"id": "co-1", "name": "Plomberie X", "domain": "plomberiex.ca",
                     "website": None, "city": "Laval", "icp_segment": None,
                     "industry": "plombier", "research_json": {}, "track": "agence-ia"}]
        return []

    monkeypatch.setattr(reply.db, "insert", _insert)
    monkeypatch.setattr(reply.db, "select", _select)
    monkeypatch.setattr(reply.db, "update", AsyncMock(return_value=[{}]))
    monkeypatch.setattr(reply.slack_lib, "notify", AsyncMock(return_value=True))
    return reply, inserts


async def test_l_orphelin_garde_le_message_brut(monkeypatch) -> None:
    """🔴 LE CHEMIN QUI COMPTE LE PLUS.

    C'est la réponse qu'on ne sait PAS rattacher — donc la seule qui puisse
    nous apprendre comment la rattacher.
    """
    reply, inserts = _monter(monkeypatch, contact_trouve=False)

    out = await reply.handle_reply(_payload())

    assert out.status == "skipped_no_contact", f"pas le chemin orphelin : {out.status}"
    lignes = [r for t, r in inserts if t == "messages"]
    assert lignes, "aucun entrant enregistré"
    assert lignes[0].get("raw_payload") == BRUT, (
        "le message brut est JETÉ sur le chemin orphelin — la première vraie "
        "réponse ne nous apprendrait rien"
    )


async def test_l_entrant_rattache_garde_aussi_le_brut(monkeypatch) -> None:
    """Moins critique, mais c'est lui qui donnera la CONTRE-ÉPREUVE : comparer
    un brut qu'on a su rattacher à un brut orphelin dit quel champ diffère."""
    reply, inserts = _monter(monkeypatch, contact_trouve=True)

    await reply.handle_reply(_payload())

    lignes = [r for t, r in inserts if t == "messages"]
    assert lignes, "aucun entrant enregistré"
    assert lignes[0].get("raw_payload") == BRUT


async def test_un_brut_absent_ne_casse_rien(monkeypatch) -> None:
    """`raw_payload` est optionnel dans le modèle (replay manuel, tests).

    Écrire `None` est correct ; lever ne l'est pas — une réponse perdue parce
    qu'on n'avait pas son brut serait le comble.
    """
    reply, inserts = _monter(monkeypatch, contact_trouve=False)

    out = await reply.handle_reply(_payload(raw_payload=None))

    assert out.status == "skipped_no_contact"
    lignes = [r for t, r in inserts if t == "messages"]
    assert lignes and lignes[0].get("raw_payload") is None


async def test_le_brut_n_est_pas_aplati_en_texte(monkeypatch) -> None:
    """La colonne est `jsonb` : on doit pouvoir requêter les champs.

    Si quelqu'un « simplifie » en `json.dumps(...)`, on perd
    `raw_payload->>'lead'` en SQL — donc la seule façon de trouver le bon champ
    sans relire 400 lignes à la main.
    """
    reply, inserts = _monter(monkeypatch, contact_trouve=False)

    await reply.handle_reply(_payload())

    brut = [r for t, r in inserts if t == "messages"][0]["raw_payload"]
    assert isinstance(brut, dict), f"aplati en {type(brut).__name__}"
    assert brut["lead"] == "info@plomberiex.ca"
