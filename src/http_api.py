"""API HTTP REST (FastAPI) — appelée par n8n.

Expose les mêmes fonctions que le serveur MCP (qui reste utilisé en stdio par
Claude Code), mais en routes REST simples pour faciliter l'intégration avec
le node HTTP Request de n8n cloud.

Sécurité : Bearer token statique partagé (`AGENTS_HTTP_TOKEN` dans .env).
À durcir avant un déploiement public (rotation, scopes, etc.).
"""
from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from .tools import db as db_tools
from .tools import enrich as enrich_tools
from .tools import maps as maps_tools
from .tools import personalize as personalize_tools
from .tools import research as research_tools


def _expected_token() -> str | None:
    tok = os.environ.get("AGENTS_HTTP_TOKEN")
    return tok or None


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = _expected_token()
    if not expected:
        # Mode dev sans token : refuse, pour éviter l'oubli avant déploiement.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AGENTS_HTTP_TOKEN non défini côté serveur",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer")
    provided = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")


app = FastAPI(title="leadgen-mcp HTTP API", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ---------------- Sourcing ----------------

@app.get("/sourcing/next-target", dependencies=[Depends(_require_auth)])
async def next_target() -> dict[str, Any] | None:
    t = await db_tools.next_sourcing_target()
    return t.model_dump() if t else None


@app.post("/sourcing/start-run", dependencies=[Depends(_require_auth)])
async def start_run(payload: db_tools.StartRunIn) -> dict[str, Any]:
    return (await db_tools.start_sourcing_run(payload)).model_dump()


@app.post("/sourcing/complete-run", dependencies=[Depends(_require_auth)])
async def complete_run(payload: db_tools.CompleteRunIn) -> dict[str, Any]:
    return await db_tools.complete_sourcing_run(payload)


# ---------------- Companies ----------------

@app.post("/companies/insert", dependencies=[Depends(_require_auth)])
async def insert_company(payload: db_tools.CompanyIn) -> dict[str, Any]:
    return (await db_tools.insert_company(payload)).model_dump()


@app.get("/companies/recent", dependencies=[Depends(_require_auth)])
async def recent_companies(limit: int = 20) -> list[dict[str, Any]]:
    return await db_tools.list_recent_companies(limit=limit)


# ---------------- Maps ----------------

@app.post("/maps/search-places", dependencies=[Depends(_require_auth)])
async def search_places(payload: maps_tools.SearchPlacesIn) -> dict[str, Any]:
    return (await maps_tools.search_places(payload)).model_dump()


# ---------------- Contacts (Phase 1B) ----------------

@app.post("/contacts/insert", dependencies=[Depends(_require_auth)])
async def insert_contact(payload: db_tools.ContactIn) -> dict[str, Any]:
    return (await db_tools.insert_contact(payload)).model_dump()


@app.get("/companies/to-enrich", dependencies=[Depends(_require_auth)])
async def companies_to_enrich(limit: int = 50) -> list[dict[str, Any]]:
    return await db_tools.list_companies_to_enrich(limit=limit)


# ---------------- Enrich (Apollo, Phase 1B) ----------------

@app.post("/enrich/apollo/org", dependencies=[Depends(_require_auth)])
async def enrich_org(payload: enrich_tools.EnrichOrgIn) -> dict[str, Any]:
    return (await enrich_tools.enrich_org(payload)).model_dump()


@app.post("/enrich/apollo/decision-makers", dependencies=[Depends(_require_auth)])
async def enrich_decision_makers(
    payload: enrich_tools.SearchDecisionMakersIn,
) -> dict[str, Any]:
    return (await enrich_tools.search_decision_makers(payload)).model_dump()


@app.post("/enrich/apollo/match", dependencies=[Depends(_require_auth)])
async def enrich_match(payload: enrich_tools.MatchPersonIn) -> dict[str, Any]:
    return (await enrich_tools.match_person(payload)).model_dump()


# ---------------- WF-2 orchestration (Apollo enrichment, Phase 1B) ----------------

# Domaines de plateformes qui hébergent des pages d'entreprise (Facebook, Instagram,
# DoorDash, Yelp, etc.). Beaucoup de PME indé QC n'ont QUE une page Facebook comme
# "website" → Google Places retourne `facebook.com/cafelocal` → si on enrichit
# `facebook.com` via Apollo, Apollo renvoie Meta Inc. et ses employés. Faux positif
# critique : on insère des emails @meta.com pour démarcher un café québécois.
#
# Découvert 2026-05-14 sur 50 companies (Le Café NamasThé → DoorDash, CAFÉ KRÉMA
# → Meta, Augusta Café → Instagram, etc.). Voir memory
# `feedback_no_apollo_on_social_platform_domains`.
PLATFORM_DOMAINS_NEVER_ENRICH = frozenset({
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

# Industries Apollo qui signalent qu'on a enrichi une plateforme/saas/marketplace
# au lieu d'une PME commerce local ou services pro. Si Apollo retourne ces
# industries pour un prospect QC (cafés/restos/salons/plombiers/etc.), c'est
# quasi-certain qu'on est sur une plateforme tierce — rejet.
APOLLO_INDUSTRIES_NEVER_ENRICH = frozenset({
    "information technology & services",
    "internet",
    "computer software",
    "software",
    "saas",
    "online media",
    "marketplaces",
    "e-learning",
    "computer & network security",
    "computer hardware",
    "telecommunications",
    "venture capital & private equity",
    "investment management",
    "investment banking",
    "banking",  # PME indé n'est pas une banque
    "financial services",
})

# Cap de taille : nos cibles sont des PME indépendantes (1-30 employés).
# Si Apollo retourne > N employés, c'est presque sûr une plateforme ou une
# multinationale qui partage le domaine par accident. Marge: chaînes locales
# légit (Brûleries FARO ~50, Café Dépôt 120, Boulangerie Ange 3300 réelle).
# Seuil 300 = catch Stripe/Shopify/Square/Yocale tout en gardant les chaînes QC.
APOLLO_MAX_EMPLOYEES_THRESHOLD = 300


def _domain_from_website(website: str | None) -> str | None:
    """Extrait 'acme.com' depuis 'https://www.acme.com/whatever'.

    Retourne `None` si le domaine extrait est une plateforme générique
    (`facebook.com`, `instagram.com`, etc.) — auquel cas la company n'a pas
    de vrai domaine et NE DOIT PAS être enrichie via Apollo (qui renverrait
    la plateforme elle-même, ex. Meta Inc.).
    """
    if not website:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(website if "://" in website else f"https://{website}")
    host = parsed.netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    if host in PLATFORM_DOMAINS_NEVER_ENRICH:
        return None
    return host


class EnrichCompanyByIdIn(BaseModel):
    company_id: str
    max_contacts: int = 2
    reveal_personal_emails: bool = False  # True = consomme credits Apollo


class EnrichCompanyByIdOut(BaseModel):
    company_id: str
    status: str  # "ok" | "no_domain" | "no_apollo_org" | "no_decision_makers" | "error"
    domain: str | None = None
    apollo_org_id: str | None = None
    contacts_inserted: int = 0
    contacts_duplicate: int = 0
    contacts_skipped_no_email: int = 0
    contacts_skipped_domain_mismatch: int = 0
    error_text: str | None = None


def _email_domain_matches(email: str | None, expected_domain: str | None) -> bool:
    """True si le domaine de l'email correspond au domaine attendu (ou en est un
    sous-domaine). Défense en profondeur : si Apollo retourne un email avec un
    domaine qui ne ressemble pas à celui qu'on a passé à enrich_org, on rejette
    l'insert.

    Exemples :
    - email=`mfabi@cafefaro.com`, expected=`cafefaro.com` → True (match exact)
    - email=`john@mail.cafefaro.com`, expected=`cafefaro.com` → True (sous-domaine)
    - email=`ssingh@meta.com`, expected=`cafefaro.com` → False (mismatch flagrant)
    - email=`ssingh@meta.com`, expected=`facebook.com` → False (Apollo n'a même
      pas répondu avec le domaine qu'on lui a passé — la blocklist en amont
      empêche normalement ce cas, mais défense en profondeur).
    """
    if not email or "@" not in email or not expected_domain:
        return False
    dom = email.rsplit("@", 1)[1].lower()
    exp = expected_domain.lower()
    if dom == exp:
        return True
    if dom.endswith("." + exp):
        return True
    return False


@app.post(
    "/wf2/run-company",
    dependencies=[Depends(_require_auth)],
    response_model=EnrichCompanyByIdOut,
)
async def enrich_company_by_id(payload: EnrichCompanyByIdIn) -> EnrichCompanyByIdOut:
    """Enrichit UNE company : org enrich → search décideurs → match emails → insert contacts.

    Étapes :
      1. Résout domain (champ `domain` direct, sinon dérivé de `website`).
      2. `organizations/enrich` → récupère organization_id Apollo + taille.
      3. `mixed_people/search` (organization_ids=[id], titles=décideurs) → top N personnes.
      4. Pour chaque personne sans email vérifié : `people/match` pour révéler l'email.
      5. Insert contacts (dédup company_id+email).
      6. Mark company 'enriched' (ou 'disqualified' si échec dur).
    """
    co = await db_tools.get_company(payload.company_id)
    if not co:
        return EnrichCompanyByIdOut(
            company_id=payload.company_id, status="error", error_text="company_not_found",
        )

    # Le domain stocké peut déjà être une plateforme (`facebook.com`, etc.) à cause
    # d'un sourcing antérieur — re-filtrer ici pour éviter d'enrichir via Apollo.
    existing_domain = (co.get("domain") or "").lower() or None
    if existing_domain and existing_domain in PLATFORM_DOMAINS_NEVER_ENRICH:
        existing_domain = None
    domain = existing_domain or _domain_from_website(co.get("website"))
    if not domain:
        await db_tools.mark_company_disqualified(
            payload.company_id, "enrich_failed_no_domain_or_platform_only"
        )
        return EnrichCompanyByIdOut(
            company_id=payload.company_id, status="no_domain",
            error_text="ni `domain` ni `website` exploitable (ou plateforme générique facebook/instagram/etc.)",
        )

    # 1) Org enrich
    try:
        org = await enrich_tools.enrich_org(enrich_tools.EnrichOrgIn(domain=domain))
    except Exception as e:  # noqa: BLE001
        return EnrichCompanyByIdOut(
            company_id=payload.company_id, status="error",
            domain=domain, error_text=f"apollo_org_enrich: {e!r}",
        )

    if not org.organization_id:
        await db_tools.mark_company_disqualified(
            payload.company_id, "enrich_failed_apollo_no_org"
        )
        return EnrichCompanyByIdOut(
            company_id=payload.company_id, status="no_apollo_org",
            domain=domain,
        )

    # 1.5) Guard contre les plateformes/big-tech qui auraient échappé à la blocklist.
    # Si Apollo retourne une org dont l'industry est tech/saas/marketplace OU dont
    # la taille dépasse le profil PME indépendante (>300 emp), on rejette : presque
    # sûr que le `domain` est une plateforme tierce hébergeant la PME (booking,
    # directory, builder de site, etc.) et Apollo nous a renvoyé la plateforme,
    # pas le commerce local qu'on visait. WF-3 (scraping) prendra la relève si
    # le site brut publie un email.
    org_industry = (org.industry or "").lower().strip()
    org_employees = org.estimated_num_employees or 0
    is_blocked_industry = org_industry in APOLLO_INDUSTRIES_NEVER_ENRICH
    is_oversize = org_employees > APOLLO_MAX_EMPLOYEES_THRESHOLD
    if is_blocked_industry or is_oversize:
        reason_parts = []
        if is_blocked_industry:
            reason_parts.append(f"industry={org_industry!r}")
        if is_oversize:
            reason_parts.append(f"employees={org_employees}>{APOLLO_MAX_EMPLOYEES_THRESHOLD}")
        await db_tools.mark_company_disqualified(
            payload.company_id,
            f"apollo_org_is_platform_or_oversize ({', '.join(reason_parts)})",
        )
        # Reset le domain pour éviter ré-enrichissement futur.
        await db_tools.update_company_apollo_fields(
            payload.company_id, domain=None, estimated_employees=org_employees,
        )
        return EnrichCompanyByIdOut(
            company_id=payload.company_id, status="no_apollo_org",
            domain=domain, apollo_org_id=org.organization_id,
            error_text=f"apollo_org_platform_guard: {', '.join(reason_parts)}",
        )

    # Patch domain + taille sur la company (utile si domain était null).
    await db_tools.update_company_apollo_fields(
        payload.company_id,
        domain=domain if not co.get("domain") else None,
        estimated_employees=org.estimated_num_employees,
    )

    # 2) Search décideurs
    try:
        search_out = await enrich_tools.search_decision_makers(
            enrich_tools.SearchDecisionMakersIn(
                organization_id=org.organization_id,
                per_page=max(payload.max_contacts, 3),
            )
        )
    except Exception as e:  # noqa: BLE001
        return EnrichCompanyByIdOut(
            company_id=payload.company_id, status="error",
            domain=domain, apollo_org_id=org.organization_id,
            error_text=f"apollo_people_search: {e!r}",
        )

    if not search_out.people:
        # Pas un échec dur — l'entreprise existe mais Apollo n'a pas de décideur.
        # On marque 'enriched' (le travail est fait) avec 0 contacts.
        await db_tools.mark_company_enriched(payload.company_id, "enriched")
        return EnrichCompanyByIdOut(
            company_id=payload.company_id, status="no_decision_makers",
            domain=domain, apollo_org_id=org.organization_id,
        )

    # 3) Pour chaque personne du top N : récupère email (déjà fourni ou via match).
    # NB: Apollo Basic obfusque last_name dans search (ex: "Ro***i") → on match
    # systématiquement via apollo_id pour révéler email + last_name complet.
    inserted = duplicate = skipped = domain_mismatch = 0
    for person in search_out.people[: payload.max_contacts]:
        first_name = person.first_name
        last_name = person.last_name
        email = person.email
        email_status = person.email_status
        title = person.title
        phone = person.phone
        linkedin = person.linkedin_url

        needs_match = (
            not email
            or email_status not in ("verified", "guessed", "likely_to_engage")
            or not last_name  # obfusqué dans search → forcer match
        )
        if needs_match and (person.apollo_id or (first_name and last_name)):
            try:
                m = await enrich_tools.match_person(
                    enrich_tools.MatchPersonIn(
                        apollo_id=person.apollo_id,
                        first_name=first_name if not person.apollo_id else None,
                        last_name=last_name if not person.apollo_id else None,
                        organization_name=co.get("name") if not person.apollo_id else None,
                        domain=domain if not person.apollo_id else None,
                        reveal_personal_emails=payload.reveal_personal_emails,
                    )
                )
                if m.matched:
                    email = m.email or email
                    email_status = m.email_status or email_status
                    title = m.title or title
                    phone = m.phone or phone
                    linkedin = m.linkedin_url or linkedin
                    first_name = m.first_name or first_name
                    last_name = m.last_name or last_name
            except Exception:  # noqa: BLE001
                # Pas bloquant — on insère ce qu'on a (sans email ça sera skip_no_email).
                pass

        # Défense en profondeur : si l'email Apollo n'est pas sur le domaine qu'on
        # a passé à enrich_org, on rejette. Catch les cas où une plateforme
        # inconnue échappe à la blocklist (ex: domain=`unknownsocial.com` →
        # Apollo renvoie une org tierce → email @autrechose.com). WF-3 ramassera
        # l'email scrapé du vrai site si dispo.
        if email and not _email_domain_matches(email, domain):
            domain_mismatch += 1
            continue

        res = await db_tools.insert_contact(
            db_tools.ContactIn(
                company_id=payload.company_id,
                first_name=first_name,
                last_name=last_name,
                email=email,
                email_verified=(email_status == "verified"),
                email_verification_source="apollo" if email else None,
                phone=phone,
                linkedin_url=linkedin,
                title=title,
                seniority=person.seniority,
                is_decision_maker=True,
                source="apollo",
                raw_payload={"apollo_id": person.apollo_id, "email_status": email_status},
            )
        )
        if res.status == "inserted":
            inserted += 1
        elif res.status == "duplicate":
            duplicate += 1
        else:
            skipped += 1

    await db_tools.mark_company_enriched(payload.company_id, "enriched")

    return EnrichCompanyByIdOut(
        company_id=payload.company_id, status="ok",
        domain=domain, apollo_org_id=org.organization_id,
        contacts_inserted=inserted,
        contacts_duplicate=duplicate,
        contacts_skipped_no_email=skipped,
        contacts_skipped_domain_mismatch=domain_mismatch,
    )


class RunWf2In(BaseModel):
    """Pass complet WF-2 : prend N companies status='sourced', les enrichit séquentiellement.

    Apollo Basic = 2 500 credits/mois. ~3 credits par company. Limite réelle MVP = throughput
    Instantly (warmup), pas Apollo.
    """
    limit: int = 20
    max_contacts: int = 2
    reveal_personal_emails: bool = False


class RunWf2Item(BaseModel):
    company_id: str
    name: str | None = None
    status: str
    contacts_inserted: int = 0
    error_text: str | None = None


class RunWf2Out(BaseModel):
    processed: int
    enriched: int
    disqualified: int
    failed: int
    total_contacts_inserted: int
    items: list[RunWf2Item]


@app.post("/wf2/run", dependencies=[Depends(_require_auth)], response_model=RunWf2Out)
async def run_wf2(payload: RunWf2In) -> RunWf2Out:
    backlog = await db_tools.list_companies_to_enrich(limit=payload.limit)

    items: list[RunWf2Item] = []
    enriched = disqualified = failed = total_contacts = 0

    for co in backlog:
        try:
            res = await enrich_company_by_id(
                EnrichCompanyByIdIn(
                    company_id=co["id"],
                    max_contacts=payload.max_contacts,
                    reveal_personal_emails=payload.reveal_personal_emails,
                )
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            items.append(RunWf2Item(
                company_id=co["id"], name=co.get("name"),
                status="error", error_text=repr(e),
            ))
            continue

        if res.status == "ok" or res.status == "no_decision_makers":
            enriched += 1
        elif res.status in ("no_domain", "no_apollo_org"):
            disqualified += 1
        else:
            failed += 1
        total_contacts += res.contacts_inserted
        items.append(RunWf2Item(
            company_id=co["id"], name=co.get("name"),
            status=res.status,
            contacts_inserted=res.contacts_inserted,
            error_text=res.error_text,
        ))

    return RunWf2Out(
        processed=len(items), enriched=enriched,
        disqualified=disqualified, failed=failed,
        total_contacts_inserted=total_contacts,
        items=items,
    )


# ---------------- High-level workflow (WF-1 en un appel) ----------------

class RunWf1In(BaseModel):
    """Lance un pass complet WF-1 côté serveur — pratique pour n8n qui n'a
    qu'à déclencher le cron, le serveur gère le reste."""
    city: str | None = None
    sector: str | None = None
    icp_segment: str | None = None
    max_pages: int = 3
    dry_run: bool = False


class RunWf1Out(BaseModel):
    target: dict[str, Any] | None
    run_id: str | None
    total_results: int
    new_companies_count: int
    duplicates_count: int
    error_text: str | None = None


@app.post("/wf1/run", dependencies=[Depends(_require_auth)], response_model=RunWf1Out)
async def run_wf1(payload: RunWf1In) -> RunWf1Out:
    import asyncio

    # 1) Pick target
    if payload.city and payload.sector and payload.icp_segment:
        city, sector, icp = payload.city, payload.sector, payload.icp_segment
        target_meta = {"city": city, "sector": sector, "icp_segment": icp, "reason": "explicit"}
    else:
        t = await db_tools.next_sourcing_target()
        if not t:
            return RunWf1Out(
                target=None, run_id=None, total_results=0,
                new_companies_count=0, duplicates_count=0,
                error_text="no_target_available",
            )
        city, sector, icp = t.city, t.sector, t.icp_segment
        target_meta = t.model_dump()

    # 2) Start run (sauf dry_run)
    run_id: str | None = None
    if not payload.dry_run:
        run = await db_tools.start_sourcing_run(
            db_tools.StartRunIn(city=city, sector=sector, icp_segment=icp)
        )
        run_id = run.run_id

    page_token: str | None = None
    total_results = 0
    new_count = 0
    dup_count = 0
    error_text: str | None = None

    try:
        for page_num in range(payload.max_pages):
            if page_num > 0:
                if not page_token:
                    break
                await asyncio.sleep(2.5)  # warm-up nextPageToken Google
            out = await maps_tools.search_places(
                maps_tools.SearchPlacesIn(
                    city=city, sector=sector, page_token=page_token, max_results=20
                )
            )
            total_results += len(out.results)

            for p in out.results:
                if payload.dry_run:
                    continue
                res = await db_tools.insert_company(
                    db_tools.CompanyIn(
                        name=p.name,
                        google_place_id=p.google_place_id,
                        address=p.formatted_address,
                        city=p.city or city,
                        postal_code=p.postal_code,
                        latitude=p.latitude,
                        longitude=p.longitude,
                        website=p.website,
                        domain=p.domain,
                        icp_segment=icp,
                        industry=sector,
                        google_types=p.google_types,
                        google_rating=p.google_rating,
                        google_reviews_count=p.google_reviews_count,
                        raw_payload=p.raw_payload,
                    )
                )
                if res.status == "inserted":
                    new_count += 1
                else:
                    dup_count += 1

            page_token = out.next_page_token
            if not page_token:
                break

    except Exception as e:  # noqa: BLE001
        error_text = repr(e)

    if run_id:
        await db_tools.complete_sourcing_run(
            db_tools.CompleteRunIn(
                run_id=run_id,
                status="failed" if error_text else "completed",
                next_page_token=page_token,
                results_count=total_results,
                new_companies_count=new_count,
                duplicates_count=dup_count,
                error_text=error_text,
            )
        )

    return RunWf1Out(
        target=target_meta,
        run_id=run_id,
        total_results=total_results,
        new_companies_count=new_count,
        duplicates_count=dup_count,
        error_text=error_text,
    )


# ---------------- Research (Phase 2 — WF-3) ----------------

@app.get("/companies/to-research", dependencies=[Depends(_require_auth)])
async def companies_to_research(
    limit: int = 20,
    require_website: bool = True,
) -> list[dict[str, Any]]:
    """Companies sans research_json. Utilisé par n8n pour visualiser le backlog."""
    return await db_tools.list_companies_to_research(
        limit=limit, require_website=require_website
    )


class ResearchCompanyByIdIn(BaseModel):
    company_id: str
    model: str = "claude-sonnet-4-6"


class ResearchCompanyByIdOut(BaseModel):
    company_id: str
    status: str  # "ok" | "skipped_no_place_id" | "error"
    research_json: dict[str, Any] | None = None
    duration_ms: int | None = None
    error_text: str | None = None
    emails_scraped_inserted: int = 0
    emails_scraped_duplicate: int = 0


@app.post(
    "/research/company",
    dependencies=[Depends(_require_auth)],
    response_model=ResearchCompanyByIdOut,
)
async def research_company_by_id(payload: ResearchCompanyByIdIn) -> ResearchCompanyByIdOut:
    """Research d'UNE company. Pratique pour n8n quand on veut traiter
    company-par-company (avec retry par item)."""
    from . import supabase_client as db
    matches = await db.select(
        "companies",
        params={
            "select": "id,google_place_id,website,name",
            "id": f"eq.{payload.company_id}",
            "limit": "1",
        },
    )
    if not matches:
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="error",
            error_text="company_not_found",
        )
    co = matches[0]
    if not co.get("google_place_id"):
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="skipped_no_place_id",
        )

    try:
        out = await research_tools.research_company(
            research_tools.ResearchCompanyIn(
                google_place_id=co["google_place_id"],
                website=co.get("website"),
                model=payload.model,
            )
        )
    except Exception as e:  # noqa: BLE001
        # Audit l'échec, sans bloquer
        try:
            await db_tools.record_agent_run(
                db_tools.AgentRunIn(
                    agent="research",
                    model=payload.model,
                    company_id=payload.company_id,
                    error_text=repr(e),
                )
            )
        except Exception:  # noqa: BLE001
            pass
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="error",
            error_text=repr(e),
        )

    await db_tools.update_company_research(payload.company_id, out.research_json)
    try:
        await db_tools.record_agent_run(
            db_tools.AgentRunIn(
                agent="research",
                model=out.model,
                company_id=payload.company_id,
                input_payload={
                    "google_place_id": co["google_place_id"],
                    "website": co.get("website"),
                },
                output_payload=out.research_json,
                duration_ms=out.duration_ms,
                input_tokens=out.usage.input_tokens,
                output_tokens=out.usage.output_tokens,
                cache_read_tokens=out.usage.cache_read_input_tokens,
                cache_creation_tokens=out.usage.cache_creation_input_tokens,
            )
        )
    except Exception:  # noqa: BLE001
        pass

    # Fallback Apollo: insère les emails scrapés du site comme contacts.
    # Apollo couvre mal les PME indépendantes QC (~10% de match sur cafés/restos).
    # Les emails du site (info@, contact@, ou nominatifs) comblent le trou.
    # `email_verified=False` → marqueur que ces emails n'ont pas été validés par Apollo.
    inserted_scraped = duplicate_scraped = 0
    for em in out.emails_found:
        res = await db_tools.insert_contact(
            db_tools.ContactIn(
                company_id=payload.company_id,
                email=em["email"],
                email_verified=False,
                email_verification_source="website_scrape",
                title=None,
                is_decision_maker=(em["kind"] != "other"),
                source="website",
                raw_payload={
                    "kind": em["kind"],  # nominative | generic | other
                    "source_url": em.get("source_url"),
                    "local": em["local"],
                },
            )
        )
        if res.status == "inserted":
            inserted_scraped += 1
        elif res.status == "duplicate":
            duplicate_scraped += 1

    return ResearchCompanyByIdOut(
        company_id=payload.company_id,
        status="ok",
        research_json=out.research_json,
        duration_ms=out.duration_ms,
        emails_scraped_inserted=inserted_scraped,
        emails_scraped_duplicate=duplicate_scraped,
    )


class RunWf3In(BaseModel):
    """Pass complet WF-3 : prend N companies sans research_json, les traite séquentiellement.

    On reste séquentiel volontairement — l'API Anthropic et Google Places ont des
    rate limits, et un cron quotidien sur 10-20 companies tolère bien 30s par item.

    `inter_company_sleep_seconds` (défaut 3s) espace les appels Anthropic pour éviter
    les pics qui déclenchent des 529 Overloaded en cascade. Combiné au retry interne
    de `_call_llm`, on absorbe les saturations transitoires sans laisser de companies
    en erreur. 0 = pas de pause (pour test).
    """
    limit: int = 10
    model: str = "claude-sonnet-4-6"
    require_website: bool = True
    inter_company_sleep_seconds: float = 3.0


class RunWf3Item(BaseModel):
    company_id: str
    name: str | None = None
    status: str
    duration_ms: int | None = None
    error_text: str | None = None


class RunWf3Out(BaseModel):
    processed: int
    succeeded: int
    failed: int
    skipped: int
    items: list[RunWf3Item]


@app.post("/wf3/run", dependencies=[Depends(_require_auth)], response_model=RunWf3Out)
async def run_wf3(payload: RunWf3In) -> RunWf3Out:
    import asyncio

    backlog = await db_tools.list_companies_to_research(
        limit=payload.limit, require_website=payload.require_website
    )

    items: list[RunWf3Item] = []
    succeeded = failed = skipped = 0
    sleep_s = max(0.0, payload.inter_company_sleep_seconds)

    for idx, co in enumerate(backlog):
        try:
            res = await research_company_by_id(
                ResearchCompanyByIdIn(company_id=co["id"], model=payload.model)
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            items.append(RunWf3Item(
                company_id=co["id"], name=co.get("name"),
                status="error", error_text=repr(e),
            ))
            # Continue avec sleep même en cas d'erreur (évite hammer si problème global).
            if sleep_s > 0 and idx < len(backlog) - 1:
                await asyncio.sleep(sleep_s)
            continue

        if res.status == "ok":
            succeeded += 1
        elif res.status.startswith("skipped"):
            skipped += 1
        else:
            failed += 1
        items.append(RunWf3Item(
            company_id=co["id"], name=co.get("name"),
            status=res.status, duration_ms=res.duration_ms,
            error_text=res.error_text,
        ))
        # Petit délai entre companies pour ne pas saturer Anthropic ni Google Places.
        # 3s = compromis raisonnable (10 companies → +30s total, négligeable sur cron).
        if sleep_s > 0 and idx < len(backlog) - 1:
            await asyncio.sleep(sleep_s)

    return RunWf3Out(
        processed=len(items), succeeded=succeeded, failed=failed,
        skipped=skipped, items=items,
    )


# ---------------- Personalize (Phase 2 — WF-4) ----------------

import json as _json  # local alias to avoid clashing with model fields

_REFERENCES_PATH = os.environ.get(
    "CLIENT_REFERENCES_PATH",
    str(__file__).replace("http_api.py", "../client_references.json"),
)


def _load_client_references() -> list[dict[str, Any]]:
    """Charge la liste de social_proof. Fichier optionnel — `[]` si absent."""
    from pathlib import Path
    p = Path(_REFERENCES_PATH)
    if not p.exists():
        return []
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
        return data.get("references", [])
    except Exception:  # noqa: BLE001
        return []


def _contact_for_prompt(contact_row: dict[str, Any]) -> dict[str, Any]:
    """Format minimal du contact pour le prompt — uniquement champs utiles.

    `email_source` permet au prompt d'adapter le ton :
    - 'apollo' (vérifié) : email nominatif, peut tutoyer par prénom.
    - 'website_scrape' + kind='nominative' : email perso du proprio, salutation prudente.
    - 'website_scrape' + kind='generic' : info@/contact@, ne PAS adresser au nom.
    """
    raw = contact_row.get("raw_payload") or {}
    return {
        "first_name": contact_row.get("first_name"),
        "last_name": contact_row.get("last_name"),
        "title": contact_row.get("title"),
        "email": contact_row.get("email"),
        "email_source": contact_row.get("email_verification_source"),
        "email_kind": raw.get("kind") if isinstance(raw, dict) else None,
    }


@app.get("/contacts/to-personalize", dependencies=[Depends(_require_auth)])
async def contacts_to_personalize(
    limit: int = 20, max_per_company: int = 1,
) -> list[dict[str, Any]]:
    """Backlog WF-4 : contacts avec email + company.research_json + sans draft outbound.

    `max_per_company=1` (défaut) : un seul contact par entreprise, prioritisé.
    """
    return await db_tools.list_contacts_to_personalize(
        limit=limit, max_per_company=max_per_company,
    )


class PersonalizeContactIn(BaseModel):
    contact_id: str
    template_choice: str = "A"  # "A" ou "B"
    model: str = "claude-sonnet-4-6"
    persist: bool = True  # False → dry-run, retourne juste l'email sans insérer dans messages
    available_slots: list[dict[str, Any]] | None = None  # override (sinon fetch Cal.com)


class PersonalizeContactOut(BaseModel):
    contact_id: str
    status: str  # "ok" | "error" | "skipped_no_email" | "skipped_no_research"
    message_id: str | None = None
    email: dict[str, Any] | None = None
    duration_ms: int | None = None
    template_used: str | None = None
    error_text: str | None = None


async def _personalize_one(
    contact_row: dict[str, Any],
    company_row: dict[str, Any],
    *,
    template_choice: str,
    model: str,
    persist: bool,
    available_slots: list[dict[str, Any]],
    social_proof: list[dict[str, Any]],
) -> PersonalizeContactOut:
    """Coeur partagé entre /personalize/contact et /wf4/run."""
    contact_id = contact_row["id"]
    if not contact_row.get("email"):
        return PersonalizeContactOut(contact_id=contact_id, status="skipped_no_email")
    research = company_row.get("research_json")
    if not research:
        return PersonalizeContactOut(contact_id=contact_id, status="skipped_no_research")

    try:
        out = await personalize_tools.personalize(
            personalize_tools.PersonalizeIn(
                research_json=research,
                company={
                    "name": company_row.get("name"),
                    "website": company_row.get("website"),
                    "city": company_row.get("city"),
                    "icp_segment": company_row.get("icp_segment"),
                    "industry": company_row.get("industry"),
                },
                contact=_contact_for_prompt(contact_row),
                social_proof=social_proof,
                template_choice=template_choice,
                available_slots=available_slots,
                model=model,
            )
        )
    except Exception as e:  # noqa: BLE001
        # Audit l'échec sans bloquer le batch
        try:
            await db_tools.record_agent_run(
                db_tools.AgentRunIn(
                    agent="personalization",
                    model=model,
                    contact_id=contact_id,
                    company_id=company_row["id"],
                    error_text=repr(e),
                )
            )
        except Exception:  # noqa: BLE001
            pass
        return PersonalizeContactOut(contact_id=contact_id, status="error", error_text=repr(e))

    email = out.email or {}
    subject = email.get("subject") or ""
    body = email.get("body_text") or ""
    warnings = email.get("warnings") or []

    # Audit succès dans agent_runs (avant insert message pour avoir l'id à référencer)
    agent_run_id: str | None = None
    try:
        ar = await db_tools.record_agent_run(
            db_tools.AgentRunIn(
                agent="personalization",
                model=out.model,
                contact_id=contact_id,
                company_id=company_row["id"],
                input_payload={
                    "template_choice": template_choice,
                    "slots_count": sum(len(s.get("times", [])) for s in available_slots),
                    "social_proof_count": len(social_proof),
                },
                output_payload=email,
                duration_ms=out.duration_ms,
                input_tokens=out.usage.input_tokens,
                output_tokens=out.usage.output_tokens,
                cache_read_tokens=out.usage.cache_read_input_tokens,
                cache_creation_tokens=out.usage.cache_creation_input_tokens,
            )
        )
        agent_run_id = ar.get("agent_run_id")
    except Exception:  # noqa: BLE001
        pass

    message_id: str | None = None
    if persist and subject and body:
        try:
            ins = await db_tools.insert_message_draft(
                db_tools.MessageDraftIn(
                    contact_id=contact_id,
                    subject=subject,
                    body_text=body,
                    to_email=contact_row["email"],
                    generated_by_agent_run=agent_run_id,
                    compliance_check_passed=None,  # WF-5 le valide
                    compliance_notes=("; ".join(warnings) if warnings else None),
                )
            )
            message_id = ins.get("message_id")
        except Exception as e:  # noqa: BLE001
            return PersonalizeContactOut(
                contact_id=contact_id, status="error",
                error_text=f"insert_message_draft: {e!r}",
                email=email, template_used=out.template_used,
            )

    return PersonalizeContactOut(
        contact_id=contact_id, status="ok",
        message_id=message_id, email=email,
        duration_ms=out.duration_ms, template_used=out.template_used,
    )


@app.post(
    "/personalize/contact",
    dependencies=[Depends(_require_auth)],
    response_model=PersonalizeContactOut,
)
async def personalize_contact(payload: PersonalizeContactIn) -> PersonalizeContactOut:
    """Personnalisation d'UN contact. Le mode `persist=False` est utile pour
    QA / preview sans polluer la table messages.
    """
    from . import supabase_client as db

    contacts = await db.select(
        "contacts",
        params={
            "select": "id,first_name,last_name,email,title,company_id,email_verification_source,raw_payload",
            "id": f"eq.{payload.contact_id}",
            "limit": "1",
        },
    )
    if not contacts:
        return PersonalizeContactOut(
            contact_id=payload.contact_id, status="error", error_text="contact_not_found",
        )
    contact = contacts[0]

    companies = await db.select(
        "companies",
        params={
            "select": "id,name,website,city,icp_segment,industry,research_json",
            "id": f"eq.{contact['company_id']}",
            "limit": "1",
        },
    )
    if not companies:
        return PersonalizeContactOut(
            contact_id=payload.contact_id, status="error", error_text="company_not_found",
        )
    company = companies[0]

    # Fetch Cal.com une fois (ou utilise l'override). Si échec : on tombe sur slots=[]
    # et le prompt fallback sur un CTA générique.
    slots: list[dict[str, Any]] = payload.available_slots or []
    if not payload.available_slots:
        from .lib.calcom import CalcomError, get_available_slots
        try:
            slots = get_available_slots(days_ahead=7)
        except CalcomError:
            slots = []

    return await _personalize_one(
        contact, company,
        template_choice=payload.template_choice,
        model=payload.model,
        persist=payload.persist,
        available_slots=slots,
        social_proof=_load_client_references(),
    )


class RunWf4In(BaseModel):
    """Pass complet WF-4 : prend N contacts à personnaliser, génère drafts.

    Cron-friendly. La sélection des contacts évite ceux qui ont déjà un draft outbound.
    `max_per_company=1` (défaut) garantit qu'on n'envoie pas plusieurs emails à
    la même entreprise dans un même batch (brûlerait la company).
    """
    limit: int = 10
    template_choice: str = "A"
    model: str = "claude-sonnet-4-6"
    persist: bool = True
    max_per_company: int = 1


class RunWf4Item(BaseModel):
    contact_id: str
    company_name: str | None = None
    status: str
    message_id: str | None = None
    template_used: str | None = None
    duration_ms: int | None = None
    error_text: str | None = None


class RunWf4Out(BaseModel):
    processed: int
    drafts_created: int
    skipped: int
    failed: int
    slots_available: int  # nb total créneaux Cal.com fetched
    items: list[RunWf4Item]


@app.post("/wf4/run", dependencies=[Depends(_require_auth)], response_model=RunWf4Out)
async def run_wf4(payload: RunWf4In) -> RunWf4Out:
    backlog = await db_tools.list_contacts_to_personalize(
        limit=payload.limit, max_per_company=payload.max_per_company,
    )

    # Fetch Cal.com une seule fois pour tout le batch — évite N appels API et
    # garantit que tous les emails du batch piochent dans la même liste de créneaux.
    from .lib.calcom import CalcomError, get_available_slots
    try:
        slots = get_available_slots(days_ahead=7)
    except CalcomError:
        slots = []
    total_slots = sum(len(s.get("times", [])) for s in slots)

    social_proof = _load_client_references()

    items: list[RunWf4Item] = []
    drafts = skipped = failed = 0

    for entry in backlog:
        contact = entry["contact"]
        company = entry["company"]
        try:
            res = await _personalize_one(
                contact, company,
                template_choice=payload.template_choice,
                model=payload.model,
                persist=payload.persist,
                available_slots=slots,
                social_proof=social_proof,
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            items.append(RunWf4Item(
                contact_id=contact["id"], company_name=company.get("name"),
                status="error", error_text=repr(e),
            ))
            continue

        if res.status == "ok":
            drafts += 1
        elif res.status.startswith("skipped"):
            skipped += 1
        else:
            failed += 1
        items.append(RunWf4Item(
            contact_id=contact["id"], company_name=company.get("name"),
            status=res.status, message_id=res.message_id,
            template_used=res.template_used, duration_ms=res.duration_ms,
            error_text=res.error_text,
        ))

    return RunWf4Out(
        processed=len(items), drafts_created=drafts,
        skipped=skipped, failed=failed,
        slots_available=total_slots, items=items,
    )
