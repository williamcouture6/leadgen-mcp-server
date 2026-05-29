"""Tests pour le tool meeting (Part B — rapport post-RDV Granola).

Couvre :
  - format_company_context (avec/sans research_json, shape wrappé)
  - analyze_meeting (LLM mocké via monkeypatch de _call_llm) + garde transcript vide
  - render_markdown (rapport complet + rapport partiel, pas de crash)

Aucun appel réseau : on patche meeting._call_llm.
"""
from __future__ import annotations

import asyncio

import pytest

from src.tools import meeting
from src.tools.meeting import LLMUsage


# ---------------------------------------------------------------------
# format_company_context
# ---------------------------------------------------------------------

def test_context_empty_when_nothing_provided() -> None:
    assert meeting.format_company_context(None, None) == "(aucun contexte fourni)"


def test_context_includes_contact_and_company() -> None:
    ctx = meeting.format_company_context(
        company={"name": "Plomberie A+", "industry": "plomberie", "city": "Montréal"},
        contact={"first_name": "Adam", "last_name": "Verge", "email": "adam@x.com"},
    )
    assert "Adam Verge" in ctx
    assert "adam@x.com" in ctx
    assert "Plomberie A+" in ctx
    assert "plomberie" in ctx


def test_context_pulls_research_summary_and_pains() -> None:
    ctx = meeting.format_company_context(
        company={
            "name": "Co",
            "research_json": {
                "company_summary": "Entreprise familiale de plomberie.",
                "pain_points_detected": [{"pain": "Pas de réponse hors heures"}],
            },
        },
    )
    assert "Entreprise familiale" in ctx
    assert "Pas de réponse hors heures" in ctx


def test_context_tolerates_wrapped_research_shape() -> None:
    ctx = meeting.format_company_context(
        company={"name": "Co", "research_json": {"research": {"company_summary": "Clinique privée."}}},
    )
    assert "Clinique privée" in ctx


# ---------------------------------------------------------------------
# analyze_meeting
# ---------------------------------------------------------------------

def test_analyze_meeting_raises_on_empty_transcript() -> None:
    with pytest.raises(ValueError):
        asyncio.run(meeting.analyze_meeting("   "))


def test_analyze_meeting_returns_report(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_report = {"resume_executif": "RDV test", "fit_score": "chaud"}

    def fake_call(transcript, company_context, model=meeting._DEFAULT_MODEL, max_tokens=3000):
        assert "appel" in transcript or transcript.strip()
        return fake_report, LLMUsage(input_tokens=10, output_tokens=20)

    monkeypatch.setattr(meeting, "_call_llm", fake_call)
    out = asyncio.run(meeting.analyze_meeting("transcript de l'appel", "(aucun contexte fourni)"))
    assert out.report == fake_report
    assert out.usage.input_tokens == 10
    assert out.model == meeting._DEFAULT_MODEL
    assert out.duration_ms >= 0


# ---------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------

def _full_report() -> dict:
    return {
        "resume_executif": "PME plomberie, veut récupérer les leads du soir.",
        "contexte_entreprise": "2 employés, beaucoup d'appels manqués.",
        "plans_objectifs_client": ["Embaucher un 3e plombier", "Doubler le résidentiel"],
        "problemes_identifies": [
            {"probleme": "Appels manqués hors heures", "verbatim": "On perd des clients le soir"},
        ],
        "automatisation_souhaitee_client": ["Répondre aux appels la nuit"],
        "opportunites_automatisation": [
            {"processus": "Prise de RDV", "solution": "Assistant 24/7", "impact": "Moins de no-shows",
             "complexite": "moyenne"},
        ],
        "angle_vente": "Commencer par l'assistant téléphonique 24/7.",
        "objections_signaux": ["Budget serré ce trimestre"],
        "prochaines_etapes": [
            {"action": "Envoyer une proposition", "responsable": "moi", "echeance": "vendredi"},
        ],
        "citations_cles": ["On perd des clients le soir"],
        "fit_score": "chaud",
    }


def test_render_markdown_full_report_has_all_sections() -> None:
    md = meeting.render_markdown(_full_report(), {"company_name": "Plomberie A+"})
    assert "# Rapport post-RDV — Plomberie A+" in md
    assert "## Résumé exécutif" in md
    assert "## Plans & objectifs du client" in md
    assert "## Problèmes identifiés" in md
    assert "## Ce que le client veut automatiser" in md
    assert "## Opportunités d'automatisation (repérées)" in md
    assert "## Angle de vente recommandé" in md
    assert "## Prochaines étapes" in md
    assert "Assistant 24/7" in md
    assert "[moi] Envoyer une proposition" in md
    assert "> On perd des clients le soir" in md


def test_render_markdown_handles_empty_report() -> None:
    """Rapport vide → pas de crash, sections listes affichent (aucun)."""
    md = meeting.render_markdown({}, {})
    assert "# Rapport post-RDV" in md
    assert "_(aucun)_" in md  # plans/problèmes vides


# ---------------------------------------------------------------------
# match_granola_note (Part C / WF-9)
# ---------------------------------------------------------------------

def _note(
    *, id="not_1", title="Meeting", valid=True,
    attendees=None, creator_email="me@couture-ia.com",
    created_at="2026-05-28T18:00:00Z", summary="…", transcript=None,
    gcal_id=None,
):
    n = {
        "id": id, "title": title, "valid_meeting": valid,
        "created_at": created_at, "summary": summary,
        "people": {
            "creator": {"email": creator_email},
            "attendees": [{"email": e} for e in (attendees or [])],
        },
    }
    if transcript is not None:
        n["transcript"] = transcript
    if gcal_id:
        n["google_calendar_event"] = {"id": gcal_id}
    return n


def test_match_picks_note_with_attendee_email() -> None:
    notes = [
        _note(id="not_other", attendees=["someone@else.com"]),
        _note(id="not_target", attendees=["adam@x.com"]),
    ]
    matched, score = meeting.match_granola_note(
        notes, attendee_email="adam@x.com",
        meeting_start_iso="2026-05-28T18:00:00Z",
    )
    assert matched is not None
    assert matched["id"] == "not_target"
    # 25 (valid) + 50 (email) + 20 (±2h) = 95
    assert score >= 50


def test_match_returns_none_when_no_signal() -> None:
    """Pure proximité temporelle ne suffit pas : on retourne None < threshold 50."""
    notes = [_note(attendees=["random@nope.com"])]
    matched, score = meeting.match_granola_note(
        notes, attendee_email="adam@x.com",
        meeting_start_iso="2026-05-28T18:00:00Z",
    )
    assert matched is None
    # Score = 25 (valid) + 20 (window) = 45 < 50
    assert score < 50


def test_match_gcal_id_beats_email() -> None:
    """GCal event ID = match déterministe, score le plus haut."""
    notes = [
        _note(id="not_email_only", attendees=["adam@x.com"]),
        _note(id="not_gcal", attendees=["nobody@x.com"], gcal_id="cal_abc"),
    ]
    matched, score = meeting.match_granola_note(
        notes, attendee_email="adam@x.com",
        meeting_start_iso="2026-05-28T18:00:00Z",
        gcal_event_id="cal_abc",
    )
    assert matched["id"] == "not_gcal"
    assert score >= 100


def test_match_rejects_invalid_meeting() -> None:
    """valid_meeting=False pénalise lourdement (−100)."""
    notes = [_note(attendees=["adam@x.com"], valid=False)]
    matched, score = meeting.match_granola_note(
        notes, attendee_email="adam@x.com",
        meeting_start_iso="2026-05-28T18:00:00Z",
    )
    # 50 (email) + 20 (window) − 100 = −30
    assert matched is None


def test_match_empty_list_returns_none() -> None:
    assert meeting.match_granola_note([], attendee_email="x", meeting_start_iso=None) == (None, 0)


# ---------------------------------------------------------------------
# granola_note_to_text
# ---------------------------------------------------------------------

def test_flatten_combines_title_summary_transcript() -> None:
    note = _note(
        title="Discovery — Plomberie A+",
        summary="Le client veut récupérer les leads du soir.",
        transcript=[
            {"speaker": {"source": "microphone"}, "text": "Bonjour Adam."},
            {"speaker": {"source": "speaker"}, "text": "Salut William."},
        ],
    )
    text = meeting.granola_note_to_text(note)
    assert "Discovery — Plomberie A+" in text
    assert "récupérer les leads du soir" in text
    assert "Bonjour Adam." in text
    assert "**microphone:**" in text
    assert "**speaker:**" in text


def test_flatten_handles_missing_transcript() -> None:
    note = _note(summary="Résumé seul.", transcript=None)
    text = meeting.granola_note_to_text(note)
    assert "Résumé seul." in text
    assert "Transcript" not in text


def test_flatten_tolerates_dict_notes_field() -> None:
    """Granola peut wrapper les notes en {content: '...'} ou ProseMirror."""
    note = _note(summary="x")
    note["notes"] = {"content": "Notes en dict format"}
    text = meeting.granola_note_to_text(note)
    assert "Notes en dict format" in text
