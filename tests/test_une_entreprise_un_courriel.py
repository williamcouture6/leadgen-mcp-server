"""Une entreprise ne reçoit qu'UN courriel froid, même à plusieurs jours d'écart.

🔴 LE DÉFAUT, mesuré le 2026-09-10 sur la vraie base : **85 leads pour 78
entreprises**, et PROGAZON en avait reçu **quatre** — sur les bras A, C et D.

La cause tient en une ligne : l'exclusion portait sur le CONTACT
(`already_drafted`), et `max_per_company` ne valait qu'À L'INTÉRIEUR d'un lot.
Le lot de 12 h retenait le contact 1 de PROGAZON et écartait le contact 2 ;
celui de 12 h 30 revoyait le contact 2, ne le trouvait dans aucun message, et
le servait.

⚠️ ET LE FRÈRE DÉJÀ SERVI N'EST PAS VISIBLE DANS LA FILE. `send.py` passe le
contact à `status='contacted'` au moment du push, donc il sort du
`status in (new, ready)` que lit la sélection. Déduire l'exclusion des contacts
de la page raterait donc EXACTEMENT le cas dangereux — celui où l'entreprise a
déjà reçu un courriel. Il faut une lecture qui remonte des entreprises vers
tous leurs contacts, pas seulement ceux de la page.

CE QUE ÇA COÛTE, ET POURQUOI ON LE PAIE. C'est d'abord un problème d'ENVOI :
quatre courriels froids à la même entreprise, c'est ce qui fait dire « c'est du
spam », et ça se voit chez le prospect avant de se voir chez nous. C'est
accessoirement un confondant de mesure — le bras du deuxième courriel est mêlé
à « cette boîte nous a déjà vus ».

⚠️ Un message `failed` ne bloque PAS : c'est la façon prévue de retirer un
brouillon à la main, et le contact doit redevenir servable. Même règle que pour
`already_drafted`.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.tools import db as db_tools


class _BaseDeuxFreres:
    """Une entreprise, deux contacts. Le premier a déjà reçu son courriel et a
    quitté la file (`status='contacted'`) ; le second y est encore."""

    def __init__(self, *, statut_du_message: str = "sent") -> None:
        self.statut_du_message = statut_du_message
        self.contacts_de_la_file = [
            {
                "id": "c2", "company_id": "co1", "email": "second@progazon.ca",
                "first_name": "Second", "last_name": "B", "email_verified": True,
                "title": None, "status": "new",
                "email_verification_source": "scrape", "raw_payload": {},
                "track": "agence-ia", "owner_confidence": None, "potential_owner": None,
            },
            {
                "id": "c9", "company_id": "co9", "email": "seul@autre.ca",
                "first_name": "Seul", "last_name": "B", "email_verified": True,
                "title": None, "status": "new",
                "email_verification_source": "scrape", "raw_payload": {},
                "track": "agence-ia", "owner_confidence": None, "potential_owner": None,
            },
        ]
        # c1 a été poussé hier : il n'est PLUS dans la file (statut 'contacted'),
        # mais il existe toujours et il porte un message vivant.
        self.tous_les_contacts = [
            {"id": "c1", "company_id": "co1"},
            {"id": "c2", "company_id": "co1"},
            {"id": "c9", "company_id": "co9"},
        ]

    async def select_all(self, table: str, *, order: str,
                         params: dict[str, Any] | None = None,
                         page_size: int = 1000, schema: str | None = None):
        """Les lectures non bornees passent par select_all (plafond PostgREST).

        Ce faux ne pagine pas : il rend tout d un coup. La pagination elle-meme
        est tenue par tests/test_supabase_plafond_1000.py ; ici on verifie
        seulement que l appelant demande bien la lecture NON BORNEE.
        """
        return await self.select(table, params or {})

    async def select(self, table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        # ⚠️ ON DISTINGUE PAR LE FILTRE, PAS PAR LA PRÉSENCE D'UN `limit`.
        # La lecture des frères est passée par `_select_par_tranches` le
        # 2026-09-20 — l'URL dépassait 14 ko et un 414 aurait vidé le lot EN
        # SILENCE, soit exactement le défaut que ce fichier protège. Or la
        # pagination par tranches ajoute un `limit` : l'ancien discriminant
        # rendait donc la PAGE quand on demandait les FRÈRES, et PROGAZON
        # redevenait éligible. Le faux mentait, pas le code.
        if table == "contacts" and str(params.get("company_id", "")).startswith("in.("):
            ids = params["company_id"].removeprefix("in.(").rstrip(")").split(",")
            return [c for c in self.tous_les_contacts if c["company_id"] in ids]
        if table == "contacts" and "limit" in params:
            deb = int(params.get("offset", 0))
            return self.contacts_de_la_file[deb:deb + int(params["limit"])]
        if table == "companies":
            ids = params["id"].removeprefix("in.(").rstrip(")").split(",")
            return [
                {
                    "id": i, "name": f"Ent {i}", "domain": "x.ca",
                    "website": "https://x.ca", "city": "Laval",
                    "icp_segment": None, "industry": None,
                    "research_json": {"services_offered": ["Déneigement résidentiel"]},
                    "track": "agence-ia", "google_rating": 4.8,
                    "google_reviews_count": 40, "google_place_id": "p",
                }
                for i in ids
            ]
        if table == "messages":
            demandes = params["contact_id"].removeprefix("in.(").rstrip(")").split(",")
            if self.statut_du_message == "failed":
                return []  # un brouillon retiré ne bloque rien
            return [{"contact_id": c} for c in demandes if c == "c1"]
        return []


@pytest.fixture
def _decembre(monkeypatch: pytest.MonkeyPatch):
    """La fenêtre saisonnière du déneigement doit être ouverte."""
    import datetime as _dt

    from src.lib import metiers as _metiers

    class _Decembre(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date(2026, 12, 15)

    monkeypatch.setattr(_metiers, "date", _Decembre)
    return _Decembre


@pytest.mark.asyncio
async def test_une_entreprise_deja_servie_ne_revient_pas(monkeypatch, _decembre) -> None:
    """🔴 LE CŒUR. Le frère de c1 ne doit pas être retenu, même s'il n'a
    lui-même aucun message et même si c1 n'est plus dans la file."""
    faux = _BaseDeuxFreres()
    monkeypatch.setattr(db_tools, "db", faux)

    lot = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")

    retenus = {x["contact"]["id"] for x in lot}
    assert "c2" not in retenus, (
        "le second contact de PROGAZON a ete retenu alors que l entreprise a "
        "deja recu un courriel froid"
    )
    assert retenus == {"c9"}, f"attendu le seul contact libre, obtenu {retenus}"


@pytest.mark.asyncio
async def test_un_brouillon_retire_libere_lentreprise(monkeypatch, _decembre) -> None:
    """Contrôle négatif : `failed` est la façon prévue de retirer un brouillon.
    L'entreprise doit redevenir servable, sinon retirer un brouillon gèlerait
    toute la boîte au lieu de la libérer."""
    faux = _BaseDeuxFreres(statut_du_message="failed")
    monkeypatch.setattr(db_tools, "db", faux)

    lot = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")

    retenus = {x["contact"]["id"] for x in lot}
    assert retenus == {"c2", "c9"}, (
        f"un brouillon retire ne doit bloquer personne, obtenu {retenus}"
    )


class _BaseQuiTronque(_BaseDeuxFreres):
    """PostgREST coupe TOUTE reponse a 1000 lignes, sans rien signaler.

    Ce faux coupe a 1 pour rendre le defaut visible sur un petit decor : si le
    code appelait `select` au lieu de `select_all`, la lecture des freres
    perdrait des lignes et l entreprise deja demarchee redeviendrait eligible —
    en silence, le defaut meme que ce module referme.
    """

    PLAFOND = 1

    async def select(self, table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        lignes = await super().select(table, params)
        return lignes[: self.PLAFOND]

    async def select_all(self, table: str, *, order: str, params=None,
                         page_size: int = 1000, schema: str | None = None):
        # La vraie `select_all` pagine et ne perd rien : on rend tout.
        return await _BaseDeuxFreres.select(self, table, params or {})


@pytest.mark.asyncio
async def test_la_lecture_des_freres_ne_se_fait_pas_couper(monkeypatch, _decembre) -> None:
    """🔴 Le plafond de 1000 lignes de PostgREST, applique a la lecture des freres.

    Mesure du 2026-09-13 : 1,27 contact par entreprise, donc la coupe mord vers
    790 entreprises en file — environ 2,5 fois celle d aujourd hui. Elle ne
    previent pas : la liste revient simplement plus courte.

    Deux consequences, toutes deux muettes : une entreprise deja demarchee
    redevient eligible, et un contact de la page tombe hors des 1000 n a plus
    son propre message interrogé, donc il est RE-REDIGE — une regression par
    rapport a la version qui interrogeait directement les ids de la page.
    """
    faux = _BaseQuiTronque()
    monkeypatch.setattr(db_tools, "db", faux)

    lot = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")

    retenus = {x["contact"]["id"] for x in lot}
    assert "c2" not in retenus, (
        "la lecture des freres s est fait couper : l entreprise deja servie "
        "est redevenue eligible"
    )
