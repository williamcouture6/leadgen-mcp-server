"""Signaux de scoring WF-3 : heures d'ouverture et outils réellement en place.

Deux trous mesurés le 2026-09-01, tous deux invisibles depuis le pipeline.

1. `regularOpeningHours` est demandé dans `PLACE_DETAILS_FIELD_MASK` — donc
   facturé par Google à chaque research — mais `_format_place_for_llm` ne le
   mettait pas dans le bloc envoyé au modèle. Or l'exposition hors-heures est
   l'ancre du barème : sur les 281 boîtes scorées en prod, 155 justifications
   parlaient déjà d'heures d'ouverture, devinées à partir du texte du site.

2. `tech_keyword_hits` ne cherchait que dans le texte visible, or `_clean_text`
   retire `<script>`, `<iframe>`, `<header>`, `<footer>` et `<nav>` avant
   d'extraire ce texte. Tous les widgets (embed Calendly, bouton Jobber, chat
   Podium) y étaient donc invisibles et une PME déjà équipée ressortait
   « manuelle », donc en douleur. En prime, l'empreinte `"ai "` matchait
   « j'ai » sur n'importe quelle page française.

Ces tests verrouillent aussi les deux décisions de barème de William
(2026-09-01) : une entreprise d'une seule personne ne change pas le score, et
un outil déjà en place retire 30 points au lieu de disqualifier.
"""
from __future__ import annotations

import httpx
import respx

from src.tools import research


# --- 1. les heures d'ouverture arrivent au modèle ---------------------------

_HEURES = {
    "regularOpeningHours": {
        "weekdayDescriptions": [
            "lundi: 08:00 – 17:00",
            "mardi: 08:00 – 17:00",
            "mercredi: 08:00 – 17:00",
            "jeudi: 08:00 – 17:00",
            "vendredi: 08:00 – 16:00",
            "samedi: Fermé",
            "dimanche: Fermé",
        ]
    }
}


def test_les_heures_google_arrivent_au_modele() -> None:
    bloc = research._format_place_for_llm({"displayName": {"text": "Toiture X"}, **_HEURES})
    assert "opening_hours:" in bloc
    assert "samedi: Fermé" in bloc
    # Les 7 jours sur une seule ligne : le modèle doit voir la fin de semaine
    # sans avoir à recoller des lignes.
    ligne = next(x for x in bloc.split("\n") if x.startswith("opening_hours:"))
    assert "lundi" in ligne and "dimanche" in ligne


def test_heures_absentes_sont_dites_inconnues() -> None:
    # Explicite plutôt que vide : sans ça le modèle comble le trou en devinant.
    bloc = research._format_place_for_llm({"displayName": {"text": "Toiture X"}})
    assert "opening_hours: (inconnu)" in bloc


def test_horaires_vides_comptent_comme_absents() -> None:
    place = {"regularOpeningHours": {"weekdayDescriptions": []}}
    assert research._horaires_pour_llm(place) == "(inconnu)"


# --- 2. les outils invisibles dans le texte ---------------------------------

_HTML_CALENDLY = (
    "<html><body><h1>Plomberie Untel</h1>"
    "<p>Appelez-nous pour une soumission.</p>"
    '<footer><script src="https://assets.calendly.com/assets/external/widget.js">'
    "</script></footer></body></html>"
)


def test_le_widget_invisible_dans_le_texte_est_vu_dans_le_html() -> None:
    # La moitié qui documente le bug : le texte nettoyé ne contient rien.
    assert "calendly" not in research._clean_text(_HTML_CALENDLY).lower()
    # La moitié qui le corrige.
    assert research._outils_detectes(_HTML_CALENDLY) == {"Calendly"}


def test_plusieurs_outils_remontent_sous_leur_nom_canonique() -> None:
    html = (
        '<script src="https://cdn.getjobber.com/booking.js"></script>'
        '<script src="https://embed.tawk.to/abc/default"></script>'
    )
    assert research._outils_detectes(html) == {"Jobber", "Tawk.to"}


def test_un_site_sans_outil_ne_declenche_rien() -> None:
    html = "<html><body>Déneigement résidentiel à Lévis. Appelez le 418-555-0000.</body></html>"
    assert research._outils_detectes(html) == set()


def test_la_promesse_de_reservation_en_ligne_compte_comme_un_outil() -> None:
    # Pas d'outil nommé, mais la boîte annonce elle-même le canal : la douleur
    # « tout passe par le téléphone » ne tient plus.
    html = "<html><body><a href='/rdv'>Prendre rendez-vous en ligne</a></body></html>"
    assert research._outils_detectes(html) == {"Réservation en ligne (générique)"}


# --- 3. le faux positif « ai » ----------------------------------------------

def test_jai_ne_declenche_plus_le_drapeau_tech() -> None:
    # Texte français ordinaire : « j'ai », « délai », « essai » contiennent tous
    # « ai ». L'ancienne liste les comptait comme une preuve d'outillage IA.
    texte = "j'ai reçu un devis dans un délai court, un essai vrai gratuit".lower()
    assert research._mots_tech_du_texte(texte) == []


def test_un_vrai_mot_tech_est_toujours_releve() -> None:
    assert "chatbot" in research._mots_tech_du_texte("notre chatbot répond en tout temps")


# --- 4. bout en bout : le scrape remonte les outils -------------------------

@respx.mock
async def test_fetch_site_remonte_les_outils_du_pied_de_page() -> None:
    respx.get("https://x.ca/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get("https://x.ca/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get("https://x.ca/").mock(return_value=httpx.Response(200, html=_HTML_CALENDLY))

    site = await research.fetch_site("https://x.ca/", max_pages=1)

    assert site["outils_detectes"] == ["Calendly"]
    # L'ancien canal reste ce qu'il était : des mots vagues, pas des outils.
    assert site["tech_keyword_hits"] == []


@respx.mock
async def test_site_injoignable_rend_une_liste_vide() -> None:
    respx.get("https://x.ca/").mock(side_effect=httpx.ConnectError("boom"))
    site = await research.fetch_site("https://x.ca/", max_pages=1)
    assert site["outils_detectes"] == []


# --- 5. ce que le modèle lit ------------------------------------------------

def test_le_bloc_site_liste_les_outils() -> None:
    bloc = research._format_site_for_llm(
        {"status": "http_200", "outils_detectes": ["Calendly", "Jobber"]}
    )
    assert "outils_detectes: Calendly, Jobber" in bloc


def test_le_bloc_site_dit_none_quand_aucun_outil() -> None:
    # « (none) » explicite : c'est ce qui autorise le modèle à conclure « aucun
    # filet » au lieu de supposer qu'il manque l'information.
    bloc = research._format_site_for_llm({"status": "http_200"})
    assert "outils_detectes: (none)" in bloc


# --- 6. les deux décisions de barème de William -----------------------------

def _prompt() -> str:
    return research._PROMPT_PATH.read_text(encoding="utf-8")


def test_le_prompt_impose_le_moins_30_et_pas_la_disqualification() -> None:
    p = _prompt()
    assert "retire exactement 30 points" in p
    assert "Ce n'est PAS un effondrement" in p


def test_le_prompt_neutralise_la_taille_de_l_equipe() -> None:
    p = _prompt()
    assert "le score ne bouge pas" in p
    # L'ancienne règle notait « micro one-person » comme bas potentiel.
    assert "micro one-person" not in p


def test_le_prompt_ancre_le_score_sur_les_heures() -> None:
    p = _prompt()
    assert "opening_hours" in p
    assert "outils_detectes" in p
