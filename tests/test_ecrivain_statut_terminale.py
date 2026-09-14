"""Une fiche fermée par la recherche ne doit pas être dite « verrouillée ».

🔴 LE CAS RÉEL, ET C'EST LUI QUI A DÉCLENCHÉ CE FICHIER. Le 2026-09-14 à
15 h 00, la première passe de WF-3 sous le nouveau code a traité 20 fiches et en
a doté 19. La vingtième — « Groupe AZ Extermination » — n'a pas eu ses colonnes,
et le système a rapporté `verrouillee`.

Ce qui s'était passé : la recherche n'a rien rendu sur elle. Le PREMIER `update`
l'a donc passée en `disqualified` (il écrit `status` en même temps que
`research_json`), et le SECOND — qui exclut `disqualified` et `suppressed` — l'a
écartée. Comportement parfaitement correct.

Mais `verrouillee` désigne **l'anti-clobber** : « William a corrigé cette fiche à
la main, ne l'écrase pas ». Deux causes opposées sous une seule étiquette :
d'un côté une décision humaine qu'on protège, de l'autre une fiche que la
recherche vient de fermer.

🔧 Décision William du 2026-09-14 : « le 20e qui a été mis comme verrouillé, il
doit être mis comme disqualifié ». D'où la cinquième valeur, `terminale`.

⚠️ CE N'ÉTAIT PAS UN FAUX POSITIF — aucune des deux valeurs n'alerte, et le
compteur `sans_colonnes_metiers` ne les compte ni l'une ni l'autre. Ce fichier
ne répare donc pas une alarme : il répare un **diagnostic**, qui envoyait
chercher du côté des corrections manuelles une fiche que la recherche avait
fermée.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


RECHERCHE = {"services_offered": ["Extermination de fourmis"], "summary": "x"}


class _Base:
    """Le PREMIER update réussit, le SECOND ne touche rien. Seule la RAISON
    varie — c'est exactement la situation de Groupe AZ le 2026-09-14."""

    def __init__(self, statut_apres: str):
        self.statut_apres = statut_apres

    # ⚠️ `filters` est keyword-only et sans défaut dans la vraie signature.
    async def update(self, table, patch, *, filters):
        if "metiers" in patch or "fenetre_mois" in patch:
            return []
        return [{"id": "co-1"}]

    async def select(self, table, params=None, **_):
        return []


def _monter(monkeypatch, *, statut_apres: str):
    from src.tools import db as db_tools
    import src.tools.db as mod

    monkeypatch.setattr(mod, "db", _Base(statut_apres))
    # Le statut est calculé par `_statut_apres_recherche` : on le force pour
    # placer la fiche dans l'état voulu APRÈS le premier update.
    async def _statut(company_id, emails_found):
        return statut_apres
    monkeypatch.setattr(mod, "_statut_apres_recherche", _statut)
    return db_tools


async def test_une_fiche_fermee_par_la_recherche_est_dite_terminale(monkeypatch) -> None:
    db = _monter(monkeypatch, statut_apres="disqualified")

    out = await db.update_company_research("co-1", RECHERCHE)

    assert out["statut_metiers"] == "terminale", (
        f"obtenu {out['statut_metiers']!r} — une fiche que la recherche vient de "
        f"fermer ne doit pas être rapportée comme corrigée à la main"
    )
    assert out["metiers_ecrits"] is False


async def test_suppressed_aussi(monkeypatch) -> None:
    """Un retrait de consentement est terminal au même titre, et c'est le cas
    où écrire « les mois où on peut la démarcher » serait le plus déplacé."""
    db = _monter(monkeypatch, statut_apres="suppressed")

    out = await db.update_company_research("co-1", RECHERCHE)

    assert out["statut_metiers"] == "terminale"


async def test_une_correction_a_la_main_reste_verrouillee(monkeypatch) -> None:
    """🔴 LA CONTRE-ÉPREUVE, et elle compte autant que le reste.

    Si `terminale` avalait aussi ce cas, on perdrait le signal qui dit
    « n'écrase pas le travail de William ». La distinction ne vaut que si les
    deux branches restent distinguables.
    """
    db = _monter(monkeypatch, statut_apres="enriched")

    out = await db.update_company_research("co-1", RECHERCHE)

    assert out["statut_metiers"] == "verrouillee", (
        f"obtenu {out['statut_metiers']!r} — l'anti-clobber a perdu son étiquette"
    )


async def test_aucune_des_deux_ne_fait_crier(monkeypatch) -> None:
    """Le compteur d'alerte ne compte que `echec` et `absente`.

    Sans cette assertion, ajouter une valeur au vocabulaire pourrait la faire
    tomber dans la branche d'alerte de `/wf3/run` — et WF-3 crierait à chaque
    fiche disqualifiée, c'est-à-dire tous les jours. Une alarme qui sonne au
    nominal est une alarme morte.
    """
    for statut in ("disqualified", "enriched"):
        db = _monter(monkeypatch, statut_apres=statut)
        out = await db.update_company_research("co-1", RECHERCHE)
        assert out["statut_metiers"] not in ("echec", "absente"), (
            f"{statut} → {out['statut_metiers']!r} tombe dans la branche qui alerte"
        )
