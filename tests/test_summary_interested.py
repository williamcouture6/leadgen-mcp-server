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
    sur "contacts" — sert au test qui vérifie le filtre d'exclusion de statut.
    supprimes : courriels présents dans `suppression_list` (chemin du clic sur
    le lien du footer, qui ne garantit PAS que contacts.status ait basculé).
    Ensemble = motif `opt_out` implicite ; dict courriel -> motif pour tester
    les motifs qui ne sont PAS un retrait de consentement (`hard_bounce`).

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
            # Le filtre de motif est la moitié du contrat : `hard_bounce` vit
            # dans la même table sans être un retrait de consentement. On le
            # simule ici, faute de quoi le test ne prouverait rien.
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
