"""WF-5 : câblage du verdict `non_juge` et alerte de fin de passe.

`compliance_check` (le tool) sait rendre `non_juge` depuis la livraison
précédente, mais la route qui l'appelle ne lui passait ni `track` ni
`tentatives`, et écrivait `compliance_check_passed = (verdict == "approved")` —
donc `false` sur un `non_juge`. `false` FIGE le draft hors du lot pour toujours
(la requête du lot ne cherche que `is.null`) : la garde des 3 tentatives ne
serait jamais atteinte et une panne passagère du juge deviendrait un refus
définitif.

Les selects sont projetés ici comme PostgREST le fait vraiment — une colonne
absente du `select` est absente de la ligne rendue. C'est ce qui rend les tests
de câblage non-falsifiables : si l'implémentation retire `compliance_tentatives`
du select, la valeur n'arrive plus au juge et le test tombe.
"""
from __future__ import annotations

from typing import Any

import pytest

from src import http_api
from src.http_api import (
    ComplianceCheckIn,
    RunWf5In,
    _doit_alerter_wf5,
    _patch_verdict_conformite,
    _regle_qui_a_tranche,
)
from src.tools import compliance as compliance_tools


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# =====================================================================
# A2 — la fonction pure du patch
# =====================================================================

def test_patch_laisse_passed_null_sur_non_juge():
    p = _patch_verdict_conformite("non_juge", tentatives_avant=0)
    assert p["compliance_verdict"] == "non_juge"
    assert "compliance_check_passed" not in p, "passed doit rester NULL"
    assert p["compliance_tentatives"] == 1


def test_patch_pose_passed_true_sur_approved():
    assert _patch_verdict_conformite("approved", tentatives_avant=0)["compliance_check_passed"] is True


def test_patch_sort_l_orphelin_du_lot():
    """Un message orphelin est une anomalie de DONNÉES : le re-juger cent fois
    ne le réparera jamais. `passed = false` est ce qui le sort du lot, dont la
    requête ne cherche que `is.null`."""
    p = _patch_verdict_conformite("orphelin", tentatives_avant=0)
    assert p["compliance_check_passed"] is False
    assert p["compliance_verdict"] == "orphelin"


def test_patch_pose_passed_false_sur_refus():
    assert _patch_verdict_conformite("needs_revision", tentatives_avant=1)["compliance_check_passed"] is False


def test_patch_incremente_toujours_les_tentatives():
    assert _patch_verdict_conformite("approved", tentatives_avant=2)["compliance_tentatives"] == 3


def test_patch_tolere_des_tentatives_nulles():
    """`compliance_tentatives` absent d'un SELECT rend None : `None + 1` lèverait."""
    assert _patch_verdict_conformite("approved", tentatives_avant=None)["compliance_tentatives"] == 1


# =====================================================================
# B1 — la condition d'alerte
# =====================================================================

def test_alerte_wf5_se_declenche_sur_non_juge_seul():
    assert _doit_alerter_wf5(needs_revision=0, blocked=0, non_juge=1)


def test_alerte_wf5_silencieuse_quand_tout_est_approuve():
    assert not _doit_alerter_wf5(needs_revision=0, blocked=0, non_juge=0)


def test_alerte_wf5_se_declenche_sur_refus():
    assert _doit_alerter_wf5(needs_revision=3, blocked=0, non_juge=0)


def test_alerte_wf5_se_declenche_sur_blocage():
    assert _doit_alerter_wf5(needs_revision=0, blocked=2, non_juge=0)


def test_alerte_wf5_se_declenche_sur_orphelin_seul():
    """Sortir un orphelin du lot SANS le dire le rendrait invisible pour
    toujours : c'est la dernière passe qui le voit."""
    assert _doit_alerter_wf5(needs_revision=0, blocked=0, non_juge=0, orphelins=1)


# =====================================================================
# La règle qui a tranché (ce que l'alerte nomme)
# =====================================================================

def _out(**kw: Any) -> compliance_tools.ComplianceCheckOut:
    base: dict[str, Any] = dict(
        message_id="m", verdict="approved", send_decision="SEND",
    )
    base.update(kw)
    return compliance_tools.ComplianceCheckOut(**base)


def test_regle_nomme_le_bloqueur_deterministe():
    out = _out(
        verdict="blocked", send_decision="DO_NOT_SEND",
        deterministic_blockers=[{"name": "legal_footer", "message": "x", "matches": []}],
    )
    assert _regle_qui_a_tranche(out) == "legal_footer"


def test_regle_nomme_la_panne_du_juge_sur_non_juge():
    out = _out(
        verdict="non_juge", send_decision="DO_NOT_SEND",
        llm_judge={"error": "LLM judge failed: RuntimeError: overloaded"},
    )
    assert _regle_qui_a_tranche(out) == "juge_llm_injoignable"


def test_regle_nomme_la_panne_meme_au_plafond_des_tentatives():
    """3e tentative : le verdict devient `needs_revision`, la cause reste la panne."""
    out = _out(
        verdict="needs_revision", send_decision="REVIEW_THEN_SEND",
        llm_judge={"error": "LLM judge failed: RuntimeError: overloaded"},
    )
    assert _regle_qui_a_tranche(out) == "juge_llm_injoignable"


def test_regle_nomme_le_juge_quand_il_a_repondu_bloque():
    out = _out(
        verdict="blocked", send_decision="DO_NOT_SEND",
        llm_judge={"send_decision": "DO_NOT_SEND"},
    )
    assert _regle_qui_a_tranche(out) == "juge_llm"


def test_regle_nomme_le_warning_deterministe():
    out = _out(
        verdict="needs_revision", send_decision="REVIEW_THEN_SEND",
        deterministic_warnings=[{"name": "length", "message": "x", "matches": []}],
    )
    assert _regle_qui_a_tranche(out) == "length"


def test_regle_nomme_le_motif_de_l_orphelin():
    """« orphelin [orphelin] » n'apprendrait rien à qui lit l'alerte : c'est le
    MOTIF qui dit où chercher en base."""
    out = _out(verdict="orphelin", send_decision="DO_NOT_SEND",
               error_text="contact_not_found")
    assert _regle_qui_a_tranche(out) == "contact_not_found"


def test_regle_retombe_sur_le_verdict_si_rien_ne_le_nomme():
    assert _regle_qui_a_tranche(_out(verdict="approved")) == "approved"


# =====================================================================
# A3/A4 — la route /compliance/check
# =====================================================================

def _projeter(row: dict[str, Any], params: dict[str, str] | None) -> dict[str, Any]:
    """Imite PostgREST : une colonne hors du `select` n'existe pas dans la ligne."""
    demandees = [c.strip() for c in ((params or {}).get("select") or "*").split(",")]
    if "*" in demandees:
        return dict(row)
    return {k: v for k, v in row.items() if k in demandees}


def _socle_check(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: str = "approved",
    tentatives_en_base: int = 0,
    track_en_base: str | None = "agence-ia",
    company_absente: bool = False,
    contact_absent: bool = False,
    message_absent: bool = False,
    update_leve: bool = False,
    update_rend_vide: bool = False,
) -> dict[str, Any]:
    """Rend un dict de capture : `appels` (kwargs reçus par le juge), `maj` (patches)."""
    from src import supabase_client as sb
    from src.lib import calcom as calcom_mod

    capture: dict[str, Any] = {"appels": {}, "maj": []}

    ligne_message = {
        "id": "msg-1", "subject": "Une question", "body_text": "Corps du courriel",
        "contact_id": None if contact_absent else "c-1",
        "generated_by_agent_run": None,
        "compliance_check_passed": None,
        "compliance_tentatives": tentatives_en_base,
    }
    ligne_contact = {
        "id": "c-1", "company_id": "co-1", "first_name": "Marc",
        "last_name": "Tremblay", "title": "Proprio",
        "email_verification_source": "website_scrape",
    }
    ligne_company = {"id": "co-1", "research_json": {"nom": "X"}, "track": track_en_base}

    async def fake_select(table, *, params=None, schema=None, **kw):
        if table == "messages":
            return [] if message_absent else [_projeter(ligne_message, params)]
        if table == "contacts":
            return [] if contact_absent else [_projeter(ligne_contact, params)]
        if table == "companies":
            return [] if company_absente else [_projeter(ligne_company, params)]
        return []

    async def fake_update(table, patch, *, filters=None, schema=None, **kw):
        capture["maj"].append({"table": table, "patch": patch, "filters": filters})
        if update_leve:
            raise RuntimeError("supabase injoignable")
        return [] if update_rend_vide else [{"id": "msg-1"}]

    async def fake_juge(**kw):
        capture["appels"] = kw
        return compliance_tools.ComplianceCheckOut(
            message_id=kw["message_id"], verdict=verdict,
            send_decision="SEND" if verdict == "approved" else "DO_NOT_SEND",
        )

    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(sb, "update", fake_update)
    monkeypatch.setattr(compliance_tools, "compliance_check", fake_juge)
    monkeypatch.setattr(calcom_mod, "get_available_slots", lambda **kw: [])
    return capture


@pytest.mark.asyncio
async def test_route_passe_le_track_reel_au_juge(monkeypatch):
    cap = _socle_check(monkeypatch, track_en_base="agence-ia")
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"]["track"] == "agence-ia"


@pytest.mark.asyncio
async def test_route_ne_force_jamais_le_track_a_opt(monkeypatch):
    """`or "OPT"` ferait vouvoyer un corps tutoyé et bloquerait tout agence-ia."""
    cap = _socle_check(monkeypatch, track_en_base=None)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"]["track"] is None


@pytest.mark.asyncio
async def test_route_sans_company_ne_force_pas_de_track(monkeypatch):
    cap = _socle_check(monkeypatch, company_absente=True)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"]["track"] is None
    assert cap["appels"]["research_json"] == {}


@pytest.mark.asyncio
async def test_route_passe_les_tentatives_lues_en_base(monkeypatch):
    cap = _socle_check(monkeypatch, tentatives_en_base=2)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"]["tentatives"] == 2


@pytest.mark.asyncio
async def test_non_juge_laisse_passed_null_en_base(monkeypatch):
    cap = _socle_check(monkeypatch, verdict="non_juge", tentatives_en_base=1)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    patch = cap["maj"][0]["patch"]
    assert "compliance_check_passed" not in patch
    assert patch["compliance_verdict"] == "non_juge"
    assert patch["compliance_tentatives"] == 2


@pytest.mark.asyncio
async def test_approved_ecrit_passed_true_en_base(monkeypatch):
    cap = _socle_check(monkeypatch, verdict="approved")
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    patch = cap["maj"][0]["patch"]
    assert patch["compliance_check_passed"] is True
    assert patch["compliance_verdict"] == "approved"
    assert "compliance_notes" in patch


@pytest.mark.asyncio
async def test_dry_run_n_ecrit_rien(monkeypatch):
    cap = _socle_check(monkeypatch, verdict="blocked")
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1", persist=False))
    assert cap["maj"] == []


@pytest.mark.asyncio
async def test_echec_decriture_finit_dans_le_journal(monkeypatch):
    """Un verdict non persisté qui se croit persisté est le mode d'échec de 1bfb918."""
    _socle_check(monkeypatch, verdict="blocked", update_leve=True)
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.verdict == "blocked", "le verdict rendu ne change pas"
    assert out.error_text and "persist" in out.error_text


@pytest.mark.asyncio
async def test_ecriture_sans_ligne_touchee_est_un_echec(monkeypatch):
    """PostgREST rend [] quand aucune ligne ne matche : silencieux, mais raté."""
    _socle_check(monkeypatch, verdict="non_juge", update_rend_vide=True)
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.error_text and "persist" in out.error_text


@pytest.mark.asyncio
async def test_ecriture_reussie_ne_salit_pas_le_journal(monkeypatch):
    _socle_check(monkeypatch, verdict="approved")
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.error_text is None


# =====================================================================
# Le message orphelin : la boucle infinie du lot quotidien
# =====================================================================
#
# Le défaut mesuré : quand un message n'a pas de contact rattaché, la route
# sortait tôt avec `verdict="error"` SANS RIEN ÉCRIRE. `compliance_tentatives`
# n'était jamais incrémenté et `compliance_check_passed` restait NULL — or la
# requête du lot ne cherche QUE `is.null`. Le message revenait donc tous les
# jours, indéfiniment, sans jamais atteindre le plafond des 3 tentatives, en
# consommant une place du lot à chaque passe et sans que rien ne le dise.


@pytest.mark.asyncio
async def test_message_sans_contact_ne_juge_rien(monkeypatch):
    """Le juge n'est pas appelé : sans contact, il n'y a rien à inspecter."""
    cap = _socle_check(monkeypatch, contact_absent=True)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"] == {}, "le juge LLM ne doit pas être payé pour ça"


@pytest.mark.asyncio
async def test_message_sans_contact_sort_du_lot(monkeypatch):
    """`compliance_check_passed = false` est ce qui ferme la boucle : la
    requête du lot ne cherche que `is.null`."""
    cap = _socle_check(monkeypatch, contact_absent=True)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert len(cap["maj"]) == 1, "le verdict d'un orphelin DOIT s'écrire"
    patch = cap["maj"][0]["patch"]
    assert patch["compliance_check_passed"] is False
    assert patch["compliance_verdict"] == "orphelin"
    assert patch["compliance_tentatives"] == 1


@pytest.mark.asyncio
async def test_orphelin_est_un_verdict_a_lui_seul(monkeypatch):
    """Pas `error` (la passe n'a pas planté) et pas `needs_revision` (la copie
    n'est pas en cause) : c'est une anomalie de DONNÉES."""
    _socle_check(monkeypatch, contact_absent=True)
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.verdict == "orphelin"
    assert out.send_decision == "DO_NOT_SEND"
    assert out.error_text == "contact_not_found"


@pytest.mark.asyncio
async def test_la_note_de_l_orphelin_dit_pourquoi(monkeypatch):
    """La trace durable : `compliance_notes` se relit en base des mois plus
    tard, quand le ping Slack du jour est oublié depuis longtemps."""
    cap = _socle_check(monkeypatch, contact_absent=True)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    notes = cap["maj"][0]["patch"]["compliance_notes"]
    assert "ORPHELIN" in notes
    assert "contact_not_found" in notes


@pytest.mark.asyncio
async def test_orphelin_en_dry_run_n_ecrit_rien(monkeypatch):
    cap = _socle_check(monkeypatch, contact_absent=True)
    await http_api.compliance_check(
        ComplianceCheckIn(message_id="msg-1", persist=False)
    )
    assert cap["maj"] == []


@pytest.mark.asyncio
async def test_message_disparu_ne_tente_pas_d_ecrire(monkeypatch):
    """La ligne n'existe plus : il n'y a rien à marquer, et un `update` sur
    zéro ligne ne ferait qu'ajouter un faux « persist_failed » au journal. Le
    verdict reste `orphelin` pour que la passe le NOMME quand même."""
    cap = _socle_check(monkeypatch, message_absent=True)
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.verdict == "orphelin"
    assert out.error_text == "message_not_found"
    assert cap["maj"] == []


@pytest.mark.asyncio
async def test_le_non_juge_lui_revient_toujours_dans_le_lot(monkeypatch):
    """Les deux cas DOIVENT coexister : le juge en panne repasse demain
    (`passed` reste NULL), l'orphelin sort pour de bon (`passed = false`).
    Fermer la boucle de l'un ne doit pas fermer celle de l'autre."""
    cap_panne = _socle_check(monkeypatch, verdict="non_juge")
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert "compliance_check_passed" not in cap_panne["maj"][0]["patch"]

    cap_orphelin = _socle_check(monkeypatch, contact_absent=True)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap_orphelin["maj"][0]["patch"]["compliance_check_passed"] is False


# =====================================================================
# B2 — /wf5/run compte les non_juge et crie sur #alertes
# =====================================================================

def _socle_wf5(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdicts: list[tuple[str, compliance_tools.ComplianceCheckOut | None]],
    slack_ok: bool = True,
) -> dict[str, Any]:
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    capture: dict[str, Any] = {"pings": []}

    async def fake_select(table, *, params=None, schema=None, **kw):
        if table == "messages":
            return [{"id": mid, "subject": f"sujet {mid}"} for mid, _ in verdicts]
        return []

    par_id = dict(verdicts)

    async def fake_check(payload: ComplianceCheckIn):
        res = par_id[payload.message_id]
        if res is None:
            raise RuntimeError("boom")
        return res

    async def fake_notify(**kw):
        capture["pings"].append(kw)
        return slack_ok

    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(http_api, "compliance_check", fake_check)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    return capture


def _res(mid: str, verdict: str, **kw: Any) -> compliance_tools.ComplianceCheckOut:
    return _out(message_id=mid, verdict=verdict,
                send_decision="SEND" if verdict == "approved" else "DO_NOT_SEND", **kw)


@pytest.mark.asyncio
async def test_wf5_compte_les_non_juge_a_part_des_erreurs(monkeypatch):
    _socle_wf5(monkeypatch, verdicts=[
        ("a", _res("a", "approved")),
        ("b", _res("b", "non_juge", llm_judge={"error": "x"})),
    ])
    out = await http_api.run_wf5(RunWf5In(limit=10))
    assert out.non_juge == 1
    assert out.errors == 0
    assert out.approved == 1


@pytest.mark.asyncio
async def test_wf5_alerte_sur_non_juge_seul(monkeypatch):
    """Sans `non_juge` dans la condition, la panne la plus grave serait muette."""
    cap = _socle_wf5(monkeypatch, verdicts=[
        ("id-approuve", _res("id-approuve", "approved")),
        ("id-non-juge", _res("id-non-juge", "non_juge", llm_judge={"error": "overloaded"})),
    ])
    out = await http_api.run_wf5(RunWf5In(limit=10))
    assert len(cap["pings"]) == 1
    ping = cap["pings"][0]
    assert ping["category"] == "alerts"
    assert "non_juge : 1" in ping["text"]
    assert "id-non-juge" in ping["text"]
    assert "id-approuve" not in ping["text"], "un draft approuvé n'a rien à faire dans l'alerte"
    assert "juge_llm_injoignable" in ping["text"]
    assert out.alerte_envoyee is True


@pytest.mark.asyncio
async def test_wf5_silencieux_quand_tout_est_approuve(monkeypatch):
    cap = _socle_wf5(monkeypatch, verdicts=[("a", _res("a", "approved"))])
    out = await http_api.run_wf5(RunWf5In(limit=10))
    assert cap["pings"] == []
    assert out.alerte_envoyee is None


@pytest.mark.asyncio
async def test_wf5_alerte_porte_les_trois_compteurs(monkeypatch):
    cap = _socle_wf5(monkeypatch, verdicts=[
        ("a", _res("a", "needs_revision",
                   deterministic_warnings=[{"name": "length", "message": "m", "matches": []}])),
        ("b", _res("b", "blocked",
                   deterministic_blockers=[{"name": "legal_footer", "message": "m", "matches": []}])),
        ("c", _res("c", "non_juge", llm_judge={"error": "x"})),
    ])
    await http_api.run_wf5(RunWf5In(limit=10))
    texte = cap["pings"][0]["text"]
    assert "needs_revision : 1" in texte
    assert "blocked : 1" in texte
    assert "non_juge : 1" in texte
    assert "length" in texte and "legal_footer" in texte and "juge_llm_injoignable" in texte


@pytest.mark.asyncio
async def test_wf5_alerte_liste_au_plus_cinq_message_id(monkeypatch):
    cap = _socle_wf5(monkeypatch, verdicts=[
        (f"m{i}", _res(f"m{i}", "blocked",
                       deterministic_blockers=[{"name": "legal_footer", "message": "m", "matches": []}]))
        for i in range(8)
    ])
    await http_api.run_wf5(RunWf5In(limit=10))
    texte = cap["pings"][0]["text"]
    assert texte.count("legal_footer") == 5
    assert "m5" not in texte and "m7" not in texte


@pytest.mark.asyncio
async def test_wf5_lit_le_retour_de_slack(monkeypatch):
    """Une alerte non partie qui se croit partie est le mode d'échec de 57edcaf."""
    _socle_wf5(monkeypatch, slack_ok=False, verdicts=[
        ("a", _res("a", "non_juge", llm_judge={"error": "x"})),
    ])
    out = await http_api.run_wf5(RunWf5In(limit=10))
    assert out.alerte_envoyee is False


@pytest.mark.asyncio
async def test_wf5_lot_vide_ne_crie_pas(monkeypatch):
    cap = _socle_wf5(monkeypatch, verdicts=[])
    out = await http_api.run_wf5(RunWf5In(limit=10))
    assert cap["pings"] == []
    assert out.processed == 0
    assert out.alerte_envoyee is None


@pytest.mark.asyncio
async def test_wf5_compte_les_orphelins_a_part(monkeypatch):
    """Ni `errors` (la passe n'a pas planté) ni `blocked` (la copie n'est pas
    en cause) : fondu dans l'un ou l'autre, un orphelin enverrait relire un
    courriel alors que le défaut est en base."""
    _socle_wf5(monkeypatch, verdicts=[
        ("a", _res("a", "approved")),
        ("b", _res("b", "orphelin", error_text="contact_not_found")),
    ])
    out = await http_api.run_wf5(RunWf5In(limit=10))
    assert out.orphelins == 1
    assert out.errors == 0
    assert out.blocked == 0
    assert out.approved == 1


@pytest.mark.asyncio
async def test_wf5_crie_le_message_id_de_l_orphelin(monkeypatch):
    """La boucle se ferme ET se dit : c'est la dernière fois que ce message
    passe devant la conformité, donc la dernière occasion de le nommer."""
    cap = _socle_wf5(monkeypatch, verdicts=[
        ("id-orphelin", _res("id-orphelin", "orphelin", error_text="contact_not_found")),
    ])
    out = await http_api.run_wf5(RunWf5In(limit=10))
    assert len(cap["pings"]) == 1
    texte = cap["pings"][0]["text"]
    assert "id-orphelin" in texte
    assert "contact_not_found" in texte
    assert "orphelin : 1" in texte
    assert out.alerte_envoyee is True


@pytest.mark.asyncio
async def test_wf5_exception_de_passe_reste_comptee_en_erreurs(monkeypatch):
    """Le COMPTEUR reste distinct des verdicts de refus, et c'est ce que ce
    test protège depuis le début : `errors` n'est ni `non_juge` ni `blocked`.

    ⚠️ **Ce qui a changé le 2026-08-30, et pourquoi ce n'est pas un test
    assoupli.** L'assertion `cap["pings"] == []` exigeait le SILENCE sur une
    erreur de passe. Elle tenait tant que `errors` ne comptait que des
    exceptions isolées sur un draft. Depuis le layer 0 de conformité, une
    CONFIGURATION LCAP incomplète rend `error` sur **tout le lot** : la seule
    panne qui arrête l'envoi en entier serait devenue la seule totalement
    muette. Et le workflow n8n WF-5 ne porte aucun nœud d'alerte, donc le
    silence aurait été complet, pas seulement côté serveur.

    Le contrat testé se déplace donc d'un cran : `errors` garde son compteur
    propre — ce qui est l'objet du test — mais il crie.
    """
    cap = _socle_wf5(monkeypatch, verdicts=[("a", None)])
    out = await http_api.run_wf5(RunWf5In(limit=10))
    assert out.errors == 1
    assert out.non_juge == 0, "errors ne doit jamais être confondu avec non_juge"
    assert cap["pings"], "une erreur de passe ne doit pas être silencieuse"
