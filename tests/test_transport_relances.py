"""Les trois corps voyagent jusqu'à Instantly, ou rien ne part.

🔴 Cinq lentilles sur six du conseil de revue de la spec ont trouvé,
SÉPARÉMENT, qu'aucun code ne transportait les relances. Quand cinq équipes
aveugles l'une à l'autre trouvent le même trou, ce n'est pas du bruit.

Vérifié le 2026-08-30 : `add_lead_to_campaign` ne poussait que `email_subject`
et `email_body`. Les relances étaient écrites, jugées, stockées en base — et le
lead partait avec le seul corps de tri.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.lib import instantly as instantly_lib
from src.tools import send as send_tools

TRIPLET = {"relance_1": "corps de la relance 1", "relance_2": "corps de la relance 2"}


# ---------------- Le payload reellement destine a Instantly ----------------

@pytest.mark.asyncio
async def test_le_payload_porte_les_deux_relances(monkeypatch: pytest.MonkeyPatch) -> None:
    """Critère de fin nº11 de la spec du 2026-08-26 — celui qui manquait, et
    dont l'absence est ce qui laissait passer le tuyau non percé : le contrôle
    doit porter sur le PAYLOAD, pas sur `messages.followups`."""
    capture: dict[str, Any] = {}

    class FausseReponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"id": "lead-1"}

    async def faux_post(client, url, headers=None, json=None):
        capture["body"] = json
        return FausseReponse()

    monkeypatch.setenv("INSTANTLY_API_KEY", "k")
    monkeypatch.setattr(instantly_lib, "_http_post_with_retry", faux_post)

    await instantly_lib.add_lead_to_campaign(
        email="a@ex.ca", subject="s", body_text="corps",
        campaign_id="camp-1", followups=TRIPLET,
    )

    variables = capture["body"]["custom_variables"]
    assert variables["email_body"] == "corps"
    assert variables["followup_1_body"] == "corps de la relance 1"
    assert variables["followup_2_body"] == "corps de la relance 2"


@pytest.mark.asyncio
async def test_sans_relances_les_variables_nexistent_pas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La piste OPT n'a pas de relances. Envoyer des variables vides pousserait
    Instantly à expédier deux courriels vides si la campagne portait les
    étapes."""
    capture: dict[str, Any] = {}

    class FausseReponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"id": "lead-1"}

    async def faux_post(client, url, headers=None, json=None):
        capture["body"] = json
        return FausseReponse()

    monkeypatch.setenv("INSTANTLY_API_KEY", "k")
    monkeypatch.setattr(instantly_lib, "_http_post_with_retry", faux_post)

    await instantly_lib.add_lead_to_campaign(
        email="a@ex.ca", subject="s", body_text="corps", campaign_id="camp-1",
    )

    assert "followup_1_body" not in capture["body"]["custom_variables"]


# ---------------- Le refus est TOTAL, jamais partiel ----------------

def _message(**extra: Any) -> dict[str, Any]:
    base = {
        "id": "m1", "subject": "s", "body_text": "corps", "to_email": "a@ex.ca",
        "status": "draft", "direction": "outbound", "compliance_check_passed": True,
        "contact_id": "ct-1", "track": "agence-ia", "compliance_notes": None,
        "followups": TRIPLET,
    }
    base.update(extra)
    return base


@pytest.fixture
def _pipeline_vert(monkeypatch: pytest.MonkeyPatch):
    """Neutralise tout ce qui précède le push, pour que le seul refus possible
    soit celui des relances."""
    monkeypatch.setenv("WARMUP_DISABLED", "true")

    def _poser(message: dict[str, Any]) -> None:
        async def faux_select(table, params=None):
            if table == "messages":
                return [message]
            if table == "contacts":
                return [{"id": "ct-1", "email": "a@ex.ca", "first_name": "A"}]
            if table == "companies":
                return [{"id": "co-1", "name": "Ex", "domain": "ex.ca"}]
            return []

        monkeypatch.setattr(send_tools.db, "select", faux_select)

    return _poser


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "followups,cas",
    [
        (None, "colonne NULL"),
        ({}, "objet vide"),
        ({"relance_1": "r1"}, "relance_2 absente"),
        ({"relance_1": "r1", "relance_2": ""}, "relance_2 vide"),
        ({"relance_1": "   ", "relance_2": "r2"}, "relance_1 blanche"),
    ],
)
async def test_un_triplet_incomplet_ne_part_jamais(
    _pipeline_vert, followups, cas: str
) -> None:
    """Un lead poussé sans ses relances recevrait UN courriel et personne ne le
    saurait : la campagne tournerait, les compteurs seraient verts, et 68 % des
    réponses positives — celles qui arrivent après la 2ᵉ touche — ne viendraient
    simplement jamais."""
    _pipeline_vert(_message(followups=followups))

    res = await send_tools.send_one_message(
        send_tools.SendMessageIn(message_id="m1", dry_run=True, campaign_id="camp-1")
    )

    assert res.status == "skipped_followups_manquants", f"{cas} → {res.status}"


@pytest.mark.asyncio
async def test_un_triplet_complet_passe(_pipeline_vert) -> None:
    _pipeline_vert(_message())

    res = await send_tools.send_one_message(
        send_tools.SendMessageIn(message_id="m1", dry_run=True, campaign_id="camp-1")
    )

    assert res.status == "ok", res.skipped_reason or res.error_text


@pytest.mark.asyncio
async def test_la_piste_OPT_nest_pas_soumise_a_la_garde(_pipeline_vert) -> None:
    """OPT n'a pas de relances et n'en aura jamais : lui appliquer la garde
    bloquerait 100 % de ses envois."""
    _pipeline_vert(_message(track="OPT", followups=None))

    res = await send_tools.send_one_message(
        send_tools.SendMessageIn(message_id="m1", dry_run=True, campaign_id="camp-1")
    )

    assert res.status == "ok", res.skipped_reason or res.error_text
