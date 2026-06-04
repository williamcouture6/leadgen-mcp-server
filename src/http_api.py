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

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel

from .lib.platform_domains import PLATFORM_DOMAINS_NEVER_USE
from .tools import booking as booking_tools
from .tools import compliance as compliance_tools
from .tools import db as db_tools
from .tools import enrich as enrich_tools
from .tools import maps as maps_tools
from .tools import personalize as personalize_tools
from .tools import reply as reply_tools
from .tools import research as research_tools
from .tools import send as send_tools
from .tools import send_status as send_status_tools


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

import logging

_startup_log = logging.getLogger("leadgen.startup")


@app.on_event("startup")
async def _validate_env_on_startup() -> None:
    """Fail-soft : loggue les env vars manquantes au démarrage (audit #10).

    Ne bloque jamais le boot — un warning visible dans les logs Railway au deploy
    vaut mieux qu'une feature qui no-op silencieusement des jours plus tard."""
    from .config import validate_env

    res = validate_env()
    if res["missing_required"]:
        _startup_log.error(
            "ENV REQUISES MANQUANTES: %s — le serveur risque de ne pas fonctionner",
            ", ".join(res["missing_required"]),
        )
    if res["missing_recommended"]:
        _startup_log.warning(
            "ENV recommandées manquantes (features dégradées): %s",
            ", ".join(res["missing_recommended"]),
        )
    if not res["missing_required"] and not res["missing_recommended"]:
        _startup_log.info("Config env OK (requises + recommandées présentes)")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


class AlertIn(BaseModel):
    text: str
    category: str = "errors"  # route vers SLACK_WEBHOOK_ERRORS (canal pannes pipeline)


@app.post("/alert", dependencies=[Depends(_require_auth)])
async def post_alert(payload: AlertIn) -> dict[str, Any]:
    """Poste une alerte Slack. Utilisé par le workflow n8n 'Error Handler' pour
    pinger les pannes de n'importe quel workflow (OPT + REACTI) dans le canal erreurs."""
    from .lib import slack as slack_lib

    ok = await slack_lib.notify(
        text=payload.text, context="n8n_error_handler", category=payload.category
    )
    return {"ok": ok, "category": payload.category}


class DailySummaryIn(BaseModel):
    category: str = "summary"          # canal Slack du résumé (SLACK_WEBHOOK_SUMMARY)
    tracks: list[str] = ["OPT", "REACTI"]
    post: bool = True                  # False = renvoie les chiffres sans poster (test)


@app.post("/summary/daily", dependencies=[Depends(_require_auth)])
async def summary_daily(payload: DailySummaryIn) -> dict[str, Any]:
    """Résumé quotidien de l'activité pipeline par track (sourcées/emails/drafts/
    envoyés/réponses) + RDV → Slack. Compté depuis minuit America/Toronto.
    Appelé par un cron n8n en fin de journée."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from . import supabase_client as sb
    from .lib import slack as slack_lib

    tz = ZoneInfo("America/Toronto")
    start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = start_local.astimezone(timezone.utc).isoformat()
    date_str = start_local.strftime("%Y-%m-%d")

    async def _cnt(table: str, extra: dict[str, str], date_field: str = "created_at") -> int:
        params = {"select": "id", date_field: f"gte.{cutoff}", **extra}
        rows = await sb.select(table, params=params)
        return len(rows)

    lines: list[str] = []
    totals: dict[str, Any] = {}
    for tk in payload.tracks:
        t = {"track": f"eq.{tk}"}
        sourced = await _cnt("companies", t)
        emails = await _cnt("contacts", t)
        drafts = await _cnt("messages", {**t, "direction": "eq.outbound", "status": "eq.draft"})
        sent = await _cnt(
            "messages", {**t, "direction": "eq.outbound", "status": "neq.draft"},
            date_field="scheduled_at",
        )
        replies = await _cnt("messages", {**t, "direction": "eq.inbound"})
        totals[tk] = {
            "sourced": sourced, "emails": emails, "drafts": drafts,
            "sent": sent, "replies": replies,
        }
        lines.append(
            f"*{tk}* — sourcées {sourced} · emails {emails} · drafts {drafts} · "
            f"envoyés {sent} · réponses {replies}"
        )

    bookings = await _cnt("booking_events", {})
    totals["bookings_total"] = bookings

    text = (
        f"📊 *Résumé quotidien — {date_str}*\n"
        + "\n".join(lines)
        + f"\n📅 RDV bookés: {bookings}"
    )

    posted = False
    if payload.post:
        posted = await slack_lib.notify(
            text=text, context="daily_summary", category=payload.category
        )
    return {"date": date_str, "totals": totals, "posted": posted, "text": text}


# ---------------- Sourcing ----------------

@app.get("/sourcing/next-target", dependencies=[Depends(_require_auth)])
async def next_target(track: str = "OPT") -> dict[str, Any] | None:
    t = await db_tools.next_sourcing_target(track=track)
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
async def companies_to_enrich(limit: int = 50, track: str = "OPT") -> list[dict[str, Any]]:
    return await db_tools.list_companies_to_enrich(limit=limit, track=track)


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

# Au-delà de ce nombre d'échecs research cumulés sur une même company, on la
# disqualifie pour qu'elle arrête de boucler dans le backlog WF-3 (voir handler
# d'erreur de /research/company).
_RESEARCH_MAX_FAILURES = 3


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
    if host in PLATFORM_DOMAINS_NEVER_USE:
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
    if existing_domain and existing_domain in PLATFORM_DOMAINS_NEVER_USE:
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
    track: str = "OPT"  # OPT | REACTI — isole le backlog enrichi par track
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
    backlog = await db_tools.list_companies_to_enrich(limit=payload.limit, track=payload.track)

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
    track: str = "OPT"  # OPT | REACTI — catalogue + tag à l'insert


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
        t = await db_tools.next_sourcing_target(track=payload.track)
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
                        track=payload.track,
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
    track: str = "OPT",
) -> list[dict[str, Any]]:
    """Companies sans research_json. Utilisé par n8n pour visualiser le backlog."""
    return await db_tools.list_companies_to_research(
        limit=limit, require_website=require_website, track=track
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
        # Garde-fou anti-coincement : `list_companies_to_research` re-sélectionne
        # toute company avec research_json=null, donc un échec récurrent (JSON
        # cassé, site/place inaccessible) revient chaque jour et bloque un slot du
        # batch indéfiniment. Au-delà de _RESEARCH_MAX_FAILURES échecs cumulés,
        # on disqualifie pour la sortir du backlog (exclu via status neq.disqualified).
        try:
            prior_failures = await db.select(
                "agent_runs",
                params={
                    "select": "id",
                    "agent": "eq.research",
                    "company_id": f"eq.{payload.company_id}",
                    "error_text": "not.is.null",
                    "limit": str(_RESEARCH_MAX_FAILURES + 1),
                },
            )
            if len(prior_failures) >= _RESEARCH_MAX_FAILURES:
                await db_tools.mark_company_disqualified(
                    payload.company_id,
                    f"research_failed_repeatedly ({len(prior_failures)}x): {e!r}"[:500],
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
    """Pass complet WF-3 : prend N companies sans research_json, les traite en parallèle borné.

    `concurrency` : nb de companies recherchées en parallèle (sémaphore bornée). Garde
    l'appel `/wf3/run` court — 10 en série ≈ 270-300s déclenchait un 502 edge Railway
    (timeout ~300s). En parallèle borné à 4, un lot de 10 tient en ~80-100s. Le retry
    interne de `_call_llm` (tenacity backoff) absorbe les 529 Anthropic transitoires ;
    plus besoin d'espacer les appels manuellement.

    `inter_company_sleep_seconds` : conservé pour rétro-compat de l'API, mais ignoré
    depuis le passage en parallèle (le sémaphore borne déjà la pression sur Anthropic).
    """
    limit: int = 10
    model: str = "claude-sonnet-4-6"
    require_website: bool = True
    concurrency: int = 4
    inter_company_sleep_seconds: float = 3.0  # déprécié — ignoré (cf. docstring)
    track: str = "OPT"  # OPT | REACTI — isole le backlog research par track


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
        limit=payload.limit, require_website=payload.require_website, track=payload.track
    )

    sem = asyncio.Semaphore(max(1, payload.concurrency))

    async def _research_one(
        co: dict[str, Any],
    ) -> tuple[dict[str, Any], "ResearchCompanyByIdOut | None", str | None]:
        async with sem:
            try:
                res = await research_company_by_id(
                    ResearchCompanyByIdIn(company_id=co["id"], model=payload.model)
                )
                return co, res, None
            except Exception as e:  # noqa: BLE001
                return co, None, repr(e)

    # Recherche les companies en parallèle (borné par `concurrency`) — garde l'appel
    # HTTP n8n unique ET court, vs ~270-300s en série qui déclenchait un 502 edge Railway.
    results = await asyncio.gather(*(_research_one(co) for co in backlog))

    items: list[RunWf3Item] = []
    succeeded = failed = skipped = 0
    for co, res, err in results:
        if res is None:
            failed += 1
            items.append(RunWf3Item(
                company_id=co["id"], name=co.get("name"),
                status="error", error_text=err,
            ))
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
    limit: int = 20, max_per_company: int = 1, track: str = "OPT",
) -> list[dict[str, Any]]:
    """Backlog WF-4 : contacts avec email + company.research_json + sans draft outbound.

    `max_per_company=1` (défaut) : un seul contact par entreprise, prioritisé.
    """
    return await db_tools.list_contacts_to_personalize(
        limit=limit, max_per_company=max_per_company, track=track,
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
                track=(company_row.get("track") or "OPT"),
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
        import asyncio
        from .lib.calcom import CalcomError, get_available_slots
        try:
            # get_available_slots est synchrone (httpx.get) — wrap via to_thread
            # pour ne pas bloquer l'event loop FastAPI pendant l'appel Cal.com
            # (jusqu'à 10s timeout).
            slots = await asyncio.to_thread(get_available_slots, days_ahead=7)
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
    track: str = "OPT"  # OPT | REACTI — isole le backlog personalize par track


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
        limit=payload.limit, max_per_company=payload.max_per_company, track=payload.track,
    )

    # Fetch Cal.com une seule fois pour tout le batch — évite N appels API et
    # garantit que tous les emails du batch piochent dans la même liste de créneaux.
    import asyncio
    from .lib.calcom import CalcomError, get_available_slots
    try:
        # Wrap sync httpx.get dans to_thread pour ne pas bloquer l'event loop
        # pendant l'appel Cal.com (jusqu'à 10s timeout).
        slots = await asyncio.to_thread(get_available_slots, days_ahead=7)
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


# ---------------- Compliance (Phase 2 — WF-5) ----------------

class ComplianceCheckIn(BaseModel):
    """Lance les 2 layers de compliance sur un draft.

    Si `persist=True` (défaut), met à jour `messages.compliance_check_passed`
    et `messages.compliance_notes` avec le verdict. `persist=False` = dry-run
    (utile pour QA, ne touche pas la DB).
    """
    message_id: str
    skip_llm: bool = False
    model: str = "claude-sonnet-4-6"
    persist: bool = True


@app.post(
    "/compliance/check",
    dependencies=[Depends(_require_auth)],
    response_model=compliance_tools.ComplianceCheckOut,
)
async def compliance_check(payload: ComplianceCheckIn) -> compliance_tools.ComplianceCheckOut:
    """Compliance d'UN draft. Pratique pour n8n traitement individuel."""
    from . import supabase_client as db

    # 1) Fetch le message + contact + company + agent_run (pour available_slots)
    msgs = await db.select(
        "messages",
        params={
            "select": "id,subject,body_text,contact_id,generated_by_agent_run,compliance_check_passed",
            "id": f"eq.{payload.message_id}",
            "limit": "1",
        },
    )
    if not msgs:
        return compliance_tools.ComplianceCheckOut(
            message_id=payload.message_id, verdict="error",
            send_decision="DO_NOT_SEND",
            error_text="message_not_found",
        )
    msg = msgs[0]

    contact_id = msg.get("contact_id")
    contact_rows = await db.select(
        "contacts",
        params={
            "select": "id,company_id,first_name,last_name,title,email_verification_source",
            "id": f"eq.{contact_id}",
            "limit": "1",
        },
    ) if contact_id else []
    if not contact_rows:
        return compliance_tools.ComplianceCheckOut(
            message_id=payload.message_id, verdict="error",
            send_decision="DO_NOT_SEND",
            error_text="contact_not_found",
        )
    company_id = contact_rows[0].get("company_id")
    # Destinataire vérifié = source de vérité de l'identité (prénom/titre), distincte du
    # research_json (scrape du site/page équipe). Sans ça, le juge LLM flagge à tort un
    # contact Apollo (OPT) absent de la page équipe comme "inventé". Track-agnostic :
    # email_source = apollo (OPT) | website_scrape (REACTI). Voir compliance.md §7.
    contact = {
        "first_name": contact_rows[0].get("first_name"),
        "last_name": contact_rows[0].get("last_name"),
        "title": contact_rows[0].get("title"),
        "email_source": contact_rows[0].get("email_verification_source"),
    }

    company_rows = await db.select(
        "companies",
        params={
            "select": "research_json",
            "id": f"eq.{company_id}",
            "limit": "1",
        },
    ) if company_id else []
    research_json = (company_rows[0].get("research_json") if company_rows else None) or {}

    # 2) Charger le contexte du draft (template + slots) depuis agent_runs
    template_used: str | None = None
    available_slots: list[dict[str, Any]] = []
    social_proof: list[dict[str, Any]] = _load_client_references()
    agent_run_id = msg.get("generated_by_agent_run")
    if agent_run_id:
        runs = await db.select(
            "agent_runs",
            params={
                "select": "input_payload,output_payload",
                "id": f"eq.{agent_run_id}",
                "limit": "1",
            },
        )
        if runs:
            inp = runs[0].get("input_payload") or {}
            outp = runs[0].get("output_payload") or {}
            template_used = (inp.get("template_choice")
                             or outp.get("template_used"))
            # available_slots peut être stocké dans input_payload mais on a juste un count
            # → on re-fetch Cal.com pour avoir la liste actuelle (acceptable car compliance
            # se fait peu après personalize, slots quasi identiques).

    if not available_slots:
        try:
            import asyncio
            from .lib.calcom import CalcomError, get_available_slots
            # Wrap sync httpx.get dans to_thread (event loop non bloqué).
            available_slots = await asyncio.to_thread(
                get_available_slots, days_ahead=14
            )
        except Exception:  # noqa: BLE001
            available_slots = []

    # 3) Run compliance
    try:
        out = await compliance_tools.compliance_check(
            message_id=payload.message_id,
            body=msg.get("body_text") or "",
            subject=msg.get("subject") or "",
            template_used=template_used,
            research_json=research_json,
            contact=contact,
            social_proof=social_proof,
            available_slots=available_slots,
            skip_llm=payload.skip_llm,
            model=payload.model,
        )
    except Exception as e:  # noqa: BLE001
        return compliance_tools.ComplianceCheckOut(
            message_id=payload.message_id, verdict="error",
            send_decision="DO_NOT_SEND",
            error_text=repr(e),
        )

    # 4) Persist verdict
    if payload.persist:
        try:
            await db.update(
                "messages",
                {
                    "compliance_check_passed": (out.verdict == "approved"),
                    "compliance_notes": compliance_tools.format_compliance_notes(out),
                },
                filters={"id": f"eq.{payload.message_id}"},
            )
        except Exception:  # noqa: BLE001
            pass  # Non bloquant — l'agent retourne le verdict même si update échoue

    return out


class RunWf5In(BaseModel):
    """Pass complet WF-5 : prend N drafts non encore validés, lance compliance.

    Limite : drafts avec `compliance_check_passed IS NULL` AND `status='draft'`.
    Re-traite ceux dont les notes contiennent "llm_error" (transient).

    `concurrency` : nb de drafts jugés en parallèle (sémaphore bornée). Garde
    l'appel `/wf5/run` court — 20 en série ≈ 130s déclenchait un 502 edge Railway.
    `inter_message_sleep_seconds` : conservé pour rétro-compat, ignoré (la
    sémaphore régule désormais la charge Anthropic).
    """
    limit: int = 20
    skip_llm: bool = False
    model: str = "claude-sonnet-4-6"
    concurrency: int = 4
    inter_message_sleep_seconds: float = 2.0


class RunWf5Item(BaseModel):
    message_id: str
    subject: str | None = None
    verdict: str
    send_decision: str
    duration_ms: int | None = None
    error_text: str | None = None


class RunWf5Out(BaseModel):
    processed: int
    approved: int
    needs_revision: int
    blocked: int
    errors: int
    items: list[RunWf5Item]


@app.post("/wf5/run", dependencies=[Depends(_require_auth)], response_model=RunWf5Out)
async def run_wf5(payload: RunWf5In) -> RunWf5Out:
    """Batch compliance sur tous les drafts non encore checked."""
    import asyncio
    from . import supabase_client as db

    # Fetch drafts pending compliance
    drafts = await db.select(
        "messages",
        params={
            "select": "id,subject",
            "direction": "eq.outbound",
            "status": "eq.draft",
            "compliance_check_passed": "is.null",
            "order": "created_at.asc",
            "limit": str(payload.limit),
        },
    )

    sem = asyncio.Semaphore(max(1, payload.concurrency))

    async def _judge_one(
        draft: dict[str, Any],
    ) -> tuple[dict[str, Any], compliance_tools.ComplianceCheckOut | None, str | None]:
        async with sem:
            try:
                res = await compliance_check(
                    ComplianceCheckIn(
                        message_id=draft["id"],
                        skip_llm=payload.skip_llm,
                        model=payload.model,
                        persist=True,
                    )
                )
                return draft, res, None
            except Exception as e:  # noqa: BLE001
                return draft, None, repr(e)

    # Juge les drafts en parallèle (borné par `concurrency`) — garde l'appel HTTP
    # n8n unique ET court, vs ~130s en série qui déclenchait un 502 edge Railway.
    results = await asyncio.gather(*(_judge_one(d) for d in drafts))

    items: list[RunWf5Item] = []
    approved = needs_revision = blocked = errors = 0
    for draft, res, err in results:
        if res is None:
            errors += 1
            items.append(RunWf5Item(
                message_id=draft["id"], subject=draft.get("subject"),
                verdict="error", send_decision="DO_NOT_SEND",
                error_text=err,
            ))
            continue
        if res.verdict == "approved":
            approved += 1
        elif res.verdict == "needs_revision":
            needs_revision += 1
        elif res.verdict == "blocked":
            blocked += 1
        else:
            errors += 1
        items.append(RunWf5Item(
            message_id=draft["id"], subject=draft.get("subject"),
            verdict=res.verdict, send_decision=res.send_decision,
            duration_ms=res.duration_ms, error_text=res.error_text,
        ))

    return RunWf5Out(
        processed=len(items), approved=approved,
        needs_revision=needs_revision, blocked=blocked, errors=errors,
        items=items,
    )


# ---------------- Send (Phase 2 — WF-6) ----------------

@app.post(
    "/send/message",
    dependencies=[Depends(_require_auth)],
    response_model=send_tools.SendMessageOut,
)
async def send_message(payload: send_tools.SendMessageIn) -> send_tools.SendMessageOut:
    """Push UN draft approuvé à Instantly. Idempotent : si status != 'draft',
    skip. Defense in depth : revérifie warmup gate + suppression list même
    si WF-5 a déjà approuvé.
    """
    return await send_tools.send_one_message(payload)


@app.post(
    "/wf6/run",
    dependencies=[Depends(_require_auth)],
    response_model=send_tools.RunWf6Out,
)
async def run_wf6(payload: send_tools.RunWf6In) -> send_tools.RunWf6Out:
    """Pass complet WF-6 : pousse jusqu'à `limit` drafts approuvés à Instantly,
    en respectant le daily cap (compté sur fenêtre America/Toronto).

    `dry_run=true` : simule le push sans appel Instantly (pour tester la
    sélection des drafts pendant le warmup).
    """
    return await send_tools.run_wf6(payload)


@app.get("/send/healthcheck", dependencies=[Depends(_require_auth)])
async def send_healthcheck() -> dict[str, Any]:
    """Vérifie que l'API Instantly est joignable et que la campagne existe.
    Utilisable comme smoke test avant d'activer le cron WF-6.

    Retourne toujours 200 — `ok=false` + `error=<msg>` si problème. Évite
    qu'un 500 cache le vrai diagnostic (env var manquante, réseau, etc.).
    """
    from .lib import instantly as instantly_lib
    try:
        camp = await instantly_lib.get_campaign()
        return {"ok": True, "campaign_id": camp.get("id"), "name": camp.get("name")}
    except Exception as e:  # noqa: BLE001 — endpoint diag, on veut tout voir
        return {"ok": False, "error_type": type(e).__name__, "error": str(e)[:500]}


@app.post(
    "/wf6/sync-status",
    dependencies=[Depends(_require_auth)],
    response_model=send_status_tools.SyncStatusOut,
)
async def wf6_sync_status(
    payload: send_status_tools.SyncStatusIn,
) -> send_status_tools.SyncStatusOut:
    """Réconcilie le statut d'envoi des messages 'queued' avec Instantly (audit #5).

    Pour chaque message outbound encore 'queued', interroge le lead Instantly
    (par l'id stocké dans provider_message_id) et flippe le statut :
    sent / bounced / replied. Sur hard bounce → ajoute l'email à suppression_list
    (reason='hard_bounce') ; sur unsubscribe → suppression (opt_out) + contact
    opted_out. Idempotent (ne touche que les 'queued').

    `dry_run=true` : retourne les outcomes sans écrire en DB (QA / 1ère validation
    du mapping des champs Instantly). Cron-friendly : à appeler ~toutes les 15 min
    pendant les fenêtres d'envoi.
    """
    return await send_status_tools.sync_send_status(payload)


# ---------------- Reply (Phase 2 — WF-7) ----------------

# Le webhook public utilise un secret en QUERY PARAM (pas Bearer) car Instantly
# ne sait pas envoyer de header custom standardisé sur tous les events. n8n nous
# relaie typiquement la requête, donc on garde la même convention en cas d'accès
# direct depuis Instantly (Phase 3 bypass n8n).
#
# Le secret est dans l'env WF7_WEBHOOK_SECRET. URL ressemble à :
#   POST /wf7/instantly-webhook?secret=<long_random>
#
# Choisir un secret >= 32 chars, non-deviné. À rotater régulièrement.

def _wf7_webhook_secret() -> str | None:
    return os.environ.get("WF7_WEBHOOK_SECRET", "").strip() or None


def _require_wf7_webhook_secret(secret: str | None) -> None:
    expected = _wf7_webhook_secret()
    if not expected:
        # Refuse en prod si pas configuré — éviter qu'un webhook public traîne
        # sans auth si l'env var est oubliée.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WF7_WEBHOOK_SECRET non défini côté serveur",
        )
    if not secret or not secrets.compare_digest(secret, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook secret")


@app.post("/wf7/instantly-webhook", response_model=reply_tools.HandleReplyOut)
async def wf7_instantly_webhook(
    payload: dict[str, Any],
    secret: str | None = None,
) -> reply_tools.HandleReplyOut:
    """Endpoint public — reçoit le webhook brut d'Instantly (via n8n relais ou
    direct). Auth via query param `?secret=<WF7_WEBHOOK_SECRET>`.

    Pipeline :
      1. Valide le secret
      2. Extrait les champs du payload Instantly (event_type, lead_email, body,
         provider IDs, etc.)
      3. Si pas un `reply_received` → retourne ok=ignored sans crash
      4. Délègue à `reply_tools.handle_reply` pour le LLM + actions DB
    """
    _require_wf7_webhook_secret(secret)

    extracted = reply_tools.extract_from_instantly_webhook(payload or {})
    if extracted is None:
        # Pas un reply event — on accepte le webhook mais on ne fait rien.
        return reply_tools.HandleReplyOut(
            status="ok",
            actions_taken=["event_ignored_not_reply"],
        )
    return await reply_tools.handle_reply(extracted)


@app.post(
    "/wf7/handle-reply",
    dependencies=[Depends(_require_auth)],
    response_model=reply_tools.HandleReplyOut,
)
async def wf7_handle_reply(payload: reply_tools.HandleReplyIn) -> reply_tools.HandleReplyOut:
    """Endpoint interne (Bearer) pour replay manuel d'un reply ou test.

    Permet de re-processer un reply en passant directement les champs normalisés
    (sans le payload Instantly brut). Utile pour QA, debug, ou pour re-classer
    avec un modèle différent.
    """
    return await reply_tools.handle_reply(payload)


@app.get("/wf7/hot-leads", dependencies=[Depends(_require_auth)])
async def wf7_hot_leads(limit: int = 50) -> list[dict[str, Any]]:
    """Liste les contacts hot (status='replied', conversation.state='hot').
    Dashboard manuel — utile si Slack pas configuré ou pour audit.
    """
    from . import supabase_client as db
    # Approximation simple : on liste les contacts récemment passés 'replied'.
    # Une vue SQL dédiée serait plus rigoureuse, suffit pour MVP.
    rows = await db.select(
        "contacts",
        params={
            "select": "id,first_name,last_name,email,company_id,status,updated_at",
            "status": "eq.replied",
            "order": "updated_at.desc",
            "limit": str(min(limit, 200)),
        },
    )
    # Enrichir avec company name
    out: list[dict[str, Any]] = []
    for r in rows:
        company_name: str | None = None
        cid = r.get("company_id")
        if cid:
            co_rows = await db.select(
                "companies",
                params={"select": "name,city", "id": f"eq.{cid}", "limit": "1"},
            )
            if co_rows:
                company_name = co_rows[0].get("name")
        out.append({
            "contact_id": r["id"],
            "name": f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip(),
            "email": r.get("email"),
            "company": company_name,
            "replied_at": r.get("updated_at"),
        })
    return out


@app.post(
    "/wf7/poll-replies",
    dependencies=[Depends(_require_auth)],
    response_model=reply_tools.PollRepliesOut,
)
async def wf7_poll_replies(payload: reply_tools.PollRepliesIn) -> reply_tools.PollRepliesOut:
    """Poll les N derniers emails received d'Instantly et traite ceux non encore
    processés (idempotent via provider_message_id). Alternative au webhook pour
    les plans Instantly sans webhook.

    Cron-friendly. Recommandé toutes les 5-10 min via n8n.
    """
    return await reply_tools.poll_and_process_replies(payload)


@app.get("/wf7/webhook-healthcheck")
async def wf7_webhook_healthcheck(secret: str | None = None) -> dict[str, Any]:
    """Vérifie que le secret webhook est bien configuré et que Slack répond.
    Public (auth via secret) — utile pour valider la config Railway sans
    déclencher de vrai reply.
    """
    _require_wf7_webhook_secret(secret)
    from .lib import slack as slack_lib
    slack_configured = bool(os.environ.get(slack_lib.SLACK_WEBHOOK_ENV))
    sender = os.environ.get("INSTANTLY_SENDER_EMAIL", "").strip() or None
    booking = os.environ.get("CALCOM_BOOKING_URL", "").strip() or None
    return {
        "ok": True,
        "wf7_secret_configured": True,
        "slack_configured": slack_configured,
        "instantly_sender_configured": bool(sender),
        "calcom_booking_url_configured": bool(booking),
        "auto_reply_confidence_threshold": reply_tools.AUTO_REPLY_CONFIDENCE_THRESHOLD,
    }


# ---------------- Booking (Phase 2 — WF-8) ----------------

# Cal.com webhook signe le raw body (HMAC-SHA256) avec un secret partagé.
# Le secret est dans `CALCOM_WEBHOOK_SECRET` (env). Le header envoyé par
# Cal.com est `X-Cal-Signature-256` (signature hex sans préfixe).
#
# Différence vs WF-7 webhook (Instantly) : Instantly utilise un secret query
# param. Cal.com supporte HMAC natif — on l'utilise.

def _calcom_webhook_secret() -> str | None:
    return os.environ.get("CALCOM_WEBHOOK_SECRET", "").strip() or None


@app.post(
    "/wf8/calcom-webhook",
    response_model=booking_tools.HandleBookingOut,
)
async def wf8_calcom_webhook(request: Request) -> booking_tools.HandleBookingOut:
    """Endpoint public Cal.com webhook — BOOKING_CREATED / RESCHEDULED /
    CANCELLED / MEETING_ENDED.

    Pipeline:
      1. Valide HMAC-SHA256 du raw body via `X-Cal-Signature-256`
      2. Parse JSON et extrait les champs Cal.com normalisés
      3. Persiste dans `booking_events`, update `conversations.state`
      4. Slack ping (build_booked_blocks pour CREATED, texte simple pour autres)
    """
    expected_secret = _calcom_webhook_secret()
    if not expected_secret:
        # Refuse en prod si pas configuré — éviter qu'un webhook public
        # accepte n'importe quoi si l'env var est oubliée.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CALCOM_WEBHOOK_SECRET non défini côté serveur",
        )

    raw_body = await request.body()
    signature = request.headers.get("X-Cal-Signature-256") or request.headers.get(
        "x-cal-signature-256"
    )
    if not booking_tools.verify_calcom_signature(raw_body, signature, expected_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook signature")

    import json as _json
    try:
        body = _json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except _json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid json body")

    extracted = booking_tools.extract_from_calcom_webhook(body or {})
    if extracted is None:
        return booking_tools.HandleBookingOut(
            status="ignored_unsupported_trigger",
            actions_taken=["payload_not_extractable"],
        )
    return await booking_tools.handle_calcom_booking(extracted)


class HandleBookingReplayIn(BaseModel):
    """Payload pour replay manuel (Bearer auth, pas de HMAC). Sert au QA et
    au re-processing d'un webhook capturé.
    """
    body: dict[str, Any]


@app.post(
    "/wf8/handle-booking",
    dependencies=[Depends(_require_auth)],
    response_model=booking_tools.HandleBookingOut,
)
async def wf8_handle_booking(payload: HandleBookingReplayIn) -> booking_tools.HandleBookingOut:
    """Replay manuel d'un webhook Cal.com (Bearer auth, bypass HMAC).

    Utile pour QA / debug / re-processer un event capturé. Le payload doit
    avoir le shape Cal.com brut (triggerEvent + payload).
    """
    extracted = booking_tools.extract_from_calcom_webhook(payload.body or {})
    if extracted is None:
        return booking_tools.HandleBookingOut(
            status="ignored_unsupported_trigger",
            actions_taken=["payload_not_extractable"],
        )
    return await booking_tools.handle_calcom_booking(extracted)


@app.get("/wf8/webhook-healthcheck")
async def wf8_webhook_healthcheck() -> dict[str, Any]:
    """Vérifie config WF-8. Public (pas d'auth — pas de secret à divulguer)."""
    from .lib import slack as slack_lib
    return {
        "ok": True,
        "wf8_secret_configured": bool(_calcom_webhook_secret()),
        "slack_configured": bool(os.environ.get(slack_lib.SLACK_WEBHOOK_ENV)),
    }


# ---------------- Meeting report (Phase 2 — WF-9, auto Granola) ----------------

# Pipeline auto post-RDV : n8n cron (toutes les 10 min) appelle
# `GET /wf9/pending-bookings` pour lister les booking_events finis sans rapport,
# puis pour chaque ID il appelle `POST /wf9/process-booking`. Le serveur fetch
# la note Granola correspondante (matching attendee email + window temporelle),
# appelle `meeting.analyze_meeting`, persiste le rapport et ping Slack.
#
# Granola enregistre LOCALEMENT sur la machine de William → si Granola ne
# tournait pas (ou pas de note pour ce booking), `process-booking` retourne
# `no_match_yet`. Le compteur `meeting_fetch_attempts` cap à 10 essais (~100 min)
# avant d'arrêter de re-tenter automatiquement.

MAX_FETCH_ATTEMPTS = 10


class Wf9PendingOut(BaseModel):
    booking_event_ids: list[str]
    count: int


@app.get(
    "/wf9/pending-bookings",
    dependencies=[Depends(_require_auth)],
    response_model=Wf9PendingOut,
)
async def wf9_pending_bookings(limit: int = 20) -> Wf9PendingOut:
    """Liste les booking_events finis (`meeting_outcome=held`) sans rapport encore
    généré (`meeting_analyzed_at IS NULL`) et qui n'ont pas dépassé le cap de
    re-tentatives Granola. Triés du plus ancien au plus récent.

    n8n cron toutes les 10 min : GET cette liste, puis POST /wf9/process-booking
    pour chaque ID.
    """
    from . import supabase_client as db_low

    rows = await db_low.select(
        "booking_events",
        params={
            "select": "id",
            "meeting_outcome": "eq.held",
            "meeting_analyzed_at": "is.null",
            "meeting_fetch_attempts": f"lt.{MAX_FETCH_ATTEMPTS}",
            "order": "meeting_scheduled_for.asc.nullsfirst",
            "limit": str(max(1, min(limit, 100))),
        },
    )
    ids = [r["id"] for r in rows if r.get("id")]
    return Wf9PendingOut(booking_event_ids=ids, count=len(ids))


class Wf9ProcessIn(BaseModel):
    booking_event_id: str


class Wf9ProcessOut(BaseModel):
    status: str  # "ok" | "no_match_yet" | "note_not_ready" | "max_attempts" | "skipped_no_attendee" | "error"
    booking_event_id: str
    note_id: str | None = None
    match_score: int | None = None
    fit_score: str | None = None
    attempts: int | None = None
    duration_ms: int | None = None
    error_text: str | None = None


@app.post(
    "/wf9/process-booking",
    dependencies=[Depends(_require_auth)],
    response_model=Wf9ProcessOut,
)
async def wf9_process_booking(payload: Wf9ProcessIn) -> Wf9ProcessOut:
    """Traite UN booking_event : fetch Granola note + analyse + persiste + Slack.

    Retours possibles :
      - `ok`              : note trouvée, rapport généré et persisté
      - `no_match_yet`    : aucune note Granola matche (ré-essai au prochain cron)
      - `note_not_ready`  : note trouvée mais summary IA pas encore prête (re-try)
      - `max_attempts`    : on a déjà tenté MAX_FETCH_ATTEMPTS fois → on lâche
      - `skipped_no_attendee` : booking sans email → impossible de matcher
      - `error`           : exception inattendue (Granola down, Anthropic, etc.)
    """
    import time
    from datetime import datetime, timedelta, timezone

    from . import supabase_client as db_low
    from .lib import granola as granola_lib
    from .lib import slack as slack_lib
    from .tools import meeting as meeting_tools

    started = time.monotonic()
    bid = payload.booking_event_id

    # 1) Charge le booking_event
    rows = await db_low.select(
        "booking_events",
        params={
            "select": "id,contact_id,external_event_id,meeting_scheduled_for,"
                      "meeting_outcome,meeting_analyzed_at,meeting_fetch_attempts",
            "id": f"eq.{bid}",
            "limit": "1",
        },
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"booking_event {bid} introuvable")
    be = rows[0]

    attempts = int(be.get("meeting_fetch_attempts") or 0)
    if attempts >= MAX_FETCH_ATTEMPTS:
        return Wf9ProcessOut(
            status="max_attempts", booking_event_id=bid, attempts=attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    if be.get("meeting_analyzed_at"):
        # Race avec un autre run : déjà traité.
        return Wf9ProcessOut(
            status="ok", booking_event_id=bid, attempts=attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 2) Charge contact + company (pour email de matching et contexte LLM)
    contact = None
    company = None
    if be.get("contact_id"):
        c_rows = await db_low.select(
            "contacts",
            params={
                "select": "id,company_id,first_name,last_name,email",
                "id": f"eq.{be['contact_id']}",
                "limit": "1",
            },
        )
        contact = c_rows[0] if c_rows else None
        if contact and contact.get("company_id"):
            co_rows = await db_low.select(
                "companies",
                params={
                    "select": "id,name,city,icp_segment,industry,research_json",
                    "id": f"eq.{contact['company_id']}",
                    "limit": "1",
                },
            )
            company = co_rows[0] if co_rows else None

    attendee_email = (contact or {}).get("email")
    if not attendee_email:
        # Pas d'email → matching impossible. On incrémente quand même pour
        # éviter une boucle infinie ; max_attempts y mettra fin.
        await db_low.update(
            "booking_events",
            {"meeting_fetch_attempts": attempts + 1},
            filters={"id": f"eq.{bid}"},
        )
        return Wf9ProcessOut(
            status="skipped_no_attendee", booking_event_id=bid, attempts=attempts + 1,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 3) Fetch Granola — window = meeting_start − 1h pour rattraper les notes
    # créées légèrement avant (timezone slop) ou tout de suite après.
    meeting_start = None
    if be.get("meeting_scheduled_for"):
        try:
            meeting_start = datetime.fromisoformat(
                str(be["meeting_scheduled_for"]).replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            meeting_start = None
    created_after = (meeting_start or datetime.now(timezone.utc)) - timedelta(hours=1)

    try:
        notes = await granola_lib.list_notes_paginated(
            created_after=created_after, max_pages=3,
        )
    except granola_lib.GranolaError as e:
        # Auth/clé manquante ou erreur permanente → on retourne error, on
        # n'incrémente PAS attempts (le problème est côté serveur, pas Granola
        # qui n'a pas encore généré la note).
        return Wf9ProcessOut(
            status="error", booking_event_id=bid, attempts=attempts,
            error_text=f"granola_list_failed: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    contact_name = None
    if contact:
        contact_name = (
            f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}"
        ).strip() or None

    matched, score = meeting_tools.match_granola_note(
        notes,
        attendee_email=attendee_email,
        meeting_start_iso=be.get("meeting_scheduled_for"),
        contact_name=contact_name,
        company_name=(company or {}).get("name"),
    )

    if matched is None:
        # Pas de match — incrémente attempts pour qu'on arrête après MAX_FETCH_ATTEMPTS
        await db_low.update(
            "booking_events",
            {"meeting_fetch_attempts": attempts + 1},
            filters={"id": f"eq.{bid}"},
        )
        return Wf9ProcessOut(
            status="no_match_yet", booking_event_id=bid, match_score=score,
            attempts=attempts + 1,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 4) Fetch transcript complet de la note matchée
    note_id = matched.get("id")
    try:
        full_note = await granola_lib.get_note(note_id, include_transcript=True) if note_id else matched
    except granola_lib.GranolaNoteNotReady:
        # Note trouvée mais summary IA pas encore générée. Re-try plus tard.
        await db_low.update(
            "booking_events",
            {"meeting_fetch_attempts": attempts + 1},
            filters={"id": f"eq.{bid}"},
        )
        return Wf9ProcessOut(
            status="note_not_ready", booking_event_id=bid, note_id=note_id,
            match_score=score, attempts=attempts + 1,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except granola_lib.GranolaError as e:
        return Wf9ProcessOut(
            status="error", booking_event_id=bid, note_id=note_id, attempts=attempts,
            error_text=f"granola_get_failed: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    transcript_blob = meeting_tools.granola_note_to_text(full_note)
    if not transcript_blob.strip():
        return Wf9ProcessOut(
            status="error", booking_event_id=bid, note_id=note_id, attempts=attempts,
            error_text="granola note vide après flattening",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 5) Analyse LLM
    context = meeting_tools.format_company_context(company, contact)
    try:
        out = await meeting_tools.analyze_meeting(transcript_blob, company_context=context)
    except Exception as e:  # noqa: BLE001
        return Wf9ProcessOut(
            status="error", booking_event_id=bid, note_id=note_id, attempts=attempts,
            error_text=f"analyze_failed: {type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 6) Persiste — meeting_source='granola' distingue du CLI manuel
    await db_low.update(
        "booking_events",
        {
            "meeting_report_json": out.report,
            "meeting_analyzed_at": datetime.now(timezone.utc).isoformat(),
            "meeting_source": "granola",
            "meeting_fetch_attempts": attempts + 1,
        },
        filters={"id": f"eq.{bid}"},
    )

    # 7) Slack ping #bookings — résumé court
    fit = out.report.get("fit_score") or "?"
    company_name = (company or {}).get("name") or ""
    summary_line = (out.report.get("resume_executif") or "").strip().replace("\n", " ")
    if len(summary_line) > 280:
        summary_line = summary_line[:279] + "…"
    top_opp = ""
    opps = out.report.get("opportunites_automatisation")
    if isinstance(opps, list) and opps:
        first = opps[0] if isinstance(opps[0], dict) else None
        if first and first.get("processus"):
            top_opp = f"\n*Top opportunité :* {first.get('processus')} → {first.get('solution', '')}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📝 Rapport post-RDV prêt — fit {fit}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{contact_name or attendee_email}*" + (f" @ *{company_name}*" if company_name else "")}},
    ]
    if summary_line:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary_line}})
    if top_opp:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": top_opp}})
    await slack_lib.notify(
        text=f"📝 Rapport post-RDV prêt — {contact_name or attendee_email} (fit {fit})",
        blocks=blocks, context="wf9_report_ready", category="bookings",
    )

    return Wf9ProcessOut(
        status="ok", booking_event_id=bid, note_id=note_id, match_score=score,
        fit_score=str(fit), attempts=attempts + 1,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


@app.get("/wf9/healthcheck")
async def wf9_healthcheck() -> dict[str, Any]:
    """Vérifie config WF-9. Public (pas de secret divulgué)."""
    from .lib import granola as granola_lib
    from .lib import slack as slack_lib
    return {
        "ok": True,
        "granola_key_configured": bool(os.environ.get(granola_lib.GRANOLA_API_KEY_ENV)),
        "anthropic_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "slack_bookings_configured": bool(
            os.environ.get("SLACK_WEBHOOK_BOOKINGS")
            or os.environ.get(slack_lib.SLACK_WEBHOOK_ENV)
        ),
        "max_fetch_attempts": MAX_FETCH_ATTEMPTS,
    }
