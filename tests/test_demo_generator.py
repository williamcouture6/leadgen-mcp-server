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
