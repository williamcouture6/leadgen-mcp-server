"""Tool `db` — accès Supabase pour le pipeline.

Phase 1 (sourcing) :
- next_sourcing_target : trouve le prochain (city, sector) à scraper (cooldown 30j)
- start_sourcing_run : crée une trace de pass (status=running)
- complete_sourcing_run : marque completed/failed avec métriques
- insert_company : insert avec dédup 3 clés (google_place_id, neq, dedup_key)
- list_recent_companies : pour vérif manuelle après WF-1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import supabase_client as db


# ----------------------------------------------------------------------
# Catalogue de cibles (city, sector) — ordre de priorité
# ----------------------------------------------------------------------
# Aligné avec docs/icp-playbooks.md. Pour MVP on reste sur 3 segments × top villes.
# Le sector correspond à un keyword Google Places (`type` ou `keyword`).

DEFAULT_CITIES: list[str] = [
    "Montréal", "Québec", "Laval", "Gatineau", "Longueuil",
    "Sherbrooke", "Saguenay", "Lévis", "Trois-Rivières", "Terrebonne",
]

# 3 segments ICP × keywords Google Places
SECTOR_CATALOG: dict[str, list[str]] = {
    "commerce_local": [
        # Restauration
        "restaurant",
        # Santé (5-50 empl., privé)
        "clinique dentaire",
        "clinique de physiothérapie",
        "clinique médicale privée",
        # Services auto
        "garage automobile",
        "carrosserie",
        "concessionnaire automobile",
        # Beauté / bien-être
        "spa",
        "salon de coiffure",
        "centre esthétique",
        # Retail local
        "boutique de mode",
        "quincaillerie",
        "animalerie",
        # Services résidentiels
        "plombier",
        "électricien",
        "entrepreneur CVAC",
        "paysagiste",
        "entretien ménager commercial",
        "entrepreneur en déneigement",
        "couvreur",
        "entrepreneur général en rénovation",
        "peintre résidentiel",
        "exterminateur",
        "inspecteur en bâtiment",
        "piscines et spas",
        "pavage",
        "réparation électroménagers",
        "lavage de vitres",
        "menuisier",
    ],
    "services_pro": [
        # Cabinets comptables / fiscalité
        "cabinet comptable",
        "CPA",
        # Juridique
        "cabinet d'avocats",
        "notaire",
        # RH / recrutement
        "agence de recrutement",
        "firme de consultation RH",
        # Agences créatives
        "agence de marketing",
        "agence web",
        "agence de design",
        # Ingénierie / architecture
        "firme d'ingénierie",
        "cabinet d'architecture",
        # Consultation
        "consultant en gestion",
    ],
    "manufacturier": [
        # Note : Google Places n'est pas idéal pour les manufacturiers
        # (peu de discoverabilité locale). Phase 1B Apollo prendra le relais.
        "manufacturier alimentaire",
        "manufacturier de boissons",
        "atelier de machinage",
        "fabricant de produits métalliques",
        "manufacturier de plastique",
        "fabricant d'emballages",
        "grossiste industriel",
        "fabricant d'équipement industriel",
    ],
}

COOLDOWN_DAYS = 30


def _all_targets() -> list[tuple[str, str, str]]:
    """Retourne la liste complète (city, sector, icp_segment) dans l'ordre de priorité."""
    targets: list[tuple[str, str, str]] = []
    for city in DEFAULT_CITIES:
        for icp, sectors in SECTOR_CATALOG.items():
            for sector in sectors:
                targets.append((city, sector, icp))
    return targets


# ----------------------------------------------------------------------
# Schémas Pydantic (input/output des tools)
# ----------------------------------------------------------------------

class NextTargetOut(BaseModel):
    city: str
    sector: str
    icp_segment: str
    reason: Literal["never_scraped", "cooldown_expired"]


class StartRunIn(BaseModel):
    city: str
    sector: str
    icp_segment: str
    search_query: str | None = None


class StartRunOut(BaseModel):
    run_id: str
    started_at: str


class CompleteRunIn(BaseModel):
    run_id: str
    status: Literal["completed", "failed"]
    next_page_token: str | None = None
    results_count: int = 0
    new_companies_count: int = 0
    duplicates_count: int = 0
    error_text: str | None = None


class CompanyIn(BaseModel):
    name: str
    google_place_id: str | None = None
    address: str | None = None
    city: str | None = None
    region: str = "QC"
    postal_code: str | None = None
    country: str = "CA"
    latitude: float | None = None
    longitude: float | None = None
    website: str | None = None
    domain: str | None = None
    icp_segment: str | None = None
    industry: str | None = None
    google_types: list[str] = Field(default_factory=list)
    estimated_employees: int | None = None
    google_rating: float | None = None
    google_reviews_count: int | None = None
    source: str = "google_places"
    raw_payload: dict[str, Any] | None = None


class InsertCompanyOut(BaseModel):
    status: Literal["inserted", "duplicate"]
    company_id: str | None = None
    dedup_reason: str | None = None


# ----------------------------------------------------------------------
# Logique
# ----------------------------------------------------------------------

async def next_sourcing_target() -> NextTargetOut | None:
    """Retourne la prochaine cible (city, sector) à scraper, ou None si rien à faire.

    Stratégie :
    1. On itère le catalogue dans l'ordre de priorité.
    2. Pour chaque (city, sector), on regarde le `max(created_at)` dans sourcing_runs.
    3. Si jamais scrapé OU >30j → on retourne cette cible.
    4. Sinon, on passe à la suivante.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS)).isoformat()

    # On charge toutes les runs récentes en une seule query pour éviter N requêtes.
    recent = await db.select(
        "sourcing_runs",
        params={
            "select": "city,sector,created_at",
            "created_at": f"gte.{cutoff}",
            "order": "created_at.desc",
        },
    )
    recent_keys = {(r["city"], r["sector"]) for r in recent}

    for city, sector, icp in _all_targets():
        if (city, sector) in recent_keys:
            continue
        reason: Literal["never_scraped", "cooldown_expired"] = (
            "cooldown_expired" if recent else "never_scraped"
        )
        return NextTargetOut(city=city, sector=sector, icp_segment=icp, reason=reason)
    return None


async def start_sourcing_run(payload: StartRunIn) -> StartRunOut:
    now = datetime.now(timezone.utc).isoformat()
    rows = await db.insert(
        "sourcing_runs",
        {
            "city": payload.city,
            "sector": payload.sector,
            "icp_segment": payload.icp_segment,
            "search_query": payload.search_query
            or f"{payload.sector} in {payload.city}",
            "status": "running",
            "started_at": now,
        },
    )
    row = rows[0]
    return StartRunOut(run_id=row["id"], started_at=row["started_at"])


async def complete_sourcing_run(payload: CompleteRunIn) -> dict[str, Any]:
    patch: dict[str, Any] = {
        "status": payload.status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "results_count": payload.results_count,
        "new_companies_count": payload.new_companies_count,
        "duplicates_count": payload.duplicates_count,
    }
    if payload.next_page_token is not None:
        patch["next_page_token"] = payload.next_page_token
    if payload.error_text:
        patch["error_text"] = payload.error_text
    rows = await db.update("sourcing_runs", patch, filters={"id": f"eq.{payload.run_id}"})
    return {"updated": len(rows)}


async def insert_company(payload: CompanyIn) -> InsertCompanyOut:
    """Insert avec dédup. Retourne 'duplicate' si une clé conflit."""
    # Pré-check sur les 2 clés business (google_place_id, neq).
    if payload.google_place_id:
        existing = await db.select(
            "companies",
            params={
                "select": "id",
                "google_place_id": f"eq.{payload.google_place_id}",
                "limit": "1",
            },
        )
        if existing:
            return InsertCompanyOut(
                status="duplicate",
                company_id=existing[0]["id"],
                dedup_reason="google_place_id",
            )

    row = payload.model_dump(exclude_none=False)
    # status par défaut = 'sourced' (défini dans la migration)
    try:
        rows = await db.insert("companies", row)
    except Exception as e:  # noqa: BLE001
        # PostgREST renvoie 409 sur conflit unique (dedup_key). On l'attrape grossièrement
        # et on relit la company existante via dedup_key.
        msg = str(e)
        if "23505" in msg or "duplicate key" in msg.lower() or "409" in msg:
            # Re-fetch par nom + ville + postal pour récupérer l'id existant
            existing = await db.select(
                "companies",
                params={
                    "select": "id",
                    "name": f"eq.{payload.name}",
                    "city": f"eq.{payload.city or ''}",
                    "limit": "1",
                },
            )
            if existing:
                return InsertCompanyOut(
                    status="duplicate",
                    company_id=existing[0]["id"],
                    dedup_reason="dedup_key",
                )
        raise
    return InsertCompanyOut(status="inserted", company_id=rows[0]["id"])


async def list_recent_companies(limit: int = 20) -> list[dict[str, Any]]:
    return await db.select(
        "companies",
        params={
            "select": "id,name,city,icp_segment,status,created_at,google_rating",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )


# ----------------------------------------------------------------------
# Contacts (Phase 1B — utilisé par WF-2 après enrichissement Apollo)
# ----------------------------------------------------------------------

class ContactIn(BaseModel):
    company_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    email_verified: bool = False
    email_verification_source: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    title: str | None = None
    seniority: str | None = None
    is_decision_maker: bool = False
    source: str = "apollo"
    raw_payload: dict[str, Any] | None = None


class InsertContactOut(BaseModel):
    status: Literal["inserted", "duplicate", "skipped_no_email"]
    contact_id: str | None = None


async def insert_contact(payload: ContactIn) -> InsertContactOut:
    """Insert contact, dédup sur (company_id, email)."""
    if not payload.email:
        return InsertContactOut(status="skipped_no_email")

    existing = await db.select(
        "contacts",
        params={
            "select": "id",
            "company_id": f"eq.{payload.company_id}",
            "email": f"eq.{payload.email}",
            "limit": "1",
        },
    )
    if existing:
        return InsertContactOut(status="duplicate", contact_id=existing[0]["id"])

    row = payload.model_dump(exclude_none=False)
    rows = await db.insert("contacts", row)
    return InsertContactOut(status="inserted", contact_id=rows[0]["id"])


async def mark_company_enriched(company_id: str, status: str = "enriched") -> dict[str, Any]:
    return {
        "updated": len(
            await db.update(
                "companies",
                {"status": status, "last_enriched_at": datetime.now(timezone.utc).isoformat()},
                filters={"id": f"eq.{company_id}"},
            )
        )
    }


async def mark_company_disqualified(company_id: str, reason: str) -> dict[str, Any]:
    """Marque une company comme disqualifiée (échec dur d'enrichissement, hors-ICP, etc.).

    `list_companies_to_enrich` filtre sur status='sourced' donc une disqualified
    sortira automatiquement du backlog WF-2 (et du backlog WF-3 via le filtre
    `status neq.disqualified`).
    """
    return {
        "updated": len(
            await db.update(
                "companies",
                {
                    "status": "disqualified",
                    "disqualified_reason": reason,
                    "last_enriched_at": datetime.now(timezone.utc).isoformat(),
                },
                filters={"id": f"eq.{company_id}"},
            )
        )
    }


async def update_company_apollo_fields(
    company_id: str,
    *,
    domain: str | None = None,
    estimated_employees: int | None = None,
) -> dict[str, Any]:
    """Patch les colonnes companies enrichies par Apollo (domain manquant, taille).

    On ne touche pas au `status` ici — c'est `mark_company_enriched` qui le fait
    à la fin du flow WF-2.
    """
    patch: dict[str, Any] = {}
    if domain:
        patch["domain"] = domain
    if estimated_employees is not None:
        patch["estimated_employees"] = estimated_employees
    if not patch:
        return {"updated": 0}
    rows = await db.update(
        "companies",
        patch,
        filters={"id": f"eq.{company_id}"},
    )
    return {"updated": len(rows)}


async def get_company(company_id: str) -> dict[str, Any] | None:
    rows = await db.select(
        "companies",
        params={
            "select": "id,name,domain,website,city,icp_segment,industry,status,google_place_id",
            "id": f"eq.{company_id}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


# ----------------------------------------------------------------------
# Personalize (Phase 2 — WF-4)
# ----------------------------------------------------------------------

def _contact_priority_score(contact: dict[str, Any]) -> int:
    """Score de priorité (plus bas = meilleur) pour choisir 1 contact par company.

    Apollo verified > Apollo > scrape nominative same-domain > scrape nominative
    personal-domain > scrape generic > other.

    Évite d'envoyer plusieurs emails à la même entreprise (brûle la company).
    Le contact retenu est celui qui a la plus forte probabilité de joindre un
    décideur réel.
    """
    src = contact.get("email_verification_source")
    raw = contact.get("raw_payload") or {}
    kind = raw.get("kind") if isinstance(raw, dict) else None
    email_dom = (contact.get("email") or "").rsplit("@", 1)[-1].lower()
    # Domaines persos (gmail, hotmail, etc.) — on déclasse vs same-domain.
    PERSONAL_DOMS = {
        "gmail.com", "hotmail.com", "hotmail.ca", "hotmail.fr",
        "outlook.com", "outlook.fr", "live.com", "live.ca",
        "yahoo.com", "yahoo.ca", "yahoo.fr",
        "icloud.com", "me.com", "videotron.ca", "sympatico.ca",
        "bellnet.ca", "rogers.com",
    }
    is_personal = email_dom in PERSONAL_DOMS

    if src == "apollo" and contact.get("email_verified"):
        return 1
    if src == "apollo":
        return 2
    if src == "website_scrape" and kind == "nominative" and not is_personal:
        return 3
    if src == "website_scrape" and kind == "nominative" and is_personal:
        return 4
    if src == "website_scrape" and kind == "generic":
        return 5
    return 9


async def list_contacts_to_personalize(
    limit: int = 20,
    *,
    require_research: bool = True,
    max_per_company: int = 1,
) -> list[dict[str, Any]]:
    """Contacts prêts pour personnalisation : email présent, company.research_json
    présent (sinon le prompt n'a rien à se mettre sous la dent), pas encore de
    draft outbound dans messages.

    `max_per_company` (défaut 1) limite le nombre de contacts retournés par
    company, en gardant les meilleurs selon `_contact_priority_score`. Évite
    d'envoyer plusieurs emails séparés à la même entreprise.

    On filtre côté Python plutôt que via une jointure PostgREST compliquée :
    1) On récupère les contacts avec email + status='new' ou 'ready'.
    2) On joint manuellement avec companies.research_json.
    3) On exclut ceux qui ont déjà un message outbound (peu importe le status).
    4) On garde les top-N contacts par company selon priorité.
    """
    contacts = await db.select(
        "contacts",
        params={
            "select": (
                "id,first_name,last_name,email,email_verified,title,company_id,"
                "status,email_verification_source,raw_payload"
            ),
            "email": "not.is.null",
            "status": "in.(new,ready)",
            "order": "created_at.asc",
            "limit": str(limit * 5),  # over-fetch, on filtrera + dédup par company
        },
    )
    if not contacts:
        return []

    company_ids = list({c["company_id"] for c in contacts})
    companies = await db.select(
        "companies",
        params={
            "select": "id,name,domain,website,city,icp_segment,industry,research_json",
            "id": f"in.({','.join(company_ids)})",
        },
    )
    by_id = {c["id"]: c for c in companies}

    existing_msgs = await db.select(
        "messages",
        params={
            "select": "contact_id",
            "contact_id": f"in.({','.join(c['id'] for c in contacts)})",
            "direction": "eq.outbound",
        },
    )
    already_drafted = {m["contact_id"] for m in existing_msgs}

    # Filtre + groupe par company
    eligible: dict[str, list[dict[str, Any]]] = {}
    for c in contacts:
        if c["id"] in already_drafted:
            continue
        company = by_id.get(c["company_id"])
        if not company:
            continue
        if require_research and not company.get("research_json"):
            continue
        eligible.setdefault(c["company_id"], []).append(c)

    out: list[dict[str, Any]] = []
    # Préserve l'ordre d'arrivée des companies (created_at.asc du premier contact).
    seen_companies: list[str] = []
    for c in contacts:
        if c["company_id"] in eligible and c["company_id"] not in seen_companies:
            seen_companies.append(c["company_id"])

    # Dédup global sur email : si plusieurs companies pointent vers le même email
    # (cas chaînes où Google Places retourne plusieurs succursales), garder
    # uniquement la première company rencontrée pour ce email.
    seen_emails: set[str] = set()
    for company_id in seen_companies:
        group = eligible[company_id]
        group.sort(key=_contact_priority_score)
        for c in group[:max_per_company]:
            email_key = (c.get("email") or "").lower()
            if email_key in seen_emails:
                continue
            seen_emails.add(email_key)
            out.append({"contact": c, "company": by_id[company_id]})
            if len(out) >= limit:
                return out
    return out


class MessageDraftIn(BaseModel):
    contact_id: str
    campaign_id: str | None = None
    sequence_step_id: str | None = None
    subject: str
    body_text: str
    from_email: str | None = None
    to_email: str
    generated_by_agent_run: str | None = None
    compliance_check_passed: bool | None = None
    compliance_notes: str | None = None


async def insert_message_draft(payload: MessageDraftIn) -> dict[str, Any]:
    """Insert un draft outbound dans messages. Le Compliance Agent (WF-5) le
    validera avant envoi."""
    row = payload.model_dump(exclude_none=True)
    row["direction"] = "outbound"
    row["status"] = "draft"
    rows = await db.insert("messages", row)
    return {"message_id": rows[0]["id"] if rows else None}


async def list_companies_to_enrich(limit: int = 50) -> list[dict[str, Any]]:
    """Companies status='sourced' à passer dans WF-2."""
    return await db.select(
        "companies",
        params={
            "select": "id,name,domain,website,city,icp_segment,industry",
            "status": "eq.sourced",
            "order": "created_at.asc",
            "limit": str(limit),
        },
    )


# ----------------------------------------------------------------------
# Research (Phase 2 — WF-3)
# ----------------------------------------------------------------------

async def list_companies_to_research(
    limit: int = 20,
    *,
    require_website: bool = True,
) -> list[dict[str, Any]]:
    """Companies sans research_json. On exige par défaut un website : pas de site =
    pas assez de matière pour un research utile, le coût LLM est gaspillé.

    `status='disqualified'` est aussi exclu pour ne pas brûler de tokens sur des
    leads écartés.
    """
    params: dict[str, str] = {
        "select": "id,name,domain,website,city,icp_segment,industry,google_place_id,status",
        "research_json": "is.null",
        "google_place_id": "not.is.null",
        "status": "neq.disqualified",
        "order": "created_at.asc",
        "limit": str(limit),
    }
    if require_website:
        params["website"] = "not.is.null"
    return await db.select("companies", params=params)


async def update_company_research(
    company_id: str,
    research_json: dict[str, Any],
) -> dict[str, Any]:
    """Patch companies.research_json. N'écrase pas le status — la sourcing flow
    le gère séparément. On met juste à jour le payload du Research Agent.
    """
    rows = await db.update(
        "companies",
        {"research_json": research_json},
        filters={"id": f"eq.{company_id}"},
    )
    return {"updated": len(rows)}


class AgentRunIn(BaseModel):
    agent: Literal["research", "personalization", "qualification", "call_prep", "compliance"]
    model: str
    company_id: str | None = None
    contact_id: str | None = None
    campaign_id: str | None = None
    input_payload: dict[str, Any] | None = None
    output_payload: dict[str, Any] | None = None
    error_text: str | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


async def record_agent_run(payload: AgentRunIn) -> dict[str, Any]:
    """Audit trail — chaque appel d'agent laisse une trace.

    Pas critique pour le pipeline (un échec d'insert ne doit pas bloquer le run).
    L'appelant peut try/except sans risque.
    """
    now = datetime.now(timezone.utc).isoformat()
    row = payload.model_dump(exclude_none=True)
    row["started_at"] = now
    row["finished_at"] = now
    rows = await db.insert("agent_runs", row)
    return {"agent_run_id": rows[0]["id"] if rows else None}
