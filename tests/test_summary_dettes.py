"""PT3 — le résumé quotidien rappelle lui-même les dettes qu'on a assumées.

Les deux dettes de PT3 sont écrites dans `docs/go-live-checklist.md`. Mais une
dette consignée dans un fichier que personne ne rouvre au bon moment n'existe
pas : ce projet a déjà payé ce mode d'échec deux fois (le runbook qui disait
l'inverse de la réalité sur `WARMUP_END_DATE`, la panne Google Places restée
invisible cinq semaines). William est seul ; le jour où la dette devient
exigible, il vend, il ne relit pas une checklist de l'été.

Le résumé le lui dit donc lui-même, au moment où ça devient vrai.

Le socle patche les MODULES SOURCE (`sb`, `slack_mod`) et non `http_api`,
parce que `summary_daily` les importe LOCALEMENT dans la fonction.
"""
from __future__ import annotations

import pytest

# Empreintes stables des deux rappels — les tests ne recopient pas la phrase
# entière (elle se reformulera), seulement ce qui la rend reconnaissable.
_MARQUE_DETTE_A = "corriger le ping WF-7"
_MARQUE_DETTE_B = "Error Workflow"
_MARQUE_LECTURE_KO = "lecture de contacts.interested_at en ÉCHEC"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    # Les deux variables d'extinction sont ABSENTES par défaut, comme en prod :
    # c'est l'état dans lequel les rappels doivent s'afficher (fail-safe).
    monkeypatch.delenv("DETTE_WF7_REGLEE", raising=False)
    monkeypatch.delenv("DETTE_ERRORWF_VERIFIEE", raising=False)


def _socle(monkeypatch, *, des_oui: int = 0, lecture_oui_leve: bool = False, appels=None):
    """des_oui : nombre de contacts portant `interested_at` (tous tracks).
    lecture_oui_leve : la lecture de cette condition lève.
    appels : liste où empiler les appels à `count()` (pour vérifier la forme
    de la requête — un agrégat serveur, pas un select qu'on compterait)."""
    from src import http_api
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    async def fake_count(table, params=None, schema=None):
        p = params or {}
        if appels is not None:
            appels.append({"table": table, "params": p, "schema": schema})
        if table == "contacts" and "interested_at" in p:
            if lecture_oui_leve:
                raise RuntimeError("boom PostgREST")
            return des_oui
        return 0

    async def fake_select_all(table, order=None, params=None, schema=None, **kw):
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


async def _resume(http_api):
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    return out["text"]


# ------------------------------------------------------------- Dette A (WF-7)

async def test_la_dette_du_ping_wf7_apparait_des_le_premier_oui(monkeypatch):
    """Condition d'apparition : au moins un `contacts.interested_at` posé. Le
    scénario du double-réponse devient possible ce jour-là, pas avant."""
    http_api = _socle(monkeypatch, des_oui=1)
    texte = await _resume(http_api)
    assert _MARQUE_DETTE_A in texte
    assert "4bis" in texte, "le rappel doit dire OÙ lire le détail et le piège"


async def test_la_dette_du_ping_wf7_s_eteint_avec_la_variable(monkeypatch):
    """Poser la variable est le geste qui éteint le rappel — pas un commentaire
    dans un fichier, pas une ligne cochée dans une checklist."""
    monkeypatch.setenv("DETTE_WF7_REGLEE", "true")
    http_api = _socle(monkeypatch, des_oui=3)
    assert _MARQUE_DETTE_A not in await _resume(http_api)


async def test_sans_aucun_oui_la_dette_du_ping_ne_pollue_pas_le_resume(monkeypatch):
    """L'état d'aujourd'hui : zéro courriel parti, donc zéro oui. Un rappel qui
    s'afficherait des mois avant d'être actionnable est un rappel qu'on apprend
    à ignorer — et alors il ne sert plus le jour J."""
    http_api = _socle(monkeypatch, des_oui=0)
    assert _MARQUE_DETTE_A not in await _resume(http_api)


async def test_la_condition_du_ping_se_lit_par_un_agregat_serveur(monkeypatch):
    """`count()` et non `select_all()` : la question est « y en a-t-il AU MOINS
    un ? ». Ramener les lignes pour les compter en Python, c'est le N+1 et le
    plafond PostgREST de 1000 réunis. Les agrégats PostgREST (`select=count()`)
    sont désactivés sur ce projet (PGRST123) — d'où `sb.count`, qui lit
    Content-Range. Et pas de filtre `track` : le premier oui compte quel que
    soit le track."""
    appels: list[dict] = []
    http_api = _socle(monkeypatch, des_oui=1, appels=appels)
    await _resume(http_api)
    dette = [a for a in appels if a["table"] == "contacts" and "interested_at" in a["params"]]
    assert len(dette) == 1, "une seule lecture, hors de la boucle par track"
    assert dette[0]["params"]["interested_at"] == "not.is.null"
    assert "track" not in dette[0]["params"]
    assert "created_at" not in dette[0]["params"], "c'est un ÉTAT, pas l'activité du jour"


async def test_une_lecture_en_echec_de_la_condition_est_dite(monkeypatch):
    """Fail-soft, jamais silencieux : le résumé continue, mais il DIT qu'il n'a
    pas pu décider. Sinon l'absence de rappel serait indiscernable d'une panne
    — exactement le mode d'échec que ce bloc existe pour éteindre."""
    http_api = _socle(monkeypatch, lecture_oui_leve=True)
    texte = await _resume(http_api)
    assert _MARQUE_LECTURE_KO in texte
    assert "📅 RDV bookés" in texte, "le reste du résumé doit survivre à la panne"


# --------------------------------------------- Dette B (Error Workflow n8n)

async def test_la_dette_du_workflow_d_erreur_est_rappelee_par_defaut(monkeypatch):
    """Elle n'a pas de condition d'apparition : elle est vraie tant que
    personne n'a ouvert le menu n8n. Fail-safe — variable absente ⇒ rappel."""
    http_api = _socle(monkeypatch)
    texte = await _resume(http_api)
    assert _MARQUE_DETTE_B in texte
    assert "weHbzb97xdjo2OEd" in texte, "l'id à comparer doit être dans le rappel"


async def test_la_dette_du_workflow_d_erreur_s_eteint_avec_la_variable(monkeypatch):
    monkeypatch.setenv("DETTE_ERRORWF_VERIFIEE", "true")
    assert _MARQUE_DETTE_B not in await _resume(_socle(monkeypatch))


async def test_les_deux_variables_posees_ne_laissent_aucun_bloc(monkeypatch):
    """Le rappel doit pouvoir DISPARAÎTRE entièrement : un bloc résiduel vide
    (un en-tête sans ligne) rendrait l'extinction douteuse."""
    monkeypatch.setenv("DETTE_WF7_REGLEE", "true")
    monkeypatch.setenv("DETTE_ERRORWF_VERIFIEE", "true")
    texte = await _resume(_socle(monkeypatch, des_oui=5))
    assert "Dette PT3" not in texte
    assert "\n\n" not in texte, "aucune ligne vide laissée derrière"
    assert texte.rstrip("\n") == texte


async def test_le_bloc_des_dettes_est_le_dernier_du_resume(monkeypatch):
    """Placé APRÈS les motifs 🧱/🔎 : c'est une note de bas de page, pas une
    information du jour — elle ne doit pas repousser les leads chauds."""
    from src import supabase_client as sb

    http_api = _socle(monkeypatch, des_oui=1)

    async def _motifs(table, order=None, params=None, schema=None, **kw):
        if table == "v_pourquoi_pas_de_courriel":
            return [{"motif": "aucun_contact", "recontactable": "plus_tard"}] * 3
        return []

    monkeypatch.setattr(sb, "select_all", _motifs)
    texte = await _resume(http_api)
    assert texte.index("🧱") < texte.index(_MARQUE_DETTE_A)
    assert texte.index("📅 RDV bookés") < texte.index(_MARQUE_DETTE_A)
