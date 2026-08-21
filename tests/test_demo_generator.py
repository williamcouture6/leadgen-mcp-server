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


class TestPersonalizeTri:
    """Pivot tri (PT1) : le draft agence-ia part SANS lien démo et SANS
    pré-création de ligne agence.demo_sites. Un prompt sans {{DEMO_URL}} ne
    suffisait pas : l'ancien wiring appendait le lien au corps."""

    @staticmethod
    def _fake_personalize_out(body: str):
        class _Usage:
            input_tokens = 1
            output_tokens = 1
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        class _Out:
            email = {"subject": "Sujet", "body_text": body, "warnings": []}
            model = "test-model"
            duration_ms = 5
            template_used = "A"
            usage = _Usage()

        return _Out()

    async def test_agence_ia_draft_sans_lien_ni_demo_url(self, monkeypatch):
        from src import http_api

        out_obj = self._fake_personalize_out("Bonjour, votre site est déjà refait.")

        async def fake_personalize(_payload):
            return out_obj

        inserted: dict = {}

        async def fake_insert(payload):
            inserted.update(payload.model_dump(exclude_none=True))
            return {"message_id": "m-1"}

        async def fake_record(_payload):
            return {"agent_run_id": "ar-1"}

        monkeypatch.setattr(http_api.personalize_tools, "personalize", fake_personalize)
        monkeypatch.setattr(http_api.db_tools, "insert_message_draft", fake_insert)
        monkeypatch.setattr(http_api.db_tools, "record_agent_run", fake_record)

        res = await http_api._personalize_one(
            {"id": "ct-1", "email": "jean@plomberiex.ca"},
            {"id": "co-1", "track": "agence-ia", "research_json": {"ok": True}},
            template_choice="auto", model="test-model", persist=True,
            available_slots=[], social_proof=[],
        )

        assert res.status == "ok"
        assert inserted["body_text"] == "Bonjour, votre site est déjà refait."
        assert "aperçu personnalisé" not in inserted["body_text"]
        assert "couture-ia.com/demo" not in inserted["body_text"]
        assert "demo_url" not in inserted  # exclude_none : jamais posé

    async def test_http_api_nimporte_plus_le_demo_generator(self):
        from src import http_api

        assert not hasattr(http_api, "ensure_demo_site")
        assert not hasattr(http_api, "inject_demo_link")
