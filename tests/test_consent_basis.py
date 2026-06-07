# mcp-server/tests/test_consent_basis.py
from src.tools.db import _consent_basis_for_contact


def test_website_scrape_is_conspicuous():
    assert _consent_basis_for_contact(
        source="website", email_verification_source="website_scrape",
    ) == "implied_conspicuous"


def test_discovery_own_page_is_conspicuous():
    assert _consent_basis_for_contact(
        source="reacti_discovery", email_verification_source="reacti_discovery_own_page",
    ) == "implied_conspicuous"


def test_discovery_directory_is_legitimate_interest():
    assert _consent_basis_for_contact(
        source="reacti_discovery", email_verification_source="reacti_discovery_directory",
    ) == "legitimate_interest"
