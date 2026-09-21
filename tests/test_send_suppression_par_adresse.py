"""Le désabonnement suit l'ADRESSE, pas la ligne de contact.

🔴 LE DÉFAUT QUE CE FICHIER FERME (dernier 🔴 de `docs/go-live-checklist.md`
avant l'envoi, trouvaille C6 du conseil PT1).

`send.py` portait déjà DEUX gardes, et elles fonctionnent :
  · la garde 3a lit `contacts.status` et n'accepte que
    `CONTACT_STATUTS_DEMARCHABLES` — mais **du contact qu'elle a en main** ;
  · la garde 3b lit `suppression_list` sur l'adresse et sur le domaine.

Entre les deux passe un cas précis : **une même adresse vit sur PLUSIEURS
lignes de `contacts`**. Le scraping trouve `info@x.ca` sur la page d'accueil et
la même adresse sur la page contact — deux lignes, une adresse. Que l'une
passe `opted_out` n'apprend RIEN à l'autre, restée `new`. La garde 3a prend la
ligne `new`, la trouve démarchable, et laisse partir un courriel vers quelqu'un
qui a demandé qu'on arrête.

📏 MESURÉ LE 2026-09-20, avant tout envoi : **502 contacts portent une adresse,
492 adresses sont distinctes** — donc 9 adresses vivent déjà sur plus d'une
ligne, et **8 d'entre elles sont refusées par cette garde** parce qu'elles
portent une ligne `new` À CÔTÉ d'une ligne `disqualified` (`client@cvert.ca`,
`info@entreprisemroy.ca`, `impallari@gmail.com` — celle-là est un artefact de
scraping, l'auteur d'une police Google Fonts ramassé dans un pied de page…).
L'effet n'est donc PAS théorique et ne commence pas au premier désabonnement :
huit adresses seraient parties aujourd'hui malgré un jugement explicite porté
sur l'une de leurs lignes. `opted_out` n'apparaîtra qu'après l'allumage de
WF-6 — la garde sera déjà en place.

✅ VÉRIFIÉ CONTRE LE VRAI POSTGREST, pas seulement contre ces doubles :
`in.("a","b")` est accepté, et `freekout_man@hotmail.com` (avec son underscore)
ne ramène QUE lui-même. Les tests ci-dessous simulent `db.select` — ils
prouvent que la garde demande le bon filtre, jamais que le serveur l'accepte.
Comme elle échoue en se fermant, une syntaxe refusée aurait bloqué TOUS les
envois. Script de contrôle : `scratchpad/verif_postgrest.py` de la session.

⚠️ `suppression_list` devrait rattraper le coup — sauf que l'autre moitié de
C6, livrée le 2026-08-26, existe précisément parce que cette écriture PEUT
échouer : `_add_to_suppression` rend désormais un booléen et crie sur
#alertes quand elle rate. Quand elle rate, le statut du contact est la seule
trace qui subsiste — et c'est exactement celle que personne ne reliait à
l'adresse.

🔴 CE QUI BLOQUE EST LA MÊME LISTE BLANCHE QUE LA GARDE 3a, pas une nouvelle
politique : si une ligne sœur porte un statut absent de
`CONTACT_STATUTS_DEMARCHABLES`, l'adresse est refusée. Une onzième valeur
ajoutée un jour à l'enum `contact_status` sera donc refusée tant que personne
ne l'aura explicitement autorisée.

🔴 ET LE FILTRE NE PEUT PAS ÊTRE UN `ilike`. Mesuré sur la même base : **5
adresses contiennent `_` et 1 contient `%`** — les deux jokers de SQL. Un
`ilike` ferait correspondre `jean_roy@x.ca` à `jeanXroy@x.ca` et bloquerait
une adresse étrangère au désabonnement. La comparaison doit rester une
ÉGALITÉ, sur les deux casses connues.

⚠️ CETTE GARDE-CI ÉCHOUE EN SE FERMANT, contrairement à sa jumelle
`_interested_lead_is_suppressed` (`reply.py`) qui échoue en s'ouvrant. Ce
n'est pas une incohérence : là-bas, bloquer coûte un lead chaud perdu en
silence ; ici, laisser passer coûte un courriel illégal. On refuse par défaut
du côté où l'erreur est irréversible.
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


ADRESSE = "info@deneigementx.ca"


def _msg(to_email: str = ADRESSE) -> dict:
    return {
        "id": "m-1", "subject": "S", "body_text": "Allo.",
        "to_email": to_email, "status": "draft", "direction": "outbound",
        "compliance_check_passed": True, "contact_id": "ct-1",
        "track": "agence-ia", "compliance_notes": None,
        "followups": {"relance_1": "r1", "relance_2": "r2", "relance_3": "r3"},
    }


def _monter(monkeypatch, *, freres: list[dict], to_email: str = ADRESSE):
    """Monte le VRAI `send_one_message` sur un brouillon par ailleurs
    parfaitement envoyable : le contact VISÉ est `new`, rien n'est sur
    `suppression_list`, le warmup est passé, les relances sont là.

    `freres` = ce que la base rend quand on interroge `contacts` PAR ADRESSE.
    """
    from src.tools import send

    vus: list[dict] = []

    async def _select(table, *, params=None, schema=None):
        vus.append({"table": table, **(params or {})})
        if table == "messages":
            return [_msg(to_email)]
        if table == "contacts":
            # Lecture par id (garde 3a) : le contact visé, démarchable.
            if str(params.get("id", "")).startswith("eq."):
                return [{"id": "ct-1", "first_name": "Info", "last_name": "",
                         "email": to_email, "company_id": "co-1", "status": "new"}]
            # Lecture par adresse : c'est la garde qu'on teste.
            return freres
        if table == "companies":
            return [{"name": "Déneigement X", "domain": "deneigementx.ca"}]
        if table == "suppression_list":
            return []
        return []

    updates: list[tuple[str, dict]] = []

    async def _update(table, patch, **kw):
        updates.append((table, patch))
        return [{}]

    add_lead = AsyncMock(return_value={"id": "lead-1"})
    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", _update)
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", add_lead)
    return send, updates, add_lead, vus


# ── Ce qui doit être BLOQUÉ ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "statut_du_frere",
    ["opted_out", "bounced", "disqualified", "replied", "qualified", "booked",
     "contacted", "valeur_future_inconnue"],
)
async def test_une_ligne_soeur_non_demarchable_bloque_l_adresse(
    monkeypatch, statut_du_frere: str
) -> None:
    """Le contact visé est `new` et passe la garde 3a. C'est SA JUMELLE, qui
    porte la même adresse, qui doit l'arrêter.

    `opted_out` est le cas LCAP. `bounced` protège la réputation du domaine
    d'envoi. `contacted` est le cas « une entreprise = un courriel froid » vu
    par l'adresse : réécrire à une adresse déjà servie est exactement ce qui
    fait dire « c'est du spam »."""
    send, _, add_lead, _ = _monter(
        monkeypatch,
        freres=[{"status": "new", "email": ADRESSE},
                {"status": statut_du_frere, "email": ADRESSE}],
    )

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_suppressed", (
        f"frère {statut_du_frere!r} : attendu un refus, obtenu {out.status!r}"
    )
    add_lead.assert_not_awaited()
    assert statut_du_frere in (out.skipped_reason or ""), (
        "le motif doit NOMMER le statut fautif — sinon on relit la base à la main"
    )


async def test_le_refus_est_terminal_le_message_passe_failed(monkeypatch) -> None:
    """Même contrat que les deux autres gardes : un skip qui laisserait le
    brouillon en `draft` le ferait squatter la tête de la file FIFO à chaque
    passe, pour toujours."""
    send, updates, _, _ = _monter(
        monkeypatch, freres=[{"status": "opted_out", "email": ADRESSE}]
    )

    await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert any(t == "messages" and p.get("status") == "failed" for t, p in updates), (
        f"aucun passage à 'failed' ; écritures vues : {updates}"
    )


async def test_dry_run_ne_grave_rien(monkeypatch) -> None:
    send, updates, add_lead, _ = _monter(
        monkeypatch, freres=[{"status": "opted_out", "email": ADRESSE}]
    )

    out = await send.send_one_message(
        send.SendMessageIn(message_id="m-1", dry_run=True)
    )

    assert out.status == "skipped_suppressed"
    add_lead.assert_not_awaited()
    assert not any(p.get("status") == "failed" for _, p in updates), (
        "un dry_run ne doit JAMAIS graver 'failed'"
    )


async def test_la_casse_ne_sauve_pas_un_desabonne(monkeypatch) -> None:
    """Une seule ligne de la base de production porte une majuscule, mais elle
    suffit : `Info@X.ca` désabonné doit bloquer `info@x.ca`."""
    send, _, add_lead, vus = _monter(
        monkeypatch,
        freres=[{"status": "opted_out", "email": ADRESSE.upper()}],
        to_email=ADRESSE.upper(),
    )

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "skipped_suppressed"
    add_lead.assert_not_awaited()
    filtre = next(v["email"] for v in vus
                  if v["table"] == "contacts" and "email" in v)
    assert ADRESSE in filtre and ADRESSE.upper() in filtre, (
        f"les deux casses doivent être interrogées ; filtre vu : {filtre!r}"
    )


# ── Ce qui doit PASSER ─────────────────────────────────────────────────────

async def test_des_freres_tous_demarchables_ne_bloquent_rien(monkeypatch) -> None:
    send, _, add_lead, _ = _monter(
        monkeypatch,
        freres=[{"status": "new", "email": ADRESSE},
                {"status": "ready", "email": ADRESSE},
                {"status": "researching", "email": ADRESSE}],
    )

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "ok", f"refus injustifié : {out.skipped_reason!r}"
    add_lead.assert_awaited_once()


async def test_aucune_ligne_soeur_ne_bloque_rien(monkeypatch) -> None:
    """Le cas normal : l'adresse n'existe qu'une fois."""
    send, _, add_lead, _ = _monter(
        monkeypatch, freres=[{"status": "new", "email": ADRESSE}]
    )

    out = await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    assert out.status == "ok"
    add_lead.assert_awaited_once()


# ── La forme du filtre, qui est elle-même la garde ─────────────────────────

async def test_le_filtre_est_une_egalite_jamais_un_like(monkeypatch) -> None:
    """🔴 5 adresses de la base contiennent `_` et 1 contient `%`. Un `ilike`
    les traiterait comme des jokers et bloquerait des adresses étrangères au
    désabonnement. Si ce test tombe parce que quelqu'un a « simplifié » en
    `ilike`, c'est le filtre qu'il faut remettre — pas ce test."""
    piege = "jean_roy@x.ca"
    send, _, _, vus = _monter(
        monkeypatch, freres=[{"status": "new", "email": piege}], to_email=piege
    )

    await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    filtres = [v["email"] for v in vus if v["table"] == "contacts" and "email" in v]
    assert filtres, "la garde n'a jamais interrogé `contacts` par adresse"
    for f in filtres:
        assert "like" not in f.lower(), (
            f"filtre à joker sur une adresse contenant '_' : {f!r}"
        )
        assert piege in f, f"l'adresse doit voyager telle quelle : {f!r}"


async def test_une_lecture_en_panne_ne_laisse_pas_partir_le_courriel(
    monkeypatch,
) -> None:
    """🔴 L'INVERSE DE `reply.py`, ET C'EST VOULU. Là-bas la vérif échoue en
    s'ouvrant, parce que bloquer coûterait un lead chaud perdu en silence.
    Ici, laisser passer coûte un courriel à un désabonné : on se ferme."""
    from src.tools import send

    async def _select(table, *, params=None, schema=None):
        if table == "messages":
            return [_msg()]
        if table == "contacts":
            if str((params or {}).get("id", "")).startswith("eq."):
                return [{"id": "ct-1", "first_name": "Info", "last_name": "",
                         "email": ADRESSE, "company_id": "co-1", "status": "new"}]
            raise RuntimeError("PostgREST 503")
        if table == "companies":
            return [{"name": "Déneigement X", "domain": "deneigementx.ca"}]
        return []

    add_lead = AsyncMock(return_value={"id": "lead-1"})
    monkeypatch.setattr(send.db, "select", _select)
    monkeypatch.setattr(send.db, "update", AsyncMock(return_value=[{}]))
    monkeypatch.setattr(send.instantly_lib, "add_lead_to_campaign", add_lead)

    with pytest.raises(RuntimeError):
        await send.send_one_message(send.SendMessageIn(message_id="m-1"))

    add_lead.assert_not_awaited()
