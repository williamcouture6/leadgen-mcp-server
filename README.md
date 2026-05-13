# leadgen-mcp

Serveur MCP custom (FastMCP, Python) qui expose les outils unifiés du pipeline lead gen PME QC aux agents Claude Code et à n8n.

## Tools exposés (Phase 1)

| Tool | Description |
|---|---|
| `db.next_sourcing_target` | Retourne le prochain `(city, sector)` à sourcer (cooldown 30j) |
| `db.start_sourcing_run` | Crée un `sourcing_runs` en `status=running` |
| `db.complete_sourcing_run` | Marque un run `completed` ou `failed`, met à jour métriques |
| `db.insert_company` | Insert avec dédup 3 clés (`google_place_id`, `neq`, `dedup_key`) |
| `db.list_recent_companies` | Liste les companies récentes pour vérif manuelle |
| `maps.search_places` | Google Places Nearby Search + Details (avec pagination) |

Tools Phase 1B (après purchase Apollo) : `enrich.apollo_match`, `db.insert_contact`.

## Endpoints HTTP supplémentaires (Phase 2 — WF-3 Research)

| Endpoint | Description |
|---|---|
| `GET /companies/to-research?limit=N&require_website=true` | Backlog des companies sans `research_json` |
| `POST /research/company` `{company_id, model?}` | Research d'une seule company (fetch Place Details avec reviews + scrape site + Claude Sonnet → persist `companies.research_json` + `agent_runs`) |
| `POST /wf3/run` `{limit, model?, require_website?}` | Pass complet : prend N companies du backlog et les traite séquentiellement |

Variables d'env requises (en plus de Phase 1) :
- `ANTHROPIC_API_KEY` (Claude Sonnet 4.6 pour le Research Agent)

## Setup

```powershell
cd mcp-server
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Le serveur lit `.env` à la racine du repo (pas dans `mcp-server/`).

## Lancement

```powershell
# Mode stdio MCP (pour Claude Code / mcp-inspector)
python -m src.server

# Mode HTTP REST (pour n8n cloud via HTTP Request node)
uvicorn src.http_api:app --host 0.0.0.0 --port 8765
```

L'API HTTP exige `AGENTS_HTTP_TOKEN` dans `.env` (header `Authorization: Bearer <token>`).

## Tests

```powershell
pytest
```
