"""Découverte et priorisation des pages à scraper (Research Agent, WF-3).

Mesuré au banc d'essai du 2026-08-31 sur 5 sites (Gauthier, BL Vitres, Lauzon,
LH Les Haiexperts, Pelchat) : le scrape ne coûtait pas cher parce qu'il lisait peu,
il coûtait cher parce qu'il lisait *mal*.

Deux défauts concrets relevés :

1. `_rank_internal_pages` ne connaît que le mot « contact ». Le site des Entretiens
   Gauthier nomme sa page `/nous-joindre/` — elle n'était donc jamais chargée, et
   c'est précisément elle qui porte les 9 secteurs desservis. La base n'avait que
   « Québec » et « Grande région de Québec ».

2. Rien ne lisait le sitemap. Sur BL Vitres, une collecte à plat rapportait 25 pages
   dont 25 billets de blogue et 12 doublons `/en/` — 185 000 tokens dépensés, et zéro
   occurrence des quatre propriétaires, qui vivent sur `/notre-equipe/`. Avec les
   mêmes pages priorisées : moitié moins de tokens et les 4 noms trouvés.

Ces tests verrouillent le classement des URL et le fait que la lecture du sitemap
reste *fail-soft* : un site sans sitemap doit se comporter exactement comme avant.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from src.tools import research


# --- classement d'une URL ---------------------------------------------------

def test_score_place_le_contact_devant_le_blogue() -> None:
    contact = research._score_page_url("https://x.ca/contact/")
    blogue = research._score_page_url("https://x.ca/blog/5-trucs-pour-vos-vitres/")
    assert contact > blogue


def test_score_reconnait_nous_joindre_comme_page_de_contact() -> None:
    # Le cas Gauthier : /nous-joindre/ ne contient pas la chaîne « contact ».
    joindre = research._score_page_url("https://x.ca/nous-joindre/")
    contact = research._score_page_url("https://x.ca/contact/")
    assert joindre == contact


def test_score_reconnait_la_page_equipe() -> None:
    equipe = research._score_page_url("https://x.ca/notre-equipe/")
    quelconque = research._score_page_url("https://x.ca/une-page/")
    assert equipe > quelconque


def test_score_ecarte_les_doublons_de_langue() -> None:
    fr = research._score_page_url("https://x.ca/notre-equipe/")
    en = research._score_page_url("https://x.ca/en/notre-equipe/")
    assert en < fr
    assert en < 0


def test_score_ecarte_le_juridique_et_le_panier() -> None:
    for url in (
        "https://x.ca/politique-de-confidentialite/",
        "https://x.ca/conditions-dutilisation/",
        "https://x.ca/panier/",
    ):
        assert research._score_page_url(url) < 0


def test_score_ecarte_un_billet_date() -> None:
    assert research._score_page_url("https://x.ca/2025/03/tonte-au-printemps/") < 0


def test_score_ecarte_un_titre_d_article_servant_de_slug() -> None:
    # Relevé sur les vrais sites : les billets de blogue à permalien plat n'ont
    # ni /blog/ ni date dans leur URL — leur signe distinctif est d'être une
    # PHRASE. Pire, celui de Pelchat contient « contact » et raflait la note
    # maximale, évinçant /services-extermination.
    for url in (
        "https://x.ca/restez-en-contact-avec-les-visiteurs-de-votre-site-et-augmentez-les-chances/",
        "https://x.ca/comment-fonctionne-lequipement-de-lavage-de-vitres-a-leau-pure/",
        "https://x.ca/quel-est-le-prix-dun-lavage-de-vitres-professionnel/",
    ):
        assert research._score_page_url(url) < 0, url


def test_score_ecarte_la_page_de_remerciement_malgre_le_mot_contact() -> None:
    # BL Vitres a une page /merci-de-nous-avoir-contacte/ : elle contient
    # « contact » et ne dit rien de l'entreprise.
    url = "https://x.ca/merci-de-nous-avoir-contacte/"
    assert research._score_page_url(url) < 0


def test_score_ecarte_les_pages_gabarit_du_cms() -> None:
    # Vu chez Entretien GFR (2026-09-01) : /hello-world/, le billet d'exemple
    # que WordPress cree a l'installation, a pris le 4e emplacement. Il est
    # encore publie sur quantite de sites de PME et ne dit rien de personne.
    for url in (
        "https://x.ca/hello-world/",
        "https://x.ca/sample-page/",
        "https://x.ca/exemple-de-page/",
    ):
        assert research._score_page_url(url) < 0, url


def test_score_ecarte_les_pages_de_recrutement() -> None:
    # /emploi/ prenait la place de la page de services chez Lauzon. On ne
    # prospecte pas des candidats.
    for url in ("https://x.ca/emploi/", "https://x.ca/carrieres/", "https://x.ca/postuler/"):
        assert research._score_page_url(url) < 0, url


def test_score_prefere_une_page_de_premier_niveau_a_egalite() -> None:
    # Chez Pelchat, /zone-de-services/exterminateur-sainte-marie évinçait
    # /services-extermination : même mot-clé, mais l'une est LA page de services
    # et l'autre une déclinaison locale parmi douze.
    haut = research._score_page_url("https://x.ca/services-extermination")
    profond = research._score_page_url("https://x.ca/zone-de-services/exterminateur-sainte-marie")
    assert haut > profond > 0


def test_score_garde_un_nom_de_page_compose() -> None:
    # La frontière mesurée : un nom de page réel monte à 4-5 tirets, un titre
    # d'article commence à 8. Ces deux-là doivent rester positifs.
    for url in (
        "https://x.ca/demande-de-soumission-travaux-deneigement/",
        "https://x.ca/service-darboriculture-et-darbres-a-quebec/",
    ):
        assert research._score_page_url(url) > 0, url


# --- priorisation d'une liste ----------------------------------------------

def test_prioriser_coupe_au_budget_et_classe_par_valeur() -> None:
    urls = [
        "https://x.ca/blog/un-billet-de-blogue-assez-long/",
        "https://x.ca/services/",
        "https://x.ca/nous-joindre/",
        "https://x.ca/a-propos/",
    ]
    assert research._prioriser_urls("https://x.ca/", urls, 2) == [
        "https://x.ca/nous-joindre/",
        "https://x.ca/a-propos/",
    ]


def test_prioriser_jette_les_urls_negatives_meme_si_le_budget_reste() -> None:
    urls = ["https://x.ca/blog/cinq-trucs-pour-des-vitres-propres/", "https://x.ca/contact/"]
    assert research._prioriser_urls("https://x.ca/", urls, 5) == ["https://x.ca/contact/"]


def test_prioriser_dedupe_au_slash_final() -> None:
    # Vu en production le 2026-09-01 sur rivenordextermination.com : le menu
    # porte href="/contacts" ET href="https://.../contacts/". Les deux passaient
    # la deduplication (chaines differentes), la page etait chargee DEUX fois et
    # mangeait un des 4 emplacements.
    urls = ["https://x.ca/contacts", "https://x.ca/contacts/", "https://x.ca/a-propos/"]
    assert research._prioriser_urls("https://x.ca/", urls, 4) == [
        "https://x.ca/contacts",
        "https://x.ca/a-propos/",
    ]


def test_liens_du_menu_dedupes_au_slash_final() -> None:
    # Meme defaut dans le repli sans sitemap.
    html = '<a href="/contacts">Contact</a><a href="https://x.ca/contacts/">Nous joindre</a>'
    assert research._rank_internal_pages("https://x.ca/", html, 5) == ["https://x.ca/contacts"]


def test_prioriser_reconnait_la_home_malgre_les_parametres_de_campagne() -> None:
    # Beaucoup de fiches Google Places portent le site avec des `?utm_source=`
    # (Piscines Rive-Nord, 2026-09-01). La home du sitemap n'a pas ces
    # parametres : sans normalisation elle passe pour une page interne et se
    # fait charger une DEUXIEME fois. Le defaut ne se voit que sur un petit
    # site, ou elle n'est pas evincee par des pages mieux notees.
    base = "https://x.ca/?utm_source=google&utm_medium=local"
    urls = ["https://x.ca/", "https://x.ca/contact/"]
    assert research._prioriser_urls(base, urls, 4) == ["https://x.ca/contact/"]


def test_prioriser_garde_une_page_designee_par_un_parametre() -> None:
    # La borne du correctif precedent. Le menu des Entretiens Gauthier pointe
    # ses promotions vers /?page_id=75 : c'est une VRAIE page, distincte de la
    # home. Retirer les parametres en bloc la ferait disparaitre.
    urls = ["https://x.ca/?page_id=75"]
    assert research._prioriser_urls("https://x.ca/", urls, 4) == ["https://x.ca/?page_id=75"]


def test_prioriser_ignore_la_home_et_les_hotes_externes() -> None:
    urls = ["https://x.ca/", "https://autre.ca/contact/", "https://x.ca/contact/"]
    assert research._prioriser_urls("https://x.ca/", urls, 5) == ["https://x.ca/contact/"]


# --- lecture du sitemap -----------------------------------------------------

_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://x.ca/</loc></url>"
    "<url><loc>https://x.ca/nous-joindre/</loc></url>"
    "</urlset>"
)
_INDEX = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<sitemap><loc>https://x.ca/page-sitemap.xml</loc></sitemap>"
    "</sitemapindex>"
)


@pytest.mark.anyio
@respx.mock
async def test_sitemap_index_suit_ses_sous_sitemaps() -> None:
    respx.get("https://x.ca/sitemap_index.xml").mock(return_value=httpx.Response(200, text=_INDEX))
    respx.get("https://x.ca/page-sitemap.xml").mock(return_value=httpx.Response(200, text=_SITEMAP))
    async with httpx.AsyncClient() as client:
        urls = await research._urls_du_sitemap(client, "https://x.ca/")
    assert "https://x.ca/nous-joindre/" in urls


@pytest.mark.anyio
@respx.mock
async def test_sitemap_ignore_le_sous_sitemap_des_billets() -> None:
    # WordPress sépare lui-même ses billets (`post-sitemap`) de ses pages
    # (`page-sitemap`) : autant se servir de son classement plutôt que de le
    # deviner. Sur BL Vitres, `post-sitemap.xml` vient EN PREMIER et noyait les
    # pages utiles.
    index = (
        '<?xml version="1.0"?><sitemapindex>'
        "<sitemap><loc>https://x.ca/post-sitemap.xml</loc></sitemap>"
        "<sitemap><loc>https://x.ca/category-sitemap.xml</loc></sitemap>"
        "<sitemap><loc>https://x.ca/page-sitemap.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    respx.get("https://x.ca/sitemap_index.xml").mock(return_value=httpx.Response(200, text=index))
    respx.get("https://x.ca/page-sitemap.xml").mock(return_value=httpx.Response(200, text=_SITEMAP))
    urls = None
    async with httpx.AsyncClient() as client:
        urls = await research._urls_du_sitemap(client, "https://x.ca/")
    assert urls == ["https://x.ca/", "https://x.ca/nous-joindre/"]


@pytest.mark.anyio
@respx.mock
async def test_sitemap_absent_renvoie_une_liste_vide() -> None:
    respx.get("https://x.ca/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get("https://x.ca/sitemap.xml").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        assert await research._urls_du_sitemap(client, "https://x.ca/") == []


@pytest.mark.anyio
@respx.mock
async def test_sitemap_illisible_ne_leve_pas() -> None:
    # Un 200 qui rend du HTML (page 404 déguisée, très courant sur Wix) ne doit
    # ni lever ni produire d'URL.
    respx.get("https://x.ca/sitemap_index.xml").mock(
        return_value=httpx.Response(200, html="<html><body>Page introuvable</body></html>")
    )
    respx.get("https://x.ca/sitemap.xml").mock(side_effect=httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as client:
        assert await research._urls_du_sitemap(client, "https://x.ca/") == []


# --- intégration dans fetch_site -------------------------------------------

@pytest.mark.anyio
@respx.mock
async def test_fetch_site_charge_les_pages_du_sitemap_absentes_du_menu() -> None:
    # La home ne lie que le blogue ; le sitemap, lui, connaît /nous-joindre/.
    # Sans sitemap la page de contact serait invisible — c'est le cas Gauthier.
    respx.get("https://x.ca/sitemap_index.xml").mock(
        return_value=httpx.Response(
            200,
            text=_SITEMAP.replace(
                "<url><loc>https://x.ca/</loc></url>",
                "<url><loc>https://x.ca/</loc></url><url><loc>https://x.ca/blog/truc/</loc></url>",
            ),
        )
    )
    respx.get("https://x.ca/").mock(
        return_value=httpx.Response(200, html='<a href="/blog/truc/">Blogue</a>')
    )
    respx.get("https://x.ca/nous-joindre/").mock(
        return_value=httpx.Response(200, html="<p>Secteurs: Lebourgneuf, Beauport</p>")
    )
    site = await research.fetch_site("https://x.ca/")
    urls = [p["url"] for p in site["pages"]]
    assert "https://x.ca/nous-joindre/" in urls
    assert "https://x.ca/blog/truc/" not in urls


@pytest.mark.anyio
@respx.mock
async def test_fetch_site_retombe_sur_les_liens_du_menu_sans_sitemap() -> None:
    respx.get("https://x.ca/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get("https://x.ca/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get("https://x.ca/").mock(
        return_value=httpx.Response(200, html='<a href="/contact/">Contact</a>')
    )
    respx.get("https://x.ca/contact/").mock(return_value=httpx.Response(200, html="<p>ici</p>"))
    site = await research.fetch_site("https://x.ca/")
    assert [p["url"] for p in site["pages"]] == ["https://x.ca/", "https://x.ca/contact/"]


# --- liens sociaux ----------------------------------------------------------

@pytest.mark.anyio
@respx.mock
async def test_fetch_site_extrait_les_liens_sociaux_reels() -> None:
    # Chez Gauthier le pied de page affiche « Facebook / Instagram / TikTok » en
    # texte, sans href : le LLM y lisait une présence Instagram qui n'existe pas.
    # Seuls les vrais href comptent.
    respx.get("https://x.ca/sitemap_index.xml").mock(return_value=httpx.Response(404))
    respx.get("https://x.ca/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get("https://x.ca/").mock(
        return_value=httpx.Response(
            200,
            html=(
                '<a href="https://www.facebook.com/maboite/">Facebook</a>'
                "<span>Instagram</span>"
                '<a href="https://linktr.ee/maboite">Nos liens</a>'
            ),
        )
    )
    site = await research.fetch_site("https://x.ca/")
    assert site["social_links"] == [
        "https://linktr.ee/maboite",
        "https://www.facebook.com/maboite",  # slash final normalisé
    ]


def test_format_site_pour_llm_annonce_les_liens_sociaux() -> None:
    bloc = research._format_site_for_llm(
        {
            "status": "http_200",
            "pages": [],
            "tech_keyword_hits": [],
            "social_links": ["https://www.facebook.com/maboite/"],
        }
    )
    assert "social_links: https://www.facebook.com/maboite/" in bloc


def test_liens_sociaux_dedupliques_au_slash_final() -> None:
    # Relevé chez Lauzon : le même Facebook apparaît avec et sans slash selon la
    # page. Deux entrées feraient croire à deux comptes.
    html = (
        '<a href="https://www.facebook.com/Lauzon">a</a>'
        '<a href="https://www.facebook.com/Lauzon/">b</a>'
    )
    assert research._extract_social_links(html, "https://x.ca/") == [
        "https://www.facebook.com/Lauzon"
    ]


# --- JSON-LD ----------------------------------------------------------------

_JSONLD_PELCHAT = json.dumps(
    {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": "Pelchat gestion parasitaire",
        "telephone": "581 984-9283",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "1190B rue de Courchevel",
            "addressLocality": "Lévis",
            "postalCode": "G6X 0A1",
        },
        "openingHoursSpecification": [
            {"@type": "OpeningHoursSpecification", "dayOfWeek": ["Monday"],
             "opens": "07:00", "closes": "18:00"}
        ],
        "areaServed": [
            {"@type": "City", "name": "Sainte-Foy"},
            {"@type": "City", "name": "Lévis"},
        ],
        "sameAs": ["https://facebook.com/Pelchat.GP"],
    },
    ensure_ascii=False,
)
_HTML_JSONLD = f'<script type="application/ld+json">{_JSONLD_PELCHAT}</script>'


def test_jsonld_donne_telephone_et_adresse() -> None:
    # Relevé chez Pelchat : le JSON-LD portait déjà tout ça, structuré, gratuit,
    # et la fiche en base n'en avait rien.
    s = research._signaux_jsonld(_HTML_JSONLD, "https://pelchat.ca/")
    assert s["telephone"] == "581 984-9283"
    assert "1190B rue de Courchevel" in s["adresse"]


def test_jsonld_donne_les_villes_desservies() -> None:
    # `areaServed` n'est extrait nulle part ailleurs dans le repo, et c'est la
    # donnée qui manquait le plus (Gauthier : « Québec » au lieu de 9 secteurs).
    s = research._signaux_jsonld(_HTML_JSONLD, "https://pelchat.ca/")
    assert s["villes_desservies"] == ["Sainte-Foy", "Lévis"]


def test_jsonld_absent_rend_des_champs_vides_sans_lever() -> None:
    s = research._signaux_jsonld("<html><body>rien</body></html>", "https://x.ca/")
    assert s["telephone"] is None
    assert s["villes_desservies"] == []


# --- noms de fichiers d'images ----------------------------------------------

def test_logos_ne_retient_que_les_fichiers_explicitement_marques() -> None:
    # Chez Gauthier, `logo-petro-canada` et `partenaire-st-hubert` disent ce
    # qu'ils sont. `costco.jpg` ne dit rien : un nom de fichier nu ne prouve
    # aucune relation d'affaires, et une relation client inventée dans un
    # courriel est exactement ce qu'on s'interdit.
    html = (
        '<img src="/up/logo-petro-canada.jpg">'
        '<img src="/up/partenaire-st-hubert.jpg">'
        '<img src="/up/costco.jpg">'
        '<img src="/up/hero-banner.jpg">'
    )
    assert research._noms_de_fichiers_marques(html) == [
        "logo-petro-canada",
        "partenaire-st-hubert",
    ]


def test_logos_gardent_les_certifications() -> None:
    html = '<img src="/up/Logo_du_MELCCFP.png"><img src="/up/certification-rbq.png">'
    assert research._noms_de_fichiers_marques(html) == [
        "Logo_du_MELCCFP",
        "certification-rbq",
    ]


def test_logos_nettoient_hachages_dimensions_et_suffixes() -> None:
    html = (
        '<img src="/up/logo-petro-canada-qxjknrt9u37k3pav9ivqhc7tn09kjhj3mdlp6pteom-1.jpg">'
        '<img src="/up/cropped-LOGO_BLVitres_descriptif_vf_Original-1024x625.png">'
        '<img src="/up/logo-signature-fr-black-1920w.png">'
    )
    assert research._noms_de_fichiers_marques(html) == [
        "cropped-LOGO_BLVitres_descriptif_vf_Original",
        "logo-petro-canada",
        "logo-signature-fr-black",
    ]


# --- passage au LLM ---------------------------------------------------------

def test_format_site_pour_llm_annonce_jsonld_et_logos() -> None:
    bloc = research._format_site_for_llm(
        {
            "status": "http_200", "pages": [], "tech_keyword_hits": [], "social_links": [],
            "jsonld": {"telephone": "581 984-9283", "adresse": "1190B rue de Courchevel, Lévis",
                       "horaires": [], "villes_desservies": ["Sainte-Foy"]},
            "logos_fichiers": ["logo-petro-canada"],
        }
    )
    assert "jsonld_telephone: 581 984-9283" in bloc
    assert "jsonld_villes_desservies: Sainte-Foy" in bloc
    assert "image_filenames: logo-petro-canada" in bloc


@pytest.mark.anyio
@respx.mock
async def test_fetch_site_remonte_les_signaux_jsonld_et_les_logos() -> None:
    respx.get(url__regex=r"https?://[^/]+/sitemap(_index)?\.xml").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://x.ca/").mock(
        return_value=httpx.Response(200, html=_HTML_JSONLD + '<img src="/up/logo-trudel.jpg">')
    )
    site = await research.fetch_site("https://x.ca/")
    assert site["jsonld"]["villes_desservies"] == ["Sainte-Foy", "Lévis"]
    assert site["logos_fichiers"] == ["logo-trudel"]


def test_format_site_pour_llm_dit_explicitement_aucun_lien_social() -> None:
    # « (none) » vaut mieux qu'un champ absent : le prompt doit pouvoir conclure
    # « pas de présence sociale » au lieu de deviner.
    bloc = research._format_site_for_llm(
        {"status": "http_200", "pages": [], "tech_keyword_hits": [], "social_links": []}
    )
    assert "social_links: (none)" in bloc
