"""PostgREST plafonne toute réponse à 1000 lignes — compter en les ramenant ment.

Mesuré sur la vraie base le 2026-08-17 : la vue v_pourquoi_pas_de_courriel a
1895 lignes, un GET sans filtre en renvoie exactement 1000, et `limit=5000` ne
lève pas le plafond (c'est `max-rows`, réglé côté serveur). Aucune erreur, aucun
en-tête de troncature : `len(rows)` rend 1000 et personne ne le sait.

Deux primitives ferment le trou :
  - `count()`  : Prefer: count=exact → l'en-tête Content-Range donne le total
                 EXACT sans ramener une seule ligne. C'est la bonne réponse
                 quand on veut un nombre.
  - `select_all()` : pagine par tranches ordonnées quand on a besoin du CONTENU
                 des lignes, pas seulement de leur nombre.

Les agrégats côté serveur (`select=motif,count()`) auraient évité la pagination
mais ils sont désactivés sur ce projet : PGRST123 « Use of aggregate functions
is not allowed ».
"""
from __future__ import annotations

import httpx
import pytest
import respx


@pytest.fixture(autouse=True)
def _db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# --------------------------------------------------------------- count()

@respx.mock
async def test_count_rend_le_total_exact_au_dela_du_plafond() -> None:
    """1895 > 1000 : c'est tout l'intérêt. Un len(select()) aurait rendu 1000."""
    from src import supabase_client as db

    route = respx.get("https://test.supabase.co/rest/v1/v_pourquoi_pas_de_courriel").mock(
        return_value=httpx.Response(206, json=[], headers={"Content-Range": "*/1895"})
    )
    n = await db.count("v_pourquoi_pas_de_courriel")

    assert n == 1895
    assert route.called
    req = route.calls.last.request
    assert "count=exact" in req.headers["Prefer"]
    # zéro ligne transférée : on ne paie pas 1895 lignes pour un entier
    assert "limit=0" in str(req.url)


@respx.mock
async def test_count_respecte_les_filtres() -> None:
    from src import supabase_client as db

    route = respx.get("https://test.supabase.co/rest/v1/v_pourquoi_pas_de_courriel").mock(
        return_value=httpx.Response(206, json=[], headers={"Content-Range": "*/219"})
    )
    n = await db.count(
        "v_pourquoi_pas_de_courriel",
        params={"track": "eq.agence-ia", "motif": "eq.jamais_researchee"},
    )

    assert n == 219
    assert "motif=eq.jamais_researchee" in str(route.calls.last.request.url)


@respx.mock
async def test_count_zero_est_un_vrai_zero() -> None:
    from src import supabase_client as db

    respx.get("https://test.supabase.co/rest/v1/messages").mock(
        return_value=httpx.Response(200, json=[], headers={"Content-Range": "*/0"})
    )
    assert await db.count("messages") == 0


@respx.mock
async def test_count_lit_aussi_la_forme_avec_bornes() -> None:
    """Une requête HEAD rend `0-999/1895` plutôt que `*/1895`."""
    from src import supabase_client as db

    respx.get("https://test.supabase.co/rest/v1/messages").mock(
        return_value=httpx.Response(206, json=[], headers={"Content-Range": "0-999/1895"})
    )
    assert await db.count("messages") == 1895


@respx.mock
async def test_count_leve_plutot_que_de_rendre_zero_en_silence() -> None:
    """Un en-tête absent ou illisible doit CRIER. Rendre 0 reproduirait
    exactement le bug qu'on ferme : un chiffre faux et silencieux."""
    from src import supabase_client as db

    respx.get("https://test.supabase.co/rest/v1/messages").mock(
        return_value=httpx.Response(200, json=[])
    )
    with pytest.raises(RuntimeError, match="Content-Range"):
        await db.count("messages")


@respx.mock
async def test_count_leve_sur_un_total_inconnu() -> None:
    """count=planned/estimated rendrait `*/*` : ce n'est pas un compte exact."""
    from src import supabase_client as db

    respx.get("https://test.supabase.co/rest/v1/messages").mock(
        return_value=httpx.Response(206, json=[], headers={"Content-Range": "*/*"})
    )
    with pytest.raises(RuntimeError, match="Content-Range"):
        await db.count("messages")


@respx.mock
async def test_count_pose_le_profile_de_schema() -> None:
    from src import supabase_client as db

    route = respx.get("https://test.supabase.co/rest/v1/site_configs").mock(
        return_value=httpx.Response(206, json=[], headers={"Content-Range": "*/3"})
    )
    assert await db.count("site_configs", schema="agence") == 3
    assert route.calls.last.request.headers["Accept-Profile"] == "agence"


# ----------------------------------------------------------- select_all()

@respx.mock
async def test_select_all_ramene_les_1895_lignes_pas_1000() -> None:
    """La preuve : deux tranches, 1000 + 895, et l'offset avance."""
    from src import supabase_client as db

    pages = [
        httpx.Response(200, json=[{"company_id": f"c{i}"} for i in range(1000)]),
        httpx.Response(200, json=[{"company_id": f"c{i}"} for i in range(1000, 1895)]),
    ]
    route = respx.get("https://test.supabase.co/rest/v1/v_pourquoi_pas_de_courriel").mock(
        side_effect=pages
    )

    rows = await db.select_all(
        "v_pourquoi_pas_de_courriel", params={"select": "motif"}, order="company_id"
    )

    assert len(rows) == 1895
    assert len({r["company_id"] for r in rows}) == 1895, "aucun doublon entre tranches"
    assert route.call_count == 2
    urls = [str(c.request.url) for c in route.calls]
    assert "offset=0" in urls[0] and "limit=1000" in urls[0]
    assert "offset=1000" in urls[1]
    # ordre stable obligatoire : sans ORDER BY, offset peut sauter ou dupliquer
    assert "order=company_id" in urls[0]


@respx.mock
async def test_select_all_une_seule_requete_si_la_page_est_courte() -> None:
    """Le cas normal (816 lignes) ne doit pas coûter une requête de plus."""
    from src import supabase_client as db

    route = respx.get("https://test.supabase.co/rest/v1/v_pourquoi_pas_de_courriel").mock(
        return_value=httpx.Response(200, json=[{"company_id": f"c{i}"} for i in range(816)])
    )
    rows = await db.select_all("v_pourquoi_pas_de_courriel", order="company_id")

    assert len(rows) == 816
    assert route.call_count == 1


@respx.mock
async def test_select_all_gere_un_total_multiple_exact_de_la_tranche() -> None:
    """2000 lignes pile : la 2e tranche est pleine, la 3e vide. Sans le tour de
    boucle supplémentaire on s'arrêterait pile au bon endroit par chance ; avec
    2000 lignes exactement il FAUT la page vide pour savoir qu'on a fini."""
    from src import supabase_client as db

    pages = [
        httpx.Response(200, json=[{"company_id": f"c{i}"} for i in range(1000)]),
        httpx.Response(200, json=[{"company_id": f"c{i}"} for i in range(1000, 2000)]),
        httpx.Response(200, json=[]),
    ]
    route = respx.get("https://test.supabase.co/rest/v1/companies").mock(side_effect=pages)

    rows = await db.select_all("companies", order="id")

    assert len(rows) == 2000
    assert route.call_count == 3


@respx.mock
async def test_select_all_pose_le_profile_de_schema() -> None:
    from src import supabase_client as db

    route = respx.get("https://test.supabase.co/rest/v1/site_configs").mock(
        return_value=httpx.Response(200, json=[])
    )
    await db.select_all("site_configs", order="id", schema="agence")
    assert route.calls.last.request.headers["Accept-Profile"] == "agence"
