"""PT1 — la ligne « intéressés en attente de site » du résumé quotidien.
N = contacts.interested_at non nul ET aucune ligne agence.demo_sites pour ce
contact. La frappe du jeton (PT2) fait redescendre N sans écriture dédiée."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _socle(monkeypatch, *, interesses, demo_par_contact, captured=None, supprimes=()):
    """demo_par_contact : dict contact_id -> lignes demo_sites à retourner.
    captured : liste optionnelle où empiler les `params` de chaque select_all
    sur "contacts" — sert au test qui pin l'ABSENCE de filtre de statut dans la
    requête PostgREST (le tri se fait en Python, cf. plus bas : le remettre en
    SQL viderait le compteur des désabonnés en silence).
    supprimes : courriels présents dans `suppression_list` (chemin du clic sur
    le lien du footer, qui ne garantit PAS que contacts.status ait basculé).
    Ensemble = motif `opt_out` implicite ; dict courriel -> motif pour tester
    les motifs qui ne sont PAS un retrait de consentement (`hard_bounce`,
    `manual`, `competitor`, `dncl`).

    ⚠️ summary_daily importe `sb` et `slack_lib` LOCALEMENT dans la fonction
    (`from . import supabase_client as sb` / `from .lib import slack as
    slack_lib`) : patcher les attributs des MODULES SOURCE, pas http_api."""
    from src import http_api
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    async def fake_count(table, params=None):
        return 0

    async def fake_select_all(table, order=None, params=None, **kw):
        if table == "contacts" and "interested_at" in (params or {}):
            if captured is not None:
                captured.append(params or {})
            return interesses
        return []  # dont la vue v_pourquoi_pas_de_courriel : vide suffit ici

    async def fake_select(table, params=None, schema=None, **kw):
        if table == "suppression_list":
            courriel = (params or {}).get("email", "").removeprefix("eq.")
            # Le MOTIF est la moitié du contrat : `hard_bounce` (adresse morte),
            # `manual`/`competitor`/`dncl` (nos décisions) vivent dans la même
            # table que les vrais retraits de consentement. Le fake rend donc
            # toujours le motif, et c'est l'appelant qui trie — comme en prod
            # depuis qu'on interroge la table SANS filtre de motif.
            motifs = (params or {}).get("reason", "")
            garde = supprimes.get(courriel) if isinstance(supprimes, dict) else (
                "opt_out" if courriel in supprimes else None
            )
            if garde is None:
                return []
            if motifs and f"{garde}" not in motifs:
                return []
            return [{"email": courriel, "reason": garde}]
        assert table == "demo_sites" and schema == "agence"
        cid = (params or {}).get("contact_id", "").removeprefix("eq.")
        return demo_par_contact.get(cid, [])

    async def fake_notify(**kw):
        return True

    monkeypatch.setattr(sb, "count", fake_count)
    monkeypatch.setattr(sb, "select_all", fake_select_all)
    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    return http_api


async def test_compte_les_interesses_sans_demo(monkeypatch):
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1"}, {"id": "ct-2"}, {"id": "ct-3"}],
        demo_par_contact={"ct-2": [{"id": "d-1"}]},  # ct-2 est servi → sort du compteur
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 2
    assert "intéressés en attente de site 2" in out["text"]


async def test_zero_interesse_pas_de_ligne(monkeypatch):
    http_api = _socle(monkeypatch, interesses=[], demo_par_contact={})
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0
    assert "en attente de site" not in out["text"]


async def test_les_impasses_sortent_du_compteur(monkeypatch):
    """Un intéressé qui bascule opted_out/disqualified/bounced ne doit pas rester
    compté « en attente de site » à vie — interested_at ne redescend jamais seul.

    L'exclusion vivait dans le filtre SQL ; depuis la ligne « intéressés
    désabonnés » (2026-08-23) la requête doit VOIR les opted_out, donc le tri se
    fait en Python. Ce test pin le comportement observable, pas la mécanique."""
    captured: list[dict] = []
    http_api = _socle(
        monkeypatch,
        interesses=[
            {"id": "ct-vivant", "email": "v@x.ca", "status": "replied"},
            {"id": "ct-parti", "email": "p@x.ca", "status": "opted_out"},
            {"id": "ct-dq", "email": "d@x.ca", "status": "disqualified"},
            {"id": "ct-bounce", "email": "b@x.ca", "status": "bounced"},
        ],
        demo_par_contact={},  # aucun n'a de démo
        captured=captured,
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert len(captured) == 1
    # Garde de non-régression : le filtre de statut ne doit JAMAIS revenir dans
    # la requête PostgREST. Le geste est tentant (le N+1 est juste en dessous),
    # mais il rendrait les opted_out invisibles à la lecture — le chemin
    # majoritaire du compteur des désabonnés tomberait à zéro, suite verte.
    assert "status" not in captured[0]
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 1


# =====================================================================
# La ligne « intéressés désabonnés » (2026-08-23) — les DEUX chemins
# =====================================================================

async def test_un_interesse_passe_opted_out_est_compte(monkeypatch):
    """Chemin réponse « désabonnez-moi » (WF-7) ET chemin normal du clic sur le
    lien du footer : les deux posent contacts.status='opted_out'."""
    http_api = _socle(
        monkeypatch,
        interesses=[
            {"id": "ct-1", "email": "a@x.ca", "status": "opted_out"},
            {"id": "ct-2", "email": "b@x.ca", "status": "replied"},
        ],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 1
    assert "intéressés désabonnés 1" in out["text"]
    # complémentaire, pas redondant : le désabonné sort de l'autre compteur
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 1


async def test_un_interesse_sur_la_liste_de_suppression_est_compte(monkeypatch):
    """Chemin du clic sur le lien du footer en mode dégradé : l'Edge Function
    écrit TOUJOURS suppression_list mais ne pose contacts.status qu'au mieux
    (erreur de lecture/écriture journalisée puis ignorée). Sans le croisement
    par courriel, ce désabonnement resterait invisible — pire, le contact
    resterait dans la file « en attente de site »."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "a@x.ca", "status": "replied"}],
        demo_par_contact={},
        supprimes={"a@x.ca"},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 1
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0


async def test_une_adresse_morte_nest_pas_un_desabonnement(monkeypatch):
    """`hard_bounce` (posé par WF-6b) vit dans la MÊME table que les opt-outs.
    Le compter comme un désabonnement mentirait deux fois : le chiffre serait
    faux, et le garde-fou LCAP serait appliqué à quelqu'un qui n'a jamais retiré
    son consentement — son adresse est simplement morte."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "mort@x.ca", "status": "bounced"}],
        demo_par_contact={},
        supprimes={"mort@x.ca": "hard_bounce"},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 0
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0


async def test_un_interesse_sain_nest_pas_compte_comme_desabonne(monkeypatch):
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "a@x.ca", "status": "replied"}],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 0
    assert "intéressés désabonnés" not in out["text"]


async def test_zero_desabonne_pas_de_ligne(monkeypatch):
    http_api = _socle(monkeypatch, interesses=[], demo_par_contact={})
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 0
    assert "désabonnés" not in out["text"]


# =====================================================================
# La ligne doit être ACTIONNABLE (2026-08-23) — un nombre nu ne décide rien
# =====================================================================

def _desabonne(n, *, prenom, nom, courriel, oui):
    return {"id": f"ct-{n}", "email": courriel, "first_name": prenom,
            "last_name": nom, "interested_at": oui, "status": "opted_out"}


async def test_la_ligne_nomme_les_leads_et_porte_linterdit_lcap(monkeypatch):
    """Le rendu EXACT. Un « ⚠️ intéressés désabonnés 3 » nu n'aide pas William à
    décider : il lui faut QUI, QUAND il avait dit oui, et l'interdit — le
    réflexe naturel devant un chiffre inexpliqué étant justement d'aller
    relancer par courriel, ce que la LCAP interdit."""
    from src.lib import slack

    http_api = _socle(
        monkeypatch,
        interesses=[
            _desabonne(1, prenom="Jean", nom="Roy", courriel="jean@plomberiex.ca",
                       oui="2026-08-12T09:00:00+00:00"),
            _desabonne(2, prenom="Marie", nom="Tremblay", courriel="info@toiturey.ca",
                       oui="2026-08-09T14:30:00+00:00"),
        ],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert (
        "  ⚠️ intéressés désabonnés 2 (cumul) — "
        "Jean Roy <jean@plomberiex.ca> oui le 2026-08-12 · "
        "Marie Tremblay <info@toiturey.ca> oui le 2026-08-09\n"
        f"    {slack.GARDE_LCAP_APRES_DESABONNEMENT}"
    ) in out["text"]


async def test_le_mot_cumul_est_dans_le_libelle(monkeypatch):
    """Ce compteur ne redescend JAMAIS (interested_at est un journal, opted_out
    ne revient pas en arrière) et aucun horodatage fiable du désabonnement
    n'existe pour lui donner une fenêtre. On ne peut pas lui donner une sortie
    honnête — alors on le NOMME, plutôt que de laisser croire à une file de
    travail qui ne se vide pas."""
    http_api = _socle(
        monkeypatch,
        interesses=[_desabonne(1, prenom="Jean", nom="Roy", courriel="j@x.ca",
                               oui="2026-08-12T09:00:00+00:00")],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "intéressés désabonnés 1 (cumul)" in out["text"]
    # la file de travail, elle, garde son libellé sans « cumul » : elle a une sortie
    assert "en attente de site" not in out["text"]


async def test_les_ouis_les_plus_recents_passent_en_tete(monkeypatch):
    """Faute de fenêtre temporelle, le tri par date du oui décroissante est ce
    qui remonte les cas encore actionnables."""
    http_api = _socle(
        monkeypatch,
        interesses=[
            _desabonne(1, prenom="Vieux", nom="Oui", courriel="v@x.ca",
                       oui="2026-05-01T09:00:00+00:00"),
            _desabonne(2, prenom="Frais", nom="Oui", courriel="f@x.ca",
                       oui="2026-08-19T09:00:00+00:00"),
            _desabonne(3, prenom="Moyen", nom="Oui", courriel="m@x.ca",
                       oui="2026-07-04T09:00:00+00:00"),
        ],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert ("Frais Oui <f@x.ca> oui le 2026-08-19 · "
            "Moyen Oui <m@x.ca> oui le 2026-07-04 · "
            "Vieux Oui <v@x.ca> oui le 2026-05-01") in out["text"]


async def test_au_dela_de_cinq_noms_le_reste_est_replie(monkeypatch):
    """Le total reste affiché ; seule la liste est plafonnée — sinon la ligne
    devient un mur illisible dans Slack."""
    http_api = _socle(
        monkeypatch,
        interesses=[
            _desabonne(i, prenom="Lead", nom=f"N{i}", courriel=f"l{i}@x.ca",
                       oui=f"2026-08-{20 - i:02d}T09:00:00+00:00")
            for i in range(1, 8)
        ],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 7
    assert "intéressés désabonnés 7 (cumul)" in out["text"]
    assert "Lead N5 <l5@x.ca> oui le 2026-08-15 · … +2" in out["text"]
    assert "Lead N6" not in out["text"] and "Lead N7" not in out["text"]


async def test_un_contact_sans_nom_retombe_sur_le_courriel(monkeypatch):
    """Un « <vide> <info@x.ca> » ferait douter de la donnée elle-même."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "info@toiturey.ca", "first_name": None,
                     "last_name": "", "interested_at": "2026-08-12T09:00:00+00:00",
                     "status": "opted_out"}],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "— info@toiturey.ca oui le 2026-08-12" in out["text"]
    assert "<" not in out["text"].split("désabonnés 1 (cumul)")[1].split("\n")[0]


# =====================================================================
# Le troisième état : supprimé pour une AUTRE raison (2026-08-23)
# =====================================================================

@pytest.mark.parametrize("motif", ["manual", "competitor", "dncl", "hard_bounce"])
async def test_un_supprime_pour_autre_motif_sort_de_la_file_en_silence(
    monkeypatch, motif
):
    """Le tableau de bord ne doit pas envoyer William produire un site pour un
    prospect que NOUS avons écarté (ou dont l'adresse est morte). Il sort de
    « en attente de site » sans pour autant devenir un désabonné : personne n'a
    retiré de consentement, donc pas d'annonce et surtout pas de garde-fou LCAP
    appliqué à tort."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "a@x.ca", "status": "replied",
                     "interested_at": "2026-08-12T09:00:00+00:00"}],
        demo_par_contact={},
        supprimes={"a@x.ca": motif},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 0
    assert "désabonnés" not in out["text"]
    assert "en attente de site" not in out["text"]
