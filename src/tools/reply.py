"""Tool `reply` — Reply Handler (WF-7).

Reçoit un reply Instantly (via webhook), le classe via LLM, et orchestre l'action
appropriée :
  - `interested` (confidence ≥ AUTO_REPLY_CONFIDENCE_THRESHOLD) → compose reply
    avec Cal.com slots + envoie via Instantly /emails/reply + Slack ping
  - `interested` (confidence < seuil) OU compose/send failure → flag hot lead,
    pas d'auto-reply, Slack ping pour review manuel
  - `unsubscribe` → suppression_list + contact.status='opted_out'
  - `not_interested` → contact.status='disqualified'
  - `out_of_office` → log only, contact reste 'contacted'
  - `other` → flag review manuel, Slack ping

Idempotence : si on a déjà un row inbound dans `messages` avec le même
`provider_message_id`, on skip — Instantly peut renvoyer le webhook plusieurs fois.

Audit : chaque run écrit dans `agent_runs` (agent='qualification' — réutilisé du
schema enum existant ; reply classification est l'output du Qualification Agent
au sens large).
"""
from __future__ import annotations

import asyncio
import html as html_module
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    RateLimitError,
)
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .. import supabase_client as db
from ..lib import instantly as instantly_lib
from ..lib import slack as slack_lib
from ..lib.calcom import CalcomError, format_slots_for_prompt, get_available_slots

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

_CLASSIFIER_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "reply_classifier.md"
)
_COMPOSER_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "reply_compose.md"
)
_DEFAULT_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_COMPOSER_MODEL = "claude-sonnet-4-6"

# Seuil minimum de confidence pour déclencher auto-reply sur 'interested'.
# Sous ce seuil → flag hot lead + Slack ping pour review manuel.
AUTO_REPLY_CONFIDENCE_THRESHOLD = 0.8


def _booking_url() -> str:
    """URL Cal.com publique à inclure dans les replies auto.

    Lit CALCOM_BOOKING_URL en priorité (var spécifique au composer), puis
    BOOKING_URL (var partagée avec le reste de la config Cal.com). Fallback
    sur l'URL prod connue pour ne jamais envoyer de lien mort.
    """
    return (
        os.environ.get("CALCOM_BOOKING_URL", "").strip()
        or os.environ.get("BOOKING_URL", "").strip()
        or "https://cal.com/william-couture/20-min"
    )


def _sender_eaccount() -> str | None:
    """Email du sending account Instantly utilisé pour /emails/reply.

    DOIT être un sending account configuré dans le workspace Instantly. Sinon
    Instantly retourne 4xx. None = pas configuré → on saute l'auto-reply.
    """
    return os.environ.get("INSTANTLY_SENDER_EMAIL", "").strip() or None


# ----------------------------------------------------------------------
# LLM retry (réutilise le pattern de tools/compliance.py)
# ----------------------------------------------------------------------

def _is_transient_anthropic_error(exc: BaseException) -> bool:
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        return status in (502, 503, 504, 529)
    return False


def _parse_llm_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in LLM response: {text[:300]}")
    return json.loads(match.group(0))


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_classifier(
    reply_text: str,
    *,
    original_email_text: str | None,
    model: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Appel synchrone au classifier — wrappé en async via to_thread."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)
    system_prompt = _CLASSIFIER_PROMPT_PATH.read_text(encoding="utf-8")

    user_parts: list[str] = []
    if original_email_text:
        user_parts.append(
            "## Email d'origine envoyé par Couture IA (contexte uniquement)\n"
            f"```\n{original_email_text}\n```"
        )
    user_parts.append(
        f"## Reply reçu (à classer)\n```\n{reply_text}\n```"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=600,
        temperature=0.0,
        system=[
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": "\n\n".join(user_parts)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = resp.usage
    return (
        _parse_llm_json(text),
        {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        },
    )


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _call_composer(
    *,
    original_email_text: str,
    lead_reply_text: str,
    research_json: dict[str, Any] | None,
    available_slots: list[dict[str, Any]],
    booking_url: str,
    model: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Compose la réponse auto pour les leads 'interested'."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)
    system_prompt = _COMPOSER_PROMPT_PATH.read_text(encoding="utf-8")

    slots_block = format_slots_for_prompt(available_slots)

    user = (
        "## original_email (cold email envoyé)\n"
        f"```\n{original_email_text}\n```\n\n"
        "## lead_reply (réponse positive du prospect)\n"
        f"```\n{lead_reply_text}\n```\n\n"
        "## research_json (contexte entreprise)\n"
        f"```json\n{json.dumps(research_json or {}, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## booking_url\n`{booking_url}`\n\n"
        f"{slots_block}\n"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=1200,
        temperature=0.3,
        system=[
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = resp.usage
    return (
        _parse_llm_json(text),
        {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        },
    )


# ----------------------------------------------------------------------
# DB helpers (reply-specific — pas mis dans tools/db.py pour rester local)
# ----------------------------------------------------------------------

async def _find_inbound_by_provider_id(provider_message_id: str) -> dict[str, Any] | None:
    """Idempotence : retourne le row inbound existant si déjà processé."""
    if not provider_message_id:
        return None
    rows = await db.select(
        "messages",
        params={
            "select": "id,contact_id,status,created_at",
            "direction": "eq.inbound",
            "provider_message_id": f"eq.{provider_message_id}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _find_parent_outbound(
    *,
    parent_provider_id: str | None,
    lead_email: str | None,
) -> dict[str, Any] | None:
    """Trouve le message outbound auquel le reply répond.

    Priorité 1: match exact sur provider_message_id (= UUID Instantly).
    Priorité 2 (fallback): dernier outbound vers `lead_email` (au cas où
    Instantly ne nous renvoie pas le parent UUID dans le webhook).
    """
    if parent_provider_id:
        rows = await db.select(
            "messages",
            params={
                "select": "id,contact_id,campaign_id,sequence_step_id,subject,body_text,to_email,provider_message_id,sent_at",
                "direction": "eq.outbound",
                "provider_message_id": f"eq.{parent_provider_id}",
                "limit": "1",
            },
        )
        if rows:
            return rows[0]
    if lead_email:
        rows = await db.select(
            "messages",
            params={
                "select": "id,contact_id,campaign_id,sequence_step_id,subject,body_text,to_email,provider_message_id,sent_at",
                "direction": "eq.outbound",
                "to_email": f"eq.{lead_email}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if rows:
            return rows[0]
    return None


async def _find_contact_by_email(email: str) -> dict[str, Any] | None:
    if not email:
        return None
    rows = await db.select(
        "contacts",
        params={
            "select": "id,company_id,first_name,last_name,email,status",
            "email": f"eq.{email}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _get_company(company_id: str) -> dict[str, Any] | None:
    rows = await db.select(
        "companies",
        params={
            "select": "id,name,domain,city,icp_segment,industry,research_json",
            "id": f"eq.{company_id}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


async def _upsert_conversation(
    *,
    contact_id: str,
    campaign_id: str | None,
    state: str,
    last_direction: str,
    last_channel: str = "email",
) -> None:
    """Upsert conversation (unique sur contact_id+campaign_id).

    PostgREST upsert via `on_conflict=contact_id,campaign_id`. NULL campaign_id
    matche NULL dans Postgres pour le UNIQUE (avec NULLS NOT DISTINCT — Postgres
    15+, OK Supabase). À vérifier en pratique : si la contrainte n'autorise pas
    NULL=NULL, l'insert créera un nouveau row à chaque appel (acceptable, juste
    plus bruyant).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    row: dict[str, Any] = {
        "contact_id": contact_id,
        "campaign_id": campaign_id,
        "state": state,
        "last_direction": last_direction,
        "last_channel": last_channel,
        "last_activity_at": now_iso,
    }
    try:
        await db.insert(
            "conversations", row,
            on_conflict="contact_id,campaign_id",
            ignore_duplicates=False,  # merge → update existing row
        )
    except Exception as e:  # noqa: BLE001 — non bloquant, juste log
        print(f"[reply] upsert conversation failed: {e!r}")


async def _add_to_suppression(
    *,
    email: str,
    reason: str = "opt_out",
    source: str = "reply_parse",
    notes: str | None = None,
) -> None:
    """Insert dans suppression_list, idempotent (unique sur email)."""
    row: dict[str, Any] = {
        "email": email,
        "reason": reason,
        "source": source,
    }
    if notes:
        row["notes"] = notes
    try:
        await db.insert(
            "suppression_list", row,
            on_conflict="email",
            ignore_duplicates=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[reply] suppression insert failed: {e!r}")


async def _update_contact_status(contact_id: str, status: str) -> None:
    try:
        await db.update(
            "contacts", {"status": status},
            filters={"id": f"eq.{contact_id}"},
        )
    except Exception as e:  # noqa: BLE001
        print(f"[reply] contact status update failed: {e!r}")


async def _record_agent_run(
    *,
    contact_id: str | None,
    company_id: str | None,
    campaign_id: str | None,
    model: str,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any] | None,
    error_text: str | None,
    duration_ms: int,
    usage: dict[str, int] | None,
    agent: str = "qualification",
) -> str | None:
    row: dict[str, Any] = {
        "agent": agent,
        "model": model,
        "duration_ms": duration_ms,
        "input_payload": input_payload,
    }
    if contact_id:
        row["contact_id"] = contact_id
    if company_id:
        row["company_id"] = company_id
    if campaign_id:
        row["campaign_id"] = campaign_id
    if output_payload is not None:
        row["output_payload"] = output_payload
    if error_text:
        row["error_text"] = error_text
    if usage:
        row.update({
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        })
    try:
        rows = await db.insert("agent_runs", row)
        return rows[0]["id"] if rows else None
    except Exception as e:  # noqa: BLE001
        print(f"[reply] agent_run insert failed: {e!r}")
        return None


# ----------------------------------------------------------------------
# Reply body cleanup (strip quote + signature)
# ----------------------------------------------------------------------

_QUOTE_LINE = re.compile(r"^>\s?")
_QUOTE_HEADER_RE = re.compile(
    r"^(?:on\s+.+wrote:|le\s+.+a\s+écrit\s*:|-{3,}\s*original\s+message\s*-{3,}|"
    r"-{3,}\s*forwarded\s+message\s*-{3,}|from:\s+.+|de\s*:\s+.+|envoyé\s*:\s+.+|"
    r"sent:\s+.+)$",
    re.IGNORECASE,
)

# Lead-in d'une signature : "Cordialement,", "Merci,", "Sent from my iPhone", etc.
# Quand on hit cette ligne, on coupe — tout ce qui suit est signature.
# Pour les sig lead-ins, on accepte soit fin-de-ligne immédiate (Cordialement,)
# soit un suffix arbitraire (Sent from my iPhone, Envoyé depuis mon Android, etc.).
_SIG_LEADIN_RE = re.compile(
    r"^\s*(?:"
    r"(?:cordialement|sincèrement|sincerement|amicalement|cdlt|bien à vous|"
    r"merci(?:\s+d'avance)?|merci\s+et\s+bonne\s+journée|bonne\s+journée|"
    r"thanks|thank\s+you|regards|best\s+regards|cheers)[,!\s.]*"
    r"|"
    r"(?:sent\s+from\s+my|envoyé\s+depuis\s+mon)\b.*"
    r")$",
    re.IGNORECASE,
)
# Phone number heuristique (FR/QC) — détecte les sigs avec un tel
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")


def strip_quote_and_signature(body: str) -> str:
    """Retire les quotes du email d'origine et la signature.

    Heuristiques :
    - Coupe à la 1ère ligne qui matche QUOTE_HEADER_RE (header de citation)
    - Coupe les lignes consécutives commençant par `>` (quote markdown)
    - Coupe à `-- ` (RFC 3676 signature delimiter)
    - Coupe à un sig lead-in ("Cordialement,", "Merci,", etc.)
    - Post-pass : si les 1-5 dernières lignes ressemblent à une signature
      (lignes courtes + nom propre + phone/title), coupe.

    On veut un texte propre à donner au classifier pour ne pas qu'il analyse
    notre propre cold email d'origine cité, ni la signature qui peut polluer
    la classification (un téléphone dans la sig pourrait ressembler à un CTA).
    """
    if not body:
        return ""
    lines = body.splitlines()
    out: list[str] = []
    quote_block_started = False
    for line in lines:
        stripped = line.strip()
        # Header de quote → on coupe tout ce qui suit
        if _QUOTE_HEADER_RE.match(stripped):
            break
        # Signature RFC 3676 ou lead-in (Cordialement, etc.)
        if stripped == "--" or _SIG_LEADIN_RE.match(stripped):
            break
        # Ligne de quote `> ...`
        if _QUOTE_LINE.match(line):
            quote_block_started = True
            continue
        # Ligne vide après début de quote = on tolère un blanc, sinon on coupe
        if quote_block_started and not stripped:
            continue
        if quote_block_started and stripped:
            # Reprise de texte après quote = on garde
            quote_block_started = False
            out.append(line)
            continue
        out.append(line)
    cleaned = "\n".join(out).strip()

    # Post-pass : trim une signature en tail (1-5 lignes courtes contenant
    # phone/email/title). Conservateur pour ne pas tronquer un vrai message.
    tail_lines = cleaned.split("\n")
    if len(tail_lines) >= 3:
        for split_idx in range(len(tail_lines) - 1, max(0, len(tail_lines) - 6), -1):
            if not tail_lines[split_idx].strip():
                tail = tail_lines[split_idx + 1:]
                if 1 <= len(tail) <= 5 and all(len(l.strip()) < 80 for l in tail):
                    joined_tail = " ".join(tail)
                    if _PHONE_RE.search(joined_tail) or "@" in joined_tail:
                        cleaned = "\n".join(tail_lines[:split_idx]).rstrip()
                break
    return cleaned


def html_to_text(html_str: str) -> str:
    """Convertit du HTML brut en texte plat pour feed au classifier.

    Strip scripts/styles, convertit les block elements en newlines, retire les
    tags restants, decode les entités HTML (&amp; → &, etc.). Best-effort,
    pas un parseur HTML complet — suffit pour des replies email standards.
    """
    if not html_str:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_str, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</?(p|div|tr|li|h[1-6])[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_module.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

class HandleReplyIn(BaseModel):
    """Payload normalisé que le webhook handler passe à handle_reply.

    Le webhook FastAPI (`/wf7/instantly-webhook`) extrait ces champs du payload
    Instantly brut et les passe ici. Permet aussi le replay manuel depuis
    `/wf7/handle-reply` en injectant un payload synthétique.
    """
    lead_email: str
    reply_subject: str | None = None
    reply_body_text: str
    reply_body_html: str | None = None
    # UUID Instantly du message inbound (pour idempotence + comme reply_to_uuid)
    provider_message_id_inbound: str
    # UUID Instantly du message outbound parent (pour retrouver le contact)
    provider_message_id_parent: str | None = None
    received_at: str | None = None  # ISO timestamp
    # Override par défaut depuis env INSTANTLY_SENDER_EMAIL
    eaccount: str | None = None
    raw_payload: dict[str, Any] | None = None
    # Bypass auto-reply (utile pour testing / re-classifier sans renvoyer)
    skip_auto_reply: bool = False
    classifier_model: str = _DEFAULT_CLASSIFIER_MODEL
    composer_model: str = _DEFAULT_COMPOSER_MODEL


class HandleReplyOut(BaseModel):
    status: str  # "ok" | "skipped_duplicate" | "skipped_no_contact" | "error"
    inbound_message_id: str | None = None
    category: str | None = None
    confidence: float | None = None
    auto_reply_sent: bool = False
    auto_reply_provider_id: str | None = None
    auto_reply_message_id: str | None = None  # row id dans messages
    actions_taken: list[str] = []
    error_text: str | None = None
    duration_ms: int | None = None


async def handle_reply(payload: HandleReplyIn) -> HandleReplyOut:
    """Orchestrateur principal — appelé par le webhook handler ou en replay."""
    started = time.monotonic()
    actions: list[str] = []

    # 1) Idempotence
    existing = await _find_inbound_by_provider_id(payload.provider_message_id_inbound)
    if existing:
        return HandleReplyOut(
            status="skipped_duplicate",
            inbound_message_id=existing["id"],
            actions_taken=["already_processed"],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 2) Find parent outbound + contact
    parent = await _find_parent_outbound(
        parent_provider_id=payload.provider_message_id_parent,
        lead_email=payload.lead_email,
    )
    contact_id: str | None = parent.get("contact_id") if parent else None
    # On va aussi avoir besoin du contact_row plus tard pour la display (nom +
    # company). On le fetch une fois ici et on le réutilise — évite un 2e SELECT.
    contact_row: dict[str, Any] | None = None
    if not contact_id:
        # Fallback : peut-être qu'on a le contact mais pas le message parent
        # (ex: webhook arrive avant qu'on ait sync le message provider_id)
        contact_row = await _find_contact_by_email(payload.lead_email)
        if contact_row:
            contact_id = contact_row["id"]
        else:
            # Inconnu → log un inbound orphelin pour audit puis Slack ping
            try:
                ins_orphan = await db.insert(
                    "messages",
                    {
                        "direction": "inbound",
                        "status": "delivered",
                        "subject": payload.reply_subject,
                        "body_text": payload.reply_body_text,
                        "body_html": payload.reply_body_html,
                        "to_email": payload.eaccount or _sender_eaccount(),
                        "from_email": payload.lead_email,
                        "provider": "instantly",
                        "provider_message_id": payload.provider_message_id_inbound,
                    },
                )
                orphan_id = ins_orphan[0]["id"] if ins_orphan else None
            except Exception:  # noqa: BLE001
                orphan_id = None
            await slack_lib.notify(
                text=f"⚠️ Reply orphelin reçu de {payload.lead_email} — contact introuvable en DB",
                context="wf7_orphan_reply",
            )
            return HandleReplyOut(
                status="skipped_no_contact",
                inbound_message_id=orphan_id,
                actions_taken=["orphan_logged", "slack_ping"],
                error_text=f"no contact found for {payload.lead_email}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    campaign_id = parent.get("campaign_id") if parent else None
    parent_message_id = parent.get("id") if parent else None
    original_email_text = parent.get("body_text") if parent else None
    original_subject = parent.get("subject") if parent else None

    # 3) Insert inbound message (avant le LLM — pour avoir l'audit même si LLM crash)
    cleaned_reply = strip_quote_and_signature(payload.reply_body_text)
    inbound_row: dict[str, Any] = {
        "direction": "inbound",
        "status": "delivered",
        "contact_id": contact_id,
        "campaign_id": campaign_id,
        "subject": payload.reply_subject,
        "body_text": payload.reply_body_text,
        "body_html": payload.reply_body_html,
        "to_email": payload.eaccount or _sender_eaccount(),
        "from_email": payload.lead_email,
        "provider": "instantly",
        "provider_message_id": payload.provider_message_id_inbound,
    }
    if parent_message_id:
        # in_reply_to stocke le Message-ID header — on n'a pas l'header, on
        # met le provider_message_id du parent comme proxy.
        inbound_row["in_reply_to"] = payload.provider_message_id_parent
    try:
        ins = await db.insert("messages", inbound_row)
        inbound_message_id = ins[0]["id"] if ins else None
    except Exception as e:  # noqa: BLE001
        # Race condition: un autre process (poll concurrent / webhook + poll)
        # a inséré le même provider_message_id entre notre check d'idempotence
        # et notre INSERT. Le UNIQUE INDEX partiel messages_inbound_provider_id_unique_idx
        # rejette le 2e INSERT avec un 409 / code Postgres 23505. On traite ça
        # comme `skipped_duplicate` au lieu de `error` — un autre process a
        # déjà fait le travail et le LLM ne devrait pas être appelé 2 fois.
        err_str = repr(e)
        if "409" in err_str or "23505" in err_str or "duplicate key" in err_str.lower():
            # Re-fetch la row qui a gagné la race pour l'inclure dans la réponse
            existing = await _find_inbound_by_provider_id(payload.provider_message_id_inbound)
            return HandleReplyOut(
                status="skipped_duplicate",
                inbound_message_id=(existing or {}).get("id"),
                actions_taken=["race_lost_to_concurrent_insert"],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return HandleReplyOut(
            status="error",
            error_text=f"insert_inbound_failed: {e!r}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # Flag parent outbound comme replied
    if parent_message_id:
        try:
            await db.update(
                "messages",
                {"status": "replied", "replied_at": datetime.now(timezone.utc).isoformat()},
                filters={"id": f"eq.{parent_message_id}"},
            )
        except Exception:  # noqa: BLE001
            pass

    # 4) Classifier
    # contact_row peut avoir été fetché à l'étape 2 (path fallback) — réutiliser
    # si présent, sinon fetch maintenant.
    if contact_row is None:
        contact_row = await _find_contact_by_email(payload.lead_email)
    company_row: dict[str, Any] | None = None
    if contact_row and contact_row.get("company_id"):
        company_row = await _get_company(contact_row["company_id"])
    company_id = company_row.get("id") if company_row else None

    classifier_started = time.monotonic()
    try:
        classifier_out, cls_usage = await asyncio.to_thread(
            _call_classifier,
            cleaned_reply or payload.reply_body_text,
            original_email_text=original_email_text,
            model=payload.classifier_model,
        )
    except Exception as e:  # noqa: BLE001
        classifier_dur = int((time.monotonic() - classifier_started) * 1000)
        await _record_agent_run(
            contact_id=contact_id, company_id=company_id, campaign_id=campaign_id,
            model=payload.classifier_model,
            input_payload={"reply_excerpt": (cleaned_reply or "")[:500]},
            output_payload=None,
            error_text=f"classifier_failed: {e!r}",
            duration_ms=classifier_dur,
            usage=None,
        )
        # Sans classification on bloque l'auto-reply mais on ne crash pas — Slack
        # ping pour review humain.
        await slack_lib.notify(
            text=f"⚠️ Classifier LLM failed for {payload.lead_email} — review manuel requis",
            context="wf7_classifier_error",
        )
        return HandleReplyOut(
            status="error",
            inbound_message_id=inbound_message_id,
            actions_taken=["classifier_failed", "slack_ping"],
            error_text=f"classifier: {e!r}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    category = (classifier_out.get("category") or "other").lower()
    confidence = float(classifier_out.get("confidence") or 0.0)
    classifier_dur = int((time.monotonic() - classifier_started) * 1000)

    # 5) Branch par catégorie
    auto_reply_sent = False
    auto_reply_provider_id: str | None = None
    auto_reply_message_id: str | None = None
    contact_name = (
        f"{(contact_row or {}).get('first_name') or ''} "
        f"{(contact_row or {}).get('last_name') or ''}"
    ).strip() or payload.lead_email
    company_name = (company_row or {}).get("name") or "(entreprise inconnue)"

    if category == "unsubscribe":
        await _add_to_suppression(
            email=payload.lead_email,
            reason="opt_out",
            source="reply_parse",
            notes=f"reply_classified_unsubscribe; conf={confidence:.2f}",
        )
        actions.append("suppression_added")
        await _update_contact_status(contact_id, "opted_out")
        actions.append("contact_opted_out")
        await _upsert_conversation(
            contact_id=contact_id, campaign_id=campaign_id,
            state="lost", last_direction="inbound",
        )

    elif category == "not_interested":
        await _update_contact_status(contact_id, "disqualified")
        actions.append("contact_disqualified")
        # Soft suppression : on évite de les re-contacter dans 6 mois.
        # Pas dans suppression_list (réservé aux opt-outs durs).
        await _upsert_conversation(
            contact_id=contact_id, campaign_id=campaign_id,
            state="cold", last_direction="inbound",
        )

    elif category == "out_of_office":
        # Pas d'action — contact reste 'contacted'. WF-2/4 prendront le relais
        # à la prochaine sequence_step (Phase 3 follow-ups).
        await _upsert_conversation(
            contact_id=contact_id, campaign_id=campaign_id,
            state="nurturing", last_direction="inbound",
        )
        actions.append("ooo_logged")

    elif category == "interested":
        await _update_contact_status(contact_id, "replied")
        actions.append("contact_replied")
        await _upsert_conversation(
            contact_id=contact_id, campaign_id=campaign_id,
            state="hot", last_direction="inbound",
        )

        # Décider auto-reply ou review manuel
        eaccount = payload.eaccount or _sender_eaccount()
        # Defense in depth : on n'auto-reply QUE si le parent outbound avait
        # passé compliance. Si WF-5 avait flag le parent ou si on n'a pas de
        # parent du tout (orphan, ne devrait pas arriver ici car contact_id
        # check plus haut), on n'amplifie pas le risque.
        parent_compliance_ok = bool(parent and parent.get("compliance_check_passed") is True)
        can_auto_reply = (
            not payload.skip_auto_reply
            and confidence >= AUTO_REPLY_CONFIDENCE_THRESHOLD
            and eaccount is not None
            and payload.provider_message_id_inbound  # requis pour reply_to_uuid
            and parent_compliance_ok
        )
        if not parent_compliance_ok and parent is not None:
            actions.append("skipped_auto_reply_parent_not_compliant")

        if can_auto_reply:
            # Fetch Cal.com slots
            try:
                slots = await asyncio.to_thread(get_available_slots, 7)
            except CalcomError as e:
                slots = []
                actions.append(f"calcom_failed:{type(e).__name__}")

            if slots:
                # Compose
                composer_started = time.monotonic()
                try:
                    composed, comp_usage = await asyncio.to_thread(
                        _call_composer,
                        original_email_text=original_email_text or "",
                        lead_reply_text=cleaned_reply or payload.reply_body_text,
                        research_json=(company_row or {}).get("research_json"),
                        available_slots=slots,
                        booking_url=_booking_url(),
                        model=payload.composer_model,
                    )
                except Exception as e:  # noqa: BLE001
                    composed = None
                    comp_usage = None
                    actions.append(f"composer_failed:{type(e).__name__}")
                composer_dur = int((time.monotonic() - composer_started) * 1000)

                if composed and composed.get("body_text"):
                    # Audit le composer (Sonnet — modèle plus cher que le
                    # classifier Haiku). Sans ça, le coût composer est invisible
                    # dans les rapports agent_runs.
                    if comp_usage is not None:
                        await _record_agent_run(
                            contact_id=contact_id, company_id=company_id,
                            campaign_id=campaign_id,
                            agent="personalization",  # composer = sous-cas perso
                            model=payload.composer_model,
                            input_payload={
                                "agent_subtype": "reply_composer",
                                "lead_reply_excerpt": (cleaned_reply or "")[:300],
                                "slots_count": sum(len(s.get("times", [])) for s in slots),
                            },
                            output_payload={
                                "subject": composed.get("subject"),
                                "body_text": composed.get("body_text"),
                                "slots_used": composed.get("slots_used"),
                                "warnings": composed.get("warnings"),
                            },
                            error_text=None,
                            duration_ms=composer_dur,
                            usage=comp_usage,
                        )

                    reply_subject = (
                        composed.get("subject")
                        or f"Re: {original_subject or 'votre message'}"
                    )
                    reply_body = composed["body_text"]
                    try:
                        instantly_resp = await instantly_lib.reply_to_email(
                            reply_to_uuid=payload.provider_message_id_inbound,
                            eaccount=eaccount,
                            subject=reply_subject,
                            body_text=reply_body,
                        )
                        auto_reply_provider_id = str(instantly_resp.get("id") or "")
                        auto_reply_sent = True
                        actions.append("auto_reply_sent")
                    except instantly_lib.InstantlyError as e:
                        actions.append(f"instantly_reply_failed:{e}")

                    # Persist le message outbound auto-reply (même si Instantly failed,
                    # on log un draft pour audit)
                    out_row: dict[str, Any] = {
                        "direction": "outbound",
                        "status": "sent" if auto_reply_sent else "failed",
                        "contact_id": contact_id,
                        "campaign_id": campaign_id,
                        "subject": reply_subject,
                        "body_text": reply_body,
                        "to_email": payload.lead_email,
                        "from_email": eaccount,
                        "provider": "instantly",
                        "provider_message_id": auto_reply_provider_id or None,
                        "in_reply_to": payload.provider_message_id_inbound,
                        "compliance_check_passed": True,
                        "compliance_notes": f"auto_reply_to_interested; conf={confidence:.2f}",
                    }
                    if auto_reply_sent:
                        out_row["sent_at"] = datetime.now(timezone.utc).isoformat()
                    try:
                        ins_out = await db.insert("messages", out_row)
                        auto_reply_message_id = ins_out[0]["id"] if ins_out else None
                    except Exception as e:  # noqa: BLE001
                        actions.append(f"insert_outbound_reply_failed:{e!r}")
            else:
                actions.append("no_slots_available_skipped_compose")

        # Slack ping pour hot lead (qu'on ait auto-reply ou pas)
        fallback, blocks = slack_lib.build_hot_lead_blocks(
            contact_name=contact_name,
            company_name=company_name,
            contact_email=payload.lead_email,
            reply_preview=cleaned_reply or payload.reply_body_text,
            auto_reply_sent=auto_reply_sent,
            confidence=confidence,
        )
        await slack_lib.notify(text=fallback, blocks=blocks, context="wf7_hot_lead")
        actions.append("slack_hot_lead")

    else:  # 'other' ou catégorie inconnue
        await _upsert_conversation(
            contact_id=contact_id, campaign_id=campaign_id,
            state="needs_review", last_direction="inbound",
        )
        fallback, blocks = slack_lib.build_review_blocks(
            contact_name=contact_name,
            company_name=company_name,
            contact_email=payload.lead_email,
            category=category,
            confidence=confidence,
            reasoning=classifier_out.get("reasoning_one_line") or "(no reasoning)",
            reply_preview=cleaned_reply or payload.reply_body_text,
        )
        await slack_lib.notify(text=fallback, blocks=blocks, context="wf7_review")
        actions.append("slack_review")

    # 6) Audit
    await _record_agent_run(
        contact_id=contact_id, company_id=company_id, campaign_id=campaign_id,
        model=payload.classifier_model,
        input_payload={
            "reply_excerpt": (cleaned_reply or "")[:500],
            "lead_email": payload.lead_email,
            "had_parent": parent is not None,
        },
        output_payload={
            "classifier": classifier_out,
            "actions": actions,
            "auto_reply_sent": auto_reply_sent,
            "auto_reply_provider_id": auto_reply_provider_id,
        },
        error_text=None,
        duration_ms=classifier_dur,
        usage=cls_usage,
    )

    return HandleReplyOut(
        status="ok",
        inbound_message_id=inbound_message_id,
        category=category,
        confidence=confidence,
        auto_reply_sent=auto_reply_sent,
        auto_reply_provider_id=auto_reply_provider_id,
        auto_reply_message_id=auto_reply_message_id,
        actions_taken=actions,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


# ----------------------------------------------------------------------
# Webhook payload extraction (Instantly v2)
# ----------------------------------------------------------------------

def extract_from_instantly_email_list_item(item: dict[str, Any]) -> HandleReplyIn | None:
    """Convertit un item de la réponse GET /api/v2/emails (type=received) en
    HandleReplyIn. Shape légèrement différent du webhook event_type=reply_received.

    Champs courants dans Instantly v2 /emails (best-effort, on essaie plusieurs noms) :
      - id / uuid : UUID du message
      - from_address_email_list / from_email : adresse expéditeur
      - to_address_email_list : destinataire (= notre sending account)
      - subject
      - body : objet `{text: ..., html: ...}` OU string brut OU split en body_text/body_html
      - parent_email_uuid / in_reply_to_uuid / reply_to_uuid : UUID parent
      - timestamp_created
      - eaccount

    Returns None si l'item n'a pas les champs minimaux requis (id + from + body).
    """
    provider_id_inbound = str(
        item.get("id")
        or item.get("uuid")
        or ""
    ).strip()
    if not provider_id_inbound:
        return None

    # Champs from/to peuvent être string, dict, ou liste — normaliser
    def _first_str(v: Any) -> str | None:
        if not v:
            return None
        if isinstance(v, str):
            return v.strip().lower() or None
        if isinstance(v, dict):
            # Single object: {address: ..., name: ...} ou {email: ...}
            return (v.get("address") or v.get("email") or "").strip().lower() or None
        if isinstance(v, list) and v:
            head = v[0]
            if isinstance(head, str):
                return head.strip().lower() or None
            if isinstance(head, dict):
                return (head.get("address") or head.get("email") or "").strip().lower() or None
        return None

    lead_email = (
        _first_str(item.get("from_address_email_list"))
        or _first_str(item.get("from_address"))
        or _first_str(item.get("from_email"))
        or _first_str(item.get("from"))
    )
    if not lead_email:
        return None

    # Corps : peut être `body: {text, html}` ou `body_text` / `body_html` directement
    body_field = item.get("body")
    body_text: str | None = None
    body_html: str | None = None
    if isinstance(body_field, dict):
        body_text = body_field.get("text") or body_field.get("plain")
        body_html = body_field.get("html")
    elif isinstance(body_field, str):
        body_text = body_field
    body_text = body_text or item.get("body_text") or item.get("text") or item.get("plain_text")
    body_html = body_html or item.get("body_html") or item.get("html")

    if not body_text and not body_html:
        return None  # rien à classer

    # Si on n'a que du HTML, on génère un body_text exploitable par le classifier.
    # Sans ça, le classifier reçoit `<p>...</p>` brut et hallucine.
    if not body_text and body_html:
        body_text = html_to_text(body_html)

    eaccount = (
        _first_str(item.get("to_address_email_list"))
        or _first_str(item.get("to_address"))
        or item.get("eaccount")
    )

    return HandleReplyIn(
        lead_email=lead_email,
        reply_subject=item.get("subject"),
        reply_body_text=body_text or "(empty body after html strip)",
        reply_body_html=body_html,
        provider_message_id_inbound=provider_id_inbound,
        provider_message_id_parent=(
            item.get("parent_email_uuid")
            or item.get("in_reply_to_uuid")
            or item.get("reply_to_uuid")
            or item.get("thread_id")
        ),
        received_at=str(item.get("timestamp_created") or item.get("created_at") or ""),
        eaccount=eaccount,
        raw_payload=item,
    )


class PollRepliesIn(BaseModel):
    """Pass complet polling Instantly /emails (alternative au webhook)."""
    limit: int = 50  # max emails à fetch par run (Instantly cap ~100)
    skip_auto_reply: bool = False
    classifier_model: str = _DEFAULT_CLASSIFIER_MODEL
    composer_model: str = _DEFAULT_COMPOSER_MODEL


class PollRepliesItem(BaseModel):
    provider_message_id: str
    lead_email: str | None = None
    status: str
    category: str | None = None
    confidence: float | None = None
    actions: list[str] = []
    error_text: str | None = None


class PollRepliesOut(BaseModel):
    fetched: int
    processed: int
    skipped_duplicate: int
    skipped_invalid: int
    errors: int
    items: list[PollRepliesItem]


async def poll_and_process_replies(payload: PollRepliesIn) -> PollRepliesOut:
    """Fetch les N derniers emails received dans Instantly + process chaque
    nouveau via handle_reply. Idempotent : skip si provider_message_id déjà
    en `messages` (direction=inbound). Pas de cursor — on s'appuie sur l'idempotence.
    """
    try:
        resp = await instantly_lib.list_emails(
            email_type="received",
            limit=payload.limit,
        )
    except instantly_lib.InstantlyError as e:
        return PollRepliesOut(
            fetched=0, processed=0, skipped_duplicate=0,
            skipped_invalid=0, errors=1,
            items=[PollRepliesItem(
                provider_message_id="(none)",
                status="error",
                error_text=f"list_emails_failed: {e}",
            )],
        )

    # Extract items list — Instantly utilise `items` ou `data` selon version
    items_raw = resp.get("items") or resp.get("data") or []
    if not isinstance(items_raw, list):
        items_raw = []

    fetched = len(items_raw)
    processed = skipped_duplicate = skipped_invalid = errors = 0
    out_items: list[PollRepliesItem] = []

    for it in items_raw:
        extracted = extract_from_instantly_email_list_item(it if isinstance(it, dict) else {})
        if extracted is None:
            skipped_invalid += 1
            out_items.append(PollRepliesItem(
                provider_message_id=str((it or {}).get("id") or "(none)"),
                status="skipped_invalid",
                error_text="missing required fields (id/from/body)",
            ))
            continue

        # Préserve les overrides de modèles passés au poll
        extracted.skip_auto_reply = payload.skip_auto_reply
        extracted.classifier_model = payload.classifier_model
        extracted.composer_model = payload.composer_model

        try:
            res = await handle_reply(extracted)
        except Exception as e:  # noqa: BLE001
            errors += 1
            out_items.append(PollRepliesItem(
                provider_message_id=extracted.provider_message_id_inbound,
                lead_email=extracted.lead_email,
                status="error",
                error_text=f"handle_reply_failed: {e!r}",
            ))
            continue

        if res.status == "skipped_duplicate":
            skipped_duplicate += 1
        elif res.status == "ok":
            processed += 1
        else:
            errors += 1

        out_items.append(PollRepliesItem(
            provider_message_id=extracted.provider_message_id_inbound,
            lead_email=extracted.lead_email,
            status=res.status,
            category=res.category,
            confidence=res.confidence,
            actions=res.actions_taken,
            error_text=res.error_text,
        ))

    return PollRepliesOut(
        fetched=fetched,
        processed=processed,
        skipped_duplicate=skipped_duplicate,
        skipped_invalid=skipped_invalid,
        errors=errors,
        items=out_items,
    )


def extract_from_instantly_webhook(body: dict[str, Any]) -> HandleReplyIn | None:
    """Convertit un payload Instantly webhook brut en HandleReplyIn.

    Instantly v2 webhook payload shape (selon docs / observation) :
    - event_type: "reply_received" | "email_sent" | "lead_unsubscribed" | ...
    - lead_email: "prospect@..."
    - email_subject / email_text_body / email_html_body
    - reply_to_uuid OU reply_uuid : UUID du message inbound dans Instantly
    - in_reply_to / parent_email_uuid : UUID du message parent (outbound)
    - timestamp / event_timestamp
    - email_account / eaccount : compte d'envoi
    - campaign_id : UUID de la campagne

    Si le shape change, on log warning et retourne None plutôt que de crasher.
    Returns None si pas un event de type 'reply_received'.
    """
    event_type = (body.get("event_type") or body.get("event") or "").lower()
    if event_type and "reply" not in event_type:
        return None

    # Best-effort extraction — Instantly peut renommer ces champs entre versions
    lead_email = (
        body.get("lead_email")
        or body.get("email")
        or body.get("from_email")
        or ""
    ).strip().lower()
    if not lead_email:
        return None

    reply_subject = body.get("email_subject") or body.get("subject")
    reply_body_text = (
        body.get("email_text_body")
        or body.get("reply_text")
        or body.get("text")
        or body.get("body_text")
        or ""
    )
    reply_body_html = (
        body.get("email_html_body")
        or body.get("reply_html")
        or body.get("html")
        or body.get("body_html")
    )

    provider_id_inbound = (
        body.get("reply_uuid")
        or body.get("reply_to_uuid")  # ambiguïté possible — confirmer après 1er event
        or body.get("email_uuid")
        or body.get("uuid")
        or ""
    )
    provider_id_parent = (
        body.get("in_reply_to_uuid")
        or body.get("parent_email_uuid")
        or body.get("original_email_uuid")
        or body.get("in_reply_to")
    )

    eaccount = body.get("email_account") or body.get("eaccount")
    received_at = body.get("timestamp") or body.get("event_timestamp")

    if not provider_id_inbound:
        # Sans UUID inbound, on perd l'idempotence ET la capacité de reply
        # in-thread. On fabrique un ID synthétique pour quand même persister
        # l'audit, mais on flag. Ajout d'un suffix random pour éviter les
        # collisions si 2 webhooks arrivent dans la même seconde pour le même
        # lead_email (rare mais possible).
        provider_id_inbound = (
            f"synthetic-{lead_email}-{int(time.time())}-{secrets.token_hex(4)}"
        )

    # Si payload n'a que du HTML, dériver text via html_to_text pour le classifier
    if not reply_body_text and reply_body_html:
        reply_body_text = html_to_text(reply_body_html)

    return HandleReplyIn(
        lead_email=lead_email,
        reply_subject=reply_subject,
        reply_body_text=reply_body_text or "(empty body)",
        reply_body_html=reply_body_html,
        provider_message_id_inbound=str(provider_id_inbound),
        provider_message_id_parent=str(provider_id_parent) if provider_id_parent else None,
        received_at=str(received_at) if received_at else None,
        eaccount=eaccount,
        raw_payload=body,
    )
