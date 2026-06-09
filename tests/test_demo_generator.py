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
