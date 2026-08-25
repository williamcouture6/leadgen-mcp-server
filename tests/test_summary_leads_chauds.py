"""PT3 — le bloc « leads chauds » du résumé quotidien.

Remplace le compteur aveugle « intéressés en attente de site : N » par une liste
nominative. Le socle patche les MODULES SOURCE (`sb`, `slack_mod`) et non
`http_api`, parce que summary_daily les importe LOCALEMENT dans la fonction."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _il_y_a(jours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat()


def _dans(jours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=jours)).isoformat()


def _socle(monkeypatch, *, chauds, supprimes=(), lectures_vue=None, vue_leve=False):
    """chauds : lignes rendues par agence.v_suivi_lead_courant.
    supprimes : itérable de courriels présents dans suppression_list (motif opt_out).
    lectures_vue : liste où empiler les params de chaque lecture de la vue.
    vue_leve : la lecture de la vue lève — le résumé doit le DIRE, pas se taire."""
    from src import http_api
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    async def fake_count(table, params=None):
        return 0

    async def fake_select_all(table, order=None, params=None, schema=None, **kw):
        if table == "v_suivi_lead_courant":
            if lectures_vue is not None:
                lectures_vue.append({"params": params or {}, "schema": schema})
            if vue_leve:
                raise RuntimeError("boom")
            return chauds
        if table == "suppression_list":
            return [{"email": c, "reason": "opt_out", "created_at": None} for c in supprimes]
        if table == "contacts":
            return []
        return []

    async def fake_select(table, params=None, schema=None, **kw):
        return []

    async def fake_notify(**kw):
        return True

    monkeypatch.setattr(sb, "count", fake_count)
    monkeypatch.setattr(sb, "select_all", fake_select_all)
    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    return http_api


async def test_la_vue_est_lue_dans_le_schema_agence_et_epinglee_agence_ia(monkeypatch):
    """La vue vit dans le schéma `agence` et le bloc ne suit PAS payload.tracks :
    le projet a une seule offre, et la ligne s'imprimerait deux fois si elle
    restait dans la boucle par track (le cron passe OPT + agence-ia)."""
    lectures = []
    http_api = _socle(monkeypatch, chauds=[], lectures_vue=lectures)
    await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["OPT", "agence-ia"], post=False)
    )
    assert len(lectures) == 1, "la vue doit être lue UNE fois, hors de la boucle par track"
    assert lectures[0]["schema"] == "agence"
    assert lectures[0]["params"].get("track") == "eq.agence-ia"


def _lead(nom, *, etape="site_envoye", jours=3, **kw):
    base = {
        "contact_id": f"ct-{nom}", "company_name": nom,
        "contact_email": f"info@{nom.lower().replace(' ', '')}.ca",
        "contact_status": "replied", "etape": etape, "note": None, "nb_notes": 0,
        "reference_immobilite": _il_y_a(jours), "fiche_client_existe": False,
    }
    base.update(kw)
    return base


async def test_un_desabonne_avec_des_notes_reste_visible_et_marque(monkeypatch):
    """Un lead avec six notes et un RDV qui écrit « on verra l'an prochain » est
    classé disqualified par WF-7 SANS aucun ping : il quitterait la liste du jour
    au lendemain. La promesse « un lead ne disparaît jamais » ne tient que si les
    impasses ÉTIQUETTENT au lieu d'exclure."""
    http_api = _socle(
        monkeypatch,
        chauds=[{
            "contact_id": "ct-1", "company_name": "Vitres Nadeau",
            "contact_email": "info@vitresnadeau.ca", "contact_status": "opted_out",
            "etape": "feedback_recu", "note": "il aimait ça", "nb_notes": 4,
            "reference_immobilite": _il_y_a(3), "fiche_client_existe": False,
        }],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "Vitres Nadeau" in out["text"]
    assert "s'est désabonné" in out["text"]
    assert "4 notes" in out["text"]


async def test_un_supprime_par_courriel_est_marque_meme_sans_statut(monkeypatch):
    """Le clic sur le lien du footer écrit TOUJOURS suppression_list mais ne pose
    contacts.status qu'au mieux. Sans le croisement par courriel — insensible à
    la casse — ce cas dégradé passerait pour un lead sain."""
    http_api = _socle(
        monkeypatch,
        chauds=[{
            "contact_id": "ct-1", "company_name": "Toiture Gagnon",
            "contact_email": "Marc@ToitureGagnon.ca", "contact_status": "contacted",
            "etape": "site_envoye", "note": None, "nb_notes": 0,
            "reference_immobilite": _il_y_a(2), "fiche_client_existe": False,
        }],
        supprimes=["marc@toituregagnon.ca"],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "s'est désabonné" in out["text"]


async def test_un_lead_sain_n_est_pas_marque(monkeypatch):
    http_api = _socle(
        monkeypatch,
        chauds=[_lead("Paysagement Roy", note="envoyé hier", nb_notes=1, jours=9)],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "Paysagement Roy" in out["text"]
    assert "désabonné" not in out["text"]
    assert "a dit non" not in out["text"]


async def test_un_site_deja_produit_ne_dit_jamais_a_produire(monkeypatch):
    """Le défaut à deux niveaux : une ligne demo_sites existe → « site produit ».
    Sans lui, un site livré depuis deux semaines réclamerait chaque jour sa
    production — le mensonge exact que PT3 existe pour éteindre."""
    http_api = _socle(
        monkeypatch,
        chauds=[_lead("Toiture Gagnon", etape="site_produit",
                      demo_frappee_le=_il_y_a(14), jours=14)],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "site produit" in out["text"]
    assert "à produire" not in out["text"]


async def test_un_lead_sans_note_ni_demo_dit_a_produire(monkeypatch):
    http_api = _socle(
        monkeypatch,
        chauds=[_lead("Déneigement Côté", etape="a_produire", jours=3)],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "à produire" in out["text"]
    assert "a dit oui il y a 3 j" in out["text"]


async def test_perdu_sort_de_la_liste(monkeypatch):
    http_api = _socle(monkeypatch, chauds=[_lead("Vitres X", etape="perdu")])
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "Vitres X" not in out["text"]


async def test_vendu_avec_fiche_sort_vendu_sans_fiche_reste(monkeypatch):
    http_api = _socle(
        monkeypatch,
        chauds=[
            _lead("Avec Fiche", etape="vendu", fiche_client_existe=True),
            _lead("Sans Fiche", etape="vendu", fiche_client_existe=False),
        ],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "Avec Fiche" not in out["text"]
    assert "Sans Fiche" in out["text"]
    assert "fiche client à créer" in out["text"]


async def test_le_rdv_calcom_s_affiche_a_cote_de_la_note(monkeypatch):
    http_api = _socle(
        monkeypatch,
        chauds=[_lead("BL Vitres", etape="feedback_recu",
                      note="il veut changer les couleurs",
                      rdv_prochain_at=_dans(2))],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "Cal.com" in out["text"]


async def test_plafond_dix_et_reste_annonce(monkeypatch):
    http_api = _socle(
        monkeypatch,
        chauds=[_lead(f"Boite {i}", jours=30 - i) for i in range(13)],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "et 3 autres" in out["text"]
    assert "Tes leads chauds (13)" in out["text"]


async def test_en_pause_reste_affiche_mais_hors_tri(monkeypatch):
    """Un lead en pause accumule de l'immobilité indéfiniment : laissé dans le
    tri, il occuperait le haut de la liste à demeure et pousserait les leads
    actifs sous le plafond."""
    http_api = _socle(
        monkeypatch,
        chauds=[
            _lead("En Pause", etape="en_pause", jours=200),
            _lead("Actif", jours=5),
        ],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["text"].index("Actif") < out["text"].index("En Pause")


async def test_au_dela_de_21_jours_la_ligne_pose_la_question(monkeypatch):
    """Un opérateur solo ne s'assoit jamais pour déclarer une défaite : sans
    relance, `perdu` est sous-écrit et le cimetière mange la liste."""
    http_api = _socle(monkeypatch, chauds=[_lead("Vieux Lead", jours=30)])
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "toujours vivant" in out["text"]


async def test_libelle_honnete_sur_le_silence(monkeypatch):
    """« dernière note il y a 9 j » et non « rien depuis 9 jours » : la phrase
    mesure le silence de William, pas celui du lead."""
    http_api = _socle(
        monkeypatch,
        chauds=[_lead("Roy", note="envoyé", nb_notes=1,
                      derniere_note_at=_il_y_a(9), jours=9)],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "dernière note il y a 9 j" in out["text"]


async def test_la_ligne_des_desabonnes_est_inchangee(monkeypatch):
    """🔴 GARDE-FOU. La ligne « intéressés désabonnés » et son interdit LCAP ont
    été livrés le 2026-08-23 et partagent la lecture que PT3 modifie. Ce test
    existe pour qu'un futur refactor du bloc « leads chauds » ne les emporte pas
    en silence : ils ne sont couverts par aucun autre test de ce fichier."""
    from src import http_api as http_api_mod
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    interesse = {
        "id": "ct-1", "email": "info@vitresnadeau.ca", "first_name": "Marc",
        "last_name": "Nadeau", "interested_at": _il_y_a(20), "status": "opted_out",
    }

    async def fake_count(table, params=None):
        return 0

    async def fake_select_all(table, order=None, params=None, schema=None, **kw):
        if table == "contacts" and "interested_at" in (params or {}):
            return [interesse]
        if table == "suppression_list":
            return [{"email": "info@vitresnadeau.ca", "reason": "opt_out",
                     "created_at": _il_y_a(2)}]
        return []

    async def fake_select(table, params=None, schema=None, **kw):
        return []

    async def fake_notify(**kw):
        return True

    monkeypatch.setattr(sb, "count", fake_count)
    monkeypatch.setattr(sb, "select_all", fake_select_all)
    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)

    out = await http_api_mod.summary_daily(
        http_api_mod.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "intéressés désabonnés 1 (cumul" in out["text"]
    assert "depuis 7 jours" in out["text"]
    assert slack_mod.GARDE_LCAP_APRES_DESABONNEMENT in out["text"]
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 1


async def test_un_lead_en_impasse_garde_son_etape_visible(monkeypatch):
    """Un désabonnement après un RDV vidéo ne se lit pas comme un désabonnement
    avant même la production du site. La marque s'ajoute à l'étape, elle ne
    l'efface pas."""
    http_api = _socle(
        monkeypatch,
        chauds=[_lead("Vitres Nadeau", etape="demo_faite", nb_notes=6,
                      contact_status="opted_out")],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "s'est désabonné" in out["text"]
    assert "démo faite" in out["text"]
    assert "6 notes au carnet" in out["text"]


# =====================================================================
# Task 7 — le bloc « À faire » (2026-08-25)
# =====================================================================

async def test_une_action_due_remonte_en_tete(monkeypatch):
    """Sans ce bloc, prochaine_action_at n'est jamais lu par une comparaison de
    date : la jambe « rappelle » de la promesse n'existerait pas, et « je le
    rappelle mardi » ne produirait rien mardi."""
    http_api = _socle(
        monkeypatch,
        chauds=[
            _lead("Immobile", jours=30),
            _lead("À Rappeler", prochaine_action="rappeler",
                  prochaine_action_at=_il_y_a(1), jours=2),
        ],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "À faire" in out["text"]
    assert out["text"].index("À Rappeler") < out["text"].index("Immobile")


async def test_une_action_lointaine_ne_remonte_pas(monkeypatch):
    http_api = _socle(
        monkeypatch,
        chauds=[_lead("Plus Tard", prochaine_action="relancer",
                      prochaine_action_at=_dans(10))],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "À faire" not in out["text"]


async def test_un_vendu_sans_fiche_est_epingle_dans_a_faire(monkeypatch):
    """Un `vendu` a par construction zéro jour d'immobilité : il se trierait EN
    DERNIER et sortirait le premier par le plafond. C'est la ligne qu'on ne doit
    jamais perdre."""
    http_api = _socle(
        monkeypatch,
        chauds=[_lead(f"Boite {i}", jours=40 - i) for i in range(12)]
        + [_lead("Vendu Hier", etape="vendu", fiche_client_existe=False, jours=0)],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "Vendu Hier" in out["text"]
    assert "fiche client à créer" in out["text"]


# =====================================================================
# Task 8 — ne jamais se taire (2026-08-25)
# =====================================================================

async def test_liste_vide_imprime_quand_meme_une_ligne(monkeypatch):
    """Aujourd'hui la ligne 🔥 disparaît quand le compteur vaut 0 : une vue qui
    rend vide pour une MAUVAISE raison produirait exactement la même sortie
    qu'une journée calme. Ce résumé est la seule file de travail — il doit
    toujours dire quelque chose."""
    http_api = _socle(monkeypatch, chauds=[])
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "aucun lead chaud" in out["text"]


async def test_une_lecture_ratee_est_dite_dans_le_resume(monkeypatch):
    """Fail-soft, jamais silencieux : le patron d'honnêteté de WF-7 appliqué ici."""
    http_api = _socle(monkeypatch, chauds=[], vue_leve=True)
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "carnet illisible" in out["text"]
    assert "aucun lead chaud" not in out["text"]


async def test_tout_epingle_ne_laisse_pas_un_entete_sur_du_vide(monkeypatch):
    """Quand chaque lead chaud est épinglé dans « À faire », la liste du dessous
    est vide. Un « (1) » posé sur zéro ligne — suivi d'une ligne blanche — se lit
    comme une liste perdue en route : le mode d'échec exact que Task 8 éteint un
    cran plus haut. Le compte inclut bien les épinglés, on dit juste où ils sont."""
    http_api = _socle(
        monkeypatch,
        chauds=[_lead("Vendu Seul", etape="vendu", fiche_client_existe=False,
                      jours=0)],
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "Tes leads chauds (1)* — tous dans « À faire » ci-dessus" in out["text"]
    assert "\n\n" not in out["text"], "un en-tête ne doit jamais surplomber du vide"
