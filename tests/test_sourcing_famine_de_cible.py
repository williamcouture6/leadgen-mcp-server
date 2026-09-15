"""Régression — la sélection de cible ne doit PAS affamer la queue du catalogue.

Mesuré en prod le 2026-09-14 : 41 des 70 cibles `agence-ia` n'avaient JAMAIS
été scrapées, dont six villes entières (Longueuil, Sherbrooke, Saguenay, Lévis,
Trois-Rivières, Terrebonne), pendant que WF-1 re-mâchait Montréal tous les
jours. Rendement mesuré sur l'historique complet : 90,5 % de fiches neuves au
premier passage sur une cible, 9,4 % au re-passage.

La cause : `next_sourcing_target` rendait la PREMIÈRE cible du catalogue hors
cooldown, dans l'ordre de priorité. Avec un run par jour et `COOLDOWN_DAYS=30`,
le curseur avance d'un cran par jour mais retombe à zéro dès que la tête
ressort de la fenêtre de 30 jours : il ne dépasse jamais l'indice 30. Donc 31
entrées tournent en rond et les 39 autres sont mortes, quel que soit le temps
qu'on attend.

L'invariant qui doit tenir : une cible JAMAIS scrapée passe avant une cible
dont le cooldown vient d'expirer, et entre deux cibles déjà scrapées, la plus
ancienne passe la première.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import supabase_client as real_db
import src.tools.db as dbt


def _il_y_a(jours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat()


def _historique(monkeypatch: pytest.MonkeyPatch, runs: list[dict]) -> dict:
    """Faux client qui HONORE le filtre `created_at=gte.<iso>`.

    Un faux qui rend tout quel que soit le filtre ferait passer la sélection
    pour meilleure qu'elle n'est : la cible dont le cooldown vient d'expirer
    resterait dans l'ensemble « vue récemment » et serait sautée pour la
    mauvaise raison. Le filtre est donc rejoué ici.
    """

    capture: dict = {}

    def _applique(params: dict | None) -> list[dict]:
        p = params or {}
        lignes = list(runs)
        borne = p.get("created_at")
        if isinstance(borne, str) and borne.startswith("gte."):
            seuil = borne[len("gte."):]
            lignes = [r for r in lignes if r["created_at"] >= seuil]
        # PostgREST PROJETTE les colonnes de `select` : demander
        # "city,sector" rendrait des lignes SANS `created_at`. Un faux qui
        # rend le dict complet quoi qu'on demande laisserait passer
        # exactement la panne d'origine sous une autre cause — plus de date
        # lisible, donc plus de cooldown, donc Montréal re-mâché tous les
        # jours, avec quatre tests verts.
        colonnes = p.get("select")
        if isinstance(colonnes, str) and colonnes:
            garde = {c.strip() for c in colonnes.split(",") if c.strip()}
            lignes = [{k: v for k, v in r.items() if k in garde} for r in lignes]
        return lignes

    # ⚠️ La signature colle EXACTEMENT à celle de supabase_client.select_all :
    # `order` nommé et OBLIGATOIRE. Un faux permissif (`**kw`) avalerait un
    # appel qui lève `TypeError` en prod — c'est arrivé le 2026-09-14, le
    # correctif passait `order` dans `params` et les tests étaient verts.
    async def fake_select_all(table, *, order, params=None, page_size=1000,
                              schema=None):
        assert table == "sourcing_runs"
        # `id` et rien d'autre : la pagination par offset saute ou double des
        # lignes si la colonne d'ordre n'est pas unique. `created_at.desc`
        # passerait un `assert order` nu tout en rouvrant ce défaut-là.
        assert order == "id", f"ordre non unique : {order!r}"
        capture["params"] = params or {}
        return _applique(params)

    async def fake_select(table, params=None, schema=None):
        # L'historique DOIT passer par select_all. Un `select` nu se ferait
        # couper à 1000 lignes côté serveur, sans erreur : les runs les plus
        # anciens disparaîtraient, leurs cibles repasseraient pour vierges, et
        # la famine reviendrait sous une forme plus discrète.
        raise AssertionError(
            "historique lu par `select` — plafond 1000, utiliser `select_all`"
        )

    monkeypatch.setattr(real_db, "select_all", fake_select_all)
    monkeypatch.setattr(real_db, "select", fake_select)
    return capture


@pytest.mark.asyncio
async def test_jamais_scrape_passe_avant_cooldown_expire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le cas exact de la prod : Montréal fait il y a 31 j, Terrebonne vierge."""
    _historique(monkeypatch, [
        {"city": "Montréal", "sector": s, "created_at": _il_y_a(31)}
        for s in dbt.REACTI_SECTOR_CATALOG["commerce_local"]
    ])

    t = await dbt.next_sourcing_target(track="agence-ia")
    assert t is not None
    # Montréal est en tête du catalogue et son cooldown vient d'expirer, mais
    # neuf autres villes n'ont jamais été vues une seule fois.
    assert t.city != "Montréal", (
        "la tête du catalogue reprend la main alors que 63 cibles sont vierges"
    )
    assert t.reason == "never_scraped"


@pytest.mark.asyncio
async def test_entre_deux_deja_scrapees_la_plus_ancienne_dabord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalogue entièrement couvert : on repasse par le plus vieux, pas par le
    premier de la liste."""
    cibles = dbt._all_targets("agence-ia")
    runs = [
        {"city": c, "sector": s, "created_at": _il_y_a(40)}
        for c, s, _icp in cibles
    ]
    # La dernière entrée du catalogue est la plus ancienne de toutes.
    runs[-1] = {**runs[-1], "created_at": _il_y_a(200)}
    _historique(monkeypatch, runs)

    t = await dbt.next_sourcing_target(track="agence-ia")
    assert t is not None
    attendu = cibles[-1]
    assert (t.city, t.sector) == (attendu[0], attendu[1])
    assert t.reason == "cooldown_expired"


@pytest.mark.asyncio
async def test_cooldown_tient_toujours(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le plancher de 30 jours reste une garde : tout frais → plus de cible."""
    runs = [
        {"city": c, "sector": s, "created_at": _il_y_a(2)}
        for c, s, _icp in dbt._all_targets("agence-ia")
    ]
    _historique(monkeypatch, runs)
    assert await dbt.next_sourcing_target(track="agence-ia") is None


@pytest.mark.asyncio
async def test_catalogue_vierge_rend_la_premiere_cible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans aucun historique, l'ordre de priorité du catalogue fait foi."""
    _historique(monkeypatch, [])
    t = await dbt.next_sourcing_target(track="agence-ia")
    assert t is not None
    premiere = dbt._all_targets("agence-ia")[0]
    assert (t.city, t.sector) == (premiere[0], premiere[1])
    assert t.reason == "never_scraped"


@pytest.mark.asyncio
async def test_plusieurs_runs_sur_la_meme_cible_le_plus_recent_fait_foi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une cible vue il y a 200 jours ET hier est en cooldown, pas affamée.

    C'est la mutation la plus facile à introduire et la plus invisible : si la
    réduction gardait le PREMIER run rencontré au lieu du plus récent
    (`if cle not in dernier` sans le `vu > dernier[cle]`), cette cible serait
    classée « la plus ancienne de toutes » et re-scrapée en plein cooldown,
    tous les jours, pour rien.
    """
    cibles = dbt._all_targets("agence-ia")
    piegee = cibles[0]
    runs = [
        {"city": c, "sector": s, "created_at": _il_y_a(40)}
        for c, s, _icp in cibles[1:]
    ]
    # Deux runs sur la cible piégée : un très vieux, un tout frais. L'ordre de
    # lecture est volontairement « vieux d'abord », l'ordre PostgREST étant
    # `id` (un uuid), donc arbitraire — la réduction ne doit dépendre d'aucun
    # ordre d'arrivée.
    runs.insert(0, {"city": piegee[0], "sector": piegee[1], "created_at": _il_y_a(200)})
    runs.append({"city": piegee[0], "sector": piegee[1], "created_at": _il_y_a(1)})
    _historique(monkeypatch, runs)

    t = await dbt.next_sourcing_target(track="agence-ia")
    assert t is not None
    assert (t.city, t.sector) != (piegee[0], piegee[1]), (
        "cible en cooldown servie parce que son plus VIEUX run a fait foi"
    )


@pytest.mark.asyncio
async def test_a_egalite_lordre_du_catalogue_tranche(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deux cibles vues au même instant : la priorité du catalogue décide.

    Pin le `<` STRICT de la comparaison de rang. Avec `<=`, c'est la DERNIÈRE
    cible du catalogue qui gagnerait et la hiérarchie des villes écrite par
    William ne vaudrait plus rien. Les autres tests ne l'atteignent pas : leurs
    horodatages sont tous distincts, `_il_y_a()` étant réévalué à chaque tour.
    """
    cibles = dbt._all_targets("agence-ia")
    meme_instant = _il_y_a(40)
    _historique(monkeypatch, [
        {"city": c, "sector": s, "created_at": meme_instant}
        for c, s, _icp in cibles
    ])

    t = await dbt.next_sourcing_target(track="agence-ia")
    assert t is not None
    assert (t.city, t.sector) == (cibles[0][0], cibles[0][1])


@pytest.mark.asyncio
async def test_une_date_illisible_ne_fait_pas_tomber_le_cron(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrat de `_en_datetime` : jamais d'exception dans le cron de 10 h.

    Une ligne d'historique illisible est ignorée — la cible repasse donc pour
    « jamais scrapée » et coûte au pire un run en doublon. Ce qu'on refuse,
    c'est qu'elle fasse tomber le sourcing du jour.
    """
    cibles = dbt._all_targets("agence-ia")
    abimee = cibles[0]
    runs = [
        {"city": c, "sector": s, "created_at": _il_y_a(2)}   # tout en cooldown
        for c, s, _icp in cibles[1:]
    ]
    runs += [
        {"city": abimee[0], "sector": abimee[1], "created_at": None},
        {"city": abimee[0], "sector": abimee[1], "created_at": "pas-une-date"},
        {"city": abimee[0], "sector": abimee[1], "created_at": ""},
    ]
    _historique(monkeypatch, runs)

    t = await dbt.next_sourcing_target(track="agence-ia")
    assert t is not None
    assert (t.city, t.sector) == (abimee[0], abimee[1])
    assert t.reason == "never_scraped"


def test_en_datetime_rend_de_l_aware_ou_rien() -> None:
    """Les quatre branches du helper, en direct."""
    from datetime import datetime as _dt

    aware = dbt._en_datetime("2026-09-14T10:00:00+00:00")
    assert aware is not None and aware.tzinfo is not None
    # Suffixe Z : `fromisoformat` ne l'accepte pas avant Python 3.11.
    zulu = dbt._en_datetime("2026-09-14T10:00:00Z")
    assert zulu == aware
    # Naïf → traité comme UTC, sinon la comparaison au cutoff aware explose.
    naif = dbt._en_datetime(_dt(2026, 9, 14, 10, 0, 0))
    assert naif is not None and naif.tzinfo is not None
    for pourri in (None, "", "pas-une-date", 42, {}):
        assert dbt._en_datetime(pourri) is None


@pytest.mark.asyncio
async def test_lhistorique_est_lu_en_entier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aucun filtre de date dans la requête — c'est le cœur du correctif.

    Classer par ancienneté exige de savoir quand chaque cible a été vue la
    dernière fois, fût-ce il y a six mois. Une requête bornée à 30 jours rend
    « vue il y a six mois » et « jamais vue » indiscernables : c'est
    précisément l'information que l'ancienne version jetait.
    """
    capture = _historique(monkeypatch, [])
    await dbt.next_sourcing_target(track="agence-ia")
    assert "created_at" not in capture["params"], (
        "l'historique est de nouveau borné dans le temps"
    )
    assert "created_at" in capture["params"].get("select", "")
