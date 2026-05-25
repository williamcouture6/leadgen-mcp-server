"""Domaines de plateformes tierces qui ne doivent JAMAIS être traités
comme le domaine propre d'une PME.

Beaucoup de PME indé QC n'ont qu'une page Facebook, Instagram, DoorDash,
Yelp, ou un builder de site (Wix, Shopify…) comme "website" dans Google Places.
Si on stocke `domain=facebook.com` puis qu'on enrichit via Apollo, Apollo
renvoie Meta Inc. et ses employés — on insère des emails @meta.com pour
démarcher un café québécois.

Bug live découvert 2026-05-14 (16 contacts pollués). 3 lignes de défense
déployées (commits b1063ce → 89cee42), dont cette blocklist utilisée à
2 endroits du pipeline :

- WF-1 sourcing (`tools/maps.py::_domain_from_url`) : empêche de stocker
  `domain=facebook.com` au moment de l'insert company.
- WF-2 enrichment (`http_api.py::_domain_from_website`) : re-filtre au cas
  où une company préexistante a déjà `domain=facebook.com` en DB.

**Ajouter un domain ici = défense effective des 2 côtés en même temps.**
Avant le 2026-05-25, les 2 listes étaient dupliquées et risquaient de
diverger silencieusement.
"""
from __future__ import annotations

# Une seule source de vérité. Si Apollo introduit un nouveau faux positif,
# ajouter le domain ici et c'est protégé partout.
PLATFORM_DOMAINS_NEVER_USE: frozenset[str] = frozenset({
    # Réseaux sociaux
    "facebook.com", "m.facebook.com", "fb.com", "fb.me",
    "instagram.com",
    "twitter.com", "x.com",
    "linkedin.com",
    "tiktok.com",
    "youtube.com", "youtu.be",
    "pinterest.com", "pinterest.ca",
    # Avis + restos
    "yelp.com", "yelp.ca",
    "tripadvisor.com", "tripadvisor.ca",
    # Livraison resto
    "doordash.com", "ubereats.com", "skipthedishes.com",
    "foodora.ca", "foodora.com", "deliveroo.com", "grubhub.com",
    # Google + maps shorteners
    "google.com", "goo.gl", "maps.app.goo.gl", "g.page",
    # Builders de site
    "wix.com", "wixsite.com", "squarespace.com", "shopify.com",
    "wordpress.com", "weebly.com", "godaddy.com", "sites.google.com",
    "carrd.co", "webnode.com", "jimdo.com",
    # Réservation
    "bookenda.com", "opentable.com", "resy.com", "tock.com",
    "dikidi.net", "vagaro.com", "fresha.com", "mindbodyonline.com",
    "styleseat.com", "planity.com", "treatwell.com", "thumbtack.com",
    # Marketplaces / e-commerce
    "etsy.com", "amazon.com", "amazon.ca",
    # Annuaires / directories
    "411sante.com", "411.ca", "canada411.ca",
    "pagesjaunes.ca", "yellowpages.ca", "yellowpages.com",
})
