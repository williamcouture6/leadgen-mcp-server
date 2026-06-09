"""Tests demo_generator : injection du lien + mint idempotent."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


class TestInjectDemoLink:
    def test_replaces_single_placeholder(self) -> None:
        from src.lib.demo_generator import inject_demo_link
        body = "Bonjour,\n\nVoici votre aperçu : {{DEMO_URL}}\n\nMerci"
        out = inject_demo_link(body, "https://couture-ia.com/demo/abc")
        assert "{{DEMO_URL}}" not in out
        assert "https://couture-ia.com/demo/abc" in out

    def test_replaces_all_placeholders(self) -> None:
        from src.lib.demo_generator import inject_demo_link
        body = "{{DEMO_URL}} ... {{DEMO_URL}}"
        out = inject_demo_link(body, "https://x/demo/t")
        assert out.count("https://x/demo/t") == 2
        assert "{{DEMO_URL}}" not in out

    def test_appends_when_placeholder_absent(self) -> None:
        from src.lib.demo_generator import inject_demo_link
        body = "Bonjour, voici une offre."
        out = inject_demo_link(body, "https://x/demo/t")
        assert out.startswith(body)
        assert "https://x/demo/t" in out
        assert len(out) > len(body)

    def test_appends_on_empty_body(self) -> None:
        from src.lib.demo_generator import inject_demo_link
        out = inject_demo_link("", "https://x/demo/t")
        assert "https://x/demo/t" in out


from unittest.mock import AsyncMock, patch


def _amock(*, return_value=None):
    return AsyncMock(return_value=return_value)


class TestEnsureDemoSite:
    @pytest.mark.asyncio
    async def test_reuses_existing_demo_site(self) -> None:
        from src.lib import demo_generator as dg

        existing = [{"url_unique": "https://couture-ia.com/demo/EXISTING"}]
        with patch.object(dg.db, "select", new=AsyncMock(return_value=existing)) as sel, \
             patch.object(dg.db, "insert", new=AsyncMock()) as ins:
            url = await dg.ensure_demo_site("co-1", "ct-1")

        assert url == "https://couture-ia.com/demo/EXISTING"
        ins.assert_not_called()
        # filtré par contact_id, sur le schéma agence
        assert sel.call_args.kwargs["schema"] == "agence"

    @pytest.mark.asyncio
    async def test_mints_when_none_exists(self) -> None:
        from src.lib import demo_generator as dg

        async def _fake_insert(table, row, **kwargs):
            # echo la ligne insérée (PostgREST return=representation)
            return [row]

        with patch.object(dg.db, "select", new=AsyncMock(return_value=[])), \
             patch.object(dg.db, "insert", new=AsyncMock(side_effect=_fake_insert)) as ins:
            url = await dg.ensure_demo_site("co-1", "ct-1")

        assert url.startswith("https://couture-ia.com/demo/")
        # insert sur le schéma agence, statut genere, token non vide
        row = ins.call_args.args[1]
        assert ins.call_args.kwargs["schema"] == "agence"
        assert row["statut"] == "genere"
        assert row["company_id"] == "co-1"
        assert row["contact_id"] == "ct-1"
        assert row["token"] and row["url_unique"].endswith(row["token"])


class TestMessageDraftDemoUrl:
    def test_demo_url_field_included_when_set(self) -> None:
        from src.tools.db import MessageDraftIn
        m = MessageDraftIn(
            contact_id="ct-1", subject="s", body_text="b",
            to_email="x@y.com", demo_url="https://couture-ia.com/demo/t",
        )
        dumped = m.model_dump(exclude_none=True)
        assert dumped["demo_url"] == "https://couture-ia.com/demo/t"

    def test_demo_url_omitted_when_none(self) -> None:
        from src.tools.db import MessageDraftIn
        m = MessageDraftIn(contact_id="ct-1", subject="s", body_text="b", to_email="x@y.com")
        assert "demo_url" not in m.model_dump(exclude_none=True)


class TestPersonalizeWiring:
    """Le wiring demo dans _personalize_one : gated agence-ia, soft-fail."""

    def _company(self, track: str) -> dict:
        return {"id": "co-1", "name": "Plomberie X", "website": None,
                "city": "Sherbrooke", "icp_segment": None, "industry": None,
                "research_json": {"k": "v"}, "track": track}

    def _contact(self) -> dict:
        return {"id": "ct-1", "first_name": "Jean", "last_name": "Roy",
                "email": "jean@plomberiex.ca", "title": None, "company_id": "co-1"}

    @pytest.mark.asyncio
    async def test_agence_ia_sets_demo_url_and_link(self, monkeypatch) -> None:
        from src import http_api
        from src.tools import personalize as ptools

        # personalize renvoie un body avec le placeholder
        async def _fake_personalize(payload):
            return ptools.PersonalizeOut(
                email={"subject": "Sujet", "body_text": "Allo {{DEMO_URL}}", "warnings": []},
                template_used="A", contact_used=True, social_proof_count=0,
                available_slots_at_generation=[], duration_ms=10,
                model="claude-sonnet-4-6", usage=ptools.LLMUsage(),
            )
        monkeypatch.setattr(http_api.personalize_tools, "personalize", _fake_personalize)
        monkeypatch.setattr(http_api.db_tools, "record_agent_run",
                            _amock(return_value={"agent_run_id": "ar-1"}))
        monkeypatch.setattr(http_api, "ensure_demo_site",
                            _amock(return_value="https://couture-ia.com/demo/TOK"))

        captured = {}
        async def _capture_insert(payload):
            captured["body"] = payload.body_text
            captured["demo_url"] = payload.demo_url
            return {"message_id": "m-1"}
        monkeypatch.setattr(http_api.db_tools, "insert_message_draft", _capture_insert)

        out = await http_api._personalize_one(
            self._contact(), self._company("agence-ia"),
            template_choice="A", model="claude-sonnet-4-6", persist=True,
            available_slots=[], social_proof=[],
        )
        assert out.status == "ok"
        assert captured["demo_url"] == "https://couture-ia.com/demo/TOK"
        assert "{{DEMO_URL}}" not in captured["body"]
        assert "https://couture-ia.com/demo/TOK" in captured["body"]

    @pytest.mark.asyncio
    async def test_opt_track_skips_demo(self, monkeypatch) -> None:
        from src import http_api
        from src.tools import personalize as ptools

        async def _fake_personalize(payload):
            return ptools.PersonalizeOut(
                email={"subject": "S", "body_text": "Pas de placeholder ici", "warnings": []},
                template_used="A", contact_used=True, social_proof_count=0,
                available_slots_at_generation=[], duration_ms=10,
                model="claude-sonnet-4-6", usage=ptools.LLMUsage(),
            )
        monkeypatch.setattr(http_api.personalize_tools, "personalize", _fake_personalize)
        monkeypatch.setattr(http_api.db_tools, "record_agent_run",
                            _amock(return_value={"agent_run_id": "ar-1"}))
        ensure = _amock(return_value="https://x/demo/T")
        monkeypatch.setattr(http_api, "ensure_demo_site", ensure)

        captured = {}
        async def _capture_insert(payload):
            captured["demo_url"] = payload.demo_url
            captured["body"] = payload.body_text
            return {"message_id": "m-1"}
        monkeypatch.setattr(http_api.db_tools, "insert_message_draft", _capture_insert)

        out = await http_api._personalize_one(
            self._contact(), self._company("OPT"),
            template_choice="A", model="claude-sonnet-4-6", persist=True,
            available_slots=[], social_proof=[],
        )
        assert out.status == "ok"
        ensure.assert_not_called()
        assert captured["demo_url"] is None
        assert captured["body"] == "Pas de placeholder ici"

    @pytest.mark.asyncio
    async def test_demo_failure_soft_fails(self, monkeypatch) -> None:
        from src import http_api
        from src.tools import personalize as ptools

        async def _fake_personalize(payload):
            return ptools.PersonalizeOut(
                email={"subject": "S", "body_text": "Allo {{DEMO_URL}}", "warnings": []},
                template_used="A", contact_used=True, social_proof_count=0,
                available_slots_at_generation=[], duration_ms=10,
                model="claude-sonnet-4-6", usage=ptools.LLMUsage(),
            )
        monkeypatch.setattr(http_api.personalize_tools, "personalize", _fake_personalize)
        monkeypatch.setattr(http_api.db_tools, "record_agent_run",
                            _amock(return_value={"agent_run_id": "ar-1"}))

        async def _boom(*a, **k):
            raise RuntimeError("agence not exposed")
        monkeypatch.setattr(http_api, "ensure_demo_site", _boom)

        captured = {}
        async def _capture_insert(payload):
            captured["demo_url"] = payload.demo_url
            captured["body"] = payload.body_text
            captured["notes"] = payload.compliance_notes
            return {"message_id": "m-1"}
        monkeypatch.setattr(http_api.db_tools, "insert_message_draft", _capture_insert)

        out = await http_api._personalize_one(
            self._contact(), self._company("agence-ia"),
            template_choice="A", model="claude-sonnet-4-6", persist=True,
            available_slots=[], social_proof=[],
        )
        # draft inséré quand même, sans demo_url, placeholder laissé, warning posé
        assert out.status == "ok"
        assert captured["demo_url"] is None
        assert "{{DEMO_URL}}" in captured["body"]
        assert "demo" in (captured["notes"] or "").lower()
