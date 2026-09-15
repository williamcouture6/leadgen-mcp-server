"""Régression — les points d'entrée de sourcing doivent viser la piste VIVANTE.

`track='agence-ia'` est la seule piste active depuis le pivot du 2026-06-07.
`'OPT'` est gelée. Deux points d'entrée avaient déjà été basculés avec un
avertissement explicite dans le code (`/wf1/run` et `/sourcing/next-target`,
voir `http_api.py`) ; deux autres ont été oubliés, et c'est ce que ce module
verrouille.

Ce que coûtait l'oubli, dans l'ordre de gravité :

1. `scripts/run_sourcing_pass.py` ne passait AUCUN `track` à `db.CompanyIn`,
   donc taguait `'OPT'` toutes les fiches qu'il insérait. Elles sont alors
   INVISIBLES pour WF-4, qui filtre `agence-ia` — le sourcing paraît avoir
   fonctionné, les entreprises sont bien en base, et rien ne les démarchera
   jamais. C'est exactement le scénario que le commentaire de `RunWf1In`
   décrit pour s'en prémunir ; le script le reproduisait sans garde-fou.
2. Le même script et le tool MCP `db_next_sourcing_target` choisissaient leur
   cible dans le catalogue GELÉ (restaurants, cliniques, garages…) au lieu du
   catalogue des services résidentiels.
3. Ni l'un ni l'autre n'exposait de moyen de corriger côté appelant : pas de
   paramètre `track` sur le tool, pas de drapeau `--track` sur le script.

Le défaut du point 1 est silencieux et durable : une fiche mal taguée ne
lève rien, ne se voit pas au résumé quotidien, et ne se découvre qu'en
requêtant `companies.track` à la main.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.tools.db as dbt

RACINE = Path(__file__).resolve().parents[1]


def _charge_le_script():
    """Importe `scripts/run_sourcing_pass.py` — le dossier n'est pas un paquet."""
    chemin = RACINE / "scripts" / "run_sourcing_pass.py"
    spec = importlib.util.spec_from_file_location("run_sourcing_pass", chemin)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_sourcing_pass"] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------- le tool MCP

@pytest.mark.asyncio
async def test_tool_mcp_vise_la_piste_vivante(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un appel nu au tool MCP ne doit pas sourcer le catalogue gelé."""
    import src.server as serveur

    vu: dict = {}

    async def faux_next(track: str = "OPT"):
        vu["track"] = track
        return None

    monkeypatch.setattr(serveur.db_tools, "next_sourcing_target", faux_next)
    await serveur.next_sourcing_target()
    assert vu["track"] == "agence-ia"


@pytest.mark.asyncio
async def test_tool_mcp_laisse_choisir_la_piste(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le tool doit AUSSI exposer le choix — sinon l'historique OPT est hors
    d'atteinte, et un défaut de ce genre ne se corrige plus côté appelant."""
    import src.server as serveur

    vu: dict = {}

    async def faux_next(track: str = "OPT"):
        vu["track"] = track
        return None

    monkeypatch.setattr(serveur.db_tools, "next_sourcing_target", faux_next)
    await serveur.next_sourcing_target(track="OPT")
    assert vu["track"] == "OPT"


# ------------------------------------------------------- le défaut de la fonction

def test_defaut_de_next_sourcing_target_vise_la_piste_vivante() -> None:
    """Défense en profondeur : le PROCHAIN appelant qu'on écrira ne doit pas
    tomber dans le même trou. Le défaut de la fonction elle-même vise la piste
    vivante, comme celui de `RunWf1In.track` et de `/sourcing/next-target`."""
    import inspect

    defaut = inspect.signature(dbt.next_sourcing_target).parameters["track"].default
    assert defaut == "agence-ia"


# ------------------------------------------------------- le script

@pytest.mark.asyncio
async def test_script_choisit_dans_le_catalogue_vivant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _charge_le_script()
    vu: dict = {}

    async def faux_next(track: str = "OPT"):
        vu["track"] = track
        return SimpleNamespace(
            city="Longueuil", sector="entrepreneur en déneigement",
            icp_segment="commerce_local", reason="never_scraped",
        )

    monkeypatch.setattr(script.db, "next_sourcing_target", faux_next)
    args = SimpleNamespace(city=None, sector=None, icp=None, track="agence-ia")
    await script.pick_target(args)
    assert vu["track"] == "agence-ia"


@pytest.mark.asyncio
async def test_script_tague_les_fiches_sur_la_piste_vivante(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LE défaut qui coûte : une fiche taguée 'OPT' est invisible pour WF-4.

    Le chemin testé est celui des drapeaux explicites (`--city/--sector/--icp`),
    qui court-circuite la sélection de cible : même là — surtout là, puisque
    c'est le mode qu'on emploie pour rattraper une ville à la main — l'insert
    doit porter la piste vivante.
    """
    script = _charge_le_script()
    from src.tools import maps as maps_tools

    captures: list = []

    async def faux_search(payload):
        return maps_tools.SearchPlacesOut(
            results=[maps_tools.PlaceResult(
                name="Déneigement Test", google_place_id="place-1",
            )],
            next_page_token=None,
        )

    async def faux_start(payload):
        return SimpleNamespace(run_id="run-1", started_at="2026-09-15T00:00:00Z")

    async def faux_insert(payload):
        captures.append(payload)
        return SimpleNamespace(status="inserted", company_id="co-1", dedup_reason=None)

    async def faux_complete(payload):
        return {"updated": 1}

    monkeypatch.setattr(script.maps, "search_places", faux_search)
    monkeypatch.setattr(script.db, "start_sourcing_run", faux_start)
    monkeypatch.setattr(script.db, "insert_company", faux_insert)
    monkeypatch.setattr(script.db, "complete_sourcing_run", faux_complete)
    monkeypatch.setattr(sys, "argv", [
        "run_sourcing_pass.py",
        "--city", "Longueuil",
        "--sector", "entrepreneur en déneigement",
        "--icp", "commerce_local",
        "--max-pages", "1",
    ])

    await script.main()

    assert len(captures) == 1
    assert captures[0].track == "agence-ia", (
        "fiche taguée sur la piste gelée : invisible pour WF-4, en silence"
    )


@pytest.mark.asyncio
async def test_script_laisse_choisir_la_piste(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--track` doit exister ET porter jusqu'à l'insert."""
    script = _charge_le_script()
    from src.tools import maps as maps_tools

    captures: list = []

    async def faux_search(payload):
        return maps_tools.SearchPlacesOut(
            results=[maps_tools.PlaceResult(name="X", google_place_id="p")],
            next_page_token=None,
        )

    async def faux_insert(payload):
        captures.append(payload)
        return SimpleNamespace(status="inserted", company_id="c", dedup_reason=None)

    monkeypatch.setattr(script.maps, "search_places", faux_search)
    monkeypatch.setattr(script.db, "start_sourcing_run",
                        lambda p: _coroutine(SimpleNamespace(run_id="r", started_at="t")))
    monkeypatch.setattr(script.db, "insert_company", faux_insert)
    monkeypatch.setattr(script.db, "complete_sourcing_run",
                        lambda p: _coroutine({"updated": 1}))
    monkeypatch.setattr(sys, "argv", [
        "run_sourcing_pass.py",
        "--city", "Montréal", "--sector", "restaurant", "--icp", "commerce_local",
        "--max-pages", "1", "--track", "OPT",
    ])

    await script.main()
    assert captures[0].track == "OPT"


async def _coroutine(valeur):
    return valeur
