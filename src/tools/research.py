"""Tool `research` — Research Agent (WF-3).

Produit un `research_json` structuré pour une company à partir de :
  1. Google Places Details (re-fetch pour inclure les reviews — le FieldMask de WF-1
     n'inclut PAS `reviews` pour économiser les crédits)
  2. Scrape léger du site web (homepage + jusqu'à 2 pages "à propos/contact/services")
  3. Appel Claude Sonnet avec le prompt système de `src/prompts/research.md`

Le prompt système est marqué `cache_control: ephemeral` pour profiter du prompt
caching (~90% de réduction sur les tokens système après le 1er appel).
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from anthropic import Anthropic
from anthropic import APIStatusError, RateLimitError, APIConnectionError
from bs4 import BeautifulSoup
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..config import settings

# ----------------------------------------------------------------------
# Google Places Details (avec reviews)
# ----------------------------------------------------------------------

PLACES_BASE = "https://places.googleapis.com/v1"

# FieldMask étendu (inclut `reviews`) — WF-1 sourcing utilise un mask plus court
# pour économiser les crédits Google ; le research re-fetch ici avec reviews.
PLACE_DETAILS_FIELD_MASK = ",".join([
    "id",
    "displayName",
    "formattedAddress",
    "internationalPhoneNumber",
    "websiteUri",
    "rating",
    "userRatingCount",
    "businessStatus",
    "regularOpeningHours",
    "primaryType",
    "types",
    "reviews",
    "googleMapsUri",
])


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def fetch_place_details(google_place_id: str) -> dict[str, Any]:
    headers = {
        "X-Goog-Api-Key": settings().google_places_api_key,
        "X-Goog-FieldMask": PLACE_DETAILS_FIELD_MASK,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{PLACES_BASE}/places/{google_place_id}",
            headers=headers,
            params={"languageCode": "fr"},
        )
        r.raise_for_status()
        return r.json()


# ----------------------------------------------------------------------
# Website scraper — version async (httpx) avec extraction emails + tech keywords
# ----------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (compatible; CoutureIA-Research/0.1; +https://couture-ia.com)"

TECH_KEYWORDS = (
    "chatbot", "intelligence artificielle", " ia ", "ai ", "automatisation",
    "agence numérique", "agence numerique", "powered by", "built with",
    "hubspot", "salesforce", "intercom", "drift", "zendesk",
)

# Email scraping — source unique des courriels du pipeline (site officiel de la PME).
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
EMAIL_BLOCKLIST_LOCAL = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "abuse", "security", "webmaster", "spam",
}
# Domaines jamais légitimes. Le match couvre AUSSI les sous-domaines (voir
# `_domaine_bloque`) : `sentry.wixpress.com` passait au travers d'une liste qui
# ne contenait que `wixpress.com` et `sentry-next.wixpress.com`.
EMAIL_BLOCKLIST_DOMAINS = {
    "sentry.io", "wixpress.com", "googlegroups.com",
    # gabarits laissés tels quels par le thème / le constructeur de site — en
    # anglais comme en français (relevés sur le terrain le 2026-08-17).
    "example.com", "domain.com", "yourdomain.com", "companyname.com",
    "exemple.com", "monsite.com", "mondomaine.com", "votredomaine.com",
    "courriel.xyz",
}

# EMAIL_REGEX prend un nom de fichier `logo@2x.png` pour une adresse : 47 des 86
# « rejets » du lot terrain étaient ce bruit-là. On les écarte AVANT de compter
# quoi que ce soit — ce ne sont pas des candidats, juste des images.
EMAIL_EXTENSIONS_IMAGE = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif",
    ".tif", ".tiff",
)
EMAIL_GENERIC_LOCAL = {
    "info", "contact", "hello", "bonjour", "allo", "salut",
    "sales", "ventes", "vente", "admin", "marketing", "support", "service",
    "accueil", "reservation", "reservations", "booking", "commande",
    "commandes", "office", "general", "general-info", "direction",
}
# PME indépendantes publient souvent l'email perso du proprio sur leur site
# (ex: salons, traiteurs, micro-restos). On les accepte SEULEMENT si le local
# matche un pattern nominatif (≥2 segments alpha ou un seul token de ≥6 lettres).
EMAIL_PERSONAL_DOMAINS = {
    "gmail.com", "hotmail.com", "hotmail.ca", "hotmail.fr",
    "outlook.com", "outlook.fr", "live.com", "live.ca",
    "yahoo.com", "yahoo.ca", "yahoo.fr",
    "icloud.com", "me.com", "videotron.ca", "sympatico.ca",
    "bellnet.ca", "rogers.com",
}


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.split(":")[0].lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _classify_email(local: str) -> str:
    """Renvoie 'nominative' | 'generic' | 'other'.

    - nominative : ressemble à prénom.nom@, p.nom@, prenomnom@ (≥2 segments alpha
      séparés par '.', '-' ou '_', ou un seul token alpha de 6+ chars sans chiffres).
    - generic : info@, contact@, ventes@, etc.
    - other : tout le reste (chiffres, codes courts).
    """
    local_low = local.lower()
    if local_low in EMAIL_GENERIC_LOCAL:
        return "generic"
    parts = re.split(r"[._\-]", local_low)
    alpha_parts = [p for p in parts if p.isalpha() and len(p) >= 2]
    if len(alpha_parts) >= 2:
        return "nominative"
    if len(alpha_parts) == 1 and len(alpha_parts[0]) >= 6:
        return "nominative"
    return "other"


def _decode_cfemail(hex_str: str) -> str | None:
    """Décode un email obfusqué par Cloudflare (`<span data-cfemail="...">`).

    Beaucoup de PME (WordPress derrière Cloudflare) masquent leur courriel ainsi :
    EMAIL_REGEX ne voit alors AUCUN email en clair dans le HTML. Format Cloudflare :
    1er octet hex = clé XOR, octets suivants = chaque caractère XORé avec la clé.
    Renvoie None si le hex est invalide ou ne décode pas un email plausible.
    """
    try:
        data = bytes.fromhex((hex_str or "").strip())
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    out = "".join(chr(b ^ key) for b in data[1:])
    return out if "@" in out else None


def _lcs_len(a: str, b: str) -> int:
    """Longueur de la plus longue sous-chaîne commune (DP O(len(a)*len(b)))."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


_DOMAIN_SUFFIXES = (".qc.ca", ".com", ".ca", ".net", ".org", ".co", ".info", ".biz")
_BRAND_AFFINITY_MIN = 5


def _domain_main_label(dom: str) -> str:
    """Label principal d'un domaine, TLD retiré (famillelajoie.com → 'famillelajoie')."""
    dom = (dom or "").lower()
    for suf in _DOMAIN_SUFFIXES:
        if dom.endswith(suf):
            dom = dom[: -len(suf)]
            break
    return dom.split(".")[-1] if dom else ""


def _brand_affine(base_dom: str, email_dom: str) -> bool:
    """Vrai si le domaine de l'email partage un radical de marque avec le site
    (plus longue sous-chaîne commune ≥ 5 sur le label principal). Distingue un
    domaine-frère légitime (site famillelajoie.com → info@fermehorticolelajoie.com,
    « lajoie » commun) d'un email tiers sans lien (setjardin.ca → unionmd.ca)."""
    return _lcs_len(_domain_main_label(base_dom), _domain_main_label(email_dom)) >= _BRAND_AFFINITY_MIN


# ----------------------------------------------------------------------
# Propriété d'une adresse : le domaine du courriel vs le NOM de la company
# ----------------------------------------------------------------------
#
# `_brand_affine` compare le domaine du courriel à celui du SITE. C'est la
# mauvaise référence : le site n'est pas toujours la marque. www.fext.ca redirige
# vers exterminationmtl.com ; « Vitres Royal » se fait héberger sur un domaine SEO
# montrealwindowcleaning.ca ; deux boîtes sont référencées via une fiche
# 411habitation.com. Dans les trois cas l'adresse publiée est bien celle de la
# boîte — sur le domaine de sa MARQUE, et sa marque c'est son NOM.
# Mesure du 2026-08-17 (145 companies re-scrapées, 105 adresses jetées listées) :
# 20 de ces adresses étaient celles des boîtes elles-mêmes. 14 sont récupérées
# par cette règle ; les 6 autres restent dehors exprès (domaine perso à local non
# nominatif, ou adresse cassée par l'éditeur du site).
#
# Ce bloc ne FABRIQUE jamais d'adresse : il ne fait que juger un domaine déjà
# lu sur la page. Aucune fonction ici ne produit de "@".

# Formes juridiques et mots-outils : présents dans un nom sur deux, ne disent rien
# de la marque.
_NOM_SUFFIXES_LEGAUX = {
    "inc", "incorporee", "incorporated", "ltee", "ltd", "limitee", "limited",
    "enr", "senc", "sencrl", "srl", "sa", "cie", "corp", "co",
}
_NOM_MOTS_OUTILS = {
    "le", "la", "les", "l", "de", "du", "des", "d", "et", "en", "au", "aux",
    "a", "the", "of", "and", "pour", "chez", "sur", "par",
}
# Enveloppes corporatives : « Groupe X », « Les Entreprises X », « Services X ».
# Le radical de marque, c'est X — jamais l'enveloppe (sinon « Groupe Machin »
# matcherait n'importe quel groupe*.com).
_NOM_MOTS_ENVELOPPE = {
    "groupe", "groupes", "entreprise", "entreprises", "compagnie", "company",
    "service", "services", "equipe", "team",
}
# Géographie : partagée par tous les concurrents du même marché, donc aucun
# signal de propriété (« ... Montreal » traîne dans un nom Google Places sur trois).
_NOM_MOTS_GEO = {
    "montreal", "mtl", "quebec", "laval", "longueuil", "gatineau", "sherbrooke",
    "levis", "terrebonne", "brossard", "repentigny", "lachine", "laurentides",
    "monteregie", "canada", "qc",
}
_NOM_MOTS_NON_DISTINCTIFS = (
    _NOM_SUFFIXES_LEGAUX | _NOM_MOTS_OUTILS | _NOM_MOTS_ENVELOPPE | _NOM_MOTS_GEO
)

# Le nom Google Places traîne souvent un descripteur derrière un tiret ou une
# barre : « Groupe Omnex - Tonte de pelouse Repentigny ». On garde le nom entier
# pour le radical (le descripteur peut porter la marque : « Entretiens AP -
# Lavage de vitres » → lavagedefenetresap.com) mais l'acronyme, lui, se calcule
# AUSSI sur la tête seule.
_NOM_SEPARATEURS_DESCRIPTEUR = ("-", "–", "—", "|", "/", ",", ":")

# Seuils. Le radical reprend celui de `_brand_affine` (5) : en dessous, « ideal »
# ou « omnex » deviendraient « idea »/« omne », et le bruit passe.
_NOM_RADICAL_MIN = 5
# Égalité EXACTE entre le label du domaine (dépouillé de son enveloppe) et un mot
# du nom : 4 lettres, ou 3 si le mot est écrit en CAPITALES dans le nom (c'est
# alors un sigle de marque : « Entreprises CRC » → lesentreprisescrc.com, tandis
# que le « Pro » de « Services Pro » ne donne aucun droit sur pro.com).
_NOM_LABEL_EXACT_MIN = 4
_NOM_SIGLE_EXACT_MIN = 3
# Initiales : 2 lettres matcheraient tout, 3 restent très collisionnelles
# (17 576 combinaisons), 4 en font 456 976 — et l'égalité doit être exacte.
_NOM_ACRONYME_MIN = 4

# Enveloppes retirées du label d'un domaine avant l'égalité exacte
# (lesentreprisescrc → crc, cvpeinc → cvpe). Ordre = du plus long au plus court.
_LABEL_ENVELOPPES = (
    "lesentreprises", "entreprises", "entreprise", "compagnie", "services",
    "service", "groupe", "les", "inc", "ltee", "enr", "cie",
)
_SUFFIXES_COMPOSES = {"qc.ca", "co.uk", "com.au", "co.nz"}


def _sans_accents(texte: str) -> str:
    """« Déneigement Idéal » → « Deneigement Ideal » (les domaines n'ont pas d'accents)."""
    decompose = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in decompose if not unicodedata.combining(c))


def _tokens_nom(nom: str) -> list[str]:
    """Mots distinctifs d'un nom de company, dans l'ordre, sans accent ni ponctuation."""
    plat = re.sub(r"[^a-z0-9]+", " ", _sans_accents(nom or "").lower())
    return [
        mot for mot in plat.split()
        if len(mot) >= 2 and not mot.isdigit() and mot not in _NOM_MOTS_NON_DISTINCTIFS
    ]


def _sigles_nom(nom: str) -> set[str]:
    """Mots écrits tout en CAPITALES dans le nom d'origine (FEXT, CRC, AP)."""
    return {
        mot.lower()
        for mot in re.split(r"[^A-Za-zÀ-ÿ0-9]+", _sans_accents(nom or ""))
        if len(mot) >= 2 and mot.isupper()
    }


def _label_de_marque(dom: str) -> str:
    """Label de marque d'un domaine, TLD retiré, réduit à [a-z0-9].

    Distinct de `_domain_main_label` (qui ne connaît qu'une liste fermée de TLD et
    rend « coop » pour groupex.coop) : ici on retire simplement le dernier
    segment, ou les deux derniers pour un suffixe composé (regardvert.qc.ca).
    """
    parts = [p for p in (dom or "").lower().split(".") if p]
    if len(parts) >= 3 and ".".join(parts[-2:]) in _SUFFIXES_COMPOSES:
        parts = parts[:-2]
    elif len(parts) >= 2:
        parts = parts[:-1]
    return re.sub(r"[^a-z0-9]", "", parts[-1]) if parts else ""


def _label_reduit(label: str) -> str:
    """Label dépouillé de ses enveloppes de tête/queue, tant qu'il reste ≥ 3 lettres."""
    encore = True
    while encore:
        encore = False
        for mot in _LABEL_ENVELOPPES:
            if label.startswith(mot) and len(label) - len(mot) >= _NOM_SIGLE_EXACT_MIN:
                label = label[len(mot):]
                encore = True
            if label.endswith(mot) and len(label) - len(mot) >= _NOM_SIGLE_EXACT_MIN:
                label = label[: -len(mot)]
                encore = True
    return label


def _marque_affine_nom(nom: str | None, email_dom: str) -> bool:
    """Vrai si le domaine du courriel porte la marque de la company (son NOM).

    Trois règles, de la plus large à la plus étroite :
      1. **radical** — sous-chaîne commune ≥ 5 entre le label du domaine et les
         mots distinctifs du nom collés (Amiral Extermination → amiralservice.com).
      2. **mot exact** — le label dépouillé de son enveloppe EST un mot distinctif
         du nom, ≥ 4 lettres (FEXT → fext.ca) ou ≥ 3 si c'est un sigle en
         capitales (Entreprises CRC → lesentreprisescrc.com).
      3. **initiales** — le label dépouillé EST l'acronyme du nom, ≥ 4 initiales
         (Centre Ville Paysagiste Entretien Inc → cvpeinc.com).
    """
    tokens = _tokens_nom(nom or "")
    label = _label_de_marque(email_dom)
    if not tokens or not label:
        return False

    compact = "".join(tokens)
    if _lcs_len(compact, label) >= _NOM_RADICAL_MIN:
        return True

    noyau = _label_reduit(label)
    if len(noyau) < _NOM_SIGLE_EXACT_MIN:
        return False

    sigles = _sigles_nom(nom or "")
    for tok in (*tokens, compact):
        if tok != noyau:
            continue
        if len(tok) >= _NOM_LABEL_EXACT_MIN or tok in sigles:
            return True

    coupe = re.escape("".join(_NOM_SEPARATEURS_DESCRIPTEUR))
    tete = re.split(f"[{coupe}]", nom or "", maxsplit=1)[0]
    for variante in (tokens, _tokens_nom(tete)):
        acronyme = "".join(t[0] for t in variante)
        if len(acronyme) >= _NOM_ACRONYME_MIN and acronyme == noyau:
            return True
    return False


# Forme minimale d'une adresse réelle, appliquée après normalisation (minuscules,
# ponctuation de bord retirée).
_LOCAL_VALIDE = re.compile(r"^[a-z0-9][a-z0-9._%+\-]*$")
_DOMAINE_VALIDE = re.compile(r"^[a-z0-9]([a-z0-9.\-]*[a-z0-9])?\.[a-z]{2,}$")


def _domaine_bloque(dom: str) -> bool:
    """Domaine dans la blocklist — sous-domaines compris (sentry.wixpress.com)."""
    return any(dom == bl or dom.endswith("." + bl) for bl in EMAIL_BLOCKLIST_DOMAINS)


def _est_nom_de_fichier_image(brut: str) -> bool:
    """`chosen-sprite@2x.png` : ce que EMAIL_REGEX prend pour une adresse."""
    dom = _addr_normalisee(brut).rpartition("@")[2]
    return dom.endswith(EMAIL_EXTENSIONS_IMAGE)


def _addr_normalisee(brut: str) -> str:
    return brut.strip().strip(".,;:<>()[]\"'").lower()


# ----------------------------------------------------------------------
# Diagnostic d'extraction des courriels — voir/compter, jamais décider
# ----------------------------------------------------------------------
#
# Pourquoi : 145 companies sur 816 portaient « recherche faite, aucun courriel
# trouvé ». Les 145 avaient un site, les 145 avaient été scrapées, le scraper
# rendait zéro adresse à chaque fois. Une vérif manuelle sur 12 en a trouvé 10
# qui publient pourtant une adresse. Rien n'a sonné pendant deux mois parce
# qu'aucun chiffre nulle part ne disait « on a vu 47 adresses et on les a
# TOUTES jetées ». Ce bloc produit ce chiffre. Il ne filtre rien, ne réordonne
# rien, ne décide rien : il ne fait qu'incrémenter des compteurs.

# Clé unique posée dans `companies.research_json` (jsonb).
DIAGNOSTIC_KEY = "diagnostic_courriels"
_DIAG_VERSION = 1
# Plafond des listes d'adresses (par page ET pour la passe) : le diagnostic
# atterrit dans la row de CHAQUE company — on veut un ordre de grandeur, pas un
# dump de page. Une page-annuaire pathologique ne doit pas faire enfler le jsonb.
_DIAG_REJETS_MAX = 10

_MOTIFS_REJET = ("local_bloque", "domaine_bloque", "hors_domaine")
_SOURCES_CANDIDAT = ("texte", "mailto", "cloudflare")


def _diag_page_neuve(url: str, statut: str, coquille_vide: bool = False) -> dict[str, Any]:
    """Compteurs vierges pour UNE page scannée."""
    return {
        "url": url,
        "statut": statut,
        "coquille_vide": coquille_vide,
        "candidats": {"texte": 0, "mailto": 0, "cloudflare": 0, "total": 0},
        "rejets": {m: 0 for m in _MOTIFS_REJET},
        # adresses effectivement jetées par la règle de domaine (suspect n°1)
        "rejets_hors_domaine": [],
        "acceptes": 0,
    }


def _diag_passe_neuve(statut_site: str) -> dict[str, Any]:
    """Compteurs vierges pour UNE passe de research (site complet)."""
    return {
        "version": _DIAG_VERSION,
        "statut_site": statut_site,
        "pages": [],
        "pages_en_echec": [],
        "rejets_hors_domaine": [],
        "adresses_retenues": [],
        "totaux": {
            "pages_visitees": 0,
            "pages_en_echec": 0,
            "coquilles_vides": 0,
            "candidats": {"texte": 0, "mailto": 0, "cloudflare": 0, "total": 0},
            "rejets": {m: 0 for m in _MOTIFS_REJET},
            "acceptes": 0,
        },
    }


def _diag_finalise(diag: dict[str, Any], emails_by_addr: dict[str, dict[str, str]]) -> None:
    """Agrège les compteurs par page en totaux de passe. Purement additif."""
    tot = diag["totaux"]
    tot["pages_visitees"] = len(diag["pages"]) + len(diag["pages_en_echec"])
    tot["pages_en_echec"] = len(diag["pages_en_echec"])
    tot["coquilles_vides"] = sum(1 for p in diag["pages"] if p["coquille_vide"])
    for page in diag["pages"]:
        for src in _SOURCES_CANDIDAT:
            tot["candidats"][src] += page["candidats"][src]
        tot["candidats"]["total"] += page["candidats"]["total"]
        for motif in _MOTIFS_REJET:
            tot["rejets"][motif] += page["rejets"][motif]
    # Union dédupliquée des adresses jetées par la règle de domaine, plafonnée :
    # c'est la liste que l'humain lit pour trancher « filtre trop strict ou non ».
    vus: set[str] = set()
    for page in diag["pages"]:
        for addr in page["rejets_hors_domaine"]:
            if addr not in vus and len(diag["rejets_hors_domaine"]) < _DIAG_REJETS_MAX:
                vus.add(addr)
                diag["rejets_hors_domaine"].append(addr)
    # Adresses retenues + LA page où on les a vues. Répond plus tard à « combien
    # de boîtes n'avaient une adresse QUE sur leur page de politique de vie privée ».
    # Nom distinct du compteur `acceptes` (int) pour qu'une même clé n'ait jamais
    # deux types selon le niveau — on interroge ce jsonb en SQL.
    diag["adresses_retenues"] = [
        {"email": em["email"], "url": em.get("source_url")} for em in emails_by_addr.values()
    ]
    tot["acceptes"] = len(emails_by_addr)


# Heuristique « coquille SPA » : le HTML brut ne contient AUCUN lien (`<a href>`)
# ni AUCUN élément de navigation (`<nav>`, `[role=navigation]`). Un site statique
# normal — même une page unique — publie au minimum un menu ou un lien. Une SPA
# (React/Vue/Angular) sert un `<div id="root"></div>` + un bundle JS : zéro lien
# dans le HTML brut, donc zéro page interne à suivre et zéro courriel visible.
# Volontairement grossière : on veut l'ORDRE DE GRANDEUR du problème, pas un
# verdict. Faux positifs possibles (landing 100% image), faux négatifs certains
# (SPA qui rend son menu côté serveur). Aucun renderer headless n'est déployé
# (RENDER_SERVICE_URL vide) — ce compteur est aujourd'hui la SEULE mesure du trou.
def _est_coquille_vide(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if soup.find("a", href=True) is not None:
        return False
    if soup.find("nav") is not None:
        return False
    return soup.select_one("[role=navigation]") is None


def sans_diagnostic(research_json: dict[str, Any] | None) -> dict[str, Any]:
    """Copie de `research_json` privée de la clé de diagnostic.

    Le diagnostic est de la télémétrie interne : il n'a rien à faire dans un
    prompt LLM aval (personalize, juge compliance), où il ne ferait qu'ajouter
    du bruit et des adresses tierces. Copie superficielle — l'original intact.
    """
    return {k: v for k, v in (research_json or {}).items() if k != DIAGNOSTIC_KEY}


def _extract_emails_from_html(
    html: str,
    base_url: str,
    diag: dict[str, Any] | None = None,
    company_name: str | None = None,
) -> list[dict[str, str]]:
    """Extrait les emails d'un HTML. Filtre blocklist + emails hors-domaine.

    Trois sources de candidats :
      - **texte libre** (EMAIL_REGEX) → *domain-locked* : on ne garde que le domaine
        du site (ou un perso nominatif), pour ne pas scraper un email partenaire
        cité en prose.
      - **liens `mailto:`** et **emails obfusqués Cloudflare** (`data-cfemail` /
        `/cdn-cgi/l/email-protection#<hex>`) → *owner-placed* : posés volontairement
        par le proprio. Acceptés même cross-domain SI le domaine partage un radical
        de marque avec le site (domaine-frère, ex: famillelajoie.com →
        fermehorticolelajoie.com). Un domaine tiers sans radical commun reste exclu.
    Retourne [{email, local, domain, kind}] dédupliqué.

    `company_name` (optionnel) ouvre une 4e porte de propriété : le domaine porte
    la marque de la boîte (`_marque_affine_nom`). Elle vaut pour les deux sources
    — le verrou same-domain du texte libre existait parce que la prose ne dit pas
    à qui appartient l'adresse ; le nom, lui, le dit. Sans `company_name` le
    comportement est exactement l'ancien.

    `diag` (optionnel) = dict de `_diag_page_neuve()` rempli au passage : compteurs
    de candidats par source et de rejets par motif. **Écriture seule** — aucune
    valeur de `diag` n'est relue pour décider quoi que ce soit, l'extraction est
    strictement identique avec ou sans (verrouillé par
    `test_extraction_identique_avec_ou_sans_diag`).
    Les candidats sont comptés en OCCURRENCES (l'adresse du footer répétée 3 fois
    compte 3 fois : c'est ce que le scanner voit), les rejets et les acceptés en
    ADRESSES DISTINCTES (« on a vu 47 adresses et on les a jetées »).
    """
    base_dom = _domain_of(base_url)
    seen: dict[str, dict[str, str]] = {}
    soup = BeautifulSoup(html, "html.parser")

    # (addr_brut, source) — source ∈ texte | mailto | cloudflare.
    # mailto/cfemail = *explicit* (intentionnel, owner-placed) ; texte = non.
    candidates: list[tuple[str, str]] = [(m, "texte") for m in EMAIL_REGEX.findall(html)]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        if low.startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr:
                candidates.append((addr, "mailto"))
        elif "/cdn-cgi/l/email-protection#" in low:
            dec = _decode_cfemail(href.split("#", 1)[1])
            if dec:
                candidates.append((dec, "cloudflare"))
    for el in soup.select("[data-cfemail]"):
        dec = _decode_cfemail(el.get("data-cfemail", ""))
        if dec:
            candidates.append((dec, "cloudflare"))

    # `logo@2x.png` n'a jamais été un candidat : hors des compteurs, pas un rejet.
    candidates = [c for c in candidates if not _est_nom_de_fichier_image(c[0])]

    if diag is not None:
        for _, src in candidates:
            diag["candidats"][src] += 1
        diag["candidats"]["total"] = len(candidates)

    rejets_vus: set[str] = set()  # une adresse rejetée n'est comptée qu'une fois

    def _compte_rejet(addr: str, motif: str) -> None:
        if diag is None or addr in rejets_vus:
            return
        rejets_vus.add(addr)
        diag["rejets"][motif] += 1
        if motif == "hors_domaine" and len(diag["rejets_hors_domaine"]) < _DIAG_REJETS_MAX:
            diag["rejets_hors_domaine"].append(addr)

    for raw, source in candidates:
        is_explicit = source != "texte"
        addr = _addr_normalisee(raw)
        if "@" not in addr:
            continue
        local, _, dom = addr.partition("@")
        # Ce qui n'a pas la FORME d'une adresse n'a jamais été un candidat (même
        # esprit que le `"@" not in addr` au-dessus) : `%20info@x.com` (artefact
        # d'URL-encodage), ou un domaine truffé d'espaces de largeur nulle par
        # un éditeur WYSIWYG : des chaînes cassées, pas des adresses.
        if not _LOCAL_VALIDE.match(local) or not _DOMAINE_VALIDE.match(dom):
            continue
        if local in EMAIL_BLOCKLIST_LOCAL:
            _compte_rejet(addr, "local_bloque")
            continue
        if _domaine_bloque(dom):
            _compte_rejet(addr, "domaine_bloque")
            continue
        kind = _classify_email(local)
        is_same_domain = bool(base_dom) and (dom == base_dom or dom.endswith("." + base_dom))
        is_personal_nominative = dom in EMAIL_PERSONAL_DOMAINS and kind == "nominative"
        # owner-placed cross-domain : accepté seulement si domaine-frère (radical commun).
        is_owner_sibling = is_explicit and bool(base_dom) and _brand_affine(base_dom, dom)
        # le domaine porte le NOM de la boîte : c'est son domaine de marque, même
        # si le site vit ailleurs (redirection, domaine SEO, fiche d'annuaire).
        # Jamais sur un domaine perso : là, seule la règle du local nominatif
        # décide (une boîte ne possède pas gmail.com, quel que soit son nom).
        is_owner_by_name = (
            bool(company_name)
            and dom not in EMAIL_PERSONAL_DOMAINS
            and _marque_affine_nom(company_name, dom)
        )
        if not (is_same_domain or is_personal_nominative or is_owner_sibling or is_owner_by_name):
            _compte_rejet(addr, "hors_domaine")
            continue
        if addr in seen:
            continue
        seen[addr] = {
            "email": addr,
            "local": local,
            "domain": dom,
            "kind": kind,
        }
    if diag is not None:
        diag["acceptes"] = len(seen)
    return list(seen.values())


def _clean_text(html: str, max_chars: int = 8000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def _same_host(base: str, candidate: str) -> bool:
    try:
        return urlparse(base).netloc.split(":")[0] == urlparse(candidate).netloc.split(":")[0]
    except ValueError:
        return False


# Tiers de priorité des pages internes (après la home) : contact d'abord (courriel/
# téléphone), puis équipe/à-propos (décideurs), puis services. Corrige le bug où une
# page /contactez-nous loin dans le DOM était évincée par le budget de pages.
_PAGE_HINT_TIERS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (0, ("contact",)),
    (1, ("propos", "about", "equipe", "team")),
    (2, ("service", "tarif", "pricing")),
)


def _rank_internal_pages(base_url: str, html: str, max_links: int) -> list[str]:
    """Sélectionne jusqu'à `max_links` pages internes à scraper, priorisées par
    valeur (contact > équipe/à-propos > services). Déduplique les fragments
    (#horaire), ignore les hôtes externes, et garde l'ordre DOM à tier égal."""
    soup = BeautifulSoup(html, "html.parser")
    base_no_frag = base_url.split("#", 1)[0]
    seen_urls: set[str] = set()
    scored: list[tuple[int, int, str]] = []  # (tier, ordre_dom, url)
    order = 0
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#", 1)[0]
        if not href or href == base_no_frag or not _same_host(base_url, href):
            continue
        hay = href.lower() + " " + a.get_text(" ", strip=True).lower()
        tier = next((t for t, hints in _PAGE_HINT_TIERS if any(h in hay for h in hints)), None)
        if tier is None or href in seen_urls:
            continue
        seen_urls.add(href)
        scored.append((tier, order, href))
        order += 1
    scored.sort(key=lambda x: (x[0], x[1]))
    return [u for _, _, u in scored[:max_links]]


async def fetch_site(
    url: str,
    max_pages: int = 5,
    timeout: float = 15.0,
    company_name: str | None = None,
) -> dict[str, Any]:
    """Fetch homepage + up to (max_pages-1) linked internal pages.

    Returns: {url, status, pages: [{url, text}], tech_keyword_hits: [str],
              emails_found: [...], diagnostic_courriels: {...}}

    `diagnostic_courriels` (voir `_diag_passe_neuve`) trace ce que la passe a VU :
    pages scannées + statut HTTP, pages en échec, coquilles SPA, candidats par
    source, rejets par motif, et adresses retenues avec leur page d'origine.
    Ces compteurs sont en écriture seule — aucun ne conditionne un fetch ou un
    filtre, le scrape est identique à ce qu'il était avant l'instrumentation.

    `company_name` (optionnel) sert à reconnaître le domaine de marque de la boîte
    quand son site vit ailleurs — voir `_marque_affine_nom`. Il n'est JAMAIS utilisé
    pour composer une adresse, seulement pour juger une adresse lue sur la page.
    """
    diag = _diag_passe_neuve("unknown")
    out: dict[str, Any] = {
        "url": url, "status": "unknown", "pages": [],
        "tech_keyword_hits": [], "emails_found": [], DIAGNOSTIC_KEY: diag,
    }
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.5"}
    emails_by_addr: dict[str, dict[str, str]] = {}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        try:
            r = await client.get(url)
        except httpx.HTTPError as e:
            out["status"] = f"error: {type(e).__name__}"
            diag["statut_site"] = out["status"]
            diag["pages_en_echec"].append({"url": url, "statut": out["status"]})
            _diag_finalise(diag, emails_by_addr)
            return out

        out["status"] = f"http_{r.status_code}"
        diag["statut_site"] = out["status"]
        if r.status_code >= 400:
            diag["pages_en_echec"].append({"url": str(r.url), "statut": out["status"]})
            _diag_finalise(diag, emails_by_addr)
            return out

        home_text = _clean_text(r.text)
        out["pages"].append({"url": str(r.url), "text": home_text})
        diag_home = _diag_page_neuve(str(r.url), out["status"], _est_coquille_vide(r.text))
        diag["pages"].append(diag_home)
        for em in _extract_emails_from_html(
            r.text, str(r.url), diag=diag_home, company_name=company_name
        ):
            em["source_url"] = str(r.url)
            emails_by_addr.setdefault(em["email"], em)

        candidates = _rank_internal_pages(str(r.url), r.text, max_pages - 1)

        for href in candidates:
            try:
                rp = await client.get(href)
                if rp.status_code < 400:
                    out["pages"].append({"url": str(rp.url), "text": _clean_text(rp.text)})
                    diag_page = _diag_page_neuve(
                        str(rp.url), f"http_{rp.status_code}", _est_coquille_vide(rp.text)
                    )
                    diag["pages"].append(diag_page)
                    for em in _extract_emails_from_html(
                        rp.text, str(rp.url), diag=diag_page, company_name=company_name
                    ):
                        em["source_url"] = str(rp.url)
                        emails_by_addr.setdefault(em["email"], em)
                else:
                    diag["pages_en_echec"].append(
                        {"url": str(rp.url), "statut": f"http_{rp.status_code}"}
                    )
            except httpx.HTTPError as e:
                diag["pages_en_echec"].append(
                    {"url": href, "statut": f"error: {type(e).__name__}"}
                )
                continue

    haystack = " ".join(p["text"].lower() for p in out["pages"])
    out["tech_keyword_hits"] = [kw.strip() for kw in TECH_KEYWORDS if kw in haystack]
    out["emails_found"] = list(emails_by_addr.values())
    _diag_finalise(diag, emails_by_addr)
    return out


# ----------------------------------------------------------------------
# Formatting helpers (réutilisés du proto)
# ----------------------------------------------------------------------

def _format_place_for_llm(place: dict[str, Any]) -> str:
    lines = [
        f"name: {place.get('displayName', {}).get('text', '')}",
        f"address: {place.get('formattedAddress', '')}",
        f"phone: {place.get('internationalPhoneNumber', '')}",
        f"website: {place.get('websiteUri', '')}",
        f"rating: {place.get('rating', '?')} ({place.get('userRatingCount', 0)} reviews)",
        f"business_status: {place.get('businessStatus', '')}",
        f"primary_type: {place.get('primaryType', '')}",
        f"types: {', '.join(place.get('types', []))}",
        f"google_maps_uri: {place.get('googleMapsUri', '')}",
    ]
    reviews = place.get("reviews", []) or []
    if reviews:
        lines.append("")
        lines.append("recent_reviews:")
        for rv in reviews[:5]:
            text = (rv.get("text") or {}).get("text", "") or (rv.get("originalText") or {}).get("text", "")
            lines.append(
                f"  - rating={rv.get('rating')} when={rv.get('relativePublishTimeDescription', '')}: "
                f"{text[:600]}"
            )
    return "\n".join(lines)


def _format_site_for_llm(site: dict[str, Any]) -> str:
    status = site.get("status", "unknown")
    if str(status).startswith("error") or status == "unknown":
        return f"website_status: {status}\nwebsite_text: (unavailable)"
    parts = [f"website_status: {status}"]
    hits = site.get("tech_keyword_hits") or []
    parts.append(f"tech_keyword_hits: {', '.join(hits) if hits else '(none)'}")
    for page in site.get("pages", []):
        parts.append(f"\n--- {page['url']} ---\n{page['text']}")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# LLM call
# ----------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "research.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {text[:300]}")
    return json.loads(match.group(0))


# Outil structuré : force Claude à renvoyer le research via un tool_use dont l'`input`
# est déjà un dict JSON valide (parsé par le SDK). Élimine la classe de bugs
# JSONDecodeError causée par des guillemets non-échappées dans du texte libre
# (ex: une citation d'avis Google recopiée par le modèle). Le schéma calque
# `prompts/research.md` — mêmes clés, donc le Personalization Agent en aval est
# inchangé. Champs volontairement permissifs (null/array vide autorisés) pour ne
# jamais bloquer le modèle quand il manque une info.
_RESEARCH_TOOL_NAME = "save_research"
_RESEARCH_TOOL: dict[str, Any] = {
    "name": _RESEARCH_TOOL_NAME,
    "description": (
        "Enregistre le research structuré de l'entreprise. Utilise null ou un "
        "tableau vide si une info est inconnue — n'invente rien."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_summary": {"type": ["string", "null"]},
            "services_offered": {"type": "array", "items": {"type": "string"}},
            "size_signals": {
                "type": ["object", "null"],
                "properties": {
                    "estimated_employees_range": {"type": ["string", "null"]},
                    "evidence": {"type": ["string", "null"]},
                },
            },
            "decideur_candidats": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nom_complet": {"type": ["string", "null"]},
                        "titre": {"type": ["string", "null"]},
                        "source_url": {"type": ["string", "null"]},
                        "confidence": {"enum": ["high", "medium", "low"]},
                    },
                },
            },
            "pain_points_detected": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pain": {"type": ["string", "null"]},
                        "evidence": {"type": ["string", "null"]},
                        "source": {"type": ["string", "null"]},
                    },
                },
            },
            "recent_review_snippet": {
                "type": ["object", "null"],
                "properties": {
                    "quote": {"type": ["string", "null"]},
                    "rating": {"type": ["integer", "null"]},
                    "relative_time": {"type": ["string", "null"]},
                },
            },
            "tech_savvy_score": {
                "type": ["object", "null"],
                "properties": {
                    "score": {"type": ["string", "null"]},
                    "reasoning": {"type": ["string", "null"]},
                },
            },
            "form_test_hint": {
                "type": ["object", "null"],
                "properties": {
                    "has_quote_form": {"type": ["boolean", "null"]},
                    "has_chat_widget": {"type": ["boolean", "null"]},
                    "auto_response_likely": {"type": ["boolean", "null"]},
                    "notes": {"type": ["string", "null"]},
                },
            },
            "disqualifications": {"type": "array", "items": {"type": "string"}},
            "personalization_hooks": {"type": "array", "items": {"type": "string"}},
            "lead_potential": {
                "type": ["object", "null"],
                "properties": {
                    "score": {"type": ["integer", "null"]},      # 0-100
                    "reasoning": {"type": ["string", "null"]},     # 1 phrase
                },
            },
        },
        "required": ["company_summary"],
    },
}


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class LLMResult(BaseModel):
    research_json: dict[str, Any]
    model: str
    usage: LLMUsage


def _is_transient_anthropic_error(exc: BaseException) -> bool:
    """True si l'erreur Anthropic est transitoire et mérite un retry.

    Catch surtout les 529 OverloadedError + 429 RateLimitError + erreurs réseau.
    Anthropic émet des 529 pendant les pics de charge globaux —
    on retry avec backoff au lieu de laisser la company en `status='error'`.
    """
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        # 529 = Overloaded, 503 = Service Unavailable, 502 = Bad Gateway, 504 = Gateway Timeout
        return status in (502, 503, 504, 529)
    # OverloadedError (sous-classe d'APIStatusError dans SDK récents) attrapé via APIStatusError.
    return False


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_llm(
    place_block: str,
    site_block: str,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 2000,
    track: str = "agence-ia",
) -> LLMResult:
    """Synchronous Anthropic call. Wrapped via `asyncio.to_thread` from the endpoint.

    Retry avec backoff exponentiel sur les erreurs transitoires Anthropic
    (529 Overloaded, 429 Rate Limit, 502/503/504 gateway, erreurs réseau).
    5 tentatives au total, attente 4→8→16→32→60s entre essais. Couvre les
    pics de charge globaux de l'API Anthropic qui durent typiquement <2 min.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)

    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    track_norm = (track or "agence-ia").upper()
    user = (
        f"## Track\n{track_norm}\n\n"
        "## Google Places data\n"
        f"{place_block}\n\n"
        "## Website scrape\n"
        f"{site_block}\n"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[_RESEARCH_TOOL],
        tool_choice={"type": "tool", "name": _RESEARCH_TOOL_NAME},
        messages=[{"role": "user", "content": user}],
    )
    # Chemin principal : tool_use.input est déjà un dict JSON valide (parsé par le SDK).
    tool_block = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) == _RESEARCH_TOOL_NAME),
        None,
    )
    if tool_block is not None and isinstance(tool_block.input, dict):
        research_json = tool_block.input
    else:
        # Fallback défensif : si l'API ne renvoyait pas de tool_use (ne devrait pas
        # arriver avec tool_choice forcé), on retombe sur le parsing texte historique.
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        research_json = _parse_json(text)
    usage = resp.usage
    return LLMResult(
        research_json=research_json,
        model=model,
        usage=LLMUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
    )


# ----------------------------------------------------------------------
# Public API — un seul point d'entrée
# ----------------------------------------------------------------------

class ResearchCompanyIn(BaseModel):
    google_place_id: str
    website: str | None = None
    # nom en base (`companies.name`) : sert au scraper à reconnaître le domaine de
    # marque de la boîte. Optionnel — à défaut, le displayName Google Places.
    company_name: str | None = None
    model: str = _DEFAULT_MODEL
    track: str = "agence-ia"  # track live ; OPT retiré (legacy) — sélectionne les critères de scoring


class ResearchCompanyOut(BaseModel):
    research_json: dict[str, Any]
    model: str
    duration_ms: int
    usage: LLMUsage
    place_status: str
    site_status: str
    tech_keyword_hits: list[str]
    emails_found: list[dict[str, Any]] = []  # [{email, local, domain, kind, source_url}]


async def research_company(payload: ResearchCompanyIn) -> ResearchCompanyOut:
    import asyncio

    started = time.monotonic()

    place = await fetch_place_details(payload.google_place_id)
    website = payload.website or place.get("websiteUri")
    nom = payload.company_name or (place.get("displayName") or {}).get("text") or None
    if website:
        site = await fetch_site(website, company_name=nom)
    else:
        site = {
            "status": "no_website", "pages": [], "tech_keyword_hits": [],
            DIAGNOSTIC_KEY: _diag_passe_neuve("no_website"),
        }

    place_block = _format_place_for_llm(place)
    site_block = _format_site_for_llm(site)

    llm_result = await asyncio.to_thread(
        _call_llm, place_block, site_block, payload.model, 2000, payload.track
    )

    # Le diagnostic voyage DANS research_json : c'est le seul champ persisté tel
    # quel dans `companies.research_json`, donc le seul endroit interrogeable en
    # SQL pour repérer une passe qui a tout jeté. Copie superficielle pour ne pas
    # muter le dict rendu par le LLM.
    research_json = dict(llm_result.research_json or {})
    research_json[DIAGNOSTIC_KEY] = site.get(DIAGNOSTIC_KEY) or _diag_passe_neuve("unknown")

    return ResearchCompanyOut(
        research_json=research_json,
        model=llm_result.model,
        duration_ms=int((time.monotonic() - started) * 1000),
        usage=llm_result.usage,
        place_status="ok",
        site_status=site.get("status", "unknown"),
        tech_keyword_hits=site.get("tech_keyword_hits", []),
        emails_found=site.get("emails_found", []),
    )
