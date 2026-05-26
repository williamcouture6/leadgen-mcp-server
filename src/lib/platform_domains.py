"""Domaines plateformes / big tech qui ne doivent JAMAIS apparaître
comme `companies.domain` ni comme destinataire d'email outreach.

Deux blocklists distinctes, fonctionnellement complémentaires :

1. `PLATFORM_DOMAINS_NEVER_USE` — hosts de pages que Google Places
   peut retourner comme "website" d'une PME (Facebook, Instagram, DoorDash,
   Yelp, builders…). Utilisé en amont (WF-1 sourcing + WF-2 enrichment)
   pour empêcher de stocker `domain=facebook.com` et donc d'enrichir
   Apollo sur Meta.

2. `BIG_TECH_EMAIL_DOMAINS` — domaines corporate de big tech / SaaS qu'on
   ne veut jamais contacter, mais qui n'apparaissent pas comme "websites"
   de PME (`@meta.com` est le corporate Meta, distinct de `facebook.com`
   qui est le produit affiché par Google Places). Pour ces domains-là, la
   menace n'est pas qu'on enrichisse Apollo dessus, c'est qu'un contact
   bigtech se retrouve en DB par autre voie (import manuel, edge case,
   ancienne pollution) et que l'envoi parte.

3. `EMAIL_DOMAINS_NEVER_SEND` = union des 2 — utilisé par la défense
   finale dans `tools/send.py` avant push Instantly. Filet de sécurité
   après les 4 défenses amont (blocklist domain x2, industry guard,
   domain-match Apollo) — si un contact bigtech a échappé à tout ça,
   on bloque ici avant l'action irréversible.

Historique : bug 2026-05-14 a vu 16 contacts @meta.com pollués pour
des cafés. Défenses amont déployées (commits b1063ce → 89cee42), DB
nettoyée. Cette défense send.py ajoutée 2026-05-25 comme 5e couche.
"""
from __future__ import annotations

# Hosts de pages — peuvent apparaître comme `websiteUri` Google Places.
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


# Domaines corporate big tech / SaaS — JAMAIS des PME indé QC. Le bug du
# 14 mai a montré que Meta corporate utilise `@meta.com` (pas
# `@facebook.com`), donc la blocklist hosts ne suffit pas pour le
# filet send.
BIG_TECH_EMAIL_DOMAINS: frozenset[str] = frozenset({
    # GAFAM corporate
    "meta.com",
    "google.com",
    "microsoft.com",
    "apple.com",
    # Streaming / mobilité / commerce
    "netflix.com",
    "uber.com",
    "airbnb.com",
    "tesla.com",
    # SaaS / paiement / outils B2B
    "stripe.com",
    "twilio.com",
    "intercom.com",
    "hubspot.com",
    "salesforce.com",
    "mailchimp.com",
    # IA — concurrents
    "openai.com",
    "anthropic.com",
})


# Union utilisée par la 5e défense (send.py::send_one_message). Bloque
# l'envoi à n'importe quel email dont le domain (ou un parent .X) est
# dans une des 2 listes ci-dessus.
EMAIL_DOMAINS_NEVER_SEND: frozenset[str] = (
    PLATFORM_DOMAINS_NEVER_USE | BIG_TECH_EMAIL_DOMAINS
)


def is_email_on_blocked_domain(email: str | None) -> tuple[bool, str | None]:
    """True + raison si l'email tombe sur un domain de la blocklist (exact
    ou sous-domaine type `info.meta.com`). Retourne `(False, None)` si OK.

    Conçu pour servir de filet final dans `send.py` avant push Instantly.
    Match case-insensitive. Email vide / sans `@` → considéré non bloqué
    (les autres checks de send.py gèrent ces cas séparément).
    """
    if not email or "@" not in email:
        return False, None
    dom = email.rsplit("@", 1)[1].strip().lower()
    if not dom:
        return False, None
    if dom in EMAIL_DOMAINS_NEVER_SEND:
        return True, dom
    for blocked in EMAIL_DOMAINS_NEVER_SEND:
        if dom.endswith("." + blocked):
            return True, f"{dom} (sous-domaine de {blocked})"
    return False, None
