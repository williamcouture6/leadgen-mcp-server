"""WF-1 devient PIOCHEUR : il pige dans l'inventaire au lieu d'interroger Google.

🔴 CE QUE CES TESTS PROTÈGENT, dans l'ordre :

1. « DÉFINITIF » ET « RÉESSAYABLE » NE SE CONFONDENT PAS. Un `place_id` périmé
   est mort pour toujours (`ecartee`, avec son motif) ; un quota dépassé ne
   l'est pas (`echec`, on retentera). Les confondre coûte cher dans les deux
   sens : écarter sur un quota enterre des centaines de bonnes entreprises pour
   toujours, et retenter un identifiant mort le fait revenir chaque matin manger
   le budget du lot.

2. UNE FICHE QUI TOMBE N'EMPORTE PAS LE LOT. `_run_wf1` porte le défaut inverse
   — un `try/except` unique autour de toute la boucle. Sur un lot de 20, ça fait
   perdre 19 hydratations pour une seule mauvaise.

3. `derniere_tentative` S'ÉCRIT MÊME EN CAS DE SUCCÈS. C'est la colonne d'ORDRE.
   Ne la poser qu'en cas d'échec laisserait les fiches traitées éternellement
   « jamais tentées » pour le tri, et le tri deviendrait du bruit.

4. ON NE PAIE QUE CE QU'ON PEUT DÉMARCHER. Hors saison, la file est vide — pas
   par accident, par construction. Payer 0,02 $ de détails plus 0,034 $ de
   recherche six mois d'avance, c'est payer pour une fiche périmée le jour de
   l'envoi.

5. AUCUN REPLI SUR UNE VILLE DE CATALOGUE. `_run_wf1` écrivait
   `city = p.city or city` ; ici la ville vient de Google ou reste vide. Une
   région de balayage est un RECTANGLE, pas une municipalité — l'utiliser en
   repli inventerait une ville, et `companies.dedup_key` est bâtie dessus.
"""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test")


# --------------------------------------------------------------- sélection

@pytest.mark.anyio
async def test_hors_saison_la_file_est_vide_sans_appeler_la_base(monkeypatch) -> None:
    """Point 4. Et sans même interroger PostgREST : il n'y a rien à demander."""
    from src.tools import db as dbt

    appels: list[str] = []

    async def _interdit(*a, **k):
        appels.append("select")
        return []

    monkeypatch.setattr(dbt.db, "select", _interdit)
    monkeypatch.setattr(dbt, "secteurs_a_preparer", lambda **_k: [])

    assert await dbt.list_inventaire_a_piocher(20) == []
    assert appels == [], "aucune lecture ne doit partir quand rien n'est en saison"


@pytest.mark.anyio
async def test_la_selection_porte_le_litteral_et_le_bon_ordre(monkeypatch) -> None:
    """Point 3 et l'index. Deux choses se jouent dans ces paramètres :

    · `etat=eq.a_traiter` en LITTÉRAL — c'est ce qu'exige le prédicat de l'index
      GIN partiel de la 0070. Passé en paramètre lié, le plan générique perd la
      preuve du prédicat et l'index est ignoré, en silence.
    · `derniere_tentative.asc.nullsfirst` — jamais tentée d'abord. Sans ça, une
      fiche qui échoue en boucle reste en tête de file pour toujours.
    """
    from src.tools import db as dbt

    vus: dict[str, str] = {}

    async def _faux_select(table, *, params=None, **_k):
        vus.update({"table": table, **(params or {})})
        return []

    monkeypatch.setattr(dbt.db, "select", _faux_select)
    monkeypatch.setattr(
        dbt, "secteurs_a_preparer", lambda **_k: ["entrepreneur en déneigement"]
    )

    await dbt.list_inventaire_a_piocher(7, track="agence-ia")

    assert vus["table"] == "sourcing_inventaire"
    assert vus["etat"] == "eq.a_traiter"
    assert vus["order"] == "derniere_tentative.asc.nullsfirst,created_at.asc"
    assert vus["limit"] == "7"
    # Point 5 du fichier de l'alerte : les guillemets protègent les espaces.
    assert vus["trouve_par"] == 'ov.{"entrepreneur en déneigement"}'


# ------------------------------------------------------- issues d'une fiche

def _place(nom="Déneigement Test", primary_type="general_contractor"):
    from src.tools.maps import PlaceResult

    return PlaceResult(
        google_place_id="ChIJtest", name=nom, formatted_address="1 rue X",
        city="Vaudreuil-Dorion", postal_code="J7V0A1",
        latitude=45.4, longitude=-74.0, website="https://x.ca", domain="x.ca",
        google_types=["service"], primary_type=primary_type,
    )


class _Marques(list):
    """Capture les appels à `marquer_inventaire`."""

    def async_stub(self):
        async def _f(pid, **kw):
            self.append({"pid": pid, **kw})
        return _f


def _brancher(monkeypatch, *, lot, get_place, marques, insert=None):
    from src import http_api
    from src.tools import db as dbt, maps as maps_tools

    async def _lot(*_a, **_k):
        return lot

    async def _insert(payload):
        from src.tools.db import InsertCompanyOut

        if insert:
            return insert(payload)
        return InsertCompanyOut(status="inserted", company_id="cid-1")

    monkeypatch.setattr(dbt, "list_inventaire_a_piocher", _lot)
    monkeypatch.setattr(dbt, "secteurs_a_preparer", lambda **_k: ["s"])
    monkeypatch.setattr(dbt, "marquer_inventaire", marques.async_stub())
    monkeypatch.setattr(dbt, "insert_company", _insert)
    monkeypatch.setattr(maps_tools, "get_place", get_place)
    return http_api


@pytest.mark.anyio
async def test_un_place_id_perime_est_ecarte_definitivement(monkeypatch) -> None:
    """Point 1. `ecartee` + motif, PAS `echec` : le faire revenir chaque matin
    lui ferait manger le budget du lot pour toujours."""
    from src.tools.maps import PlaceIntrouvable

    async def _perime(_pid):
        raise PlaceIntrouvable("404 sur ChIJmort")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch, lot=[{"google_place_id": "ChIJmort", "tentatives": 0}],
        get_place=_perime, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))

    assert out.introuvables == 1 and out.echecs == 0
    assert marques[0]["etat"] == "ecartee"
    assert "place_id_perime" in marques[0]["motif"]


@pytest.mark.anyio
async def test_une_panne_transitoire_est_un_echec_pas_un_ecart(monkeypatch) -> None:
    """Point 1, l'autre sens. Écarter sur un quota dépassé enterrerait des
    centaines de bonnes entreprises pour toujours — `ecartee` est documentée
    comme définitive."""
    async def _quota(_pid):
        raise RuntimeError("429 quota")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch, lot=[{"google_place_id": "ChIJx", "tentatives": 2}],
        get_place=_quota, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))

    assert out.echecs == 1 and out.introuvables == 0
    assert marques[0]["etat"] == "echec"
    assert marques[0].get("motif") is None
    assert marques[0]["tentatives"] == 2, "le compteur doit repartir de l'existant"


@pytest.mark.anyio
async def test_le_junk_est_ecarte_avec_son_motif(monkeypatch) -> None:
    """Le motif est la clé de la réouverture ciblée le jour où
    `lib/sourcing_filters` change d'avis — `etat='ecartee'` est un cache que
    rien n'invalide."""
    async def _spa(_pid):
        return _place(nom="Strøm Spa Nordique", primary_type="spa")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch, lot=[{"google_place_id": "ChIJspa", "tentatives": 0}],
        get_place=_spa, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))

    assert out.junk == 1 and out.inserees == 0
    assert marques[0]["etat"] == "ecartee"
    assert marques[0]["motif"], "un écart sans motif ne se rouvre jamais"


@pytest.mark.anyio
async def test_une_fiche_qui_tombe_n_emporte_pas_le_lot(monkeypatch) -> None:
    """Point 2. La deuxième fiche doit être traitée malgré l'échec de la première."""
    appels: list[str] = []

    async def _une_sur_deux(pid):
        appels.append(pid)
        if pid == "ChIJko":
            raise RuntimeError("réseau")
        return _place()

    marques = _Marques()
    http_api = _brancher(
        monkeypatch,
        lot=[{"google_place_id": "ChIJko", "tentatives": 0},
             {"google_place_id": "ChIJok", "tentatives": 0}],
        get_place=_une_sur_deux, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=2))

    assert appels == ["ChIJko", "ChIJok"]
    assert out.echecs == 1 and out.inserees == 1


@pytest.mark.anyio
async def test_le_succes_ecrit_aussi_la_derniere_tentative(monkeypatch) -> None:
    """Point 3 — vérifié sur le vrai `marquer_inventaire`, pas sur un faux :
    c'est LUI qui pose la colonne d'ordre."""
    from src.tools import db as dbt

    patchs: list[dict] = []

    async def _faux_update(table, patch, *, filters, **_k):
        patchs.append({"table": table, **patch, **filters})
        return []

    monkeypatch.setattr(dbt.db, "update", _faux_update)
    await dbt.marquer_inventaire("ChIJok", etat="traitee", company_id="cid-9")

    assert patchs[0]["etat"] == "traitee"
    assert patchs[0]["derniere_tentative"], "sinon le tri la croit jamais tentée"
    assert patchs[0]["tentatives"] == 1
    assert patchs[0]["company_id"] == "cid-9"
    # Le trigger de fusion protégerait ces deux-là, mais s'en remettre à lui
    # pour une faute qu'on peut ne pas commettre use la garde pour rien.
    assert "trouve_par" not in patchs[0] and "regions" not in patchs[0]


@pytest.mark.anyio
async def test_la_ville_vient_de_google_sans_repli_invente(monkeypatch) -> None:
    """Point 5. Une région de balayage est un RECTANGLE, pas une municipalité :
    « Vaudreuil-Dorion » doit survivre, et surtout aucune région ne doit se
    substituer à une ville absente — `companies.dedup_key` est bâtie dessus."""
    captures: list = []

    async def _ok(_pid):
        return _place()

    def _capture(payload):
        from src.tools.db import InsertCompanyOut

        captures.append(payload)
        return InsertCompanyOut(status="inserted", company_id="cid-1")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch,
        lot=[{"google_place_id": "ChIJx", "tentatives": 0,
              "trouve_par": ["entrepreneur en déneigement"], "regions": ["Montréal"]}],
        get_place=_ok, marques=marques, insert=_capture,
    )
    await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))

    assert captures[0].city == "Vaudreuil-Dorion"
    assert captures[0].industry == "entrepreneur en déneigement"
    assert captures[0].track == "agence-ia"


@pytest.mark.anyio
async def test_le_dry_run_ne_paie_aucune_hydratation(monkeypatch) -> None:
    """Le `dry_run` de `_run_wf1` appelle Google quand même et ne saute que
    l'insert — c'est le mode le plus cher de « ne rien faire ». Ici, non."""
    appels: list[str] = []

    async def _compte(pid):
        appels.append(pid)
        return _place()

    marques = _Marques()
    http_api = _brancher(
        monkeypatch, lot=[{"google_place_id": "ChIJx", "tentatives": 0}],
        get_place=_compte, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1, dry_run=True))

    assert out.pioches == 1
    assert appels == [], "un essai à blanc ne doit RIEN facturer"
    assert marques == [], "ni rien écrire"


def test_le_masque_de_details_derive_de_celui_de_la_recherche() -> None:
    """Deux listes de champs finiraient par diverger, et la divergence serait
    invisible : une fiche hydratée sans `websiteUri` ne casse rien, elle devient
    juste éternellement inexploitable."""
    from src.tools.maps import FIELD_MASK, PLACE_DETAILS_FIELD_MASK

    attendus = {c.removeprefix("places.") for c in FIELD_MASK.split(",")}
    attendus.discard("nextPageToken")
    assert set(PLACE_DETAILS_FIELD_MASK.split(",")) == attendus
    assert "places." not in PLACE_DETAILS_FIELD_MASK
