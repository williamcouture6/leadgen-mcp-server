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
    # ⚠️ La requête SUR-LIT : on lit `limit × FACTEUR_EQUITE_REGIONALE` lignes
    # pour pouvoir ensuite alterner entre les régions. Sans ça, l'ordre par
    # `created_at` sert Montréal pendant vingt jours avant de toucher
    # Trois-Rivières. Le lot rendu, lui, fait bien `limit`.
    from src.tools.db import FACTEUR_EQUITE_REGIONALE

    assert vus["limit"] == str(7 * FACTEUR_EQUITE_REGIONALE)
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
async def test_une_panne_transitoire_REMET_LA_FICHE_EN_FILE(monkeypatch) -> None:
    """🔴 CE TEST ENCODAIT LE BUG JUSQU'AU 2026-09-16. Il exigeait `echec` sur un
    429 passager — et `echec` est un CUL-DE-SAC : la sélection ne lit que
    `a_traiter`, et aucun code du dépôt n'écrivait jamais `a_traiter`. Une fiche
    mise en `echec` par un quota dépassé sortait de la file POUR TOUJOURS, et un
    rebalayage ne la ramenait pas.

    Un quota épuisé à 10 h 03 enterrait ainsi les 18 fiches suivantes du lot.

    La bonne issue est la remise en file : `derniere_tentative` est posée, donc
    l'ordre `nulls first` la renvoie en QUEUE — elle ne bloque rien et elle
    revient. C'est ce que le commentaire d'origine décrivait déjà, sans que le
    code le fasse.
    """
    async def _quota(_pid):
        raise RuntimeError("429 quota")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch, lot=[{"google_place_id": "ChIJx", "tentatives": 2}],
        get_place=_quota, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))

    assert out.echecs == 1 and out.introuvables == 0
    assert marques[0]["etat"] == "a_traiter", "elle DOIT revenir dans la file"
    assert marques[0].get("motif") is None
    assert marques[0]["tentatives"] == 2, "le compteur doit repartir de l'existant"


@pytest.mark.anyio
async def test_au_plafond_de_tentatives_la_fiche_sort_de_la_file(monkeypatch) -> None:
    """La contrepartie, et elle est indispensable : remettre en file SANS
    plafond rouvrirait la famine en grand — une fiche qui échoue pour une raison
    structurelle reviendrait en tête chaque matin. C'est le défaut de
    `next_sourcing_target` corrigé le 2026-09-14, et la sélection le ferme par
    `tentatives < MAX_TENTATIVES_INVENTAIRE`."""
    from src.http_api import MAX_TENTATIVES_HYDRATATION
    from src.tools.db import MAX_TENTATIVES_INVENTAIRE

    assert MAX_TENTATIVES_HYDRATATION == MAX_TENTATIVES_INVENTAIRE, (
        "les deux plafonds doivent être le MÊME nombre : l'un décide d'écrire "
        "`echec`, l'autre de cesser de servir. S'ils divergent, une fiche "
        "tourne en boucle ou sort trop tôt."
    )

    async def _toujours_ko(_pid):
        raise RuntimeError("panne structurelle")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch,
        lot=[{"google_place_id": "ChIJmort",
              "tentatives": MAX_TENTATIVES_HYDRATATION - 1}],
        get_place=_toujours_ko, marques=marques,
    )
    await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))

    assert marques[0]["etat"] == "echec", "au plafond, elle cesse d'être servie"


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


# ------------------------------------------- ce que la relecture a révélé

@pytest.mark.anyio
async def test_un_lot_entierement_rate_pose_error_text(monkeypatch) -> None:
    """🔴 SANS ÇA, UNE PANNE TOTALE REND UN 200 TOUT PROPRE. Le nœud n8n ne lit
    que `error_text` : 20 échecs sur 20 donnaient une exécution VERTE. C'était
    une régression par rapport à `_run_wf1`, dont le try global le remplissait —
    et le message d'alerte que j'avais écrit disait même « pioches=0 sans
    error_text n'est pas une panne », ce qui apprenait à ignorer le signal."""
    async def _ko(_pid):
        raise RuntimeError("503 Google")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch,
        lot=[{"google_place_id": f"ChIJ{i}", "tentatives": 0} for i in range(2)],
        get_place=_ko, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=2))

    assert out.error_text, "un lot sans aucune insertion DOIT crier"
    assert "503" in out.error_text


@pytest.mark.anyio
async def test_trois_echecs_d_affilee_coupent_le_lot(monkeypatch) -> None:
    """Quand la cause est GLOBALE — quota, facturation, clé révoquée, masque
    cassé — écraser les 20 fiches une par une ne sert à rien. Trois perdues au
    lieu de vingt."""
    appels: list[str] = []

    async def _ko(pid):
        appels.append(pid)
        raise RuntimeError("panne globale")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch,
        lot=[{"google_place_id": f"ChIJ{i}", "tentatives": 0} for i in range(20)],
        get_place=_ko, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=20))

    assert len(appels) == 3, "le coupe-circuit doit mordre à 3"
    assert "panne globale probable" in (out.error_text or "")


@pytest.mark.anyio
async def test_une_panne_supabase_n_emporte_pas_le_lot(monkeypatch) -> None:
    """`insert_company` et le `marquer_inventaire` final étaient HORS du try.
    Un 503 de PostgREST tuait les fiches restantes — et la fiche en cours avait
    DÉJÀ été payée chez Google sans être marquée, donc elle repartait en tête de
    file le lendemain et on la repayait."""
    def _insert_ko(_payload):
        raise RuntimeError("503 PostgREST")

    async def _ok(_pid):
        return _place()

    marques = _Marques()
    http_api = _brancher(
        monkeypatch,
        lot=[{"google_place_id": "ChIJa", "tentatives": 0},
             {"google_place_id": "ChIJb", "tentatives": 0}],
        get_place=_ok, marques=marques, insert=_insert_ko,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=2))

    assert out.echecs == 2, "les deux fiches doivent être comptées, pas perdues"
    assert len(marques) == 2, "chacune doit être remise en file"
    assert all(m["etat"] == "a_traiter" for m in marques)


@pytest.mark.anyio
async def test_une_entreprise_hors_quebec_est_ecartee(monkeypatch) -> None:
    """🔴 LE RECTANGLE DE GATINEAU COUVRE LE CENTRE-VILLE D'OTTAWA. L'ancien
    sourcing demandait « Gatineau, Québec » à Google, ce qui suffisait ; une
    requête par rectangle n'a plus cette protection. Toute la conformité du
    projet est écrite pour le Québec — une entreprise ontarienne est hors cadre,
    pas « de moins bonne qualité »."""
    async def _ottawa(_pid):
        p = _place(nom="Ottawa Snow Removal")
        p.raw_payload = {"addressComponents": [
            {"types": ["administrative_area_level_1"], "shortText": "ON"}]}
        return p

    marques = _Marques()
    http_api = _brancher(
        monkeypatch, lot=[{"google_place_id": "ChIJon", "tentatives": 0}],
        get_place=_ottawa, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))

    assert out.junk == 1 and out.inserees == 0
    assert marques[0]["motif"] == "hors_quebec"


@pytest.mark.anyio
async def test_une_adresse_sans_province_ne_fait_PAS_ecarter(monkeypatch) -> None:
    """Écarter sur une ABSENCE d'information ferait perdre des fiches valides
    dont l'adresse est incomplète. Le doute profite à la fiche."""
    async def _sans_province(_pid):
        p = _place()
        p.raw_payload = {"addressComponents": [
            {"types": ["locality"], "shortText": "Laval"}]}
        return p

    marques = _Marques()
    http_api = _brancher(
        monkeypatch, lot=[{"google_place_id": "ChIJx", "tentatives": 0}],
        get_place=_sans_province, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))
    assert out.inserees == 1


@pytest.mark.anyio
async def test_le_secteur_retenu_est_celui_qui_a_justifie_la_pioche(
    monkeypatch,
) -> None:
    """Le trigger de fusion trie `trouve_par` par ordre ALPHABÉTIQUE. Prendre
    `[0]` étiquetait une fiche `{entretien de piscine, tonte de gazon}` piochée
    en février pour la tonte comme « entretien de piscine » — et
    `metier_depuis_industry` l'aurait classée hors saison en aval, APRÈS qu'on
    ait payé ses détails ET sa recherche."""
    from src.tools import db as dbt

    captures: list = []

    async def _ok(_pid):
        return _place()

    def _capture(payload):
        from src.tools.db import InsertCompanyOut

        captures.append(payload)
        return InsertCompanyOut(status="inserted", company_id="cid")

    marques = _Marques()
    http_api = _brancher(
        monkeypatch,
        lot=[{"google_place_id": "ChIJx", "tentatives": 0,
              "trouve_par": ["entretien de piscine", "tonte de gazon"]}],
        get_place=_ok, marques=marques, insert=_capture,
    )
    monkeypatch.setattr(dbt, "secteurs_a_preparer", lambda **_k: ["tonte de gazon"])

    await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))
    assert captures[0].industry == "tonte de gazon"


@pytest.mark.anyio
async def test_une_fiche_fermee_definitivement_est_ecartee(monkeypatch) -> None:
    """`prompts/research.md` : « personne ne lit ce champ : sans toi, cette
    entreprise traverse tout le pipeline et reçoit un courriel »."""
    async def _fermee(_pid):
        p = _place()
        p.business_status = "CLOSED_PERMANENTLY"
        return p

    marques = _Marques()
    http_api = _brancher(
        monkeypatch, lot=[{"google_place_id": "ChIJx", "tentatives": 0}],
        get_place=_fermee, marques=marques,
    )
    out = await http_api._pioche_wf1(http_api.PiocheWf1In(limit=1))
    assert out.junk == 1
    assert marques[0]["motif"] == "ferme_definitivement"


# ------------------------- la frontière définitif/réessayable, à la source

@pytest.mark.anyio
@pytest.mark.parametrize(
    ("code", "corps", "attendu"),
    [
        (404, '{"error":{"status":"NOT_FOUND"}}', "introuvable"),
        (400, '{"error":{"status":"NOT_FOUND","message":"Invalid resource"}}',
         "introuvable"),
        # 🔴 LE CAS QUI MANQUAIT, ET QUI AURAIT COÛTÉ L'INVENTAIRE.
        (400, '{"error":{"status":"INVALID_ARGUMENT",'
              '"message":"Invalid field mask"}}', "reessayable"),
    ],
)
async def test_le_400_est_departage_a_la_source(
    monkeypatch, code, corps, attendu
) -> None:
    """Cette décision vit dans `_get_place_http`, et AUCUN test ne la touchait —
    les autres monkeypatchent `get_place` en entier. C'est exactement le trou
    par lequel le défaut est passé : un 400 causé par NOTRE masque était traité
    comme un identifiant périmé, donc écarté DÉFINITIVEMENT. Or `ecartee` est
    « un cache que rien n'invalide » : un masque cassé enterrait 20 fiches par
    matin, récupérables seulement par un `like` SQL à la main."""
    import httpx

    from src.tools import maps as m

    class _Reponse:
        status_code = code
        text = corps

        def raise_for_status(self):
            raise AssertionError("ne doit pas être atteint sur un 4xx")

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Reponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())

    with pytest.raises(Exception) as leve:  # noqa: PT011
        await m._get_place_http("ChIJx")

    est_introuvable = isinstance(leve.value, m.PlaceIntrouvable)
    assert est_introuvable is (attendu == "introuvable"), (
        f"{corps} devrait être {attendu}, on a eu {type(leve.value).__name__}"
    )


# ----------------------------------------- le piocheur muet (surveillance)

def _etat_inv(**kw):
    from src.http_api import EtatInventaire

    return EtatInventaire(**kw)


def test_un_piocheur_muet_prime_sur_l_etat_de_la_file() -> None:
    """🔴 LE SIGNAL ÉTAIT INVERSÉ. Si le piocheur meurt, le rythme décroît, donc
    `jours_de_file` AUGMENTE, donc le verdict restait `ok` — la file paraissait
    de plus en plus confortable précisément parce que plus personne n'y
    piochait. Puis à 14 jours le rythme devenait None et le verdict passait
    `sans_rythme`, silencieux. Détection estimée : un mois et demi. C'est le
    mode de panne des cinq semaines de l'été 2026, refait à neuf."""
    from datetime import datetime, timedelta, timezone

    from src.http_api import _verdict_inventaire

    vieux = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    etat = _etat_inv(
        total=900, piochable=600, rythme_par_jour=1.0, jours_de_file=600.0,
        dernier_balayage="2026-09-16T01:00:00Z", derniere_pioche=vieux,
    )
    assert _verdict_inventaire(etat) == "piocheur_muet"


def test_un_inventaire_jamais_pioche_n_est_PAS_un_piocheur_muet() -> None:
    """« Pas encore » et « plus rien » ne se crient pas pareil — même
    distinction que le verdict `jamais_balaye` un cran plus tôt. Un inventaire
    fraîchement balayé n'a évidemment aucune pioche à son actif."""
    from src.http_api import _verdict_inventaire

    etat = _etat_inv(
        total=900, piochable=600, dernier_balayage="2026-09-16T01:00:00Z",
        derniere_pioche=None,
    )
    assert _verdict_inventaire(etat) != "piocheur_muet"


def test_une_pioche_recente_ne_declenche_rien() -> None:
    from datetime import datetime, timedelta, timezone

    from src.http_api import _verdict_inventaire

    recent = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    etat = _etat_inv(
        total=900, piochable=600, rythme_par_jour=20.0, jours_de_file=30.0,
        dernier_balayage="2026-09-16T01:00:00Z", derniere_pioche=recent,
    )
    assert _verdict_inventaire(etat) == "ok"


@pytest.mark.anyio
async def test_le_piocheur_muet_reveille_alertes(monkeypatch) -> None:
    from src import http_api
    from src.lib import slack as slack_lib

    captures: list[str] = []

    async def _faux_notify(*, text, blocks=None, context=None, category=None):
        captures.append(text)
        assert category == "alerts"
        return True

    monkeypatch.setattr(slack_lib, "notify", _faux_notify)
    etat = _etat_inv(total=900, piochable=600, derniere_pioche="2026-09-10T10:00:00Z")
    assert await http_api._alerter_famine_inventaire(etat, "piocheur_muet") is True
    assert "PIOCHEUR NE TOURNE PLUS" in captures[0]
    assert "600" in captures[0], "il doit dire que la file n'est PAS le problème"


# ------------------------------------------------- l'équité entre régions

def test_la_file_alterne_entre_les_regions() -> None:
    """🔴 LE TRI PAR `created_at` REJOUAIT LA FAMINE GÉOGRAPHIQUE. `created_at`
    est l'ordre de DÉCOUVERTE, donc celui dans lequel le balayeur a parcouru les
    régions. Mesuré le 2026-09-16 sur les 663 déneigeurs : Montréal servie dès
    le jour 1, Saguenay au jour 22, Trois-Rivières au jour 25 — en pleine
    ouverture de saison, et seulement parce qu'elles avaient été balayées en
    dernier. C'est le défaut corrigé le 2026-09-14 dans `next_sourcing_target`,
    reproduit un étage plus bas."""
    from src.tools.db import _repartir_par_region

    lignes = (
        [{"google_place_id": f"mtl{i}", "regions": ["Montréal"]} for i in range(50)]
        + [{"google_place_id": f"tr{i}", "regions": ["Trois-Rivières"]} for i in range(5)]
    )
    lot = _repartir_par_region(lignes, 10)

    regions = [l["regions"][0] for l in lot]
    assert "Trois-Rivières" in regions, (
        "une petite région ne doit pas attendre que Montréal soit épuisée"
    )
    assert regions.count("Trois-Rivières") == 5


def test_l_ordre_de_famine_est_garde_dans_chaque_region() -> None:
    """On ALTERNE entre les files, on ne MÉLANGE pas. Les lignes arrivent déjà
    triées `derniere_tentative nulls first, created_at` ; une fiche déjà tentée
    doit rester derrière les vierges de sa propre région."""
    from src.tools.db import _repartir_par_region

    lignes = [
        {"google_place_id": "mtl-vierge", "regions": ["Montréal"]},
        {"google_place_id": "mtl-tentee", "regions": ["Montréal"]},
        {"google_place_id": "qc-vierge", "regions": ["Québec"]},
    ]
    lot = _repartir_par_region(lignes, 3)
    ids = [l["google_place_id"] for l in lot]
    assert ids.index("mtl-vierge") < ids.index("mtl-tentee")


def test_une_region_unique_n_est_pas_penalisee() -> None:
    """Quand tout vient d'une seule région, la répartition ne doit rien changer
    — surtout pas tronquer le lot."""
    from src.tools.db import _repartir_par_region

    lignes = [{"google_place_id": f"x{i}", "regions": ["Laval"]} for i in range(30)]
    lot = _repartir_par_region(lignes, 20)
    assert len(lot) == 20
    assert [l["google_place_id"] for l in lot] == [f"x{i}" for i in range(20)]


def test_une_entreprise_multi_region_n_apparait_qu_une_fois() -> None:
    """Le trigger de fusion trie `regions` alphabétiquement : le choix de la
    première est arbitraire mais STABLE. Ce qui compte, c'est qu'elle ne soit
    pas servie deux fois."""
    from src.tools.db import _repartir_par_region

    lignes = [
        {"google_place_id": "partagee", "regions": ["Laval", "Montréal"]},
        {"google_place_id": "autre", "regions": ["Québec"]},
    ]
    lot = _repartir_par_region(lignes, 10)
    assert [l["google_place_id"] for l in lot].count("partagee") == 1
    assert len(lot) == 2


def test_une_ligne_sans_region_ne_disparait_pas() -> None:
    """Les fiches semées depuis `companies` peuvent n'avoir aucune région si
    leurs coordonnées ne tombent dans aucun rectangle. Les perdre ici les
    rendrait invisibles pour toujours."""
    from src.tools.db import _repartir_par_region

    lignes = [
        {"google_place_id": "orpheline", "regions": []},
        {"google_place_id": "situee", "regions": ["Montréal"]},
    ]
    lot = _repartir_par_region(lignes, 10)
    assert len(lot) == 2


@pytest.mark.anyio
async def test_la_selection_sur_lit_avant_de_repartir(monkeypatch) -> None:
    """Sur-lire est ce qui rend la répartition possible : PostgREST ne sait
    trier que par colonne. Même patron que `FACTEUR_SURRECOLTE` de WF-4."""
    from src.tools import db as dbt

    vus: dict[str, str] = {}

    async def _faux_select(table, *, params=None, **_k):
        vus.update(params or {})
        return []

    monkeypatch.setattr(dbt.db, "select", _faux_select)
    monkeypatch.setattr(dbt, "secteurs_a_preparer", lambda **_k: ["s"])

    await dbt.list_inventaire_a_piocher(20)
    assert vus["limit"] == str(20 * dbt.FACTEUR_EQUITE_REGIONALE)
