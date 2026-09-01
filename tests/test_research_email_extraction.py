"""Tests extraction d'emails du scraper Research (WF-3).

Régression terrain (Lajoie Paysagistes, 2026-06-21) : le scraper trouvait
0 email alors que le site en publie un. Trois causes empilées :
  1. la page /contactez-nous (où vit l'email) n'était jamais fetchée — budget de
     pages épuisé par des pages moins utiles en ordre DOM ;
  2. l'email était obfusqué par Cloudflare (`<span data-cfemail=...>`) → invisible
     à EMAIL_REGEX ;
  3. le courriel vit sur un domaine-frère (info@fermehorticolelajoie.com sur le
     site famillelajoie.com) → jeté par le filtre same-domain.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from src.tools import research


# --- (B) décodage Cloudflare cfemail ---------------------------------------

# hex réel capturé sur famillelajoie.com/contactez-nous → info@fermehorticolelajoie.com
_LAJOIE_CFEMAIL = "3f565159507f595a4d525a57504d4b565c50535a535e5550565a115c5052"


def test_decode_cfemail_decodes_real_cloudflare_hash() -> None:
    assert research._decode_cfemail(_LAJOIE_CFEMAIL) == "info@fermehorticolelajoie.com"


def test_decode_cfemail_rejects_garbage() -> None:
    assert research._decode_cfemail("zzz") is None
    assert research._decode_cfemail("") is None
    assert research._decode_cfemail("3f") is None  # trop court (clé seule)


def test_extract_decodes_cfemail_sibling_domain() -> None:
    # Cas Lajoie complet : cfemail obfusqué + domaine-frère, sur le site famillelajoie.
    html = f"""
    <a href="/cdn-cgi/l/email-protection"><span class="__cf_email__"
       data-cfemail="{_LAJOIE_CFEMAIL}">[email&#160;protected]</span></a>
    """
    emails = research._extract_emails_from_html(html, "https://famillelajoie.com/contactez-nous/")
    addrs = {e["email"] for e in emails}
    assert "info@fermehorticolelajoie.com" in addrs


# --- (C) politique cross-domain : domaine-frère vs tiers --------------------

def test_explicit_cross_domain_non_affine_is_rejected() -> None:
    # mailto vers un domaine tiers sans radical commun (cas SET Jardin → unionmd) → rejeté.
    html = '<a href="mailto:mtlestinfo@unionmd.ca">écrivez-nous</a>'
    emails = research._extract_emails_from_html(html, "https://www.setjardin.ca/")
    assert emails == []


def test_freetext_cross_domain_is_rejected() -> None:
    # Email partenaire cité en prose (pas un lien) sur un autre domaine → jamais scrapé.
    html = "<p>Réalisé en partenariat avec partenaire@autreboite.com pour le design.</p>"
    emails = research._extract_emails_from_html(html, "https://maboite.com/")
    assert emails == []


def test_same_domain_generic_still_kept() -> None:
    html = '<a href="mailto:info@maboite.com">contact</a>'
    emails = research._extract_emails_from_html(html, "https://maboite.com/contact")
    assert {e["email"] for e in emails} == {"info@maboite.com"}


def test_personal_nominative_still_kept() -> None:
    html = "<p>Écrivez à jean.tremblay@gmail.com</p>"
    emails = research._extract_emails_from_html(html, "https://maboite.com/")
    assert {e["email"] for e in emails} == {"jean.tremblay@gmail.com"}


# --- (A) priorisation des pages internes -----------------------------------

def test_rank_pages_prioritizes_contact_over_services() -> None:
    # contact apparaît APRÈS services dans le DOM, mais doit passer en premier.
    html = """
    <a href="/nos-services/">Nos services</a>
    <a href="/a-propos/">À propos</a>
    <a href="/contactez-nous/">Contact</a>
    """
    ranked = research._rank_internal_pages("https://x.ca/", html, max_links=2)
    assert ranked[0].endswith("/contactez-nous/")
    assert any(u.endswith("/a-propos/") for u in ranked)  # équipe/à-propos prioritaire sur services


def test_rank_pages_dedupes_fragments() -> None:
    html = """
    <a href="/contactez-nous/">Contact</a>
    <a href="/contactez-nous/#horaire">Horaire</a>
    <a href="/contactez-nous/#coordonnees">Coordonnées</a>
    """
    ranked = research._rank_internal_pages("https://x.ca/", html, max_links=5)
    assert ranked == ["https://x.ca/contactez-nous/"]


def test_rank_pages_skips_external_hosts() -> None:
    html = '<a href="https://plannit.io/merchants/x">Réserver</a><a href="/contact/">Contact</a>'
    ranked = research._rank_internal_pages("https://x.ca/", html, max_links=5)
    assert all("plannit.io" not in u for u in ranked)
    assert ranked == ["https://x.ca/contact/"]


# --- (D) propriété par le NOM de la company --------------------------------
#
# Mesure terrain 2026-08-17 sur 145 companies re-scrapées : 26 adresses jetées
# par la règle de domaine étaient celles des companies elles-mêmes. La règle
# comparait le domaine du courriel au domaine du SITE ; or l'adresse d'une boîte
# vit sur le domaine de sa MARQUE, et sa marque c'est son NOM. Les couples
# ci-dessous sont réels (nom en base ↔ adresse jetée).

_CAS_TERRAIN_A_RECUPERER = [
    ("Groupe Omnex - Tonte de pelouse Repentigny", "groupeomnex.ca"),
    ("FEXT Pest Control Lachine Montreal", "fext.ca"),
    ("Vitres Royal - Lavages de Vitres / Window Cleaning Montreal", "vitresroyal.ca"),
    ("Amiral Extermination Montreal", "amiralservice.com"),
    ("Centre Ville Paysagiste Entretien Inc", "cvpeinc.com"),
    ("Déneigement Idéal", "jardin-ideal.com"),
    ("Entretiens AP - Lavage de vitres", "lavagedefenetresap.com"),
    (
        "Entreprises CRC - Déneigement de Remorques, Déneigement Toiture Commerciale",
        "lesentreprisescrc.com",
    ),
]

# Tiers réels croisés dans le MÊME lot : agence web, auteur de thème, fonderie de
# caractères, agence, parking GoDaddy, concurrent cité en prose. Doivent rester dehors.
_CAS_TERRAIN_TIERS = [
    ("Amiral Extermination Montreal", "dasweb.ca"),
    ("lavages de vitre Dauphin", "micahrich.com"),
    ("Déneigement des Tropiques", "indiantypefoundry.com"),
    ("Option Paysagiste - aménagement paysager", "ndiscovered.com"),
    ("Hervé Buisson Paysagiste Inc.", "godaddy.com"),
    ("Centre Ville Paysagiste Entretien Inc", "deneigementgl.com"),
    ("Webster Paysages", "swdla.com"),
    ("Maître Paysagiste", "groupex.coop"),
    ("Fertisol Plus", "weedmancanada.com"),
    ("Le Regard Vert Paysagiste", "yourdomain.com"),
]


@pytest.mark.parametrize("nom,dom", _CAS_TERRAIN_A_RECUPERER)
def test_marque_affine_nom_reconnait_les_cas_terrain(nom: str, dom: str) -> None:
    assert research._marque_affine_nom(nom, dom) is True


@pytest.mark.parametrize("nom,dom", _CAS_TERRAIN_TIERS)
def test_marque_affine_nom_rejette_les_tiers(nom: str, dom: str) -> None:
    assert research._marque_affine_nom(nom, dom) is False


@pytest.mark.parametrize(
    "dom",
    [
        "servicespro.net",        # même enveloppe générique, autre boîte
        "prolawnmontreal.com",    # « pro » noyé dans un autre radical
        "groupeservices.ca",      # que des mots-enveloppe en commun
        "services-plus.com",
        "montrealservices.ca",
    ],
)
def test_nom_generique_n_avale_pas_le_web(dom: str) -> None:
    # « Services Pro » ne laisse qu'un radical de 3 lettres : sous le seuil de 5,
    # et l'égalité exacte de label ne peut pas matcher ces domaines-là.
    assert research._marque_affine_nom("Services Pro", dom) is False


@pytest.mark.parametrize(
    "nom",
    ["", "   ", "Inc.", "Les Entreprises"],  # rien de distinctif à comparer
)
def test_nom_vide_ou_sans_radical_ne_matche_rien(nom: str) -> None:
    assert research._marque_affine_nom(nom, "nimportequoi.com") is False


def test_acronyme_trop_court_ne_matche_pas() -> None:
    # 2-3 initiales matcheraient la moitié du web : seuil à 4.
    assert research._marque_affine_nom("Gazon Vert", "gv.ca") is False
    assert research._marque_affine_nom("Toiture Bois Massif", "tbm.ca") is False


# --- (D bis) la règle branchée dans l'extraction ---------------------------

_HTML_VITRES_ROYAL = '<a href="mailto:info@vitresroyal.ca">Écrivez-nous</a>'
_NOM_VITRES_ROYAL = "Vitres Royal - Lavages de Vitres / Window Cleaning Montreal"
_SITE_VITRES_ROYAL = "https://montrealwindowcleaning.ca/fr/soumission-rapide/"


def test_extraction_accepte_l_adresse_affine_au_nom() -> None:
    emails = research._extract_emails_from_html(
        _HTML_VITRES_ROYAL, _SITE_VITRES_ROYAL, company_name=_NOM_VITRES_ROYAL
    )
    assert {e["email"] for e in emails} == {"info@vitresroyal.ca"}


def test_extraction_sans_nom_reste_a_l_ancienne_regle() -> None:
    # Aucun appelant existant ne passe le nom : comportement inchangé pour eux.
    assert research._extract_emails_from_html(_HTML_VITRES_ROYAL, _SITE_VITRES_ROYAL) == []


def test_extraction_rejette_le_tiers_meme_avec_le_nom() -> None:
    html = '<a href="mailto:jeremy@dasweb.ca">notre site par DasWeb</a>'
    emails = research._extract_emails_from_html(
        html, "https://amiralextermination.com/", company_name="Amiral Extermination Montreal"
    )
    assert emails == []


def test_le_nom_ne_fabrique_jamais_d_adresse() -> None:
    # Garde-fou central : le nom sert UNIQUEMENT à juger un domaine vu sur la page.
    # Page sans aucune adresse → zéro adresse, quel que soit le nom.
    html = "<p>Vitres Royal, lavage de vitres à Montréal. Téléphone 514-555-0100.</p>"
    assert (
        research._extract_emails_from_html(
            html, _SITE_VITRES_ROYAL, company_name=_NOM_VITRES_ROYAL
        )
        == []
    )


def test_le_nom_ne_deverrouille_pas_un_domaine_perso() -> None:
    # La règle des domaines perso (local nominatif obligatoire) reste intacte :
    # une boîte ne possède pas videotron.ca, quel que soit son nom.
    html = '<a href="mailto:info@videotron.ca">écrire</a>'
    emails = research._extract_emails_from_html(
        html, "https://monsite-paysage.ca/", company_name="Vidéotron Paysagement"
    )
    assert emails == []


def test_toute_adresse_retenue_est_litteralement_dans_la_page() -> None:
    html = (
        '<a href="mailto:info@vitresroyal.ca">nous joindre</a>'
        "<p>info@vitresroyal.ca — soumission gratuite</p>"
    )
    emails = research._extract_emails_from_html(
        html, _SITE_VITRES_ROYAL, company_name=_NOM_VITRES_ROYAL
    )
    assert emails
    for e in emails:
        assert e["email"] in html.lower()


# --- (E) hygiène : bruit qui polluait les compteurs ------------------------

def test_noms_de_fichiers_image_ne_sont_pas_des_candidats() -> None:
    # 47 des 86 « rejets » du lot terrain étaient des fichiers @2x pris pour des
    # adresses par EMAIL_REGEX. Ils ne doivent compter nulle part.
    html = (
        '<img src="/img/chosen-sprite@2x.png"><img src="/img/aqgp@2x.png">'
        '<img srcset="/img/cropped-abat_logo@2x-150x150.png 2x">'
        '<img src="/img/section-identification@2x.jpg">'
    )
    d = research._diag_page_neuve("https://x.ca/", "http_200")
    assert research._extract_emails_from_html(html, "https://x.ca/", diag=d) == []
    assert d["candidats"]["total"] == 0
    assert d["rejets"]["hors_domaine"] == 0


@pytest.mark.parametrize(
    "addr",
    [
        "exemple@monsite.com",
        "adresse@courriel.xyz",
        "john.doe@exemple.com",
        "info@companyname.com",
        "office@yourdomain.com",
        "contact@example.com",
    ],
)
def test_placeholders_de_gabarit_rejetes(addr: str) -> None:
    html = f'<a href="mailto:{addr}">contact</a>'
    d = research._diag_page_neuve("https://x.ca/", "http_200")
    assert research._extract_emails_from_html(html, "https://x.ca/", diag=d) == []
    assert d["rejets"]["domaine_bloque"] == 1


def test_blocklist_de_domaines_couvre_les_sous_domaines() -> None:
    # sentry.wixpress.com : la blocklist avait wixpress.com mais matchait à l'exact.
    html = '<p>9a65e97ebe8141fca0c4fd686f70996b@sentry.wixpress.com</p>'
    d = research._diag_page_neuve("https://x.ca/", "http_200")
    assert research._extract_emails_from_html(html, "https://x.ca/", diag=d) == []
    assert d["rejets"]["domaine_bloque"] == 1


# --- (F) le nom voyage jusqu'à l'extraction --------------------------------

@respx.mock
async def test_fetch_site_transmet_le_nom_a_l_extraction() -> None:
    # Cas FEXT : www.fext.ca redirige vers exterminationmtl.com, donc info@fext.ca
    # est « hors domaine » du site — mais c'est bien l'adresse de la boîte.
    # fetch_site sonde le sitemap avant de choisir ses pages : ici, aucun.
    respx.get(url__regex=r"https?://[^/]+/sitemap(_index)?\.xml").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://www.exterminationmtl.com/").mock(
        return_value=httpx.Response(200, html='<a href="mailto:info@fext.ca">courriel</a>')
    )
    site = await research.fetch_site(
        "https://www.exterminationmtl.com/", company_name="FEXT Pest Control Lachine Montreal"
    )
    assert [e["email"] for e in site["emails_found"]] == ["info@fext.ca"]

    sans_nom = await research.fetch_site("https://www.exterminationmtl.com/")
    assert sans_nom["emails_found"] == []


async def test_research_company_transmet_le_nom_au_scraper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vu: dict[str, Any] = {}

    async def _place(_pid: str) -> dict[str, Any]:
        return {"websiteUri": "https://exterminationmtl.com/", "displayName": {"text": "Places"}}

    async def _site(url: str, *_a: Any, **kw: Any) -> dict[str, Any]:
        vu["url"] = url
        vu["company_name"] = kw.get("company_name")
        return {"status": "http_200", "pages": [], "tech_keyword_hits": [], "emails_found": []}

    def _llm(*_a: Any, **_k: Any) -> research.LLMResult:
        return research.LLMResult(research_json={}, model="m", usage=research.LLMUsage())

    monkeypatch.setattr(research, "fetch_place_details", _place)
    monkeypatch.setattr(research, "fetch_site", _site)
    monkeypatch.setattr(research, "_call_llm", _llm)

    await research.research_company(
        research.ResearchCompanyIn(google_place_id="p1", company_name="FEXT Pest Control")
    )
    assert vu["company_name"] == "FEXT Pest Control"

    # à défaut de nom en base, le displayName Google Places fait l'affaire
    await research.research_company(research.ResearchCompanyIn(google_place_id="p1"))
    assert vu["company_name"] == "Places"
