"""Slack Incoming Webhook — notifs pour WF-7 / WF-8.

Routing par catégorie (depuis 2026-05-27) :
  - `bookings` → SLACK_WEBHOOK_BOOKINGS (WF-8 events)
  - `leads`    → SLACK_WEBHOOK_LEADS (WF-7 hot lead, review)
  - `alerts`   → SLACK_WEBHOOK_ALERTS (orphans, classifier errors)

Fallback : si la var spécifique à la catégorie n'est pas set, on retombe
sur SLACK_WEBHOOK_URL (legacy single-channel). Si rien n'est configuré,
les notifs sont silencieusement no-op — utile pour dev/test sans Slack.

Failure-mode : Slack DOWN ne DOIT JAMAIS casser la pipeline (classification
des replies, booking, etc.). Les exceptions sont avalées + loggées en stderr. Le caller
peut inspecter `notify(...)` return = True/False pour savoir si le ping est
passé, mais ne doit pas crasher si False.

Ref: https://api.slack.com/messaging/webhooks
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

import httpx

# Fuseau d'AFFICHAGE des dates rendues à William. La base stocke en UTC ; tout
# ce qu'il lit doit être dans son heure à lui, comme les compteurs du résumé.
_FUSEAU_AFFICHAGE = "America/Toronto"

if TYPE_CHECKING:
    from .reacti_tickets import ReactiTicket

SLACK_WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
SLACK_TIMEOUT_SECONDS = 5.0  # court — on ne veut pas bloquer la pipeline

Category = Literal["bookings", "leads", "alerts", "errors", "summary"]

# Mapping catégorie → env var dédiée. Si non set, on retombe sur SLACK_WEBHOOK_URL.
_CATEGORY_ENV: dict[str, str] = {
    "bookings": "SLACK_WEBHOOK_BOOKINGS",
    "leads": "SLACK_WEBHOOK_LEADS",
    "alerts": "SLACK_WEBHOOK_ALERTS",
    "errors": "SLACK_WEBHOOK_ERRORS",    # pannes pipeline (n8n error workflow)
    "summary": "SLACK_WEBHOOK_SUMMARY",  # résumé quotidien
}


def _webhook_url(category: str | None = None) -> str | None:
    """Résout l'URL webhook pour une catégorie donnée.

    Ordre : env catégorie spécifique → SLACK_WEBHOOK_URL fallback → None.
    None = pas configuré, notify devient no-op silencieux.
    """
    if category:
        env_name = _CATEGORY_ENV.get(category)
        if env_name:
            url = os.environ.get(env_name, "").strip()
            if url:
                return url
    url = os.environ.get(SLACK_WEBHOOK_ENV, "").strip()
    return url or None


def is_configured(category: Category | None = None) -> bool:
    """Un webhook est-il résolvable pour cette catégorie (var dédiée ou fallback) ?

    Sert aux healthchecks : ils doivent tester le canal RÉELLEMENT utilisé par le
    workflow, pas SLACK_WEBHOOK_URL en dur.
    """
    return bool(_webhook_url(category))


async def notify(
    *,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    context: str | None = None,
    category: Category | None = None,
) -> bool:
    """Envoie un message Slack via Incoming Webhook. Async (httpx).

    Args:
      text: texte fallback (utilisé par notifs mobile + accessibilité).
      blocks: blocks Block Kit optionnels pour mise en forme riche.
      context: prefix court ajouté au log stderr en cas d'erreur (ex: "wf7_hot_lead").
      category: route vers le webhook dédié ("bookings"/"leads"/"alerts"). Fallback
        sur SLACK_WEBHOOK_URL si la var catégorie n'est pas set.

    Returns True si Slack a accepté (200 OK), False sinon ou si pas configuré.
    NE LÈVE JAMAIS — la pipeline ne doit pas casser à cause de Slack.
    """
    url = _webhook_url(category)
    if not url:
        return False  # pas configuré = no-op silencieux

    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        async with httpx.AsyncClient(timeout=SLACK_TIMEOUT_SECONDS) as client:
            r = await client.post(url, json=payload)
        if r.status_code == 200 and r.text.strip() == "ok":
            return True
        print(
            f"[slack:{context or '-'}] non-2xx response: {r.status_code} {r.text[:200]}",
            file=sys.stderr,
        )
        return False
    except Exception as e:  # noqa: BLE001 — Slack DOWN ne casse rien
        print(
            f"[slack:{context or '-'}] exception {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return False


# ----------------------------------------------------------------------
# Helpers de mise en forme — réutilisés par WF-7 et WF-8
# ----------------------------------------------------------------------

def _kv_field(label: str, value: str) -> dict[str, Any]:
    """Field Block Kit avec label en gras + valeur."""
    return {"type": "mrkdwn", "text": f"*{label}*\n{value}"}


def _track_prefix(track: str | None) -> str:
    """Préfixe visible du track pour les notifs partagées OPT/REACTI (ex: '[REACTI] ').

    Vide si track inconnu — la notif reste propre. Mis sur le fallback (notif mobile)
    ET le header pour qu'on sache d'un coup d'œil d'où vient l'event.
    """
    t = (track or "").strip().upper()
    # AGENCE-IA = offre vivante (pivot 2026-06-07) ; REACTI gardé (compat héritage).
    return f"[{t}] " if t in ("OPT", "REACTI", "AGENCE-IA") else ""


def build_hot_lead_blocks(
    *,
    contact_name: str,
    company_name: str,
    contact_email: str,
    reply_preview: str,
    confidence: float | None = None,
    track: str | None = None,
    website: str | None = None,
    research_json: dict[str, Any] | None = None,
    suppression_check_failed: bool = False,
    already_booked: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un reply classé 'interested' (WF-7).

    Pivot tri (2026-08-20) : ce ping EST la file de travail — William lit la
    réponse, produit le site (session artisanale), et répond avec le lien.
    C'est pourquoi le brief de recherche voyage AVEC le ping : `research_json`
    (depuis `companies.research_json`, WF-3) ajoute la section 'Brief pré-RDV'
    après l'extrait de réponse — le site se produit sans rouvrir la DB.
    Absent/vide => aucun bloc ajouté, format historique intact.

    `suppression_check_failed=True` = la garde de désabonnement n'a pas pu LIRE
    (fail-open assumé côté reply.py) : on le dit dans 'Prochain geste' pour que
    la vérif se fasse à la main avant d'écrire.

    `already_booked=True` = ce contact a DÉJÀ un RDV au calendrier (une
    conversation à l'état `booked`, posée par WF-8). Sans cette mention le ping
    est mot pour mot celui d'un lead frais : il réclame « produire le site »
    sans dire qu'un appel est déjà pris, et William risque de reproposer un
    créneau à quelqu'un qui en a un.

    Returns (fallback_text, blocks) — passer aux 2 args de `notify`.
    """
    tp = _track_prefix(track)
    status = "À toi : produire le site (session artisanale) puis répondre avec le lien démo"
    fallback = f"{tp}🔥 Hot lead — {contact_name} @ {company_name}"
    champs = [
        _kv_field("Contact", f"{contact_name}\n{contact_email}"),
        _kv_field("Entreprise", company_name),
    ]
    if website:
        champs.append(_kv_field("Site actuel", website))
    geste = f"*Prochain geste*: {status}"
    if confidence is not None:
        geste += f"\n*Confidence*: {confidence:.0%}"
    if suppression_check_failed:
        geste += "\n⚠️ vérif désabonnement en panne — vérifier avant d'écrire"
    if already_booked:
        geste += (
            "\n📅 RDV déjà au calendrier — le site sert à préparer l'appel, "
            "ne repropose pas de créneau"
        )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{tp}🔥 Hot lead"},
        },
        {"type": "section", "fields": champs},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": geste},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reply (extrait)*\n```{_truncate(reply_preview, 400)}```",
            },
        },
    ]
    blocks.extend(_research_brief_blocks(research_json))
    return fallback, blocks


# Le garde-fou LCAP, en toutes lettres. Il VOYAGE avec la notification : une
# alerte « il s'est désabonné » sans l'interdit invite au réflexe naturel (« je
# le relance pour comprendre »), qui est justement l'infraction. Le consentement
# retiré interdit tout message électronique commercial — sans exception et sans
# délai. L'appel téléphonique relève d'un autre régime (LNNTE), d'où la nuance.
GARDE_LCAP_APRES_DESABONNEMENT = (
    "⛔ Ne PAS relancer par courriel — consentement retiré (LCAP). "
    "Un appel reste possible : vérifier la LNNTE d'abord."
)

# LE MÊME INTERDIT, en fin de ligne de liste. Deux rendus, un seul endroit où on
# l'écrit : la version longue ci-dessus est un bloc à elle seule (ping WF-7,
# sous-ligne du compteur de désabonnés) ; celle-ci s'insère dans une énumération
# « nom — étape · action · note » où la phrase complète, avec son propre tiret
# cadratin et ses deux phrases, entrerait en collision avec les séparateurs de la
# ligne. Si l'interdit change, il change ICI, les deux à la fois.
GARDE_LCAP_RELANCE_COURTE = "⛔ relance interdite (LCAP)"


def jour(horodatage: str) -> str:
    """« 2026-08-21T14:03:00+00:00 » → « 2026-08-21 », en heure de Toronto.

    ⚠️ La conversion de fuseau n'est pas cosmétique : la base stocke en UTC, et
    un désabonnement à 21 h le 23 août à Toronto y est écrit « 2026-08-24T01:00Z ».
    Un découpage brut de la chaîne aurait affiché le 24 — William aurait lu
    « demain » pour un geste d'hier soir, et n'aurait pas reconnu le cas dont on
    lui parle. On rend donc la date du jour VÉCU, celle qui est aussi le repère
    de la fenêtre « depuis 7 jours » du résumé.

    Rend la valeur telle quelle si elle ne ressemble pas à de l'ISO : mieux vaut
    un horodatage brut à l'écran qu'une date inventée par un découpage aveugle.

    Public (et non `_jour`) parce qu'il traverse la frontière du module : le
    résumé quotidien (`http_api.summary_daily`) affiche les mêmes dates de
    « oui » que ce ping, et les deux doivent les rendre pareil.
    """
    s = (horodatage or "").strip()
    if not (len(s) >= 10 and s[4] == "-" and s[7] == "-"):
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10]
    if dt.tzinfo is None:  # naïf en base = UTC, comme partout ailleurs ici
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(_FUSEAU_AFFICHAGE)).strftime("%Y-%m-%d")


def build_interested_unsubscribed_blocks(
    *,
    contact_name: str,
    company_name: str,
    contact_email: str,
    interested_at: str,
    reply_preview: str,
    track: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un lead qui avait dit OUI puis s'est désabonné (WF-7).

    Besoin exprimé le 2026-08-23 : ces leads-là disparaissent en silence du
    compteur « en attente de site », et William veut décider LUI-MÊME de la
    suite. Le ping est donc de la VISIBILITÉ pure — il n'ouvre aucune porte
    d'envoi et ne déclenche aucune relance.

    Il porte quatre choses : qui, quand il avait dit oui (pour juger si le oui
    était d'hier ou d'il y a trois mois), ce qu'il vient d'écrire, et l'interdit
    LCAP. Ce dernier n'est pas décoratif : c'est le seul contrepoids au réflexe
    de rappeler par courriel un lead qu'on croyait acquis.
    """
    tp = _track_prefix(track)
    titre = "⚠️ Un intéressé s'est désabonné"
    fallback = f"{tp}{titre} — {contact_name} @ {company_name}"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{tp}{titre}"},
        },
        {
            "type": "section",
            "fields": [
                _kv_field("Contact", f"{contact_name}\n{contact_email}"),
                _kv_field("Entreprise", company_name),
                _kv_field("Avait dit oui le", jour(interested_at)),
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": GARDE_LCAP_APRES_DESABONNEMENT},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Sa demande de désabonnement (extrait)*\n"
                    f"```{_truncate(reply_preview, 400)}```"
                ),
            },
        },
    ]
    return fallback, blocks


def build_not_interested_blocks(
    *,
    contact_name: str,
    company_name: str,
    contact_email: str,
    reply_preview: str,
    confidence: float | None = None,
    track: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un reply classé 'not_interested' (WF-7).

    Le trou bouché : cette branche ne pinguait RIEN. Le contact passait
    `disqualified`, la conversation `cold`, et William ne savait même pas que le
    prospect avait répondu. Or le classifieur range dans `not_interested` des
    phrases qui n'en sont pas — « on gère ça à l'interne », « recontactez-moi
    dans 6 mois » : des objections que William sait traiter, enterrées en
    silence.

    D'où le contenu : l'EXTRAIT de la réponse est le cœur du bloc. C'est lui qui
    laisse William faire à la main le tri fin qu'AC2 automatisera (catégories
    dédiées, file de reprise, date de rappel).

    Ce bloc ne promet aucune suite. Le contact EST disqualifié et sorti du
    pipeline : écrire « à relancer » ou « en attente » ferait attendre William
    d'un système qui ne fera plus rien. On dit l'état réel, et on dit à qui
    revient le prochain geste s'il y en a un : à lui.
    """
    tp = _track_prefix(track)
    titre = "🚫 Réponse négative — sorti du pipeline"
    fallback = f"{tp}{titre} — {contact_name} @ {company_name}"
    etat = (
        "*Ce que le système a fait*: contact passé à `disqualified`, "
        "conversation `cold`. Il ne recevra plus rien — le système ne le "
        "recontactera plus."
    )
    if confidence is not None:
        etat += f"\n*Confiance du classifieur*: {confidence:.0%}"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{tp}{titre}"},
        },
        {
            "type": "section",
            "fields": [
                _kv_field("Contact", f"{contact_name}\n{contact_email}"),
                _kv_field("Entreprise", company_name),
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": etat},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Sa réponse (extrait)*\n"
                    f"```{_truncate(reply_preview, 400)}```"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "_Lis l'extrait : si ce n'est pas un vrai refus (« on gère "
                    "ça à l'interne », « rappelez-moi dans 6 mois »), le geste "
                    "t'appartient._"
                ),
            },
        },
    ]
    return fallback, blocks


def build_review_blocks(
    *,
    contact_name: str,
    company_name: str,
    contact_email: str,
    category: str,
    confidence: float,
    reasoning: str,
    reply_preview: str,
    track: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un reply en review manuel (classifier hésite / 'other')."""
    tp = _track_prefix(track)
    fallback = f"{tp}⚠️ Review manuel — {contact_name} ({category}, conf {confidence:.0%})"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{tp}⚠️ Reply à reviewer"},
        },
        {
            "type": "section",
            "fields": [
                _kv_field("Contact", f"{contact_name}\n{contact_email}"),
                _kv_field("Entreprise", company_name),
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Classification*: `{category}` (confidence {confidence:.0%})\n"
                    f"*Raisonnement*: {reasoning}"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reply (extrait)*\n```{_truncate(reply_preview, 400)}```",
            },
        },
    ]
    return fallback, blocks


def _research_brief_blocks(research_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Construit la section 'Brief pré-RDV' depuis `companies.research_json` (WF-3).

    Tout est déterministe (pas de LLM) — `research_json` est déjà structuré. Si le
    champ est absent/vide, on retourne [] et le ping booking garde son format minimal.

    Lecture défensive : `research_json` en DB est l'objet research direct (clés
    `company_summary`, `pain_points_detected`, etc.), mais on tolère un éventuel
    wrapper `{"research": {...}}` au cas où le shape changerait.
    """
    if not isinstance(research_json, dict) or not research_json:
        return []
    rj = research_json.get("research") if isinstance(research_json.get("research"), dict) else research_json

    sections: list[str] = []

    summary = (rj.get("company_summary") or "").strip()
    if summary:
        sections.append(f"*Résumé*\n{_truncate(summary, 300)}")

    pains = rj.get("pain_points_detected")
    if isinstance(pains, list) and pains:
        lines = []
        for p in pains[:3]:
            txt = (p.get("pain") if isinstance(p, dict) else str(p)) or ""
            txt = txt.strip()
            if txt:
                lines.append(f"• {_truncate(txt, 160)}")
        if lines:
            sections.append("*Pain points détectés*\n" + "\n".join(lines))

    hooks = rj.get("personalization_hooks")
    if isinstance(hooks, list) and hooks:
        lines = [f"• {_truncate(str(h).strip(), 160)}" for h in hooks[:3] if str(h).strip()]
        if lines:
            sections.append("*Accroches / pistes d'automatisation*\n" + "\n".join(lines))

    meta_bits: list[str] = []
    tss = rj.get("tech_savvy_score")
    score = tss.get("score") if isinstance(tss, dict) else None
    if score:
        meta_bits.append(f"Tech-savvy : *{score}*")
    decideurs = rj.get("decideur_candidats")
    if isinstance(decideurs, list) and decideurs:
        names = []
        for d in decideurs[:2]:
            if isinstance(d, dict) and d.get("nom_complet"):
                titre = d.get("titre")
                names.append(f"{d['nom_complet']}" + (f" ({titre})" if titre else ""))
        if names:
            meta_bits.append("Décideur(s) : " + ", ".join(names))
    if meta_bits:
        sections.append(" · ".join(meta_bits))

    if not sections:
        return []

    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🔎 Brief pré-RDV*"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n\n".join(sections)},
        },
    ]


def build_booked_blocks(
    *,
    contact_name: str,
    company_name: str | None,
    contact_email: str | None,
    meeting_start_iso: str,
    meeting_url: str | None = None,
    event_type: str | None = None,
    research_json: dict[str, Any] | None = None,
    reacti_ticket: "ReactiTicket | None" = None,
    track: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Format Slack pour un meeting confirmé via Cal.com (WF-8).

    Si `research_json` est fourni (depuis `companies.research_json`), on ajoute
    une section 'Brief pré-RDV' avec résumé, pain points, accroches et décideurs —
    pour arriver au RDV préparé sans ouvrir la DB.

    Si `reacti_ticket` est fourni (track REACTI uniquement), on ajoute une ligne
    'économie commission' : verticale + ticket moyen défaut + commission estimée,
    pour arriver à l'appel avec les chiffres en tête. Absent pour un prospect OPT
    => le brief reste strictement inchangé.
    """
    tp = _track_prefix(track)
    fallback = f"{tp}✅ RDV booké — {contact_name} le {meeting_start_iso}"
    fields = [_kv_field("Contact", contact_name)]
    if contact_email:
        fields.append(_kv_field("Email", contact_email))
    if company_name:
        fields.append(_kv_field("Entreprise", company_name))
    fields.append(_kv_field("Quand", meeting_start_iso))
    if event_type:
        fields.append(_kv_field("Type", event_type))

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{tp}✅ Meeting booké"},
        },
        {"type": "section", "fields": fields},
    ]
    if meeting_url:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<{meeting_url}|Ouvrir dans Cal.com>",
            },
        })
    if reacti_ticket is not None:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*💰 REACTI — économie commission (défaut)*\n"
                    f"Secteur : *{reacti_ticket.label}* · "
                    f"Ticket moyen : *~{reacti_ticket.ticket} $* · "
                    f"Commission {reacti_ticket.rate_pct} % : *~{reacti_ticket.commission} $/client*\n"
                    "_Confirmer le vrai ticket du client à l'appel._"
                ),
            },
        })
    blocks.extend(_research_brief_blocks(research_json))
    return fallback, blocks


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# ----------------------------------------------------------------------
# Sync versions (pour scripts CLI / tests qui ne veulent pas async)
# ----------------------------------------------------------------------

def notify_sync(
    *,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    context: str | None = None,
    category: Category | None = None,
) -> bool:
    """Version sync de `notify` pour usage en script ou test. Bloquant."""
    url = _webhook_url(category)
    if not url:
        return False
    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        r = httpx.post(url, json=payload, timeout=SLACK_TIMEOUT_SECONDS)
        if r.status_code == 200 and r.text.strip() == "ok":
            return True
        print(
            f"[slack:{context or '-'}] non-2xx response: {r.status_code} {r.text[:200]}",
            file=sys.stderr,
        )
        return False
    except Exception as e:  # noqa: BLE001
        print(
            f"[slack:{context or '-'}] exception {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return False
