"""Le résumé quotidien dit la performance de chaque gabarit.

La trace du bras existe depuis AC1b (`messages.template_choice`) et les vues
0050/0052 la calculent, mais une mesure que personne ne regarde n'existe pas —
c'est la leçon déjà payée deux fois par ce projet (le runbook qui disait
l'inverse de la réalité, la panne Google Places invisible cinq semaines). Cette
ligne est ce qui met la mesure sous les yeux, tous les soirs.

🔴 CE QUE CES TESTS PROTÈGENT AVANT TOUT, dans l'ordre :

1. UN GAGNANT NE S'AFFICHE JAMAIS NU. Le verdict voyage sur la MÊME ligne que
   le nom. « B mène » lu seul est la phrase qui fait basculer toute une file
   sur une réponse chanceuse.
2. LE BLOC NE SE TAIT JAMAIS. Trois silences possibles — aucun gabarit, rien de
   parti, tout rebondi — et chacun a sa phrase. Un bloc absent se lirait comme
   « aucun écart à signaler », soit une information rassurante qu'on n'a pas.
3. UNE PANNE SE DIT AU BON ENDROIT. Une lecture en échec ne doit pas ressembler
   à un calme plat, et une panne du seul classement ne doit pas emporter des
   compteurs parfaitement lisibles.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _perf(**kw):
    """Une ligne de agence.v_perf_par_bras, avec des zéros par défaut."""
    base = {
        "bras": "A", "leads": 0, "partis": 0, "rebonds": 0, "livres": 0,
        "desabonnements": 0,
        "reponses_brutes": 0, "oui_bruts": 0, "rdv_bruts": 0, "ventes_brutes": 0,
        "livres_reponse": 0, "livres_rdv": 0, "livres_vente": 0,
        "reponses": 0, "oui": 0, "rdv": 0, "ventes": 0, "oui_avec_reponse": 0,
        "taux_reponse": None, "taux_oui": None, "taux_rdv": None,
        "taux_vente": None, "part_oui_dans_reponses": None, "taux_rebond": None,
    }
    return {**base, **kw}


def _tete(**kw):
    """Une ligne de agence.v_bras_en_tete."""
    base = {
        "categorie": "taux_reponse", "bras_en_tete": None, "valeur_en_tete": None,
        "succes_en_tete": 0, "base_en_tete": 0, "bras_suivant": None,
        "valeur_suivant": None, "succes_suivant": 0, "base_suivant": 0,
        "z": None, "verdict": "aucune donnee",
    }
    return {**base, **kw}


def _lignes_tete(bloc: str) -> list[str]:
    """Les lignes de classement, isolées proprement.

    ⚠️ Le premier jet de ces tests faisait `bloc.split("🥇")[-1]`, ce qui ne
    marchait QUE par l'ordre du dictionnaire des catégories : réordonner
    `_LIBELLES_CATEGORIES` faisait regarder le mauvais segment sans rien casser
    de visible. On découpe par ligne, pas par séparateur.
    """
    return [l for l in bloc.split("\n") if "🥇" in l]


# ------------------------------------------------------- les trois silences

def test_aucun_gabarit_en_base_se_dit(monkeypatch) -> None:
    """Lecture réussie, zéro ligne : soit rien n'a été écrit, soit WF-4 n'écrit
    plus template_choice. Le second cas tue le test A/B en silence."""
    from src import http_api

    bloc = http_api._bloc_performance_gabarit([], [])

    assert "Performance gabarit" in bloc
    assert "template_choice" in bloc, "la cause probable doit être nommée"
    assert bloc.strip() != ""


def test_sans_aucun_envoi_le_bloc_ne_nomme_personne() -> None:
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=12), _perf(bras="B", leads=9),
         _perf(bras="C", leads=22), _perf(bras="D", leads=24)],
        [_tete(categorie=c) for c in http_api._LIBELLES_CATEGORIES],
    )

    assert "67 brouillons" in bloc
    assert "aucun courriel parti" in bloc
    assert _lignes_tete(bloc) == [], "aucun classement sans le moindre envoi"


def test_un_seul_brouillon_sécrit_au_singulier() -> None:
    from src import http_api

    bloc = http_api._bloc_performance_gabarit([_perf(bras="A", leads=1)], [])

    assert "1 brouillon en attente" in bloc
    assert "1 brouillons" not in bloc


def test_tout_rebondi_est_une_panne_pas_une_file_en_attente() -> None:
    """🔴 Le premier jet lisait `livres == 0` et affichait « aucun courriel
    parti » alors que 40 courriels étaient partis et avaient TOUS rebondi —
    une panne d'envoi maquillée en file tranquille."""
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=40, partis=40, rebonds=40, livres=0)],
        [],
    )

    assert "aucun courriel parti" not in bloc
    assert "🚨" in bloc
    assert "40 courriels partis" in bloc
    assert "40 rebonds" in bloc
    assert "RIEN" in bloc


# --------------------------------------------------------- les compteurs

def test_chaque_bras_montre_ses_chiffres() -> None:
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [
            _perf(bras="A", leads=40, partis=40, livres=38, livres_reponse=38,
                  reponses_brutes=3, oui_bruts=1, taux_reponse=7.9),
            _perf(bras="B", leads=40, partis=40, livres=40, livres_reponse=40,
                  reponses_brutes=6, oui_bruts=4, rdv_bruts=2, ventes_brutes=1,
                  taux_reponse=15.0),
        ],
        [],
    )

    assert "*A*" in bloc and "*B*" in bloc
    assert "38 livrés" in bloc and "40 livrés" in bloc
    assert "3 rép" in bloc and "6 rép" in bloc
    assert "15,0 %" in bloc, "les décimales s'écrivent à la française"
    assert "2 RDV" in bloc


def test_la_base_mure_est_dite_quand_elle_differe_des_livres() -> None:
    """Le taux ne se calcule pas sur les livrés mais sur les livrés MÛRS (0052).
    Sans la mention, « 512 livrés · 20,0 % » invite à diviser de tête et à ne
    pas retrouver son compte."""
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=512, partis=512, livres=512, livres_reponse=500,
               reponses_brutes=100, taux_reponse=20.0)],
        [],
    )
    assert "512 livrés (500 mûrs)" in bloc

    egal = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=500, partis=500, livres=500, livres_reponse=500,
               reponses_brutes=100, taux_reponse=20.0)],
        [],
    )
    assert "mûrs" not in egal, "ne rien dire quand les deux coïncident"


def test_les_rebonds_se_voient() -> None:
    """Un bras à 40 % de rebond était visuellement identique à un bras sain :
    le compteur existait dans la vue et n'était pas affiché."""
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=100, partis=100, rebonds=40, livres=60,
               livres_reponse=60, reponses_brutes=3, taux_reponse=5.0)],
        [],
    )
    assert "40 rebonds" in bloc
    assert "⚠️" in bloc


def test_le_pluriel_est_juste() -> None:
    from src import http_api

    un = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=1, partis=1, livres=1, livres_reponse=1,
               ventes_brutes=1, rebonds=1)], [])
    # `"1 vente" in bloc` passerait aussi sur « 1 ventes » : on vise la forme fautive.
    assert "1 ventes" not in un
    assert "1 vente" in un
    assert "1 livrés" not in un and "1 livré" in un
    assert "1 rebonds" not in un and "1 rebond" in un

    deux = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=2, partis=2, livres=2, livres_reponse=2,
               ventes_brutes=2)], [])
    assert "2 ventes" in deux and "2 livrés" in deux


def test_un_compteur_en_chaine_ne_fait_pas_tomber_le_rendu() -> None:
    """Si PostgREST sérialisait un jour un compteur en chaîne ('12.0'), `int()`
    lèverait — et cette exception, née dans le RENDU, serait attribuée à une
    panne de lecture. On enverrait chercher au mauvais endroit."""
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads="40", partis="40", livres="38",
               livres_reponse="38", reponses_brutes="3", taux_reponse="7.9")],
        [],
    )
    assert "38 livrés" in bloc and "3 rép" in bloc and "7,9 %" in bloc


# ----------------------------------------------------------- le classement

def test_le_gagnant_ne_sort_jamais_sans_son_verdict() -> None:
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=500, partis=500, livres=500, livres_reponse=500,
               reponses_brutes=100, taux_reponse=20.0),
         _perf(bras="B", leads=500, partis=500, livres=500, livres_reponse=500,
               reponses_brutes=150, taux_reponse=30.0)],
        [_tete(categorie="taux_reponse", bras_en_tete="B", valeur_en_tete=30.0,
               succes_en_tete=150, base_en_tete=500, bras_suivant="A",
               valeur_suivant=20.0, succes_suivant=100, base_suivant=500,
               z=3.65, verdict="ecart net")],
    )

    tetes = _lignes_tete(bloc)
    assert len(tetes) == 1
    assert "B" in tetes[0] and "devant A" in tetes[0]
    assert "écart net" in tetes[0]


def test_tous_les_verdicts_ont_une_traduction_lisible() -> None:
    """Une faute de frappe dans la table des libellés partirait en production
    sans qu'un seul test devienne rouge."""
    from src import http_api

    for brut, attendu in http_api._LIBELLES_VERDICTS.items():
        bloc = http_api._bloc_performance_gabarit(
            [_perf(bras="A", leads=500, partis=500, livres=500,
                   livres_reponse=500, reponses_brutes=100, taux_reponse=20.0)],
            [_tete(categorie="taux_reponse", bras_en_tete="A",
                   valeur_en_tete=20.0, succes_en_tete=100, base_en_tete=500,
                   verdict=brut)],
        )
        assert attendu in bloc, f"le verdict {brut!r} ne s'affiche pas en clair"


def test_toutes_les_categories_ont_un_libelle_lisible() -> None:
    from src import http_api

    for cle, libelle in http_api._LIBELLES_CATEGORIES.items():
        bloc = http_api._bloc_performance_gabarit(
            [_perf(bras="A", leads=500, partis=500, livres=500,
                   livres_reponse=500, reponses_brutes=100, taux_reponse=20.0)],
            [_tete(categorie=cle, bras_en_tete="A", valeur_en_tete=20.0,
                   succes_en_tete=100, base_en_tete=500, verdict="trop tot")],
        )
        assert f"🥇 {libelle} :" in bloc


def test_un_verdict_inconnu_se_nomme_inconnu() -> None:
    """Le commentaire du code jure que le verdict ne s'affiche jamais en langage
    de base. Le repli du premier jet faisait exactement l'inverse."""
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=500, partis=500, livres=500, livres_reponse=500,
               reponses_brutes=100, taux_reponse=20.0)],
        [_tete(categorie="taux_reponse", bras_en_tete="A", valeur_en_tete=20.0,
               succes_en_tete=100, base_en_tete=500, verdict="ecart net (saison)")],
    )

    assert "verdict inconnu (ecart net (saison))" in bloc


def test_sans_second_bras_la_ligne_ne_dit_pas_devant() -> None:
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=500, partis=500, livres=500, livres_reponse=500,
               reponses_brutes=100, taux_reponse=20.0)],
        [_tete(categorie="taux_reponse", bras_en_tete="A", valeur_en_tete=20.0,
               succes_en_tete=100, base_en_tete=500, bras_suivant=None,
               verdict="pas de comparaison")],
    )

    tetes = _lignes_tete(bloc)
    assert len(tetes) == 1
    assert "devant" not in tetes[0]
    assert "None" not in tetes[0]
    assert "un seul bras servi" in tetes[0]


def test_une_categorie_sans_succes_ne_fait_pas_de_ligne() -> None:
    """Personne n'a encore vendu : « 🥇 vente A 0,0 % » chaque soir serait du
    bruit, et un premier à zéro n'est pas un premier."""
    from src import http_api

    bloc = http_api._bloc_performance_gabarit(
        [_perf(bras="A", leads=500, partis=500, livres=500, livres_reponse=500,
               reponses_brutes=100, taux_reponse=20.0)],
        [
            _tete(categorie="taux_reponse", bras_en_tete="A", valeur_en_tete=20.0,
                  succes_en_tete=100, base_en_tete=500, verdict="trop tot"),
            _tete(categorie="taux_vente", bras_en_tete="A", valeur_en_tete=0.0,
                  succes_en_tete=0, base_en_tete=500, verdict="trop tot"),
        ],
    )

    tetes = _lignes_tete(bloc)
    assert len(tetes) == 1
    assert not any("vente" in l for l in tetes)


# ------------------------------------------------------- branché au résumé

def _stubs(monkeypatch, select_all):
    from src import supabase_client as sb

    async def _count(table, *, params=None, schema=None):
        return 0

    async def _select(table, *, params=None, schema=None):
        return []

    monkeypatch.setattr(sb, "select_all", select_all)
    monkeypatch.setattr(sb, "count", _count)
    monkeypatch.setattr(sb, "select", _select)


async def test_le_resume_lit_les_vues_dans_le_schema_agence(monkeypatch) -> None:
    from src import http_api

    lectures: list[tuple[str, str | None]] = []

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        lectures.append((table, schema))
        if table == "v_perf_par_bras":
            return [_perf(bras="A", leads=500, partis=500, livres=500,
                          livres_reponse=500, reponses_brutes=100, oui_bruts=20,
                          taux_reponse=20.0)]
        if table == "v_bras_en_tete":
            return [_tete(categorie="taux_reponse", bras_en_tete="A",
                          valeur_en_tete=20.0, succes_en_tete=100,
                          base_en_tete=500, verdict="pas de comparaison")]
        return []

    _stubs(monkeypatch, _select_all)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )

    assert ("v_perf_par_bras", "agence") in lectures
    assert ("v_bras_en_tete", "agence") in lectures
    assert "Performance gabarit" in out["text"]
    assert "un seul bras servi" in out["text"]
    # Le contrat de sortie est le même sur les deux branches — le voisin
    # `totals["conformite"]` porte toujours sa clé `lu`.
    assert out["totals"]["performance_gabarit"]["lu"] is True
    assert out["totals"]["performance_gabarit"]["bras"][0]["bras"] == "A"


async def test_le_bloc_napparait_quune_fois_avec_deux_tracks(monkeypatch) -> None:
    """Le défaut de DailySummaryIn est ["OPT", "agence-ia"]. Déplacer le bloc
    dans la boucle par track l'imprimerait deux fois, dont une section OPT vide,
    et doublerait les lectures PostgREST."""
    from src import http_api

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        if table == "v_perf_par_bras":
            return [_perf(bras="A", leads=10)]
        return []

    _stubs(monkeypatch, _select_all)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["OPT", "agence-ia"], post=False)
    )

    assert out["text"].count("Performance gabarit") == 1


async def test_une_lecture_en_echec_se_dit_au_lieu_de_disparaitre(monkeypatch) -> None:
    from src import http_api

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        if table in ("v_perf_par_bras", "v_bras_en_tete"):
            raise RuntimeError("PostgREST down")
        return []

    _stubs(monkeypatch, _select_all)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )

    assert "Performance gabarit" in out["text"]
    assert "ÉCHEC" in out["text"]
    assert out["totals"]["performance_gabarit"]["lu"] is False


async def test_une_panne_du_classement_nemporte_pas_les_compteurs(monkeypatch) -> None:
    """🔴 Le premier jet partageait un seul `try` : perdre agence.v_bras_en_tete
    faisait disparaître AUSSI le tableau par bras, dont les chiffres étaient
    parfaitement lisibles."""
    from src import http_api

    async def _select_all(table, *, order, params=None, page_size=1000, schema=None):
        if table == "v_perf_par_bras":
            return [_perf(bras="A", leads=500, partis=500, livres=500,
                          livres_reponse=500, reponses_brutes=100, taux_reponse=20.0)]
        if table == "v_bras_en_tete":
            raise RuntimeError("vue renommee")
        return []

    _stubs(monkeypatch, _select_all)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )

    assert "500 livrés" in out["text"], "les compteurs lus doivent survivre"
    assert "100 rép (20,0 %)" in out["text"]
    assert "classement indisponible" in out["text"]
    assert out["totals"]["performance_gabarit"]["lu"] is False
    # Les chiffres déjà lus ne sont PAS détruits par la panne de l'autre vue.
    assert out["totals"]["performance_gabarit"]["bras"][0]["livres"] == 500
