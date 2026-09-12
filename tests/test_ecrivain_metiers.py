"""Les deux UPDATE : ordre, anti-clobber, non fatal, compté.

🔴 CE QUE CE FICHIER PROTÈGE. `update_company_research` écrit research_json,
status ET last_enriched_at — cette dernière portant la ré-éligibilité à 90 jours
du backlog de recherche. Toute forme où l'écriture des métiers peut faire échouer
ou sauter cette écriture-là fait sortir la fiche du backlog pour trois mois, en
silence. Mesuré le 2026-09-10 : 41 fiches sont dans la fenêtre où ça se verrait.
"""
from __future__ import annotations

from src.tools import db as db_tools

RESEARCH = {"services_offered": ["Déneigement résidentiel"]}


class _FausseBase:
    """Enregistre les appels. `update` HONORE le filtre d'anti-clobber, comme la
    vraie base : un UPDATE dont le filtre ne matche rien rend []."""

    def __init__(
        self,
        verrouillee: bool = False,
        echoue_sur_metiers: bool = False,
        terminale: bool = False,
    ):
        self.updates: list[tuple[dict, dict]] = []
        self.verrouillee = verrouillee
        self.echoue_sur_metiers = echoue_sur_metiers
        self.terminale = terminale

    # ⚠️ `filters` est KEYWORD-ONLY et SANS DÉFAUT dans la vraie signature
    # (`supabase_client.update(table, patch, *, filters, schema=None)`). Un faux
    # plus permissif accepterait un appel positionnel que la production
    # refuserait — et le test passerait sur du code qui casse en vrai.
    async def update(self, table, patch, *, filters):
        if "metiers" in patch and self.echoue_sur_metiers:
            raise RuntimeError("PGRST204: column 'metiers' does not exist")
        self.updates.append((patch, filters))
        # Une fiche terminale est écartée par `status not.in.(…)` — les DEUX
        # updates portent ce filtre, donc les deux rendent 0 ligne.
        if self.terminale and "disqualified" in filters.get("status", ""):
            return []
        if filters.get("metiers_verifies_a_la_main") == "eq.false" and self.verrouillee:
            return []          # 0 ligne touchée : l'anti-clobber a joué
        return [{"id": "co-1"}]

    async def select(self, table, params=None, **_):
        return []


async def test_deux_updates_dans_le_bon_ordre(monkeypatch):
    """research_json D'ABORD, les métiers ENSUITE."""
    faux = _FausseBase()
    monkeypatch.setattr(db_tools, "db", faux)
    await db_tools.update_company_research("co-1", RESEARCH)

    assert len(faux.updates) == 2
    premier, second = faux.updates
    assert "research_json" in premier[0]
    assert "last_enriched_at" in premier[0]
    assert "metiers" not in premier[0]
    assert set(second[0]) == {"metiers", "fenetre_mois", "metier_source",
                              "metiers_calcules_le"}


async def test_une_fiche_verrouillee_recoit_QUAND_MEME_son_research_json(monkeypatch):
    """🔴 Toute la raison des DEUX updates. Un filtre unique aurait sauté
    research_json, status ET last_enriched_at — qui porte la ré-éligibilité à
    90 jours du backlog. 41 fiches en sortiraient pour trois mois."""
    faux = _FausseBase(verrouillee=True)
    monkeypatch.setattr(db_tools, "db", faux)

    out = await db_tools.update_company_research("co-1", RESEARCH)

    premier, second = faux.updates
    assert premier[0]["research_json"] == RESEARCH
    assert premier[0]["last_enriched_at"]
    assert "metiers_verifies_a_la_main" not in premier[1], (
        "l'anti-clobber s'est glissé dans le filtre du PREMIER update"
    )
    assert second[1]["metiers_verifies_a_la_main"] == "eq.false"
    assert out["metiers_ecrits"] is False     # 0 ligne touchée par le second


async def test_lechec_du_second_ne_leve_PAS_et_se_compte(monkeypatch):
    """🔴 Le cas qui décide de la forme. Perdre research_json coûte un appel LLM
    payé, la boucle des contacts scrapés, et 90 jours de backlog ; une fiche sans
    colonnes est visible (metiers_calcules_le nul) et rattrapable."""
    faux = _FausseBase(echoue_sur_metiers=True)
    monkeypatch.setattr(db_tools, "db", faux)

    out = await db_tools.update_company_research("co-1", RESEARCH)

    assert len(faux.updates) == 1             # le premier a bien eu lieu
    assert out["metiers_ecrits"] is False


def test_le_patch_est_TOUJOURS_pose_meme_sans_metier_reconnu():
    """🔴 La différence avec `extract_lead_potential_patch`, qui rend {}.

    « Aucun métier reconnu » est une INFORMATION (le défaut inversé : les douze
    mois), pas une absence. Un {} laisserait fenetre_mois à NULL, et
    `fenetre_mois @> array[9]` rend NULL sur ces lignes — donc les écarte EN
    SILENCE, sans qu'aucun compteur ne bouge. C'est le comportement dont dépend
    toute la conversation B.
    """
    CLES = {"metiers", "fenetre_mois", "metier_source", "metiers_calcules_le"}
    for entree in (None, {}, "pas un dict", {"services_offered": None},
                   {"services_offered": ["Consultation stratégique"]},
                   {"services_offered": "une chaine, pas une liste"}):
        patch = db_tools.extract_metiers_patch(entree)
        assert set(patch) == CLES, f"patch incomplet pour {entree!r} : {patch}"
        assert patch["fenetre_mois"] == list(range(1, 13)), entree
        assert patch["metier_source"] == "inconnu", entree
        assert patch["metiers_calcules_le"]


async def test_une_fiche_terminale_ne_recoit_AUCUNE_colonne(monkeypatch):
    """🔴 Le second UPDATE porte le MÊME filtre de statuts que le premier.

    Sans lui, une fiche `disqualified`/`suppressed` se voit refuser son
    research_json par le premier UPDATE mais reçoit quand même ses colonnes de
    métiers : `metiers_calcules_le` devient non nul, le backfill la croit
    complète, et le statut annonce « tout va bien » sur le seul chemin où
    l'écriture principale a été jetée.

    Et sur `suppressed`, qui est un RETRAIT DE CONSENTEMENT, écrire « les mois
    où on peut la démarcher » est un mauvais signal en soi.
    """
    faux = _FausseBase(terminale=True)
    monkeypatch.setattr(db_tools, "db", faux)

    out = await db_tools.update_company_research("co-1", RESEARCH)

    assert len(faux.updates) == 2, "les deux updates doivent PARTIR"
    _, second = faux.updates
    assert second[1]["status"] == "not.in.(disqualified,suppressed)", (
        "le second UPDATE n'a pas la garde des statuts terminaux"
    )
    assert out["updated"] == 0
    assert out["statut_metiers"] == "absente"
    assert out["metiers_ecrits"] is False


async def test_les_trois_statuts_sains_se_distinguent(monkeypatch):
    """🔴 `verrouillee` n'est PAS une anomalie : c'est l'anti-clobber qui joue.

    Les confondre avec `echec` ferait sonner l'alarme sur le fonctionnement
    nominal le jour où une fiche est vérifiée à la main — et une alarme qui
    sonne au nominal est une alarme morte.
    """
    faux = _FausseBase()
    monkeypatch.setattr(db_tools, "db", faux)
    assert (await db_tools.update_company_research("co-1", RESEARCH))[
        "statut_metiers"] == "ecrit"

    faux = _FausseBase(verrouillee=True)
    monkeypatch.setattr(db_tools, "db", faux)
    assert (await db_tools.update_company_research("co-1", RESEARCH))[
        "statut_metiers"] == "verrouillee"

    faux = _FausseBase(echoue_sur_metiers=True)
    monkeypatch.setattr(db_tools, "db", faux)
    assert (await db_tools.update_company_research("co-1", RESEARCH))[
        "statut_metiers"] == "echec"
