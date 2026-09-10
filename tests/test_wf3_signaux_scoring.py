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


# --- 6. un bouton de RDV, pas une phrase qui parle de RDV -------------------

def test_un_bouton_de_prise_de_rdv_compte_comme_un_outil() -> None:
    html = '<a class="cta" href="/rdv">Prendre rendez-vous en ligne</a>'
    assert research._outils_detectes(html) == {research.RDV_GENERIQUE}


def test_le_contraire_de_la_promesse_ne_compte_pas() -> None:
    # Le faux positif mesuré : cherchée à plat dans le HTML, la phrase
    # attrapait son propre contraire, et coûtait 28 points (−20 de malus plus
    # les +8 de « aucun RDV en ligne » perdus) au profil que le barème veut
    # justement remonter.
    html = "<p>Impossible de réserver en ligne, appelez-nous au 418-555-0000.</p>"
    assert research._outils_detectes(html) == set()


def test_un_bouton_qui_compose_un_numero_n_est_pas_un_outil() -> None:
    # Le faux positif que le conseil du 2026-09-09 a trouvé dans MON correctif :
    # « Prendre rendez-vous » qui appelle est le CTA le plus courant des PME de
    # service — c'est-à-dire la preuve que tout repasse par le téléphone.
    for html in (
        '<a href="tel:+15145551234">Prendre rendez-vous en ligne</a>',
        '<a href="mailto:info@x.ca">Réserver en ligne</a>',
    ):
        assert research._outils_detectes(html) == set(), html


def test_prendre_rendez_vous_sans_en_ligne_ne_compte_pas() -> None:
    # Un lien « Prendre rendez-vous » vers un formulaire de contact n'est pas
    # une prise de rendez-vous en ligne.
    html = '<a href="/contact">Prendre rendez-vous</a>'
    assert research._outils_detectes(html) == set()


def test_un_lien_qui_enveloppe_une_carte_ne_compte_pas() -> None:
    # `get_text()` descend dans tous les enfants : une carte de blogue
    # cliquable ramenait son titre entier, négation comprise.
    html = (
        '<a href="/blogue/1"><h3>Pourquoi il est impossible de réserver en '
        "ligne chez nous</h3><p>Appelez-nous, on répond vite et on aime mieux "
        "se parler de vive voix avant de fixer quoi que ce soit.</p></a>"
    )
    assert research._outils_detectes(html) == set()


def test_un_aria_label_suffit() -> None:
    html = '<button aria-label="Book online">📅</button>'
    assert research._outils_detectes(html) == {research.RDV_GENERIQUE}


def test_un_lien_sortant_ne_fabrique_pas_un_outil() -> None:
    # `force.com` nu attrapait `workforce.com` : un lien vers un article RH
    # faisait perdre 20 points au prospect.
    html = '<a href="https://www.workforce.com/blog">Notre partenaire RH</a>'
    assert research._outils_detectes(html) == set()


def test_un_html_illisible_ne_vaut_pas_un_outil() -> None:
    assert research._outils_detectes("<<<>>> pas du html") == set()


# --- 7. la trace : QUEL outil a coûté les points ---------------------------

def test_les_noms_des_outils_sont_conserves() -> None:
    mesures = research.signaux_mesures(
        {"userRatingCount": 40},
        {"status": "http_200", "outils_detectes": ["Calendly", "Jobber"]},
    )
    assert mesures["outils_noms"] == ["Calendly", "Jobber"]
    # Et ça ne pèse rien dans le calcul.
    from src.lib.lead_scoring import calculer_score
    avec = calculer_score({"avis_total": 40, "outils_noms": ["Calendly"]})[0]
    sans = calculer_score({"avis_total": 40})[0]
    assert avec == sans


def test_sans_site_lu_aucune_trace_inventee() -> None:
    mesures = research.signaux_mesures({}, {"status": "no_website"})
    assert mesures["outils_noms"] is None
    assert mesures["outil_en_place"] is None


def test_une_boite_sans_site_ne_s_entend_pas_dire_aucun_outil() -> None:
    # « (none) » affirmerait qu'on a regardé le site et qu'il n'y a rien. Sans
    # site lu, on ne dit rien plutôt que de mentir au modèle.
    bloc = research._format_site_for_llm({"status": "no_website"})
    assert "outils_detectes: (none)" not in bloc
    assert "website_status: no_website" in bloc


# --- 8. les deux décisions de barème de William -----------------------------

def _prompt() -> str:
    return research._PROMPT_PATH.read_text(encoding="utf-8")


def test_le_prompt_interdit_au_modele_de_noter() -> None:
    p = _prompt()
    assert "Tu ne donnes AUCUN score" in p
    # L'ancienne consigne arithmétique ne doit pas revenir par copier-coller.
    assert "retire exactement 30 points" not in p


def test_le_prompt_ferme_la_liste_des_disqualifications() -> None:
    p = _prompt()
    assert "la liste est FERMÉE" in p
    for motif in ("entité publique", "annuaire", "coopérative", "plus de 50 employés"):
        assert motif in p, motif
    # Décision William : un service de réponse humain 24/7 reste joignable.
    assert "ne disqualifie PAS" in p


def test_le_prompt_neutralise_la_taille_de_l_equipe() -> None:
    p = _prompt()
    assert "ne change rien au score" in p
    # L'ancienne règle notait « micro one-person » comme bas potentiel.
    assert "micro one-person" not in p
