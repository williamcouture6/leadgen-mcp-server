"""Les avis Google traversent-ils les quatre points jusqu'au juge ?

`google_rating` et `google_reviews_count` n'étaient lus NULLE PART dans la
chaîne. Le seul « rating » visible était `research_json.recent_review_snippet
.rating`, qui est la note d'UN SEUL avis. Sans ce câblage, deux issues
seulement : soit le bloc 2 saute 255 fois sur 255, soit le modèle écrit
« 5 étoiles sur 47 avis » — inventé — et le juge est aveugle par construction.

Un test par point. Un maillon qui saute doit casser ici, pas en production.
"""
from __future__ import annotations

from typing import Any

import pytest

import src.tools.db as dbt
from src.tools import compliance as comp
from src.tools import personalize as perso


# ---------------- Point 1 : le SELECT de db.py ----------------

@pytest.mark.asyncio
async def test_le_select_des_companies_ramene_les_avis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans les colonnes dans le `select`, PostgREST ne les rend pas et tout
    le reste du câblage lit `None` en silence."""
    captures: dict[str, str] = {}

    async def fake_select(table: str, params: dict[str, Any] | None = None) -> list[dict]:
        params = params or {}
        if table == "contacts":
            return [{"id": "ct-1", "company_id": "co-1", "email": "a@ex.ca", "status": "new"}]
        if table == "companies":
            captures["companies"] = params.get("select", "")
            return [{"id": "co-1", "name": "Ex", "track": "agence-ia", "research_json": {"x": 1}}]
        return []

    monkeypatch.setattr(dbt.db, "select", fake_select)
    await dbt.list_contacts_to_personalize(limit=1, track="agence-ia")

    select = captures["companies"]
    for colonne in ("google_rating", "google_reviews_count", "google_place_id"):
        assert colonne in select, f"{colonne} absente du select : {select}"


# ---------------- Point 2 : le dict `company` passé au rédacteur ----------------

@pytest.mark.asyncio
async def test_le_dict_company_porte_les_avis(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 Le point que le fichier ANNONÇAIT couvrir et qui n'avait aucun test.

    Vérifié par mutation le 2026-08-30 : retirer les trois lignes `google_*`
    du dict `company=` de `_personalize_one` laissait les 1054 tests VERTS.
    En production, `bloc_faits_verifies` aurait alors annoncé au rédacteur
    « aucune note et aucun avis en base » pour les 166 entreprises qui en ont —
    exactement l'issue « le bloc saute 255 fois sur 255 » que la tâche 7 existe
    pour empêcher. Et le juge, lui, recevait les vraies valeurs, donc rien ne
    criait.
    """
    from src import http_api

    capture: dict[str, Any] = {}

    async def faux_personalize(payload):
        capture["company"] = payload.company
        raise RuntimeError("on s'arrête ici, le payload est capturé")

    monkeypatch.setattr(http_api.personalize_tools, "personalize", faux_personalize)

    await http_api._personalize_one(
        {"id": "ct-1", "email": "a@ex.ca"},
        {
            "id": "co-1", "name": "Ex", "website": "https://ex.ca", "city": "Lévis",
            "research_json": {"x": 1}, "track": "agence-ia",
            "google_rating": 4.8, "google_reviews_count": 47, "google_place_id": "ChIJ",
        },
        template_choice="A", model="m", persist=False,
        available_slots=[], social_proof=[],
    )

    company = capture["company"]
    assert company["google_rating"] == 4.8
    assert company["google_reviews_count"] == 47
    assert company["google_place_id"] == "ChIJ"


# ---------------- Point 3 : le message envoyé au rédacteur ----------------

def _message_redacteur(**avis: Any) -> str:
    return perso._format_input_for_llm(
        research={"services_offered": ["tonte"]},
        company={"name": "Paysagement Rivard", "website": "https://ex.ca", **avis},
        contact=None,
        social_proof=[],
        template_choice="A",
        slots_block="",
    )


def test_le_redacteur_recoit_les_avis_dans_un_bloc_de_faits_verifies() -> None:
    msg = _message_redacteur(google_rating=4.8, google_reviews_count=47)
    assert "Faits vérifiés" in msg
    assert "4,8" in msg or "4.8" in msg
    assert "47" in msg


def test_le_redacteur_est_prevenu_quand_il_ny_a_pas_davis() -> None:
    """Le silence serait interprété comme « pas encore cherché ». Il faut dire
    explicitement qu'il n'y en a pas, sinon le modèle comble le vide."""
    msg = _message_redacteur(google_rating=None, google_reviews_count=None)
    assert "Faits vérifiés" in msg
    assert "aucune note" in msg.lower() or "aucun avis" in msg.lower()


def test_les_avis_ne_sont_pas_noyes_dans_le_json_de_recherche() -> None:
    """Un fait qui doit être recopié au mot près ne se met pas au milieu d'un
    JSON de 80 lignes : le bloc « Faits vérifiés » est distinct et court."""
    msg = _message_redacteur(google_rating=4.8, google_reviews_count=47)
    debut_faits = msg.index("Faits vérifiés")
    debut_json = msg.index("research_json")
    assert debut_faits < debut_json, "les faits vérifiés passent AVANT le JSON brut"


# ---------------- Point 4 : le message envoyé au juge ----------------

def test_le_juge_recoit_les_avis_dans_un_bloc_de_faits_verifies() -> None:
    """🔴 C'est le point qui ferme le bug de 0732d20. Le juge ne peut pas
    déclarer un chiffre inventé s'il ne connaît pas la valeur vraie."""
    msg = comp._message_utilisateur_juge(
        body="Paysagement Rivard a 4,8 étoiles sur 47 avis.",
        subject="test",
        research_json={},
        social_proof=[],
        contact=None,
        google_rating=4.8,
        google_reviews_count=47,
    )
    assert "Faits vérifiés" in msg
    assert "4,8" in msg and "47" in msg


def test_le_juge_est_prevenu_quand_aucun_avis_nexiste() -> None:
    msg = comp._message_utilisateur_juge(
        body="Du monde qui te cherche, t'en as.",
        subject="test",
        research_json={},
        social_proof=[],
        contact=None,
        google_rating=None,
        google_reviews_count=None,
    )
    assert "Faits vérifiés" in msg
    assert "aucune note" in msg.lower() or "aucun avis" in msg.lower()


# ---------------- Bout en bout : la valeur de colonne arrête un chiffre faux ----------------

@pytest.fixture
def _env_vert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARMUP_DISABLED", "true")
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture IA")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    monkeypatch.setenv("LCAP_MENTIONS_REDUITES", "true")
    monkeypatch.setenv(
        "INSTANTLY_CAMPAIGN_FOOTER",
        "Couture IA\nPour te désabonner : https://couture-ia.com/unsubscribe",
    )


_CORPS = (
    "Bonjour,\n\nÇa doit t'arriver de pas pouvoir répondre. Paysagement Rivard "
    "a 4,8 étoiles sur 47 avis. Dis-moi juste si tu veux le voir.\n"
)


@pytest.mark.asyncio
async def test_un_chiffre_conforme_ne_bloque_pas(_env_vert: None) -> None:
    out = await comp.compliance_check(
        message_id="m1", body=_CORPS, subject="s", template_used="A",
        research_json={}, social_proof=[], available_slots=[], skip_llm=True,
        track="agence-ia", google_rating=4.8, google_reviews_count=47,
    )
    noms = [b["name"] for b in out.deterministic_blockers]
    assert "avis_conformes" not in noms, noms


@pytest.mark.asyncio
async def test_un_chiffre_faux_bloque_de_bout_en_bout(_env_vert: None) -> None:
    """La colonne dit 12 avis, le corps en annonce 47 : le draft ne part pas."""
    out = await comp.compliance_check(
        message_id="m1", body=_CORPS, subject="s", template_used="A",
        research_json={}, social_proof=[], available_slots=[], skip_llm=True,
        track="agence-ia", google_rating=4.8, google_reviews_count=12,
    )
    assert out.verdict == "blocked"
    assert "avis_conformes" in [b["name"] for b in out.deterministic_blockers]


# ---------------- Le plancher, garde par le determinisme et non par le LLM ----------------
#
# Trouve par le conseil du 2026-08-30 : `check_avis_conformes` ne comparait le
# chiffre qu'a la COLONNE, jamais a l'AUTORISATION. Une note sous le plancher,
# recopiee fidelement par le modele, passait au vert parce qu'elle etait VRAIE.
# Le plancher n'etait donc garde que par l'obeissance du LLM -- exactement ce
# que la docstring du check dit refuser.

@pytest.mark.parametrize(
    "note,nb_avis,autorise,cas",
    [
        (4.8, 47, True, "au-dessus des deux seuils"),
        (2.3, 27, False, "A.M.G. Neige — note sous 4,0"),
        (2.9, 504, False, "Groupe Essa — 504 avis mais 2,9"),
        (5.0, 1, False, "Nettoyage PUR — 5,0 sur UN SEUL avis"),
        (3.0, 2, False, "Herbofleurs — les deux seuils ratés"),
        (4.0, 10, True, "exactement aux deux seuils"),
        (4.0, 9, False, "un avis sous le seuil"),
        (3.9, 10, False, "un dixième sous le seuil"),
        (None, None, False, "aucune donnée"),
        (4.8, None, False, "note sans compte"),
        (None, 47, False, "compte sans note"),
    ],
)
def test_le_plancher_du_bloc_2(note, nb_avis, autorise, cas) -> None:
    """Le plancher lui-même : 10 avis ET 4,0. Sans ces cas, une régression de
    la constante passait inaperçue — vérifié par mutation, la mettre à zéro
    laissait les 1054 tests verts."""
    from src.lib.avis import bloc_avis_autorise

    assert bloc_avis_autorise(note, nb_avis) is autorise, cas


@pytest.mark.parametrize(
    "note,nb_avis",
    [(2.3, 27), (2.9, 504), (5.0, 1), (3.0, 2)],
)
def test_une_note_sous_le_plancher_est_BLOQUEE_meme_si_elle_est_vraie(note, nb_avis) -> None:
    """🔴 Le cœur du correctif. Le chiffre est EXACT — il vient de la colonne —
    et il doit quand même bloquer, parce que le corps devait servir le repli.

    Sans ça, le prospect reçoit sa propre mauvaise note en pleine face, juste
    avant « c'est probablement pas parce que le monde t'aime pas », qui se lit
    alors comme du sarcasme. 83 des 255 envoyables sont dans ce cas.
    """
    from src.lib import compliance_checks as cc

    corps = f"Bonjour,\n\nA.M.G. Neige a {str(note).replace('.', ',')} étoiles sur {nb_avis} avis. Dis-moi."
    r = cc.check_avis_conformes(corps, google_rating=note, google_reviews_count=nb_avis)
    assert not r.passed, f"{note}/{nb_avis} : chiffre exact mais citation interdite"
    assert r.severity == "block"
    assert any("plancher" in m for m in r.matches)


def test_le_repli_passe_meme_sous_le_plancher() -> None:
    """Le corps qui n'annonce AUCUN chiffre est exactement ce que le plancher
    demande : il doit passer, pas être puni."""
    from src.lib import compliance_checks as cc

    corps = "Bonjour,\n\nDu monde qui te cherche, t'en as. Dis-moi."
    assert cc.check_avis_conformes(corps, google_rating=2.3, google_reviews_count=27).passed


def test_le_juge_garde_son_retry_sur_les_529() -> None:
    """🔴 Défaut introduit puis attrapé pendant AC1b : en extrayant le
    constructeur de message, le décorateur `@retry` s'est retrouvé sur LUI au
    lieu de rester sur `_llm_judge`. Le juge aurait perdu ses 5 tentatives
    avec backoff sur les 529 d'Anthropic — la panne exacte que WF-3 a déjà
    payée, et celle contre laquelle tout le fail-closed d'AC1a existe.

    Un décorateur déplacé ne casse aucun test métier : il faut celui-ci.
    """
    assert hasattr(comp._llm_judge, "retry"), (
        "_llm_judge a perdu son décorateur @retry"
    )
    assert not hasattr(comp._message_utilisateur_juge, "retry"), (
        "le constructeur de message n'a rien à réessayer"
    )


@pytest.mark.asyncio
async def test_sans_valeurs_de_colonne_un_chiffre_bloque(_env_vert: None) -> None:
    """Le défaut est fermé : si le câblage saute quelque part en amont, les
    valeurs arrivent à None et le draft est refusé plutôt qu'envoyé avec un
    chiffre que personne n'a vérifié."""
    out = await comp.compliance_check(
        message_id="m1", body=_CORPS, subject="s", template_used="A",
        research_json={}, social_proof=[], available_slots=[], skip_llm=True,
        track="agence-ia",
    )
    assert out.verdict == "blocked"
