"""Le résumé quotidien dit l'ÉTAT de la conformité.

`/wf5/run` crie déjà sur #alertes quand un lot n'est pas tout vert. Mais le ping
dit l'INSTANT et le résumé dit l'ÉTAT : une alerte se rate (Slack coupé,
notification balayée à 2 h du matin), un résumé se relit le lendemain matin.
Sans cette ligne, un lot entier pouvait mourir sans qu'un seul chiffre en
subsiste nulle part — et l'absence de ligne se lit « tout vert », ce qui est
exactement le trou nommé par la migration 0045.

C'est le même choix que celui déjà fait pour les désabonnements : le ping WF-7
ET la ligne du résumé, pas l'un OU l'autre.

Le socle patche les MODULES SOURCE (`sb`, `slack_mod`) et non `http_api`, parce
que `summary_daily` les importe LOCALEMENT dans la fonction.
"""
from __future__ import annotations

import pytest

from src.http_api import _ligne_resume_conformite

_MARQUE = "🚫 *Conformité*"
_MARQUE_LECTURE_KO = "lecture des verdicts en ÉCHEC"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")
    # Les dettes PT3 ne sont pas le sujet ici : on les éteint pour garder les
    # assertions de texte lisibles.
    monkeypatch.setenv("DETTE_WF7_REGLEE", "true")
    monkeypatch.setenv("DETTE_ERRORWF_VERIFIEE", "true")


# =====================================================================
# A1 — la fonction pure
# =====================================================================

def test_ligne_resume_conformite():
    assert _ligne_resume_conformite(refuses=4, a_relire=3, non_juges=1) == (
        "🚫 *Conformité* — 4 drafts refusés (dont 3 à relire) · ⚠️ 1 jamais inspecté"
    )


def test_ligne_resume_conformite_silencieuse_si_rien():
    assert _ligne_resume_conformite(refuses=0, a_relire=0, non_juges=0) == ""


def test_ligne_resume_conformite_sans_non_juge():
    assert _ligne_resume_conformite(refuses=2, a_relire=2, non_juges=0) == (
        "🚫 *Conformité* — 2 drafts refusés (dont 2 à relire)"
    )


def test_un_orphelin_seul_parle_quand_meme():
    """L'orphelin sort du lot pour de bon (`passed = false`), donc `/wf5/run`
    ne le reverra jamais et ne le criera qu'UNE fois. Si ce ping-là se rate,
    cette ligne est la seule chose qui reste — sinon fermer la boucle rendrait
    l'anomalie MOINS visible que la boucle qu'elle remplace."""
    ligne = _ligne_resume_conformite(refuses=0, a_relire=0, non_juges=0, orphelins=1)
    assert ligne == (
        "🚫 *Conformité* — 0 drafts refusés (dont 0 à relire) · "
        "🧩 1 sans contact rattaché"
    )


def test_l_orphelin_n_est_pas_fondu_dans_les_refuses():
    """Un refus est un défaut de COPIE, réparable en réécrivant. Un orphelin
    est un défaut de DONNÉES : envoyer relire le courriel serait envoyer
    chercher dans le mauvais fichier."""
    ligne = _ligne_resume_conformite(refuses=2, a_relire=2, non_juges=0, orphelins=3)
    assert "2 drafts refusés" in ligne
    assert "3 sans contact rattaché" in ligne


def test_un_non_juge_seul_parle_quand_meme():
    """Zéro refus mais un corps jamais inspecté : c'est la panne la plus grave
    (des courriels qui partiraient sans relecture) et c'est la seule qui ne
    produit ni `needs_revision` ni `blocked`. Elle doit crier quand même."""
    ligne = _ligne_resume_conformite(refuses=0, a_relire=0, non_juges=2)
    assert ligne == "🚫 *Conformité* — 0 drafts refusés (dont 0 à relire) · ⚠️ 2 jamais inspecté"


# =====================================================================
# A2 — le câblage dans /summary/daily
# =====================================================================

def _socle(monkeypatch, *, refuses=0, a_relire=0, non_juges=0, orphelins=0,
           lecture_leve=False, appels=None):
    """Les compteurs de conformité sont les SEULS que ce socle sert : tout le
    reste du résumé rend 0 pour que le texte reste lisible."""
    from src import http_api
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    async def fake_count(table, params=None, schema=None):
        p = params or {}
        verdict = p.get("compliance_verdict")
        if verdict is None:
            return 0
        if appels is not None:
            appels.append({"table": table, "params": p})
        if lecture_leve:
            raise RuntimeError("boom PostgREST")
        if verdict.startswith("in."):
            return refuses
        if verdict == "eq.needs_revision":
            return a_relire
        if verdict == "eq.non_juge":
            return non_juges
        if verdict == "eq.orphelin":
            return orphelins
        raise AssertionError(f"filtre de verdict inattendu: {verdict}")

    async def fake_select_all(table, order=None, params=None, schema=None, **kw):
        return []

    async def fake_select(table, params=None, schema=None, **kw):
        return []

    monkeypatch.setattr(sb, "count", fake_count)
    monkeypatch.setattr(sb, "select_all", fake_select_all)
    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(slack_mod, "notify", lambda **kw: _vrai())
    return http_api


async def _vrai() -> bool:
    return True


async def _resume(http_api):
    return await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )


async def test_la_ligne_apparait_dans_le_resume(monkeypatch):
    out = await _resume(_socle(monkeypatch, refuses=4, a_relire=3, non_juges=1))
    assert (
        "🚫 *Conformité* — 4 drafts refusés (dont 3 à relire) · ⚠️ 1 jamais inspecté"
        in out["text"]
    )
    assert out["totals"]["conformite"] == {
        "refuses": 4, "a_relire": 3, "non_juges": 1, "orphelins": 0, "lu": True,
    }


async def test_l_orphelin_apparait_dans_le_resume(monkeypatch):
    out = await _resume(_socle(monkeypatch, orphelins=2))
    assert "🧩 2 sans contact rattaché" in out["text"]
    assert out["totals"]["conformite"]["orphelins"] == 2


async def test_rien_a_dire_rien_d_affiche(monkeypatch):
    """Un « refusés : 0 » quotidien est du bruit, et le bruit finit par cacher
    la ligne qui compte."""
    out = await _resume(_socle(monkeypatch))
    assert _MARQUE not in out["text"]
    assert out["totals"]["conformite"]["lu"] is True


async def test_le_compte_passe_par_count_et_jamais_par_len_select(monkeypatch):
    """Le piège maison : PostgREST plafonne à 1000 lignes SANS RIEN SIGNALER, et
    les agrégats côté serveur sont désactivés (PGRST123). Compter en ramenant
    les lignes rendrait « refusés 1000 » pour toujours."""
    appels: list[dict] = []
    selects: list[str] = []
    http_api = _socle(monkeypatch, refuses=1, a_relire=1, appels=appels)

    from src import supabase_client as sb

    async def espion_select(table, params=None, schema=None, **kw):
        selects.append(table)
        return []

    monkeypatch.setattr(sb, "select", espion_select)
    await _resume(http_api)

    assert len(appels) == 4, "quatre compteurs, quatre count() exacts"
    assert all(a["table"] == "messages" for a in appels)
    assert not any(t == "messages" for t in selects), (
        "aucun select sur messages : le compte se fait côté serveur"
    )


async def test_les_compteurs_disent_l_etat_pas_la_journee(monkeypatch):
    """AUCUN filtre de date, comme les désabonnés et le bloc 🧱 : un draft
    refusé il y a trois jours et jamais repris est celui qu'on veut revoir.
    Bornée au jour, la ligne ne ferait que répéter le ping de tantôt."""
    appels: list[dict] = []
    await _resume(_socle(monkeypatch, refuses=2, a_relire=2, appels=appels))
    for a in appels:
        assert "created_at" not in a["params"], a["params"]
        assert "scheduled_at" not in a["params"], a["params"]


async def test_seuls_les_messages_vivants_comptent(monkeypatch):
    """`status=not.in.(failed)` = la définition de « message vivant » déjà posée
    par la migration 0037. C'est la SORTIE du compteur (leçon P4.10) : un draft
    retiré à la main quitte la ligne sans qu'on efface l'histoire du refus."""
    appels: list[dict] = []
    await _resume(_socle(monkeypatch, refuses=2, a_relire=2, appels=appels))
    for a in appels:
        assert a["params"]["status"] == "not.in.(failed)"
        assert a["params"]["direction"] == "eq.outbound"


async def test_les_refuses_couvrent_needs_revision_et_blocked(monkeypatch):
    appels: list[dict] = []
    await _resume(_socle(monkeypatch, refuses=5, a_relire=2, appels=appels))
    filtres = {a["params"]["compliance_verdict"] for a in appels}
    assert "in.(needs_revision,blocked)" in filtres
    assert "eq.needs_revision" in filtres
    assert "eq.non_juge" in filtres


async def test_lecture_en_echec_le_dit_au_lieu_de_faire_une_journee_calme(monkeypatch):
    """Fail-soft, jamais silencieux. Une ligne absente pour cause de panne
    serait indiscernable d'un tout-vert — le mode d'échec que cette ligne
    existe pour éteindre."""
    out = await _resume(_socle(monkeypatch, lecture_leve=True))
    assert _MARQUE_LECTURE_KO in out["text"]
    assert out["totals"]["conformite"]["lu"] is False
    # Le fail-soft protège le RESTE du résumé : il ne doit pas l'emporter.
    assert "📅 RDV bookés" in out["text"]
