"""WF-6 Send — push approved drafts vers Instantly.

Logique :
  1. Lit `messages` où status='draft' AND compliance_check_passed=true AND
     direction='outbound' (déjà validé par WF-5).
  2. Defense in depth, dans l'ordre où le code les exécute :
     - Warmup gate (WARMUP_END_DATE) — refuse l'envoi pendant le warmup même
       si WF-5 a approuvé (cas où le draft a été approuvé avant la fenêtre).
     - Domaine de plateforme / big tech — filet final avant l'action
       irréversible : une adresse @facebook.com / @meta.com / @doordash.com
       arrivée en DB malgré les blocklists amont ne part jamais (message
       marqué 'failed').
     - Suppression list — check email + domaine du contact contre
       suppression_list (opt-outs, hard bounces, DNCL).
     - Daily cap — limite N pushs/jour, fenêtre America/Toronto.
  3. Fetch contact + company pour enrichir le lead Instantly (first_name,
     last_name, company_name).
  4. Push à Instantly via `lib/instantly.add_lead_to_campaign` — passe
     subject + body comme custom variables. La campagne Instantly est
     configurée par William avec template `{{email_subject}}` / `{{email_body}}`.
  5. Update messages : status='queued', provider='instantly',
     provider_message_id=<lead_id Instantly>, scheduled_at=now().

L'envoi réel se fera selon le schedule de la campagne Instantly.

SYNC DU STATUT (audit #5 — fermé 2026-05-31) : la réconciliation du statut
d'envoi est faite par `tools/send_status.py` (`POST /wf6/sync-status`, cron
WF-6b). Il interroge le LEAD Instantly via le `provider_message_id` stocké ici
et flippe `messages.status` → 'sent' / 'bounced' / 'replied', + ajoute les hard
bounces à `suppression_list`. Le mapping des champs Instantly reste à valider
sur le 1er vrai bounce (cf `classify_lead_outcome` + docs/go-live-checklist.md).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .. import supabase_client as db
from ..lib import instantly as instantly_lib
from ..lib.compliance_checks import check_warmup_window
from ..lib.relances import CLES_RELANCES
from ..lib.platform_domains import is_email_on_blocked_domain

DAILY_CAP_DEFAULT = 10
DAILY_CAP_ENV = "INSTANTLY_DAILY_CAP"
SEND_TIMEZONE = "America/Toronto"
# Sur-récolte : un draft sauté (warmup, skip transitoire) reste 'draft' et la
# requête FIFO le re-sélectionne ; on lit plus large que limit pour que la file
# tourne. On s'arrête dès que `limit` messages sont partis. Le plafond borne le
# coût quand la file est longue.
DRAFT_OVERFETCH_FACTOR = 5
DRAFT_OVERFETCH_MAX = 100


# ----------------------------------------------------------------------
# Modèles
# ----------------------------------------------------------------------

class SendMessageIn(BaseModel):
    message_id: str
    # Override l'ID de campagne — par défaut INSTANTLY_CAMPAIGN_ID env.
    campaign_id: str | None = None
    # True = ne pousse pas vraiment à Instantly, mais simule le succès.
    # Utile pour tester la sélection des drafts pendant le warmup.
    dry_run: bool = False


class SendMessageOut(BaseModel):
    message_id: str
    status: str  # ok | skipped_warmup | skipped_not_eligible | skipped_suppressed | skipped_platform_domain | error
    provider_message_id: str | None = None
    skipped_reason: str | None = None
    error_text: str | None = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _daily_cap() -> int:
    raw = os.environ.get(DAILY_CAP_ENV, "").strip()
    if not raw:
        return DAILY_CAP_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return DAILY_CAP_DEFAULT


def _today_start_utc_iso() -> str:
    """Début de la journée en heure de Toronto, converti UTC pour Postgres.

    On compte le daily cap sur la journée locale (Toronto), pas UTC, pour
    matcher l'expérience humaine (« j'ai envoyé 10 emails aujourd'hui »)
    et le sending window d'Instantly qui suit aussi le fuseau local.
    """
    tz = ZoneInfo(SEND_TIMEZONE)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).isoformat()


async def count_pushed_today() -> int:
    """Combien de drafts on a déjà handé off à Instantly aujourd'hui (Toronto).

    On compte les messages outbound dont scheduled_at >= today_start_local.
    `scheduled_at` est set par cette même fonction au moment du push.
    Note: cnt-only via PostgREST `select=count`/`Prefer: count=exact` aurait
    été plus efficace, mais ici N quotidien est petit (~10), SELECT suffit.
    """
    today_start = _today_start_utc_iso()
    rows = await db.select(
        "messages",
        params={
            "select": "id",
            "direction": "eq.outbound",
            "scheduled_at": f"gte.{today_start}",
            "status": "neq.draft",
        },
    )
    return len(rows)


async def _is_suppressed(email: str | None, domain: str | None) -> tuple[bool, str | None]:
    """True si l'email OU le domain est sur suppression_list.

    Couvre les 3 cas de suppression_list : email exact, domaine entier.
    (phone n'est pas pertinent pour l'envoi email.)
    """
    if email:
        em_rows = await db.select(
            "suppression_list",
            params={"select": "reason", "email": f"eq.{email}", "limit": "1"},
        )
        if em_rows:
            return True, f"email on suppression ({em_rows[0].get('reason')})"
    if domain:
        dom_rows = await db.select(
            "suppression_list",
            params={"select": "reason", "domain": f"eq.{domain}", "limit": "1"},
        )
        if dom_rows:
            return True, f"domain on suppression ({dom_rows[0].get('reason')})"
    return False, None


# ----------------------------------------------------------------------
# Core
# ----------------------------------------------------------------------

async def send_one_message(payload: SendMessageIn) -> SendMessageOut:
    """Push UN draft à Instantly. Idempotent par message_id : si la message
    n'est plus en status='draft', on skip (évite double-push si retry n8n).
    """
    # 1) Fetch message + verify éligibilité
    msgs = await db.select(
        "messages",
        params={
            "select": "id,subject,body_text,to_email,status,direction,compliance_check_passed,contact_id,track,compliance_notes,followups",
            "id": f"eq.{payload.message_id}",
            "limit": "1",
        },
    )
    if not msgs:
        return SendMessageOut(
            message_id=payload.message_id, status="error",
            error_text="message_not_found",
        )
    msg = msgs[0]

    if msg.get("status") != "draft":
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_not_eligible",
            skipped_reason=f"status={msg.get('status')!r} (attendu 'draft')",
        )
    if msg.get("direction") != "outbound":
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_not_eligible",
            skipped_reason=f"direction={msg.get('direction')!r}",
        )
    if msg.get("compliance_check_passed") is not True:
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_not_eligible",
            skipped_reason="compliance_check_passed != true",
        )
    if not msg.get("to_email") or not msg.get("subject") or not msg.get("body_text"):
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_not_eligible",
            skipped_reason="to_email/subject/body_text manquant",
        )

    # 2) Defense — warmup gate. WF-5 le bloque déjà mais on revérifie au
    # send (au cas où le draft a été approuvé avant l'activation du gate ou
    # si WARMUP_END_DATE a été remis après coup).
    warmup = check_warmup_window()
    if not warmup.passed:
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_warmup",
            skipped_reason=warmup.message,
        )

    # 2b) Defense — platform / big tech email domain.
    # Filet final après la blocklist domaine en amont (WF-1 sourcing + scrape WF-3).
    # Si malgré tout un contact @meta.com / @doordash.com / etc. est arrivé en DB
    # (import manuel, ancienne pollution avant cleanup du 14 mai, contact legacy
    # Apollo, edge case), on bloque ici AVANT l'action irréversible (push Instantly).
    blocked, reason = is_email_on_blocked_domain(msg.get("to_email"))
    if blocked:
        # Marquer le message 'failed' pour qu'il ne soit pas re-tenté.
        # En dry_run on RAPPORTE le verdict sans le graver : la simulation ne
        # doit pas tuer un draft (le statut retourné, lui, reste identique).
        if not payload.dry_run:
            try:
                await db.update(
                    "messages",
                    {"status": "failed", "compliance_notes": (
                        (msg.get("compliance_notes") or "")
                        + f" | send_blocked: platform_domain ({reason})"
                    ).strip(" |")},
                    filters={"id": f"eq.{payload.message_id}"},
                )
            except Exception:  # noqa: BLE001
                pass
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_platform_domain",
            skipped_reason=f"email domain dans blocklist: {reason}",
        )

    # 3) Fetch contact + company pour Instantly metadata
    contact_id = msg.get("contact_id")
    contact_rows = await db.select(
        "contacts",
        params={
            "select": "id,first_name,last_name,email,company_id",
            "id": f"eq.{contact_id}",
            "limit": "1",
        },
    ) if contact_id else []
    if not contact_rows:
        return SendMessageOut(
            message_id=payload.message_id, status="error",
            error_text="contact_not_found",
        )
    contact = contact_rows[0]

    company_rows = await db.select(
        "companies",
        params={"select": "name,domain", "id": f"eq.{contact['company_id']}", "limit": "1"},
    ) if contact.get("company_id") else []
    company = company_rows[0] if company_rows else {}

    # 3b) Defense — suppression list (post-draft, pre-push). Un opt-out reçu
    # après la création du draft doit bloquer ici.
    suppressed, reason = await _is_suppressed(msg["to_email"], company.get("domain"))
    if suppressed:
        # On marque le message 'failed' pour que les futurs runs ne le re-tentent pas.
        # En dry_run on RAPPORTE le verdict sans le graver : la simulation ne
        # doit pas tuer un draft (le statut retourné, lui, reste identique).
        if not payload.dry_run:
            try:
                await db.update(
                    "messages",
                    {"status": "failed", "compliance_notes": (
                        (msg.get("compliance_notes") or "") + f" | send_blocked: {reason}"
                    ).strip(" |")},
                    filters={"id": f"eq.{payload.message_id}"},
                )
            except Exception:  # noqa: BLE001
                pass
        return SendMessageOut(
            message_id=payload.message_id, status="skipped_suppressed",
            skipped_reason=reason,
        )

    # 3bis) Le TRIPLET, ou rien.
    #
    # 🔴 Le refus est TOTAL, jamais partiel. Un lead poussé sans ses relances
    # recevrait un seul courriel et personne ne le saurait : la campagne
    # tournerait, les compteurs seraient verts, et 68 % des réponses positives
    # (celles qui arrivent après la 2ᵉ touche) ne viendraient simplement jamais.
    #
    # ⚠️ C'est ICI que le refus appartient, et pas dans une contrainte de base :
    # le message garde sa ligne, son statut reste `draft`, et il repartira dès
    # que ses relances existeront. Un `check` en base aurait fait échouer
    # l'insert du courriel entier — la même erreur de couche que le pied de
    # page vide.
    followups = msg.get("followups") or None
    if (msg.get("track") or "") == "agence-ia":
        manquantes = [
            cle for cle in CLES_RELANCES
            if not ((followups or {}).get(cle) or "").strip()
        ]
        if manquantes:
            return SendMessageOut(
                message_id=payload.message_id,
                status="skipped_followups_manquants",
                skipped_reason=f"relances absentes ou vides : {', '.join(manquantes)}",
            )

    # 4) Push à Instantly (ou simule si dry_run)
    provider_message_id: str | None = None
    if payload.dry_run:
        provider_message_id = f"dry_run_{payload.message_id[:8]}"
    else:
        try:
            res = await instantly_lib.add_lead_to_campaign(
                email=msg["to_email"],
                subject=msg["subject"],
                body_text=msg["body_text"],
                followups=followups,
                first_name=contact.get("first_name"),
                last_name=contact.get("last_name"),
                company_name=company.get("name"),
                campaign_id=payload.campaign_id,
            )
            provider_message_id = str(res.get("id"))
        except instantly_lib.InstantlyError as e:
            return SendMessageOut(
                message_id=payload.message_id, status="error",
                error_text=f"instantly: {e}",
            )

    if payload.dry_run:
        # dry_run : simulation complète, AUCUNE écriture. Le statut reste "ok"
        # pour que run_wf6 compte le candidat comme poussable (un statut neuf
        # tomberait dans la branche errors).
        return SendMessageOut(
            message_id=payload.message_id, status="ok",
            provider_message_id=provider_message_id,
        )

    # 5) Update messages : queued + provider + scheduled_at
    now_iso = datetime.now(timezone.utc).isoformat()
    patch: dict[str, Any] = {
        "status": "queued",
        "provider": "instantly",
        "provider_message_id": provider_message_id,
        "scheduled_at": now_iso,
    }
    try:
        await db.update(
            "messages", patch, filters={"id": f"eq.{payload.message_id}"}
        )
    except Exception as e:  # noqa: BLE001
        return SendMessageOut(
            message_id=payload.message_id, status="error",
            provider_message_id=provider_message_id,
            error_text=f"db_update_after_push: {e!r}",
        )

    # Side effect : flip contact.status à 'contacted'. Si déjà 'contacted'+,
    # on laisse (un follow-up Phase 3 ne doit pas régresser à 'contacted').
    if contact.get("id"):
        try:
            cur = await db.select(
                "contacts",
                params={"select": "status", "id": f"eq.{contact['id']}", "limit": "1"},
            )
            if cur and cur[0].get("status") in ("new", "ready", "researching"):
                await db.update(
                    "contacts", {"status": "contacted"},
                    filters={"id": f"eq.{contact['id']}"},
                )
        except Exception:  # noqa: BLE001
            pass

    return SendMessageOut(
        message_id=payload.message_id, status="ok",
        provider_message_id=provider_message_id,
    )


# ----------------------------------------------------------------------
# Batch (WF-6 run)
# ----------------------------------------------------------------------

class RunWf6In(BaseModel):
    limit: int = 10
    campaign_id: str | None = None
    dry_run: bool = False
    # Override le daily cap (défaut: env INSTANTLY_DAILY_CAP ou 10).
    daily_cap: int | None = None
    track: str = "OPT"  # OPT (legacy) | agence-ia — filtre les drafts + choisit la campagne Instantly


def _campaign_for_track(track: str) -> str | None:
    """Campagne Instantly selon le track. agence-ia → INSTANTLY_CAMPAIGN_ID_REACTI
    (nom d'env legacy gardé) ; OPT/défaut → None (lib instantly utilise
    INSTANTLY_CAMPAIGN_ID)."""
    if track and track.strip().lower() == "agence-ia":
        return os.environ.get("INSTANTLY_CAMPAIGN_ID_REACTI", "").strip() or None
    return None


class RunWf6Item(BaseModel):
    message_id: str
    to_email: str | None = None
    status: str
    provider_message_id: str | None = None
    skipped_reason: str | None = None
    error_text: str | None = None


class RunWf6Out(BaseModel):
    processed: int
    pushed: int
    skipped_cap: int
    skipped_warmup: int
    skipped_suppressed: int
    skipped_platform_domain: int = 0
    skipped_other: int
    errors: int
    daily_cap: int
    already_pushed_today: int
    items: list[RunWf6Item]


async def _horodater_tentative(message_id: str) -> None:
    """Fait reculer un message dans la file. Posé sur TOUTE tentative qui n'a
    pas abouti — saut comme exception : un message qui lève garderait sinon sa
    place en tête et re-consommerait un créneau à chaque passe."""
    try:
        await db.update(
            "messages", {"last_send_attempt_at": datetime.now(timezone.utc).isoformat()},
            filters={"id": f"eq.{message_id}"},
        )
    except Exception:  # noqa: BLE001 — un horodatage perdu ne casse pas la passe
        pass


async def _alerter_campagne_absente(track: str) -> bool:
    """Crie quand WF-6 refuse de pousser faute de campagne configurée.

    Distinct de `_alerter_file_bloquee` À DESSEIN : ici la file peut être
    pleine de brouillons APPROUVÉS, et chercher des `is.null` rendrait un
    diagnostic faux. Deux silences différents méritent deux messages
    différents — une alerte qui se trompe de cause coûte plus qu'une alerte
    absente, parce qu'on la suit.

    Rend `True` si une alerte est partie. Ne lève jamais.
    """
    from ..lib import slack as slack_lib

    corps = "\n".join([
        f"🚨 WF-6 — refus de pousser : aucune campagne Instantly configurée "
        f"pour le track {track}.",
        "Le cron a rendu `processed=0, errors=0` : sans cette alerte, ça se lit "
        "comme « rien à envoyer ».",
        f"À vérifier sur Railway : `INSTANTLY_CAMPAIGN_ID_{track.upper().replace('-', '_')}` "
        "(ou la variable équivalente du track).",
        "⚠️ Une espace de trop dans la valeur suffit : elle est nettoyée puis "
        "traitée comme absente.",
        "Aucun brouillon n'a été touché — tout repart dès la variable posée.",
    ])
    try:
        envoyee = await slack_lib.notify(
            text=corps, context="wf6_campagne_absente", category="alerts",
        )
    except Exception:  # noqa: BLE001 — un filet ne casse pas ce qu'il surveille
        envoyee = False
    if not envoyee:
        logging.getLogger("wf6").error(
            "alerte campagne absente #alertes NON partie — track=%s", track,
        )
    return envoyee


async def _alerter_file_bloquee(track: str) -> bool:
    """Crie sur #alertes quand WF-6 repart les mains vides SANS que la file
    le soit.

    Rend `True` si une alerte est partie. Ne lève jamais : une alerte est un
    filet, elle n'a pas le droit de faire tomber l'envoi qu'elle surveille.

    ⚠️ Le silence qu'on répare ici est le pire de tous, parce qu'il ressemble
    à un succès. `processed=0, errors=0` fait partir le nœud IF de n8n sur
    « Log OK ». Rien dans les journaux ne distingue « la campagne est finie »
    de « WF-5 n'a jamais été activé ».
    """
    from ..lib import slack as slack_lib

    try:
        en_attente = await db.select(
            "messages",
            params={
                "select": "id",
                "direction": "eq.outbound",
                "status": "eq.draft",
                "compliance_check_passed": "is.null",
                "track": f"eq.{track}",
                "limit": "1000",
            },
        )
    except Exception:  # noqa: BLE001 — un filet ne casse pas ce qu'il surveille
        return False

    if not en_attente:
        # File réellement vide : rien à signaler, c'est une fin de liste.
        return False

    corps = "\n".join([
        f"🚨 WF-6 — 0 courriel poussé, mais {len(en_attente)} brouillon(s) "
        f"attendent encore un verdict de conformité (track {track}).",
        "La file n'est PAS vide : quelque chose en amont ne tourne pas.",
        "Piste nº1 : **WF-5 conformité n'est pas activé.** WF-4 écrit "
        "`compliance_check_passed = NULL` et WF-6 n'accepte que `true` — "
        "sans WF-5 entre les deux, aucun brouillon ne devient envoyable.",
        "⚠️ Sans cette alerte, ce cas rend `processed=0, errors=0` et se lit "
        "comme un succès.",
    ])
    try:
        envoyee = await slack_lib.notify(
            text=corps, context="wf6_file_bloquee", category="alerts",
        )
    except Exception:  # noqa: BLE001
        envoyee = False
    if not envoyee:
        # Même réflexe que `_alerter_famine_wf4` : une alerte perdue qui se
        # croit partie est le pire des deux mondes.
        logging.getLogger("wf6").error(
            "alerte file bloquée #alertes NON partie — track=%s en_attente=%s",
            track, len(en_attente),
        )
    return envoyee


async def run_wf6(payload: RunWf6In) -> RunWf6Out:
    """Pass complet WF-6 : pousse jusqu'à `limit` drafts approuvés à Instantly,
    en respectant le daily cap (compté sur fenêtre Toronto)."""
    daily_cap = payload.daily_cap if payload.daily_cap is not None else _daily_cap()
    already = await count_pushed_today()
    remaining = max(0, daily_cap - already)
    effective_limit = min(payload.limit, remaining)

    track = (payload.track or "OPT").strip() or "OPT"
    campaign = payload.campaign_id or _campaign_for_track(track)
    # Garde : un track non-OPT DOIT avoir sa campagne dédiée, sinon on refuse —
    # ne JAMAIS pousser des drafts REACTI vers la campagne OPT par défaut.
    if track.upper() != "OPT" and not campaign:
        # 🔴 CE REFUS ETAIT MUET, et c'est le même silence que la file bloquée,
        # atteint par la porte d'à côté. Ajouté le 2026-09-02 (conseil).
        #
        # Si `INSTANTLY_CAMPAIGN_ID_REACTI` manque sur Railway — ou porte une
        # espace de trop, que `_campaign_for_track` transforme en `None` — le
        # cron quotidien rend `processed=0, pushed=0, errors=0`. Le nœud IF de
        # n8n part sur « Log OK ». Aucun brouillon n'est touché, donc rien ne
        # se répare et rien ne se signale : la campagne est simplement à
        # l'arrêt, tous les jours, en silence.
        #
        # ⚠️ On n'appelle SURTOUT PAS `_alerter_file_bloquee` ici : elle
        # cherche des brouillons `is.null` (jamais jugés) et n'en trouverait
        # aucun — la file peut être pleine de brouillons APPROUVÉS. Le
        # diagnostic serait faux et l'alerte muette.
        await _alerter_campagne_absente(track)
        return RunWf6Out(
            processed=0, pushed=0, skipped_cap=0, skipped_warmup=0,
            skipped_suppressed=0, skipped_other=0, errors=0,
            daily_cap=daily_cap, already_pushed_today=already, items=[],
        )

    items: list[RunWf6Item] = []
    pushed = sk_cap = sk_warm = sk_supp = sk_plat = sk_other = errors = 0

    if effective_limit <= 0:
        return RunWf6Out(
            processed=0, pushed=0,
            skipped_cap=0, skipped_warmup=0,
            skipped_suppressed=0, skipped_other=0, errors=0,
            daily_cap=daily_cap, already_pushed_today=already, items=[],
        )

    # Fetch drafts éligibles
    drafts = await db.select(
        "messages",
        params={
            "select": "id,to_email,created_at,track",
            "direction": "eq.outbound",
            "status": "eq.draft",
            "compliance_check_passed": "is.true",
            "track": f"eq.{track}",
            # Jamais tenté d'abord, puis le moins récemment tenté. Un draft
            # sauté recule derrière les frais au lieu d'occuper la tête de
            # file — et repasse à son tour.
            "order": "last_send_attempt_at.asc.nullsfirst,created_at.asc",
            "limit": str(min(effective_limit * DRAFT_OVERFETCH_FACTOR, DRAFT_OVERFETCH_MAX)),
        },
    )

    # 🔴 UN LOT VIDE N'EST PAS FORCÉMENT UNE FILE VIDE. Ajouté le 2026-09-01
    # après l'audit de bout en bout.
    #
    # Le scénario, entièrement muet : on active WF-6 sans avoir activé WF-5.
    # WF-4 écrit des brouillons avec `compliance_check_passed = NULL` ; le
    # filtre ci-dessus exige `is.true` ; le lot revient vide. `run_wf6` rend
    # `processed=0, errors=0`, le nœud IF de n8n part sur « Log OK », et la
    # checklist se coche entièrement pendant que RIEN ne part.
    #
    # Le même silence couvre une file pleine de brouillons refusés en
    # `needs_revision` — cas où il n'y a rien à attendre non plus.
    #
    # On distingue donc « plus rien à envoyer » de « quelque chose est cassé
    # en amont », et on crie dans le second cas. Calqué sur
    # `_alerter_famine_wf4` : l'alerte NOMME ce qui attend, parce que « 0 push »
    # ne distingue pas une panne d'une fin de liste, et une alerte qu'on ne
    # peut pas interpréter finit ignorée.
    if not drafts:
        await _alerter_file_bloquee(track)

    for d in drafts:
        # La sur-récolte regarde plus loin dans la file ; elle n'envoie pas
        # plus. Le daily cap reste la limite dure.
        if pushed >= effective_limit:
            break
        try:
            res = await send_one_message(
                SendMessageIn(
                    message_id=d["id"],
                    campaign_id=campaign,
                    dry_run=payload.dry_run,
                )
            )
        except Exception as e:  # noqa: BLE001
            errors += 1
            # Le message reste 'draft' : il doit reculer comme un saut, sinon
            # une ligne qui lève à tous les coups garde la tête de file.
            if not payload.dry_run:
                await _horodater_tentative(d["id"])
            items.append(RunWf6Item(
                message_id=d["id"], to_email=d.get("to_email"),
                status="error", error_text=repr(e),
            ))
            continue

        if res.status == "ok":
            pushed += 1
        elif res.status == "skipped_warmup":
            sk_warm += 1
        elif res.status == "skipped_suppressed":
            sk_supp += 1
        elif res.status == "skipped_platform_domain":
            sk_plat += 1
        elif res.status in ("skipped_not_eligible", "skipped_followups_manquants"):
            # 🔧 `skipped_followups_manquants` est un refus VOLONTAIRE et
            # fail-closed, pas une panne. Le compter en `errors` le faisait
            # remonter comme une erreur d'envoi alors que le nœud n8n filtre
            # sur `status === 'error'` et affichait `Failures: []` : le lot
            # rapportait des erreurs que personne ne pouvait nommer.
            sk_other += 1
        else:
            errors += 1

        if res.status != "ok" and not payload.dry_run:
            # Un message poussé quitte 'draft' et sort de la requête : inutile
            # de l'horodater. Un message sauté doit reculer dans la file.
            # `payload.dry_run` est la source de vérité de la passe (c'est lui
            # qu'on passe à chaque SendMessageIn) : une simulation ne doit pas
            # réordonner la vraie file FIFO en écrivant last_send_attempt_at.
            await _horodater_tentative(d["id"])

        items.append(RunWf6Item(
            message_id=d["id"], to_email=d.get("to_email"),
            status=res.status,
            provider_message_id=res.provider_message_id,
            skipped_reason=res.skipped_reason,
            error_text=res.error_text,
        ))

    # Si on a hit le cap avant même de fetch, marquer en compteur dédié
    if payload.limit > effective_limit:
        sk_cap = payload.limit - effective_limit

    return RunWf6Out(
        processed=len(items),
        pushed=pushed,
        skipped_cap=sk_cap,
        skipped_warmup=sk_warm,
        skipped_suppressed=sk_supp,
        skipped_platform_domain=sk_plat,
        skipped_other=sk_other,
        errors=errors,
        daily_cap=daily_cap,
        already_pushed_today=already,
        items=items,
    )
