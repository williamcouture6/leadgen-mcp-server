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

from src.lib.lead_scoring import MARQUEUR_TETE_DE_FILE
from src.tools import db as db_tools


class _FausseBase:
    """Une file où les N premiers contacts ont déjà un brouillon.

    C'est la forme exacte du bouchon réel : la file est triée du plus ancien au
    plus récent, et ce sont les plus anciens qui ont été rédigés en premier.
    """

    def __init__(
        self, total: int, bouches: int, marques: set[str] | None = None
    ) -> None:
        self.total, self.bouches = total, bouches
        # Entreprises portant le marqueur « tête de file » (un avis dit qu'on
        # n'arrive pas à les joindre). Ajouté le 2026-09-09 : sans ça, toutes
        # les companies étaient à égalité et la suite n'exerçait que la branche
        # dégénérée du tri par potentiel.
        self.marques = marques or set()
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
        if table == "contacts" and "company_id" in params:
            # La lecture des FRERES : tous les contacts des entreprises
            # candidates, y compris ceux qui ont quitte la file au push
            # (status contacted). Elle ne compte pas comme une page de file.
            ids = params["company_id"].removeprefix("in.(").rstrip(")").split(",")
            return [
                {"id": c["id"], "company_id": c["company_id"]}
                for c in self.contacts
                if c["company_id"] in ids
            ]
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
                    "lead_potential_score": 50,
                    "lead_potential_reason": (
                        f"{MARQUEUR_TETE_DE_FILE} jamais eu de retour d'appel"
                        if i in self.marques else "ferme le soir"
                    ),
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
async def test_le_cout_suit_la_taille_de_la_file(
    monkeypatch: pytest.MonkeyPatch, _decembre
) -> None:
    """Contrôle du coût. ⚠️ CETTE GARDE A CHANGÉ DE SENS le 2026-09-09.

    Elle exigeait « une page suffit quand la file est facile » : la lecture
    s'arrêtait dès que le lot était plein. Décision William du 2026-09-09 : on
    lit désormais la file ENTIÈRE avant de retenir, parce que le tri par
    potentiel ne pouvait sinon ordonner que la première page — une entreprise
    marquée « tête de file » assise plus loin ne remontait jamais (voir le test
    suivant).

    Ce qui reste garanti, et c'est ce qu'on mesure ici : le coût suit la taille
    de la FILE, jamais `MAX_PAGES_SELECTION`. Sur la vraie file — 345 contacts
    au 2026-09-09 — ça fait deux lectures au lieu d'une.
    """
    faux = _FausseBase(total=400, bouches=0)
    monkeypatch.setattr(db_tools, "db", faux)

    await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")

    taille_page = max(10 * db_tools.FACTEUR_SURRECOLTE, 200)
    # 2 pages pleines, plus la lecture vide qui dit que la file est épuisée.
    attendu = 400 // taille_page + 1
    assert faux.pages_lues == attendu, f"{faux.pages_lues} pages lues"
    assert faux.pages_lues < db_tools.MAX_PAGES_SELECTION


@pytest.mark.asyncio
async def test_la_page_ne_depasse_jamais_le_plafond_postgrest(
    monkeypatch: pytest.MonkeyPatch, _decembre
) -> None:
    """PostgREST coupe à 1000 lignes sans le dire.

    `limit=84` demandait 84 × 12 = 1008 lignes : la page en rendait 1000, et
    « page incomplète » faisait conclure « file épuisée » après une seule
    lecture. Comme la priorisation dépend d'une lecture complète, cette
    troncature muette aurait décidé de l'ordre d'envoi. Conseil du 2026-09-09.
    """
    vus: list[int] = []

    class _Espion(_FausseBase):
        async def select(self, table, params):
            # `limit` absent = la requête des FRÈRES (exclusion par entreprise,
            # AC1c), pas la lecture paginée de la file. On ne mesure que celle-ci.
            if table == "contacts" and "limit" in params:
                vus.append(int(params["limit"]))
            return await super().select(table, params)

    monkeypatch.setattr(db_tools, "db", _Espion(total=50, bouches=0))
    await db_tools.list_contacts_to_personalize(limit=84, track="agence-ia")

    assert vus, "aucune lecture de contacts"
    assert max(vus) <= 1000, f"page de {max(vus)} lignes, PostgREST coupe à 1000"


@pytest.mark.asyncio
async def test_une_tete_de_file_au_dela_de_la_premiere_page_entre_dans_le_lot(
    monkeypatch: pytest.MonkeyPatch, _decembre
) -> None:
    """Le trou relevé par le conseil du 2026-09-09, et la raison de tout lire.

    `co350` est en deuxième page. Tant que la lecture s'arrêtait à la première
    page pleine, cette entreprise — dont un avis Google dit qu'on n'arrive pas
    à la joindre — n'était jamais vue, et « tête de file » ne voulait dire que
    « tête des 200 premiers lus ». Elle doit maintenant sortir première.
    """
    faux = _FausseBase(total=400, bouches=0, marques={"co350"})
    monkeypatch.setattr(db_tools, "db", faux)

    lot = await db_tools.list_contacts_to_personalize(limit=10, track="agence-ia")

    assert lot, "le lot ne doit pas être vide"
    assert lot[0]["company"]["id"] == "co350", (
        f"tête de file attendue en premier, obtenu {lot[0]['company']['id']}"
    )


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
