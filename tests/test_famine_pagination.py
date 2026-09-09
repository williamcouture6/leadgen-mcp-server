"""La sélection lit autant de pages qu'il faut — la famine, refermée.

🔴 UN DÉFAUT OUVERT DEPUIS MAI, réveillé le 2026-09-09 en divisant le lot par
deux, et mesuré sur la vraie base :

    91 contacts éligibles, mais le premier en 87ᵉ position — les 86 premiers
    avaient déjà un brouillon.

    limit=20 → la fenêtre lit 240 → 45 éligibles dedans → rend 20   ✅
    limit=10 → la fenêtre lit 120 →  2 éligibles dedans → rend  2   ❌

La fenêtre était proportionnelle au LOT (`limit * FACTEUR_SURRECOLTE`), alors
que ce qu'il faut franchir est proportionnel à la FILE : le bouchon des contacts
déjà rédigés, qui grossit à chaque envoi.

Monter le facteur n'aurait fait que déplacer le seuil. Lire par pages jusqu'à
avoir son compte supprime la question — et ne coûte rien quand la file est
courte, une page suffisant alors.

⚠️ La famine ne se voyait PAS : le lot revenait court, sans erreur. Le cron
serait tombé à 2 brouillons par jour, puis à 0, en silence.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.tools import db as db_tools


class _FausseBase:
    """Une file où les N premiers contacts ont déjà un brouillon.

    C'est la forme exacte du bouchon réel : la file est triée du plus ancien au
    plus récent, et ce sont les plus anciens qui ont été rédigés en premier.
    """

    def __init__(self, total: int, bouches: int) -> None:
        self.total, self.bouches = total, bouches
        self.pages_lues = 0
        self.contacts = [
            {
                "id": f"c{i}", "company_id": f"co{i}", "email": f"a{i}@x.ca",
                "first_name": "A", "last_name": "B", "email_verified": True,
                "title": None, "status": "new",
                "email_verification_source": "scrape", "raw_payload": {},
                "track": "agence-ia", "owner_confidence": None,
                "potential_owner": None,
            }
            for i in range(total)
        ]

    async def select(self, table: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if table == "contacts":
            self.pages_lues += 1
            deb = int(params.get("offset", 0))
            fin = deb + int(params["limit"])
            return self.contacts[deb:fin]
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
            bouches = {f"c{i}" for i in range(self.bouches)}
            return [{"contact_id": c} for c in demandes if c in bouches]
        return []


@pytest.fixture
def _decembre(monkeypatch: pytest.MonkeyPatch):
    """Décembre : la fenêtre du déneigement est ouverte, donc la saison ne
    filtre rien. On isole ainsi la pagination du filtre saisonnier."""
    from datetime import date

    monkeypatch.setattr(
        db_tools, "fenetre_saisonniere_ouverte",
        lambda company, *, track, aujourdhui=None: True,
    )
    return date(2026, 12, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("bouches", [0, 86, 200, 340])
async def test_le_lot_est_plein_malgre_le_bouchon(
    monkeypatch: pytest.MonkeyPatch, _decembre, bouches: int
) -> None:
    """🔴 LE TEST CENTRAL. 86 est le bouchon réel du 2026-09-09 ; 340 sur 400
    est le cas extrême où presque tout est rédigé.

    Avant le correctif, `bouches=86` avec `limit=10` rendait 2.
    """
    faux = _FausseBase(total=400, bouches=bouches)
    monkeypatch.setattr(db_tools, "db", faux)

    r = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")
    assert len(r) == 10, (
        f"bouchon de {bouches} : lot de {len(r)} au lieu de 10 — la famine est "
        "revenue"
    )
    # Et jamais un contact déjà rédigé.
    assert all(int(x["contact"]["id"][1:]) >= bouches for x in r)


@pytest.mark.asyncio
async def test_une_file_courte_ne_coute_qu_une_page(
    monkeypatch: pytest.MonkeyPatch, _decembre
) -> None:
    """Contrôle négatif du coût : lire par pages ne doit pas se transformer en
    lecture systématique de toute la base. Sans bouchon, une page suffit."""
    faux = _FausseBase(total=400, bouches=0)
    monkeypatch.setattr(db_tools, "db", faux)

    await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")
    assert faux.pages_lues == 1, f"{faux.pages_lues} pages lues pour un lot facile"


@pytest.mark.asyncio
async def test_une_file_entierement_bouchee_rend_un_lot_vide(
    monkeypatch: pytest.MonkeyPatch, _decembre
) -> None:
    """Le cas où il n'y a VRAIMENT plus rien : le lot revient vide, sans boucler
    à l'infini. C'est là que l'alerte de famine doit parler — et elle le fait,
    puisqu'elle se déclenche sur `drafts == 0`."""
    faux = _FausseBase(total=300, bouches=300)
    monkeypatch.setattr(db_tools, "db", faux)

    r = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")
    assert r == []
    assert faux.pages_lues <= db_tools.MAX_PAGES_SELECTION


@pytest.mark.asyncio
async def test_la_lecture_est_bornee(
    monkeypatch: pytest.MonkeyPatch, _decembre
) -> None:
    """🔴 Sans borne, une file énorme et bouchée ferait lire la base entière à
    chaque cron. `MAX_PAGES_SELECTION` est un plafond de COÛT, pas de logique."""
    faux = _FausseBase(total=100_000, bouches=100_000)
    monkeypatch.setattr(db_tools, "db", faux)

    await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")
    assert faux.pages_lues == db_tools.MAX_PAGES_SELECTION


@pytest.mark.asyncio
async def test_pas_de_doublon_d_entreprise_entre_deux_pages(
    monkeypatch: pytest.MonkeyPatch, _decembre
) -> None:
    """Le piège de la pagination : la page 2 ne doit pas resservir une
    entreprise déjà retenue en page 1. C'est pour ça que le filtrage tourne sur
    la liste ACCUMULÉE, jamais sur la dernière page seule."""
    faux = _FausseBase(total=400, bouches=250)
    monkeypatch.setattr(db_tools, "db", faux)

    r = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")
    ids = [x["company"]["id"] for x in r]
    assert len(ids) == len(set(ids)) == 10
