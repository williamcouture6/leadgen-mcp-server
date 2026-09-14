"""La 3ᵉ version du 2ᵉ paragraphe de C et D — la supposition sur le rush.

🔴 CE QU'ELLE RÉPARE. Le 2ᵉ paragraphe de C et D avait deux versions : citer la
note (« je vois que tu as 4,8 étoiles sur 47 avis »), ou énumérer les services
(« je vois que tu fais autant X que Y pis Z »). Une entreprise qui n'a NI note
citable NI deux services ne pouvait recevoir ni l'une ni l'autre — alors le code
lui refusait le GABARIT, faute de PHRASE. Mesuré le 2026-09-14 : 7 entreprises,
qui ne pouvaient donc tirer que A ou B, soit ~2 % du test A/B amputé d'un côté.

William a écrit la phrase manquante le 2026-09-14, après avoir lu trois formes
côte à côte : « J'imagine qu'à la première bordée, ça rentre pas mal tout en
même temps! » Une par métier, servie par le code.

⚠️ Ces tests tiennent les DEUX bouts. Une phrase fixe que le juge ne reconnaît
pas fait refuser le brouillon, et un refus gèle le contact à vie — c'est
arrivé deux fois. Le rédacteur doit donc recevoir la consigne, le juge doit
recevoir la permission, et les deux doivent lire le MÊME bloc.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from src.lib.avis import bloc_faits_verifies
from src.lib.gabarits import tete_fixe_servable_pour_entreprise
from src.lib.lexique_metiers import METIERS_COUVERTS, REPLI, lexique_pour
from src.tools import compliance as juge
from src.tools import personalize as perso

SEPTEMBRE = date(2026, 9, 14)

# Deneigement St-Cyr, telle qu'en base : un service, 5,0 mais sur 5 avis.
UN_SEUL_SERVICE = {
    "name": "Deneigement St-Cyr",
    "city": "Laval",
    "research_json": {"services_offered": ["Déneigement"]},
    "google_rating": 5.0,
    "google_reviews_count": 5,
}
DEUX_SERVICES = {
    **UN_SEUL_SERVICE,
    "research_json": {"services_offered": ["Déneigement", "Tonte de pelouse"]},
}
NOTE_CITABLE = {**UN_SEUL_SERVICE, "google_rating": 4.8, "google_reviews_count": 47}


def _bloc_du_redacteur(company: dict) -> str:
    """Le vrai chemin — `_format_input_for_llm`, pas `bloc_faits_verifies`.

    🔴 Tester le helper seul ne prouve RIEN sur la production : la semaine du
    2026-09-08 a produit deux gardes vertes en isolation et mortes sur le
    chemin réel. Ce qui compte, c'est ce que le rédacteur reçoit.
    """
    return perso._format_input_for_llm(
        company=company,
        research=company["research_json"],
        contact={"first_name": "Marc", "email": "marc@st-cyr.test"},
        social_proof=[],
        slots_block="",
        track="agence-ia",
        template_choice="C",
        aujourdhui=SEPTEMBRE,
    )


# ==================================================== 1. LE RÉDACTEUR ======


def test_un_seul_service_declenche_la_troisieme_version() -> None:
    bloc = _bloc_du_redacteur(UN_SEUL_SERVICE)
    assert "TROISIÈME version" in bloc
    assert "J'imagine qu'à la première bordée, ça rentre pas mal tout en même temps!" in bloc


def test_deux_services_gardent_l_enumeration() -> None:
    """Contrôle négatif : la supposition ne remplace pas ce qui marche."""
    bloc = _bloc_du_redacteur(DEUX_SERVICES)
    assert "TROISIÈME version" not in bloc
    assert "version de repli" in bloc


def test_une_note_citable_prime_sur_tout() -> None:
    """Deuxième contrôle négatif : avec 4,8 sur 47 avis, on cite — même à un
    seul service. La supposition est le DERNIER recours, pas le premier."""
    bloc = _bloc_du_redacteur(NOTE_CITABLE)
    assert "TROISIÈME version" not in bloc
    assert "Tu PEUX citer" in bloc


def test_la_phrase_suit_le_metier() -> None:
    """Un paysagiste ne reçoit pas la phrase du déneigeur."""
    paysagiste = {
        **UN_SEUL_SERVICE,
        "research_json": {"services_offered": ["Aménagement paysager"]},
    }
    bloc = _bloc_du_redacteur(paysagiste)
    assert "J'imagine qu'au printemps" in bloc
    assert "première bordée" not in bloc


# ========================================================= 2. LE JUGE ======


def test_le_juge_lit_exactement_le_meme_bloc_que_le_redacteur() -> None:
    """🔴 LE TEST QUI COMPTE. Si le juge lit « sers la version de repli » sous
    un courriel qui porte la supposition, il conclut à un écart au gabarit —
    et un refus gèle le contact à vie."""
    vu_par_le_juge = juge._message_utilisateur_juge(
        body="peu importe",
        subject="peu importe",
        research_json=UN_SEUL_SERVICE["research_json"],
        social_proof=[],
        contact={"first_name": "Marc"},
        google_rating=UN_SEUL_SERVICE["google_rating"],
        google_reviews_count=UN_SEUL_SERVICE["google_reviews_count"],
    )
    consigne = bloc_faits_verifies(
        UN_SEUL_SERVICE["google_rating"],
        UN_SEUL_SERVICE["google_reviews_count"],
        nb_services=1,
        phrase_du_rush=lexique_pour("déneigement").phrase_du_rush,
    )
    assert consigne in vu_par_le_juge


def test_la_permission_du_juge_nomme_la_phrase() -> None:
    """Sans §1undecies, le juge voit une affirmation qu'aucun research_json ne
    confirme — le motif exact de ses refus."""
    prompt = (Path(__file__).parent.parent / "src/prompts/compliance.md").read_text(
        encoding="utf-8"
    )
    assert "1undecies." in prompt
    assert "J'imagine qu'à la première bordée" in prompt
    assert "§1undecies" in prompt, "la permission doit aussi être annoncée en tête"


def test_le_gabarit_porte_la_troisieme_version() -> None:
    prompt = (
        Path(__file__).parent.parent / "src/prompts/reacti/personalize.md"
    ).read_text(encoding="utf-8")
    plat = " ".join(prompt.split())
    assert "{PHRASE_DU_RUSH}" in plat
    assert "trois versions" in plat
    assert "Si elle ne l'est PAS et qu'il n'y a qu'UN SEUL service" in plat
    assert "porte au moins deux libellés** — sinon le code" not in plat, (
        "l'ancienne consigne affirmait le contraire de la nouvelle règle"
    )


# ====================================================== 3. LE LEXIQUE ======


@pytest.mark.parametrize("metier", sorted(METIERS_COUVERTS))
def test_chaque_metier_a_sa_phrase(metier: str) -> None:
    """Un métier sans phrase retomberait sur le repli, et le courriel parlerait
    de « début de saison » à un couvreur. Le test le rend impossible."""
    assert lexique_pour(metier).phrase_du_rush.strip()


@pytest.mark.parametrize("lex", [lexique_pour(m) for m in sorted(METIERS_COUVERTS)] + [REPLI])
def test_la_phrase_est_grammaticale(lex) -> None:
    """🔴 LE PREMIER JET NE STOCKAIT QUE LA CLAUSE DE TEMPS, à recoller après
    « J'imagine qu' ». Ça donnait « J'imagine qu'quand le gazon repart » sur la
    moitié de la table : l'élision française ne se décide pas au montage.

    Depuis, la phrase est entière — et ce test interdit d'y revenir.
    """
    phrase = lex.phrase_du_rush
    assert phrase.startswith("J'imagine qu"), phrase
    assert not re.match(r"J'imagine qu'(?:quand|le |la |les |ce |ça )", phrase), (
        f"élision fautive : {phrase}"
    )
    assert phrase.endswith("!"), phrase
    assert "ça rentre pas mal tout en même temps!" in phrase, (
        "la chute est la partie commune : elle fait la famille de phrases"
    )


# =================================================== 4. LE GABARIT S'OUVRE =


def test_ces_entreprises_peuvent_enfin_tirer_c_et_d() -> None:
    """Le bout qui donne son sens au reste : la phrase existe, donc le refus
    tombe. Sans cette assertion, on aurait écrit une version que personne ne
    reçoit jamais."""
    assert tete_fixe_servable_pour_entreprise(UN_SEUL_SERVICE) is True


def test_sans_metier_reconnu_c_et_d_restent_fermes() -> None:
    """Contrôle négatif : le SEUL refus qui reste. `{METIER}` n'a rien à
    recevoir, et aucune phrase de supposition ne rachète ça."""
    fiche = {
        **UN_SEUL_SERVICE,
        "research_json": {"services_offered": ["Coaching d'affaires"]},
    }
    assert tete_fixe_servable_pour_entreprise(fiche) is False
