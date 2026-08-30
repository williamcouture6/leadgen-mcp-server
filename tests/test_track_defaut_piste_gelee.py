"""Les défauts de piste ne visent plus `OPT`, gelée depuis le pivot du 2026-06-07.

Le pivot a retiré la piste `OPT` : sa donnée est conservée pour l'historique,
mais plus une seule boîte n'y est prospectée. Les défauts `track="OPT"` que le
code avait gardés sont devenus des pièges silencieux, et l'alerte de famine
ajoutée à `/wf4/run` les a rendus dangereux : elle compte les leads restants SUR
LE TRACK REÇU, donc un `/wf4/run` sans `track` explicite comptait sur une file
vide, concluait « fin de liste » et se taisait — exactement quand la vraie file
est pleine et qu'elle devrait crier.

La distinction qui tranche : un défaut de GÉNÉRATION (quelle piste on source,
qu'on rédige, qu'on juge) doit viser la piste vivante ; un défaut de LECTURE
(ce qu'on regarde) peut légitimement continuer d'inclure la piste gelée — c'est
le cas de `/summary/daily`, testé plus bas.
"""
from __future__ import annotations

import inspect

import pytest

from src import http_api
from src.http_api import DailySummaryIn, RunWf1In, RunWf4In

_GELEE = "OPT"
_VIVANTE = "agence-ia"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _defaut(fn, nom: str):
    return inspect.signature(fn).parameters[nom].default


# =====================================================================
# Défauts de génération — doivent viser la piste vivante
# =====================================================================

def test_wf4_ne_cible_plus_la_piste_gelee_par_defaut():
    """Le défaut de WF-4 alimente AUSSI `_compter_envoyables_restants` : posé
    sur `OPT`, il rendrait 0 restant sur une file vide et l'alerte de famine
    se tairait exactement quand elle devrait crier."""
    assert RunWf4In().track != _GELEE
    assert RunWf4In().track == _VIVANTE


def test_wf1_ne_source_plus_la_piste_gelee_par_defaut():
    """`RunWf1In.track` sert de catalogue de sourcing ET de tag à l'insert :
    sur `OPT`, un run nu remplirait la piste gelée de boîtes que WF-4 (qui
    filtre `agence-ia`) ne verrait jamais."""
    assert RunWf1In().track == _VIVANTE


def test_la_prochaine_cible_de_sourcing_vise_la_piste_vivante():
    assert _defaut(http_api.next_target, "track") == _VIVANTE


def test_le_backlog_a_personnaliser_vise_la_piste_vivante():
    """Ce GET est le MIROIR de la file de WF-4. S'il regardait une autre piste
    que celle que WF-4 rédige, un humain qui diagnostique la famine lirait la
    mauvaise file et conclurait « rien à faire »."""
    assert _defaut(http_api.contacts_to_personalize, "track") == _VIVANTE


def test_le_miroir_et_la_generation_regardent_la_meme_file():
    assert _defaut(http_api.contacts_to_personalize, "track") == RunWf4In().track


# =====================================================================
# Le filet de `_personalize_one` : une company sans track
# =====================================================================

@pytest.mark.asyncio
async def test_une_company_sans_track_est_redigee_dans_le_registre_vivant(monkeypatch):
    """`companies.track` est NOT NULL default 'agence-ia' (0003/0020), donc ce
    filet est mort en pratique. S'il tirait quand même, `OPT` ferait rédiger un
    corps VOUVOYÉ, alors que WF-5 relit le track RÉEL en base — `agence-ia` —
    et attend donc le tutoiement. Le filet fabriquerait le refus."""
    from src.tools import personalize as personalize_tools

    vus: dict[str, object] = {}

    async def faux_personalize(payload):
        vus["track"] = payload.track
        raise RuntimeError("stop — seul le track nous intéresse")

    monkeypatch.setattr(personalize_tools, "personalize", faux_personalize)

    async def faux_audit(payload):
        return None

    monkeypatch.setattr(http_api.db_tools, "record_agent_run", faux_audit)

    await http_api._personalize_one(
        {"id": "c-1", "email": "a@b.ca"},
        {"id": "co-1", "research_json": {"nom": "X"}},  # pas de clé `track`
        template_choice="A", model="m", persist=False,
        available_slots=[], social_proof=[],
    )
    assert vus["track"] == _VIVANTE


# =====================================================================
# Défaut de lecture — la piste gelée y reste, et c'est voulu
# =====================================================================

def test_le_resume_quotidien_regarde_encore_la_piste_gelee():
    """Ce défaut-ci ne fait RIEN générer, il choisit ce qu'on REGARDE. Une
    piste gelée qui se remettrait à produire des chiffres (cron oublié, insert
    manuel) doit rester visible : la retirer reviendrait à décider qu'on ne
    veut plus le savoir."""
    assert _GELEE in DailySummaryIn().tracks
    assert _VIVANTE in DailySummaryIn().tracks
