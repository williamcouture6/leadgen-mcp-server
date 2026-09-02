"""API HTTP REST (FastAPI) — appelée par n8n.

Expose les mêmes fonctions que le serveur MCP (qui reste utilisé en stdio par
Claude Code), mais en routes REST simples pour faciliter l'intégration avec
le node HTTP Request de n8n cloud.

Sécurité : Bearer token statique partagé (`AGENTS_HTTP_TOKEN` dans .env).
À durcir avant un déploiement public (rotation, scopes, etc.).
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel

from . import supabase_client as sb
from .tools import booking as booking_tools
from .tools import compliance as compliance_tools
from .tools import db as db_tools
from .tools import maps as maps_tools
from .tools import personalize as personalize_tools
from .tools import reply as reply_tools
from .tools import research as research_tools
from .tools import send as send_tools
from .tools import send_status as send_status_tools
from .tools import reacti_discover as reacti_discover_tools
from .tools import brand_kit as brand_kit_tools
from .lib.owner_match import classify_scraped_contact
from .lib.sourcing_filters import sourcing_disqualify_reason


def _expected_token() -> str | None:
    tok = os.environ.get("AGENTS_HTTP_TOKEN")
    return tok or None


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = _expected_token()
    if not expected:
        # Mode dev sans token : refuse, pour éviter l'oubli avant déploiement.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AGENTS_HTTP_TOKEN non défini côté serveur",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer")
    provided = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")


app = FastAPI(title="leadgen-mcp HTTP API", version="0.1.0")

import logging

_startup_log = logging.getLogger("leadgen.startup")


@app.on_event("startup")
async def _validate_env_on_startup() -> None:
    """Fail-soft : loggue les env vars manquantes au démarrage (audit #10).

    Ne bloque jamais le boot — un warning visible dans les logs Railway au deploy
    vaut mieux qu'une feature qui no-op silencieusement des jours plus tard."""
    from .config import validate_env

    res = validate_env()
    if res["missing_required"]:
        _startup_log.error(
            "ENV REQUISES MANQUANTES: %s — le serveur risque de ne pas fonctionner",
            ", ".join(res["missing_required"]),
        )
    if res["missing_recommended"]:
        _startup_log.warning(
            "ENV recommandées manquantes (features dégradées): %s",
            ", ".join(res["missing_recommended"]),
        )
    if not res["missing_required"] and not res["missing_recommended"]:
        _startup_log.info("Config env OK (requises + recommandées présentes)")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Vivant, et SUR QUELLE VERSION.

    Sans le `commit`, rien ne distingue un déploiement qui a pris d'un qui a
    échoué : le service répond `ok` avec l'ancien code comme avec le nouveau, et
    il fallait aller lire le SHA dans le tableau de bord Railway. Activer un
    workflow n8n en se fiant à du code qu'on CROIT déployé, c'est ce qu'on évite
    ici. Railway injecte `RAILWAY_GIT_COMMIT_SHA` tout seul ; en local elle
    n'existe pas et l'endpoint répond quand même."""
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or ""
    return {"status": "ok", "commit": sha[:7] if sha else "unknown"}


class AlertIn(BaseModel):
    text: str
    category: str = "errors"  # route vers SLACK_WEBHOOK_ERRORS (canal pannes pipeline)


@app.post("/alert", dependencies=[Depends(_require_auth)])
async def post_alert(payload: AlertIn) -> dict[str, Any]:
    """Poste une alerte Slack. Utilisé par le workflow n8n 'Error Handler' pour
    pinger les pannes de n'importe quel workflow (OPT + REACTI) dans le canal erreurs."""
    from .lib import slack as slack_lib

    ok = await slack_lib.notify(
        text=payload.text, context="n8n_error_handler", category=payload.category
    )
    return {"ok": ok, "category": payload.category}


# Les motifs de `suppression_list` qui SONT un retrait de consentement, par
# opposition aux autres lignes de la même table : `hard_bounce` (adresse morte,
# posée par WF-6b), `manual` / `competitor` / `dncl` (nos décisions à nous). Ces
# derniers sortent le contact de la file de travail — mais silencieusement : leur
# annoncer le garde-fou LCAP mentirait, personne n'a rien retiré.
_MOTIFS_RETRAIT_CONSENTEMENT = {"opt_out", "spam_complaint"}

# Nombre de désabonnés nommés sur la ligne du résumé avant repli en « … +N ».
_PLAFOND_NOMS_DESABONNES = 5

# Fenêtre du « dont N depuis 7 jours » : aujourd'hui plus les 6 jours
# précédents, bornée sur minuit America/Toronto comme tout le reste du résumé.
_JOURS_DESABONNEMENT_RECENT = 7


def _cle_courriel(valeur: Any) -> str:
    """Clé d'appariement d'une adresse : espaces retirés, casse repliée.

    `Jean@PlomberieX.ca` et `jean@plomberiex.ca` sont la MÊME boîte. Le
    croisement se faisait avant en SQL (`email=eq.…`), qui est SENSIBLE à la
    casse : un désabonné enregistré sous une autre casse restait « en attente
    de site » et le tableau de bord envoyait William lui bâtir un site.

    `ilike` a l'air d'être la réponse — c'en est un piège : `_` et `%` y sont
    des jokers, et `_` est fréquent dans une adresse (`jean_roy@x.ca` y
    apparierait `jeanXroy@x.ca`). D'où l'appariement en Python.
    """
    return str(valeur or "").strip().lower()


# Marques d'impasse du bloc « leads chauds » (PT3). Un lead en impasse n'est
# JAMAIS retiré de la liste : il est étiqueté. Un lead avec six notes et un RDV
# qui bascule `disqualified` — ce que WF-7 fait sans émettre le moindre ping —
# quitterait sinon la liste sans un mot, avec tout son historique.
_MARQUES_IMPASSE = {
    "opted_out": "⛔ s'est désabonné",
    "disqualified": "✖ a dit non",
    "bounced": "📵 adresse morte",
}


def _marque_impasse(statut: Any, ligne_suppression: dict[str, Any] | None) -> str | None:
    """La marque à afficher pour un lead chaud, ou None s'il est sain.

    Deux sources, comme partout ailleurs dans ce résumé : `contacts.status` ET la
    présence dans `suppression_list`. Le clic sur le lien du footer (Edge Function
    `unsubscribe`) écrit TOUJOURS la ligne de suppression et ne pose le statut
    qu'au mieux — sans le croisement, ce cas dégradé passerait pour un lead sain.
    Le croisement se fait sur la clé normalisée (`_cle_courriel`) : `ilike` serait
    un piège, `_` y est un joker."""
    marque = _MARQUES_IMPASSE.get(str(statut or ""))
    if marque:
        return marque
    if ligne_suppression is not None:
        motif = ligne_suppression.get("reason")
        if motif in _MOTIFS_RETRAIT_CONSENTEMENT:
            return _MARQUES_IMPASSE["opted_out"]
        if motif == "hard_bounce":
            return _MARQUES_IMPASSE["bounced"]
        # manual / competitor / dncl : NOUS l'avons écarté. On le dit sans
        # invoquer un désabonnement que personne n'a demandé.
        return "✖ écarté de notre côté"
    return None


def _mention_action_caduque(marque: str) -> str:
    """Ce qu'on écrit À LA PLACE d'une consigne de relance, sur un lead marqué.

    Une action prévue avant l'impasse ne s'exécute plus. L'afficher nue — « ⏰
    relancer par courriel » — sur un désabonné est une invitation à commettre
    l'infraction ; la retirer sans rien dire laisse William la reconstruire de
    tête. On la remplace donc par la RAISON pour laquelle elle est morte.

    Les trois raisons ne se confondent pas, et le projet ne met pas de mensonge
    à l'écran : seul l'`opted_out` est un retrait de consentement (LCAP). Un
    hard bounce ferme le canal sans que personne n'ait rien retiré, et un
    « a dit non » / « écarté de notre côté » ne ferme même pas le canal — c'est
    la relance qui n'a plus d'objet."""
    from .lib import slack as slack_lib

    if marque == _MARQUES_IMPASSE["opted_out"]:
        return slack_lib.GARDE_LCAP_RELANCE_COURTE
    if marque == _MARQUES_IMPASSE["bounced"]:
        return "⏸️ relance impossible — le courriel ne se rend plus"
    return "⏸️ relance annulée"


# Plafond de lignes nommées dans le bloc « leads chauds » avant repli en
# « et N autres ». N est toujours EXACT : une troncature silencieuse se lit
# comme « c'est tout », ce qui est le contraire de la vérité.
_PLAFOND_LEADS_CHAUDS = 10

# Au-delà, la ligne POSE la question au lieu de constater. Un opérateur solo ne
# s'assoit jamais pour déclarer une défaite : sans relance, `perdu` reste
# sous-écrit et le cimetière occupe le haut de la liste.
_JOURS_AVANT_DE_DEMANDER = 21

# Fenêtre du bloc « À faire », en JOURS CALENDAIRES à partir de minuit
# America/Toronto : 0 = aujourd'hui seulement, 1 = jusqu'à la fin de demain.
# L'échu remonte toujours. Ce n'est pas une durée en heures — une action du
# lendemain matin doit apparaître dès le résumé de la veille, pas 24 h avant.
# Plus large, le bloc devient une deuxième liste de tout ; plus étroit, une
# action du matin arrive après coup.
_HORIZON_A_FAIRE_JOURS = 1

_ETIQUETTES_ETAPE = {
    "a_produire": "à produire",
    "site_produit": "site produit",
    "site_envoye": "site envoyé",
    "feedback_recu": "feedback reçu",
    "rdv_pris": "RDV pris",
    "demo_faite": "démo faite",
    "vendu": "vendu",
    "en_pause": "en pause",
}


def _jours_depuis(valeur: Any, maintenant: datetime) -> int | None:
    moment = _instant(valeur)
    return None if moment is None else max(0, (maintenant - moment).days)


def _truncate_note(note: str, limite: int = 90) -> str:
    texte = " ".join(str(note).split())
    return texte if len(texte) <= limite else texte[: limite - 1] + "…"


def _ligne_lead_chaud(lead: dict[str, Any], marque: str | None,
                      maintenant: datetime) -> str:
    """Une ligne de la liste : qui, où il en est, et le dernier fait connu."""
    from .lib import slack as slack_lib

    nom = lead.get("company_name") or lead.get("contact_email") or "(sans nom)"
    etape = str(lead.get("etape") or "a_produire")
    bouts: list[str] = [_ETIQUETTES_ETAPE.get(etape, etape)]

    if marque:
        # La marque PRÉCÈDE l'étape, elle ne l'efface pas : la première question
        # devant un lead en impasse est « où il en était ». Un désabonnement
        # après une démo ne se lit pas comme un désabonnement avant même la
        # production du site.
        nb = lead.get("nb_notes") or 0
        bouts = [marque, _ETIQUETTES_ETAPE.get(etape, etape)] + (
            [f"{nb} note{'s' if nb > 1 else ''} au carnet"] if nb else []
        )

    # L'action prévue AVANT la note : « À faire » qui ne dit pas quoi faire manque
    # son objet, et c'est ce champ qui porte la jambe « rappelle » de la promesse.
    # Sur un lead MARQUÉ, la consigne est caduque : on affiche pourquoi, jamais
    # l'ordre de relancer (voir `_mention_action_caduque`).
    action = str(lead.get("prochaine_action") or "").strip()
    if action and marque:
        bouts.append(_mention_action_caduque(marque))
    elif action:
        quand = slack_lib.jour(lead.get("prochaine_action_at") or "")
        bouts.append(f"⏰ {action}" + (f" ({quand})" if quand else ""))

    # Le RDV est un fait dur : affiché, jamais écrit au carnet.
    rdv = slack_lib.jour(lead.get("rdv_prochain_at") or "")
    if rdv:
        bouts.append(f"RDV {rdv} (Cal.com)")

    # La dernière réponse du prospect est un fait dur du même ordre que la démo
    # frappée et le RDV : un signe de vie obtenu SANS aucune discipline de
    # William. La vue la lisait déjà et le code la jetait — d'où un résumé qui
    # pouvait demander « toujours vivant ? » sur quelqu'un qui a écrit hier.
    jours_reponse = _jours_depuis(lead.get("derniere_reponse_at"), maintenant)
    if jours_reponse is not None:
        bouts.append(f"a répondu il y a {jours_reponse} j")

    if etape == "vendu" and not lead.get("fiche_client_existe"):
        bouts.append("⚠️ fiche client à créer")

    # Le silence est celui de William, pas celui du lead : on le dit ainsi, et on
    # affiche à côté le dernier fait obtenu SANS aucune discipline.
    jours_note = _jours_depuis(lead.get("derniere_note_at"), maintenant)
    if jours_note is not None:
        bouts.append(f"dernière note il y a {jours_note} j")
    else:
        depuis = _jours_depuis(lead.get("reference_immobilite"), maintenant)
        repere = slack_lib.jour(lead.get("demo_frappee_le") or "")
        if repere:
            bouts.append(f"frappé le {repere}")
        elif depuis is not None:
            bouts.append(f"a dit oui il y a {depuis} j")

    # Une réponse dans la même fenêtre que la question RÉPOND à la question :
    # même seuil, sinon on aurait deux mesures du même silence. Au-delà, la
    # réponse est le silence elle-même et la question redevient légitime.
    repondu_recemment = (
        jours_reponse is not None and jours_reponse < _JOURS_AVANT_DE_DEMANDER
    )
    immobile = _jours_depuis(lead.get("reference_immobilite"), maintenant)
    if (immobile is not None and immobile >= _JOURS_AVANT_DE_DEMANDER
            and etape not in {"en_pause", "vendu"} and not marque
            and not repondu_recemment):
        bouts.append("❓ toujours vivant ? (`perdu` ou `en_pause`)")

    if lead.get("note"):
        bouts.append(f"« {_truncate_note(lead['note'])} »")

    return f"  • {nom} — " + " · ".join(bouts)


def _rang_ligne_suppression(row: dict[str, Any]) -> tuple[int, str]:
    """Ordre de préférence entre deux lignes qui normalisent vers la même clé.

    L'unicité de `suppression_list.email` est celle de Postgres, sensible à la
    casse : `Jean@x.ca` et `jean@x.ca` peuvent coexister. On garde alors le
    RETRAIT DE CONSENTEMENT s'il y en a un (le fait le plus lourd de
    conséquences), et parmi les retraits le PLUS ANCIEN — c'est le moment où le
    consentement est réellement tombé."""
    retrait = row.get("reason") in _MOTIFS_RETRAIT_CONSENTEMENT
    return (0 if retrait else 1, str(row.get("created_at") or "9999"))


def _index_suppression(lignes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Toute la liste de suppression indexée par courriel normalisé."""
    index: dict[str, dict[str, Any]] = {}
    for row in lignes:
        cle = _cle_courriel(row.get("email"))
        if not cle:
            continue
        garde = index.get(cle)
        if garde is None or _rang_ligne_suppression(row) < _rang_ligne_suppression(garde):
            index[cle] = row
    return index


def _instant(horodatage: Any) -> datetime | None:
    """ISO → datetime aware, ou None si l'horodatage est absent/illisible.

    None n'est pas un détail d'implémentation : c'est l'état « date de
    désabonnement inconnue », que le résumé AFFICHE tel quel plutôt que
    d'inventer un moment."""
    s = str(horodatage or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# Sentinelle de tri pour « date de désabonnement inconnue » : plus petite que
# toute date réelle, donc reléguée en fin de liste (le tri est décroissant).
_JAMAIS = datetime.min.replace(tzinfo=timezone.utc)


def _dette_reglee(variable: str) -> bool:
    """Une dette assumée est-elle éteinte ? Vrai UNIQUEMENT si la variable
    d'environnement vaut littéralement « true ».

    ⚠️ FAIL-SAFE, et c'est **l'inverse assumé** de `check_warmup_window`
    (`src/lib/compliance_checks.py`), qui est fail-CLOSED : là-bas, une variable
    absente BLOQUE l'envoi, parce que le dommage à éviter est un courriel parti
    par erreur — irréversible. Ici, une variable absente AFFICHE le rappel,
    parce que le dommage à éviter est une dette oubliée : le coût d'un rappel de
    trop est une ligne dans Slack, celui d'un rappel manquant est un défaut qui
    dort des mois. Les deux sens protègent contre des dommages opposés : ne PAS
    « harmoniser » ce helper sur le patron du warmup.

    Corollaire : une valeur illisible (`"1"`, `"oui"`, une faute de frappe) ne
    règle rien — le rappel reste. On préfère le voir une fois de trop."""
    return os.environ.get(variable, "").strip().lower() == "true"


def _identite_lead(row: dict[str, Any]) -> str:
    """« Jean Roy <jean@plomberiex.ca> » — de quoi reconnaître la boîte d'un coup
    d'œil sans une requête de plus (le domaine du courriel suffit à l'identifier).

    Un contact sans prénom ni nom retombe sur le courriel seul : un
    « <vide> <info@x.ca> » ferait douter de la donnée elle-même.
    """
    nom = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip()
    courriel = (row.get("email") or "").strip()
    if nom and courriel:
        return f"{nom} <{courriel}>"
    return nom or courriel or "(contact sans courriel)"


# Verdicts de conformité qui rendent un draft NON envoyable. `approved` n'y est
# évidemment pas ; NULL non plus — un draft jamais jugé n'est pas un refus, il
# attend son tour dans le lot de /wf5/run.
#
# `orphelin` n'y est pas non plus, et c'est délibéré : un refus est un défaut de
# COPIE, réparable en réécrivant le courriel ; un orphelin est un défaut de
# DONNÉES. Fondus ensemble, ils enverraient relire un corps alors que le défaut
# est en base. Il a sa propre ligne dans le résumé, comme `non_juge`.
_VERDICTS_REFUS = ("needs_revision", "blocked")

# Le message n'a pas de quoi être jugé : pas de contact rattaché, ou sa propre
# ligne a disparu. Voir `_out_orphelin` pour la boucle que ça referme.
_VERDICT_ORPHELIN = "orphelin"


def _ligne_resume_conformite(
    *, refuses: int, a_relire: int, non_juges: int, orphelins: int = 0,
    partis_avec_remarque: int = 0,
) -> str:
    """La ligne « conformité » du résumé quotidien, ou la chaîne vide.

    Le ping #alertes de `/wf5/run` dit l'INSTANT ; cette ligne dit l'ÉTAT. Les
    deux sont nécessaires et ne se remplacent pas : une alerte se rate (Slack
    coupé, notification balayée), un résumé se relit le lendemain. C'est
    exactement le choix déjà fait pour les désabonnements plus haut dans ce même
    résumé.

    Silencieuse quand il n'y a rien à dire : un « refusés : 0 » quotidien est du
    bruit, et le bruit finit par cacher la ligne qui compte.

    `non_juges` est annoncé À PART parce qu'il ne dit pas la même chose qu'un
    refus : le corps n'a PAS été inspecté. Fondu dans `refuses`, le mode d'échec
    le plus grave (des courriels jamais relus) se déguiserait en travail de
    relecture ordinaire.

    `orphelins` est annoncé à part pour la raison symétrique : le corps est
    peut-être parfait, c'est la DONNÉE qui manque (pas de contact rattaché). Et
    surtout, c'est le seul compteur des trois dont l'alerte ne se répétera
    jamais : un orphelin sort du lot dès la première passe, donc `/wf5/run` ne
    le criera qu'une fois. Cette ligne-ci est ce qui reste après.
    """
    if refuses + non_juges + orphelins + partis_avec_remarque == 0:
        return ""

    # Le cas « rien de refusé, mais des remarques » a sa propre phrase : dire
    # « 0 drafts refusés » pour introduire une remarque serait du bruit qui
    # ressemble à une alarme.
    if refuses + non_juges + orphelins == 0:
        return (
            f"📝 *Conformité* — {partis_avec_remarque} courriel(s) parti(s) avec "
            f"une remarque de forme (lire `compliance_notes`)"
        )

    ligne = f"🚫 *Conformité* — {refuses} drafts refusés (dont {a_relire} à relire)"
    if non_juges:
        ligne += f" · ⚠️ {non_juges} jamais inspecté"
    if orphelins:
        ligne += f" · 🧩 {orphelins} sans contact rattaché"
    if partis_avec_remarque:
        ligne += f" · 📝 {partis_avec_remarque} parti(s) avec une remarque"
    return ligne


class DailySummaryIn(BaseModel):
    category: str = "summary"          # canal Slack du résumé (SLACK_WEBHOOK_SUMMARY)
    # `OPT` reste dans ce défaut alors que la piste est gelée depuis le pivot du
    # 2026-06-07, et c'est VOULU : ce défaut-ci ne fait RIEN générer, il choisit
    # ce qu'on REGARDE. Une piste gelée qui se remettrait à produire des chiffres
    # (cron oublié, insert manuel taggé OPT) doit rester visible — la retirer du
    # défaut reviendrait à décider qu'on ne veut plus le savoir. C'est aussi ce
    # que le cron n8n passe déjà explicitement (voir le bloc leads chauds).
    tracks: list[str] = ["OPT", "agence-ia"]
    post: bool = True                  # False = renvoie les chiffres sans poster (test)


@app.post("/summary/daily", dependencies=[Depends(_require_auth)])
async def summary_daily(payload: DailySummaryIn) -> dict[str, Any]:
    """Résumé quotidien de l'activité pipeline par track (sourcées/emails/drafts/
    poussés/envoyés/réponses/intéressés en attente de site) + RDV → Slack. Compté
    depuis minuit America/Toronto. Appelé par un cron n8n en fin de journée.

    Distinction importante :
      - `poussés`  = leads ajoutés à la campagne Instantly (messages.status='queued').
                     Le lead est DANS la campagne mais le courriel n'est pas encore
                     parti (ou la campagne est en pause). NE PAS lire ça comme « envoyé ».
      - `envoyés`  = courriel réellement parti, confirmé par le WF sync-status
                     (messages.status in sent/delivered/bounced/replied).
      - `interested_waiting_site` = contacts.interested_at posé (WF-7) sans ligne
                     agence.demo_sites encore créée (frappe du jeton, PT2). File
                     de travail : elle se vide. Un contact présent dans
                     suppression_list en sort quel que soit le motif — on
                     n'envoie pas William bâtir un site pour quelqu'un qu'on a
                     soi-même écarté.
      - `interested_then_unsubscribed` = avait dit oui PUIS a retiré son
                     consentement : statut opted_out, OU courriel sur
                     suppression_list avec un motif de retrait (opt_out /
                     spam_complaint — un hard_bounce n'en est pas un).
                     CUMUL, et affiché comme tel : rien ne l'en fait
                     redescendre. La ligne nomme les leads (désabonnement le
                     plus RÉCENT en tête, 5 max), donne les deux dates (« oui
                     le … , désabonné le … ») et porte l'interdit LCAP :
                     visibilité seulement, aucune relance par courriel n'est
                     permise après un retrait. Le libellé annonce en plus
                     « dont N depuis 7 jours » quand il y en a : le cumul reste
                     honnête et le nouveau saute aux yeux.
    Avant ce correctif (2026-06-04), `envoyés` comptait status!='draft' et gonflait
    les `queued` comme des envois — d'où des « envoyés 10 » alors que rien n'était parti."""
    from zoneinfo import ZoneInfo

    from . import supabase_client as sb
    from .lib import slack as slack_lib

    tz = ZoneInfo("America/Toronto")
    start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = start_local.astimezone(timezone.utc).isoformat()
    date_str = start_local.strftime("%Y-%m-%d")

    async def _cnt(table: str, extra: dict[str, str], date_field: str = "created_at") -> int:
        # count() exact côté serveur. Avant : `len(await sb.select(...))`, qui
        # plafonnait en silence à 1000 (max-rows PostgREST) — une bonne journée
        # de sourcing aurait affiché « sourcées 1000 » pour toujours.
        return await sb.count(table, params={date_field: f"gte.{cutoff}", **extra})

    # Borne du « dont N depuis 7 jours » : minuit America/Toronto il y a 6
    # jours, donc aujourd'hui + les 6 jours précédents. Même repère de journée
    # que les compteurs du jour ci-dessus — deux fuseaux dans un seul résumé
    # donneraient deux vérités.
    seuil_recent = start_local - timedelta(days=_JOURS_DESABONNEMENT_RECENT - 1)

    # `suppression_list` lue d'UN BLOC, une seule fois pour tout le résumé
    # (tous les tracks confondus), puis appariée en Python. Ce qu'on gagne par
    # rapport au `select()` par intéressé qu'il y avait avant :
    #   - une requête au lieu de N (le N+1 grossissait avec la file) ;
    #   - l'insensibilité à la casse (cf. `_cle_courriel`) — sans elle un
    #     désabonné restait « en attente de site » et William lui bâtissait
    #     un site ;
    #   - `created_at`, c'est-à-dire la DATE RÉELLE du désabonnement pour les
    #     motifs de retrait. Les deux écrivains l'alimentent (Edge Function
    #     `unsubscribe` du repo parent et WF-7), ce qui permet enfin de trier
    #     par récence et d'annoncer les désabonnements des 7 derniers jours.
    # `select_all` et non `select` : la table dépassera un jour 1000 lignes, et
    # PostgREST couperait là sans rien signaler. Si elle devenait assez grosse
    # pour que la ramener coûte, la suite est une jointure côté serveur
    # (`contacts?select=…,suppression_list(reason,created_at)`), pas un retour
    # au N+1.
    suppression = _index_suppression(
        await sb.select_all(
            "suppression_list",
            order="email",
            params={"select": "email,reason,created_at"},
        )
    )

    lines: list[str] = []
    totals: dict[str, Any] = {}
    for tk in payload.tracks:
        t = {"track": f"eq.{tk}"}
        sourced = await _cnt("companies", t)
        emails = await _cnt("contacts", t)
        drafts = await _cnt("messages", {**t, "direction": "eq.outbound", "status": "eq.draft"})
        # 'poussés' = lead ajouté à la campagne Instantly, courriel pas encore confirmé parti
        pushed = await _cnt(
            "messages", {**t, "direction": "eq.outbound", "status": "eq.queued"},
            date_field="scheduled_at",
        )
        # 'envoyés' = courriel réellement parti (confirmé par le WF sync-status Instantly)
        sent = await _cnt(
            "messages",
            {**t, "direction": "eq.outbound", "status": "in.(sent,delivered,bounced,replied)"},
            date_field="scheduled_at",
        )
        replies = await _cnt("messages", {**t, "direction": "eq.inbound"})

        # Intéressés (pivot tri 2026-08-20) : ont répondu « oui » au courriel de
        # tri (WF-7 → contacts.interested_at). Une seule lecture alimente DEUX
        # compteurs — qui ne se lisent PAS de la même façon, et le résumé le dit :
        #
        #   🔥 en attente de site = une FILE DE TRAVAIL, avec un cycle de vie
        #      complet (leçon P4.10 : un compteur sans sortie dérive).
        #      ENTRÉE : interested_at posé par WF-7. SORTIE : la frappe du jeton
        #      (session artisanale, PT2) crée la ligne agence.demo_sites du
        #      contact, et le lead quitte la file sans écriture dédiée.
        #
        #   ⚠️ désabonnés = un CUMUL, écrit « (cumul) » à l'écran pour cette
        #      raison précise. Il n'a PAS de sortie et n'en aura pas :
        #      interested_at est un journal et opted_out ne revient jamais en
        #      arrière. On assume donc le cumul et on le NOMME — laisser croire
        #      à une file de travail qui ne se vide jamais serait le mode
        #      d'échec P4.10. Ce qui rend la ligne actionnable malgré le cumul :
        #      le tri par date de DÉSABONNEMENT décroissante et la mention
        #      « dont N depuis 7 jours », qui font ressortir le nouveau sans
        #      falsifier le total.
        #
        # Sans filtre de date, dans les deux cas : c'est un ÉTAT, pas l'activité
        # du jour — un intéressé coincé depuis six semaines est celui qu'on veut
        # voir. (booked reste compté en attente : il attend peut-être encore sa
        # démo pendant la vente.)
        #
        # ⚠️ L'exclusion de statut se fait en Python, PAS dans le filtre SQL : le
        # compteur des désabonnés a besoin de VOIR les opted_out. Le comportement
        # de « en attente de site » est identique à avant ce changement.
        #
        # Pourquoi croiser AUSSI suppression_list, et pas se fier au seul statut :
        # il y a deux chemins de désabonnement. La réponse « désabonnez-moi »
        # (WF-7) et le clic sur le lien du footer (Edge Function `unsubscribe`)
        # posent tous deux status='opted_out'. Mais l'Edge Function écrit TOUJOURS
        # suppression_list et ne pose le statut qu'au mieux (erreur de lecture ou
        # d'écriture journalisée puis ignorée, la ligne de suppression restant la
        # source de vérité). Sans le croisement par courriel, ces cas dégradés
        # resteraient invisibles ici ET coincés dans la file « en attente de site ».
        # Le croisement se fait contre `suppression`, lu d'un bloc plus haut.
        interesses = await sb.select_all(
            "contacts",
            order="id",
            params={
                # L'identité voyage avec le compteur : la ligne du résumé NOMME
                # les leads (un nombre nu ne permet aucune décision). Le domaine
                # du courriel identifie la boîte — pas de requête `companies` de
                # plus juste pour afficher un nom d'entreprise.
                "select": "id,email,first_name,last_name,interested_at,status",
                "track": f"eq.{tk}",
                "interested_at": "not.is.null",
            },
        )
        impasses = {"opted_out", "disqualified", "bounced"}
        interested_waiting_site = 0
        desabonnes: list[dict[str, Any]] = []
        for r in interesses:
            statut = r.get("status")
            # L'index est consulté SANS filtre de motif, et le tri se fait ici.
            # Deux questions distinctes se posent sur la même ligne :
            #   « est-il supprimé ? »  → oui quel que soit le motif, donc il
            #     sort de la file de travail. Un contact écarté par NOUS
            #     (manual / competitor / dncl) ou dont l'adresse est morte
            #     (hard_bounce) restait sinon compté « en attente de site » :
            #     le tableau de bord demandait à William de bâtir un site
            #     pour un prospect qu'on avait soi-même retiré.
            #   « a-t-il retiré son consentement ? » → seulement opt_out /
            #     spam_complaint, et là seulement on l'ANNONCE avec le
            #     garde-fou LCAP. L'appliquer à une adresse morte mentirait.
            # Trois états, donc, et non deux : en attente · désabonné
            # (annoncé) · supprimé pour une autre raison (silencieux).
            # Ignorer le motif ici aligne aussi ce côté du système sur
            # `_interested_lead_is_suppressed` (src/tools/reply.py), qui lit
            # déjà suppression_list sans regarder le motif : une seule
            # définition de « supprimé » des deux bords.
            sup = suppression.get(_cle_courriel(r.get("email")))
            retrait = bool(sup) and sup.get("reason") in _MOTIFS_RETRAIT_CONSENTEMENT
            desabonne = statut == "opted_out" or retrait
            supprime = desabonne or sup is not None
            if desabonne:
                # La date ne vient QUE d'un motif de retrait : le created_at
                # d'un hard_bounce n'est pas un désabonnement, et un
                # status='opted_out' seul (cas dégradé) n'horodate rien du tout
                # — il sera rendu « désabonné (date inconnue) » plutôt
                # qu'affublé d'un moment inventé.
                desabonnes.append(
                    {**r, "_desabonne_le": sup.get("created_at") if retrait else None}
                )
                continue  # on ne lui doit plus de site : jamais dans l'autre file
            if supprime or statut in impasses:
                continue
            demo = await sb.select(
                "demo_sites",
                params={"select": "id", "contact_id": f"eq.{r['id']}", "limit": "1"},
                schema="agence",
            )
            if not demo:
                interested_waiting_site += 1

        interested_then_unsubscribed = len(desabonnes)
        # Tri par date de DÉSABONNEMENT décroissante — la question posée est
        # « quand un lead se désabonne-t-il APRÈS avoir dit oui ? ». Un oui de
        # mai retiré hier est le cas actionnable du jour ; trié par date du oui
        # (ce qu'on faisait avant), il finissait enterré derrière « … +N »,
        # donc invisible au moment précis où il fallait le voir. Date inconnue
        # → sentinelle `_JAMAIS`, donc fin de liste ; la date du oui départage
        # les ex æquo.
        desabonnes.sort(
            key=lambda d: (
                _instant(d.get("_desabonne_le")) or _JAMAIS,
                str(d.get("interested_at") or ""),
            ),
            reverse=True,
        )
        recents = sum(
            1
            for d in desabonnes
            if (moment := _instant(d.get("_desabonne_le"))) is not None
            and moment >= seuil_recent
        )
        totals[tk] = {
            "sourced": sourced, "emails": emails, "drafts": drafts,
            "pushed": pushed, "sent": sent, "replies": replies,
            "interested_waiting_site": interested_waiting_site,
            "interested_then_unsubscribed": interested_then_unsubscribed,
            "interested_then_unsubscribed_recent": recents,
        }
        ligne = (
            f"*{tk}* — sourcées {sourced} · emails {emails} · drafts {drafts} · "
            f"poussés {pushed} · envoyés {sent} · réponses {replies}"
        )
        if desabonnes:
            noms = []
            for d in desabonnes[:_PLAFOND_NOMS_DESABONNES]:
                # Les deux dates : quand il a dit oui, quand il est reparti.
                # `slack_lib.jour` des deux côtés — le même helper que le ping
                # WF-7, pour que les mêmes moments s'écrivent partout pareil.
                bout = _identite_lead(d)
                oui = slack_lib.jour(d.get("interested_at") or "")
                if oui:
                    bout += f" oui le {oui}"
                parti = slack_lib.jour(d.get("_desabonne_le") or "")
                bout += f", désabonné le {parti}" if parti else ", désabonné (date inconnue)"
                noms.append(bout)
            apercu = " · ".join(noms)
            reste = interested_then_unsubscribed - len(noms)
            if reste > 0:
                apercu += f" · … +{reste}"
            # « (cumul, dont N depuis 7 jours) » : le total ne ment pas sur sa
            # nature et la nouvelle du jour se voit quand même. Sans récent, la
            # mention disparaît — un « dont 0 » quotidien serait du bruit.
            libelle = f"{interested_then_unsubscribed} (cumul"
            if recents:
                libelle += f", dont {recents} depuis {_JOURS_DESABONNEMENT_RECENT} jours"
            ligne += f"\n  ⚠️ intéressés désabonnés {libelle})"
            if apercu:
                ligne += f" — {apercu}"
            # L'interdit voyage AVEC le chiffre : un compteur inexpliqué invite au
            # réflexe « je le relance pour comprendre », qui est l'infraction.
            # Même constante que le ping WF-7 — un seul interdit, une seule source.
            ligne += f"\n    {slack_lib.GARDE_LCAP_APRES_DESABONNEMENT}"
        lines.append(ligne)

    # ---------------------------------------------------------- PT3 leads chauds
    # Hors de la boucle par track, et épinglé sur 'agence-ia' : une seule offre
    # depuis le pivot du 2026-06-07, et le cron passe encore ["OPT", "agence-ia"]
    # — laissé dans la boucle, le bloc s'imprimerait deux fois dont une section
    # OPT vide. Même choix que le bloc v_pourquoi_pas_de_courriel plus bas.
    try:
        chauds = await sb.select_all(
            "v_suivi_lead_courant",
            order="contact_id",
            params={
                "select": (
                    "contact_id,company_id,company_name,contact_email,first_name,"
                    "last_name,contact_status,interested_at,etape,note,"
                    "prochaine_action,prochaine_action_at,derniere_note_at,nb_notes,"
                    "demo_frappee_le,rdv_prochain_at,derniere_reponse_at,"
                    "fiche_client_existe,reference_immobilite"
                ),
                "track": "eq.agence-ia",
            },
            schema="agence",
        )
        lecture_chauds_ok = True
    except Exception as e:  # noqa: BLE001
        # Fail-soft, mais JAMAIS silencieux : ce bloc est la file de travail de
        # William. Une liste vide pour cause de panne serait indiscernable d'une
        # journée sans lead chaud — le mode d'échec que ce travail doit éteindre.
        print(f"[summary] lecture v_suivi_lead_courant échouée: {e!r}")
        chauds = []
        lecture_chauds_ok = False

    maintenant = datetime.now(timezone.utc)
    # Ancré sur minuit America/Toronto comme TOUT le reste du résumé (compteurs
    # du jour, fenêtre « depuis 7 jours » des désabonnés) : deux fuseaux dans un
    # seul résumé donneraient deux vérités. L'horizon couvre la fin de demain,
    # pas « dans 24 h » — c'est une journée calendaire, pas une durée.
    horizon = start_local + timedelta(days=_HORIZON_A_FAIRE_JOURS + 1)
    a_faire: list[str] = []
    visibles: list[tuple[float, str]] = []
    en_pause: list[str] = []
    total_chauds = 0
    for lead in chauds:
        etape = str(lead.get("etape") or "a_produire")
        marque = _marque_impasse(
            lead.get("contact_status"),
            suppression.get(_cle_courriel(lead.get("contact_email"))),
        )
        # Sortie de la liste : `perdu` écrit à la main, ou `vendu` dont la fiche
        # client existe. Rien d'autre — surtout pas une impasse, qui MARQUE.
        if etape == "perdu":
            continue
        if etape == "vendu" and lead.get("fiche_client_existe"):
            continue
        total_chauds += 1
        ligne_lead = _ligne_lead_chaud(lead, marque, maintenant)

        # Épinglé en tête, hors tri et hors plafond, pour DEUX motifs qui ne se
        # comportent PAS pareil devant une marque d'impasse.
        #
        # 1. Une ACTION DUE — jamais sur un lead marqué. « À faire » est la ligne
        #    la plus visible du résumé : y épingler un désabonné avec sa consigne
        #    de relance, c'est mettre l'infraction LCAP en tête d'affiche. Plus
        #    largement, l'action prévue avant une impasse ne s'exécute plus,
        #    quelle que soit l'impasse. Le lead reste dans la liste principale,
        #    marqué — il ne réclame juste plus rien.
        #
        # 2. Un `vendu` DONT LA FICHE N'EST PAS OUVERTE — épinglé TOUJOURS, marque
        #    ou pas. « fiche client à créer » est de la COMPTABILITÉ INTERNE, pas
        #    une sollicitation : ouvrir une fiche ne recontacte personne, il n'y a
        #    donc rien à interdire. Or WF-7 peut classer `disqualified` sur une
        #    réponse mal lue APRÈS la vente ; sous la règle catégorique, la ligne
        #    quittait « À faire » — et un `vendu` a par construction zéro jour
        #    d'immobilité, donc il se trie EN DERNIER et sort le premier par le
        #    plafond de 10. La marque, elle, reste affichée sur la ligne
        #    (_ligne_lead_chaud la met en tête des bouts).
        echeance = _instant(lead.get("prochaine_action_at"))
        due = echeance is not None and echeance <= horizon
        fiche_a_ouvrir = etape == "vendu" and not lead.get("fiche_client_existe")
        if fiche_a_ouvrir or (due and not marque):
            a_faire.append(ligne_lead)
            continue

        if etape == "en_pause":
            en_pause.append(ligne_lead)
            continue
        anciennete = _jours_depuis(lead.get("reference_immobilite"), maintenant) or 0
        visibles.append((float(anciennete), ligne_lead))

    # Le plus négligé en haut. `en_pause` reste affiché mais hors du tri : il
    # accumule de l'ancienneté indéfiniment et squatterait la tête de liste.
    visibles.sort(key=lambda v: v[0], reverse=True)
    rendues = [texte for _, texte in visibles] + en_pause
    reste = max(0, len(rendues) - _PLAFOND_LEADS_CHAUDS)
    bloc_chauds = ""
    if a_faire:
        bloc_chauds += "\n⏰ *À faire*\n" + "\n".join(a_faire)
    if not lecture_chauds_ok:
        # Dire la panne PLUTÔT que de rendre une liste vide qui ressemble à une
        # journée calme. Le fail-soft protège le reste du résumé ; il ne doit pas
        # protéger William de la vérité.
        bloc_chauds += "\n🔥 *Tes leads chauds* — ⚠️ carnet illisible (lecture en échec)"
    elif not total_chauds:
        bloc_chauds += "\n🔥 *Tes leads chauds* — aucun lead chaud aujourd'hui"
    else:
        entete = f"\n🔥 *Tes leads chauds ({total_chauds})*"
        if not rendues:
            # Tout ce qui est chaud est épinglé au-dessus. Le compte inclut les
            # épinglés (ils RESTENT des leads chauds), mais un « (1) » posé sur
            # zéro ligne — suivi d'une ligne vide — se lit comme une liste perdue
            # en route. On dit où sont les lignes au lieu de laisser douter.
            bloc_chauds += entete + " — tous dans « À faire » ci-dessus"
        else:
            bloc_chauds += entete + "\n" + "\n".join(rendues[:_PLAFOND_LEADS_CHAUDS])
            if reste:
                bloc_chauds += f"\n  … et {reste} autre{'s' if reste > 1 else ''}"

    bookings = await _cnt("booking_events", {})
    totals["bookings_total"] = bookings

    text = (
        f"📊 *Résumé quotidien — {date_str}*\n"
        + "\n".join(lines)
        + bloc_chauds
        + f"\n📅 RDV bookés: {bookings}"
    )

    # ------------------------------------------- Conformité : l'ÉTAT des drafts
    # Le ping #alertes de /wf5/run dit l'INSTANT du lot ; cette ligne dit l'ÉTAT.
    # Les deux sont nécessaires et ne se remplacent pas — une alerte se rate
    # (Slack coupé, notification balayée), un résumé se relit le lendemain. Même
    # choix que pour les désabonnements plus haut dans ce même résumé. Sans
    # elle, un lot entier pouvait mourir sans qu'un seul chiffre le rappelle le
    # lendemain, et « refusés : 0 » par absence de ligne a l'air excellent —
    # c'est le trou nommé par la migration 0045.
    #
    # SANS FILTRE DE DATE, comme les désabonnés et le bloc 🧱 : la question
    # posée n'est pas « qu'a fait la passe aujourd'hui ? » mais « qu'est-ce qui
    # traîne ? ». Un draft refusé il y a trois jours et jamais repris est
    # précisément celui qu'on veut revoir.
    #
    # SANS FILTRE DE TRACK, et ce n'est pas un oubli : la requête du lot de
    # /wf5/run n'en porte pas non plus. La ligne compte donc exactement ce que
    # la passe juge — deux définitions du même lot donneraient deux vérités.
    #
    # `status=not.in.(failed)` = la définition de « message vivant » déjà posée
    # par la migration 0037 et par l'éligibilité WF-4. C'est ce qui donne une
    # SORTIE au compteur (leçon P4.10) : retirer un draft à la main le marque
    # 'failed', et il quitte la ligne sans qu'on efface l'histoire du refus.
    #
    # `count()` et JAMAIS `len(select(...))` : PostgREST plafonne à 1000 lignes
    # en silence et les agrégats côté serveur sont désactivés ici (PGRST123).
    vivants = {"direction": "eq.outbound", "status": "not.in.(failed)"}
    try:
        refuses = await sb.count(
            "messages",
            params={**vivants, "compliance_verdict": f"in.({','.join(_VERDICTS_REFUS)})"},
        )
        a_relire = await sb.count(
            "messages", params={**vivants, "compliance_verdict": "eq.needs_revision"},
        )
        non_juges = await sb.count(
            "messages", params={**vivants, "compliance_verdict": "eq.non_juge"},
        )
        orphelins = await sb.count(
            "messages",
            params={**vivants, "compliance_verdict": f"eq.{_VERDICT_ORPHELIN}"},
        )
        # 🔴 Les courriels PARTIS avec une remarque de forme.
        #
        # Depuis la décision du 2026-08-31, une faute de forme (registre mêlé,
        # cinq « pis », mot de vendeur, mise en scène de la recherche) ne tue
        # plus le brouillon : elle s'écrit dans les notes et le courriel part.
        #
        # C'est cette ligne-ci qui empêche la décision de devenir « on a
        # supprimé les checks ». Sans elle, la remarque existerait dans une
        # colonne que personne ne lit — donc n'existerait pas.
        #
        # ⚠️ On filtre sur `approved` exprès : un brouillon refusé POUR AUTRE
        # CHOSE peut aussi porter une remarque, mais il n'est jamais parti.
        # Le compter ici ferait croire à un courriel envoyé qui ne l'est pas.
        partis_avec_remarque = await sb.count(
            "messages",
            params={
                **vivants,
                "compliance_verdict": "eq.approved",
                "compliance_notes": "ilike.*remarque [*",
            },
        )
        lecture_conformite_ok = True
    except Exception as e:  # noqa: BLE001
        # Fail-soft, JAMAIS silencieux — même règle que le carnet des leads
        # chauds et que la condition de la dette WF-7 : une ligne absente pour
        # cause de panne serait indiscernable d'une journée tout-vert, ce qui
        # est très exactement le mode d'échec que cette ligne existe pour
        # éteindre.
        print(f"[summary] lecture des verdicts de conformité échouée: {e!r}")
        refuses = a_relire = non_juges = orphelins = partis_avec_remarque = 0
        lecture_conformite_ok = False

    totals["conformite"] = {
        "refuses": refuses, "a_relire": a_relire,
        "non_juges": non_juges, "orphelins": orphelins,
        "partis_avec_remarque": partis_avec_remarque,
        "lu": lecture_conformite_ok,
    }
    if not lecture_conformite_ok:
        text += (
            "\n🚫 *Conformité* — ⚠️ lecture des verdicts en ÉCHEC : impossible de "
            "dire si des drafts sont refusés. L'absence de ligne ne veut PAS dire "
            "« tout vert » aujourd'hui."
        )
    else:
        ligne_conformite = _ligne_resume_conformite(
            refuses=refuses, a_relire=a_relire, non_juges=non_juges,
            orphelins=orphelins, partis_avec_remarque=partis_avec_remarque,
        )
        if ligne_conformite:
            text += "\n" + ligne_conformite

    # État du PARC, sans filtre de date : c'est l'entreprise coincée depuis six
    # semaines qu'on veut voir, pas l'activité du jour.
    #
    # `track` est figé sur 'agence-ia' à dessein, il ne suit PAS payload.tracks :
    # le projet a pivoté sur une offre unique le 2026-06-07 et 'OPT' est du legacy
    # gelé, qu'on ne prospecte plus. Ne pas « corriger » en bouclant sur les tracks.
    #
    # select_all : 816 lignes aujourd'hui pour agence-ia, sur un plafond PostgREST
    # de 1000 — et ce compteur grossit à chaque run de sourcing. Un `select()` se
    # serait fait couper sans erreur, rendant un top 3 faux dans le planner order.
    lignes_motifs = await sb.select_all(
        "v_pourquoi_pas_de_courriel",
        order="company_id",
        params={"select": "motif,recontactable", "track": "eq.agence-ia"},
    )
    compte: dict[str, int] = {}
    a_juger = 0
    for row in lignes_motifs:
        motif = row.get("motif") or "?"
        if motif != "en_file":
            compte[motif] = compte.get(motif, 0) + 1
        if row.get("recontactable") == "a_juger":
            a_juger += 1

    if compte:
        top3 = sorted(compte.items(), key=lambda kv: kv[1], reverse=True)[:3]
        text += "\n🧱 " + " · ".join(f"{m} {n}" for m, n in top3)
    if a_juger:
        text += f"\n🔎 {a_juger} entreprises que le temps ne réparera pas"

    # ------------------------------------------------ PT3 : dettes assumées
    # PT3 laisse deux dettes volontaires, écrites dans docs/go-live-checklist.md.
    # Mais une dette consignée dans un fichier que personne ne rouvre au bon
    # moment n'existe pas : ce projet a déjà payé ce mode d'échec deux fois (le
    # runbook qui disait l'inverse de la réalité sur WARMUP_END_DATE ; la panne
    # Google Places restée invisible cinq semaines). William est seul — le jour
    # où la dette devient exigible, il vend, il ne relit pas une checklist
    # rédigée des mois plus tôt. Le résumé la lui redit donc LUI-MÊME, au moment
    # où elle devient vraie.
    #
    # Extinction par variable d'environnement, en FAIL-SAFE (variable absente ⇒
    # rappel affiché) — voir `_dette_reglee` pour pourquoi le sens est l'inverse
    # de check_warmup_window. Poser la variable est le geste qui éteint le
    # rappel : rien à recoder, rien à redéployer.
    #
    # Placé en toute fin, après les motifs 🧱/🔎 : c'est une note de bas de page,
    # elle ne doit jamais repousser les leads chauds vers le bas.
    dettes: list[str] = []

    if not _dette_reglee("DETTE_WF7_REGLEE"):
        # Condition d'apparition : au moins un « oui » enregistré, tous tracks
        # confondus. Avant le premier oui, le scénario du double-réponse ne peut
        # pas se produire et le rappel ne serait que du bruit quotidien.
        #
        # `count()` et non `select_all()` : la question est « y en a-t-il AU
        # MOINS un ? ». Ramener les lignes pour les compter en Python, ce serait
        # payer le N+1 et se faire couper au plafond PostgREST de 1000. Les
        # agrégats côté serveur (`select=count()`) sont désactivés sur ce projet
        # (PGRST123) : `sb.count` lit l'en-tête Content-Range, ce qui marche.
        try:
            des_oui = await sb.count("contacts", params={"interested_at": "not.is.null"})
            condition_lue = True
        except Exception as e:  # noqa: BLE001
            # Fail-soft, mais JAMAIS silencieux : sans ça, un rappel absent pour
            # cause de panne serait indiscernable d'un rappel absent parce que
            # personne n'a encore dit oui. C'est très exactement le mode d'échec
            # que ce bloc existe pour éteindre — on ne va pas le réintroduire ici.
            print(f"[summary] lecture de la condition dette WF-7 échouée: {e!r}")
            des_oui, condition_lue = 0, False
        if not condition_lue:
            dettes.append(
                "⚠️ *Dette PT3* — lecture de contacts.interested_at en ÉCHEC : "
                "impossible de dire si un lead a déjà répondu oui, donc le rappel "
                "du ping WF-7 peut manquer aujourd'hui sans que ça se voie. "
                "Détail : docs/go-live-checklist.md § 4bis."
            )
        elif des_oui:
            dettes.append(
                "⚠️ *Dette PT3* — un lead a dit oui : au premier qui répond DEUX "
                "fois, vérifier dans les logs de /wf7/poll (ou l'Unibox Instantly) "
                "si la 2e réponse est captée, PUIS corriger le ping WF-7. "
                "Détail et piège : docs/go-live-checklist.md § 4bis."
            )

    if not _dette_reglee("DETTE_ERRORWF_VERIFIEE"):
        # Pas de condition d'apparition : elle est vraie tant que personne n'a
        # ouvert le menu n8n. Elle se rappelle DANS le message qu'elle protège —
        # si ce résumé arrive, le rappel arrive avec lui ; s'il n'arrive pas,
        # c'est précisément le défaut dont il parle.
        dettes.append(
            "⚠️ *Dette PT3* — vérifier dans n8n que le champ « Error Workflow » du "
            "résumé quotidien affiche bien [OPS] Error Handler → Slack. L'id "
            "weHbzb97xdjo2OEd vient de wf-9 et n'est vérifiable QUE dans le menu "
            "n8n. S'il pointe à côté, ce résumé peut mourir en silence."
        )

    if dettes:
        text += "\n" + "\n".join(dettes)

    posted = False
    if payload.post:
        posted = await slack_lib.notify(
            text=text, context="daily_summary", category=payload.category
        )
    return {"date": date_str, "totals": totals, "posted": posted, "text": text}


# ---------------- Sourcing ----------------

@app.get("/sourcing/next-target", dependencies=[Depends(_require_auth)])
async def next_target(track: str = "agence-ia") -> dict[str, Any] | None:
    """Prochaine (city, sector) à scraper. Le défaut vise la piste VIVANTE :
    un appel nu qui rendrait une cible `OPT` enverrait sourcer une piste
    gelée depuis le pivot du 2026-06-07."""
    t = await db_tools.next_sourcing_target(track=track)
    return t.model_dump() if t else None


@app.post("/sourcing/start-run", dependencies=[Depends(_require_auth)])
async def start_run(payload: db_tools.StartRunIn) -> dict[str, Any]:
    return (await db_tools.start_sourcing_run(payload)).model_dump()


@app.post("/sourcing/complete-run", dependencies=[Depends(_require_auth)])
async def complete_run(payload: db_tools.CompleteRunIn) -> dict[str, Any]:
    return await db_tools.complete_sourcing_run(payload)


# ---------------- Companies ----------------

@app.post("/companies/insert", dependencies=[Depends(_require_auth)])
async def insert_company(payload: db_tools.CompanyIn) -> dict[str, Any]:
    return (await db_tools.insert_company(payload)).model_dump()


@app.get("/companies/recent", dependencies=[Depends(_require_auth)])
async def recent_companies(limit: int = 20) -> list[dict[str, Any]]:
    return await db_tools.list_recent_companies(limit=limit)


# ---------------- Maps ----------------

@app.post("/maps/search-places", dependencies=[Depends(_require_auth)])
async def search_places(payload: maps_tools.SearchPlacesIn) -> dict[str, Any]:
    return (await maps_tools.search_places(payload)).model_dump()


# ---------------- Contacts ----------------

@app.post("/contacts/insert", dependencies=[Depends(_require_auth)])
async def insert_contact(payload: db_tools.ContactIn) -> dict[str, Any]:
    return (await db_tools.insert_contact(payload)).model_dump()


# ---------------- High-level workflow (WF-1 en un appel) ----------------

class RunWf1In(BaseModel):
    """Lance un pass complet WF-1 côté serveur — pratique pour n8n qui n'a
    qu'à déclencher le cron, le serveur gère le reste."""
    city: str | None = None
    sector: str | None = None
    icp_segment: str | None = None
    max_pages: int = 3
    dry_run: bool = False
    # Piste VIVANTE par défaut. `OPT` est gelée depuis le pivot du 2026-06-07 :
    # un /wf1/run nu sourcerait le catalogue gelé ET taguerait les nouvelles
    # boîtes `OPT` à l'insert — invisibles pour WF-4, qui filtre agence-ia.
    track: str = "agence-ia"  # catalogue + tag à l'insert


class RunWf1Out(BaseModel):
    target: dict[str, Any] | None
    run_id: str | None
    total_results: int
    new_companies_count: int
    duplicates_count: int
    filtered_junk_count: int = 0
    error_text: str | None = None


@app.post("/wf1/run", dependencies=[Depends(_require_auth)], response_model=RunWf1Out)
async def run_wf1(payload: RunWf1In) -> RunWf1Out:
    import asyncio

    # 1) Pick target
    if payload.city and payload.sector and payload.icp_segment:
        city, sector, icp = payload.city, payload.sector, payload.icp_segment
        target_meta = {"city": city, "sector": sector, "icp_segment": icp, "reason": "explicit"}
    else:
        t = await db_tools.next_sourcing_target(track=payload.track)
        if not t:
            return RunWf1Out(
                target=None, run_id=None, total_results=0,
                new_companies_count=0, duplicates_count=0,
                error_text="no_target_available",
            )
        city, sector, icp = t.city, t.sector, t.icp_segment
        target_meta = t.model_dump()

    # 2) Start run (sauf dry_run)
    run_id: str | None = None
    if not payload.dry_run:
        run = await db_tools.start_sourcing_run(
            db_tools.StartRunIn(city=city, sector=sector, icp_segment=icp)
        )
        run_id = run.run_id

    page_token: str | None = None
    total_results = 0
    new_count = 0
    dup_count = 0
    filtered_count = 0
    error_text: str | None = None

    try:
        for page_num in range(payload.max_pages):
            if page_num > 0:
                if not page_token:
                    break
                await asyncio.sleep(2.5)  # warm-up nextPageToken Google
            out = await maps_tools.search_places(
                maps_tools.SearchPlacesIn(
                    city=city, sector=sector, page_token=page_token, max_results=20
                )
            )
            total_results += len(out.results)

            for p in out.results:
                if payload.dry_run:
                    continue
                # Junk au sourcing (spa détente, hôtel, chaîne retail…) → skip
                # l'insert, ne pollue pas companies. Voir lib.sourcing_filters.
                if sourcing_disqualify_reason(p.name, p.primary_type):
                    filtered_count += 1
                    continue
                res = await db_tools.insert_company(
                    db_tools.CompanyIn(
                        name=p.name,
                        google_place_id=p.google_place_id,
                        address=p.formatted_address,
                        city=p.city or city,
                        postal_code=p.postal_code,
                        latitude=p.latitude,
                        longitude=p.longitude,
                        website=p.website,
                        domain=p.domain,
                        icp_segment=icp,
                        industry=sector,
                        google_types=p.google_types,
                        google_rating=p.google_rating,
                        google_reviews_count=p.google_reviews_count,
                        track=payload.track,
                        raw_payload=p.raw_payload,
                    )
                )
                if res.status == "inserted":
                    new_count += 1
                else:
                    dup_count += 1

            page_token = out.next_page_token
            if not page_token:
                break

    except Exception as e:  # noqa: BLE001
        error_text = repr(e)

    if run_id:
        await db_tools.complete_sourcing_run(
            db_tools.CompleteRunIn(
                run_id=run_id,
                status="failed" if error_text else "completed",
                next_page_token=page_token,
                results_count=total_results,
                new_companies_count=new_count,
                duplicates_count=dup_count,
                error_text=error_text,
            )
        )

    return RunWf1Out(
        target=target_meta,
        run_id=run_id,
        total_results=total_results,
        new_companies_count=new_count,
        duplicates_count=dup_count,
        filtered_junk_count=filtered_count,
        error_text=error_text,
    )


# ---------------- Research (Phase 2 — WF-3) ----------------

# Au-delà de ce nombre d'échecs research cumulés sur une même company, on la
# disqualifie pour qu'elle arrête de boucler dans le backlog WF-3 (voir handler
# d'erreur de /research/company).
_RESEARCH_MAX_FAILURES = 3


@app.get("/companies/to-research", dependencies=[Depends(_require_auth)])
async def companies_to_research(
    limit: int = 20,
    require_website: bool = True,
    track: str = "agence-ia",  # défaut = track live ; OPT retiré = jamais scrapé sauf demande explicite
) -> list[dict[str, Any]]:
    """Backlog de recherche : jamais researchée, OU researchée sans contact il y a
    plus de 90 jours. Utilisé par n8n pour visualiser le backlog."""
    return await db_tools.list_companies_to_research(
        limit=limit, require_website=require_website, track=track
    )


class ResearchCompanyByIdIn(BaseModel):
    company_id: str
    model: str = "claude-sonnet-4-6"
    track: str = "agence-ia"  # track live ; OPT retiré (legacy) — sélectionne les critères de scoring


class ResearchCompanyByIdOut(BaseModel):
    company_id: str
    status: str  # "ok" | "skipped_no_place_id" | "error"
    research_json: dict[str, Any] | None = None
    duration_ms: int | None = None
    error_text: str | None = None
    emails_scraped_inserted: int = 0
    emails_scraped_duplicate: int = 0


@app.post(
    "/research/company",
    dependencies=[Depends(_require_auth)],
    response_model=ResearchCompanyByIdOut,
)
async def research_company_by_id(payload: ResearchCompanyByIdIn) -> ResearchCompanyByIdOut:
    """Research d'UNE company. Pratique pour n8n quand on veut traiter
    company-par-company (avec retry par item)."""
    from . import supabase_client as db
    matches = await db.select(
        "companies",
        params={
            "select": "id,google_place_id,website,name",
            "id": f"eq.{payload.company_id}",
            "limit": "1",
        },
    )
    if not matches:
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="error",
            error_text="company_not_found",
        )
    co = matches[0]
    if not co.get("google_place_id"):
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="skipped_no_place_id",
        )

    try:
        out = await research_tools.research_company(
            research_tools.ResearchCompanyIn(
                google_place_id=co["google_place_id"],
                website=co.get("website"),
                company_name=co.get("name"),
                model=payload.model,
                track=payload.track,
            )
        )
    except Exception as e:  # noqa: BLE001
        # Audit l'échec, sans bloquer
        try:
            await db_tools.record_agent_run(
                db_tools.AgentRunIn(
                    agent="research",
                    model=payload.model,
                    company_id=payload.company_id,
                    error_text=repr(e),
                )
            )
        except Exception:  # noqa: BLE001
            pass
        # Garde-fou anti-coincement : `list_companies_to_research` re-sélectionne
        # toute company avec research_json=null, donc un échec récurrent (JSON
        # cassé, site/place inaccessible) revient chaque jour et bloque un slot du
        # batch indéfiniment. Au-delà de _RESEARCH_MAX_FAILURES échecs cumulés,
        # on disqualifie pour la sortir du backlog (exclu via status neq.disqualified).
        try:
            prior_failures = await db.select(
                "agent_runs",
                params={
                    "select": "id",
                    "agent": "eq.research",
                    "company_id": f"eq.{payload.company_id}",
                    "error_text": "not.is.null",
                    "limit": str(_RESEARCH_MAX_FAILURES + 1),
                },
            )
            if len(prior_failures) >= _RESEARCH_MAX_FAILURES:
                await db_tools.mark_company_disqualified(
                    payload.company_id,
                    f"research_failed_repeatedly ({len(prior_failures)}x): {e!r}"[:500],
                )
        except Exception:  # noqa: BLE001
            pass
        return ResearchCompanyByIdOut(
            company_id=payload.company_id,
            status="error",
            error_text=repr(e),
        )

    await db_tools.update_company_research(
        payload.company_id, out.research_json, emails_found=out.emails_found
    )
    try:
        await db_tools.record_agent_run(
            db_tools.AgentRunIn(
                agent="research",
                model=out.model,
                company_id=payload.company_id,
                input_payload={
                    "google_place_id": co["google_place_id"],
                    "website": co.get("website"),
                },
                output_payload=out.research_json,
                duration_ms=out.duration_ms,
                input_tokens=out.usage.input_tokens,
                output_tokens=out.usage.output_tokens,
                cache_read_tokens=out.usage.cache_read_input_tokens,
                cache_creation_tokens=out.usage.cache_creation_input_tokens,
            )
        )
    except Exception:  # noqa: BLE001
        pass

    # Insère les emails scrapés du site comme contacts (source unique du pipeline
    # depuis le retrait d'Apollo). Le `owner_confidence` (confirmed/potential/unknown)
    # est décidé par `classify_scraped_contact` à partir des `decideur_candidats`
    # extraits par le Research Agent. confirmed => nom attaché (fait) ; potential =>
    # nom deviné isolé dans potential_owner ; unknown => aucun nom. Tous sont insérés
    # (aucune quarantaine) ; c'est le template WF-4 qui varie selon owner_confidence.
    # Base légale = implied_conspicuous (voir _consent_basis_for_contact).
    # `email_verified=False` → ces emails n'ont pas été validés par un fournisseur tiers.
    decideurs = (out.research_json or {}).get("decideur_candidats") or []
    inserted_scraped = duplicate_scraped = 0
    for em in out.emails_found:
        decision = classify_scraped_contact(em, decideurs)
        res = await db_tools.insert_contact(
            db_tools.ContactIn(
                company_id=payload.company_id,
                email=em["email"],
                email_verified=False,
                email_verification_source="website_scrape",
                first_name=decision.first_name,
                last_name=decision.last_name,
                title=decision.title,
                is_decision_maker=(decision.owner_confidence == "confirmed"),
                owner_confidence=decision.owner_confidence,
                potential_owner=decision.potential_owner,
                source="website",
                raw_payload={
                    "kind": em["kind"],  # nominative | generic | other
                    "source_url": em.get("source_url"),
                    "local": em["local"],
                },
            )
        )
        if res.status == "inserted":
            inserted_scraped += 1
        elif res.status == "duplicate":
            duplicate_scraped += 1

    return ResearchCompanyByIdOut(
        company_id=payload.company_id,
        status="ok",
        research_json=out.research_json,
        duration_ms=out.duration_ms,
        emails_scraped_inserted=inserted_scraped,
        emails_scraped_duplicate=duplicate_scraped,
    )


class BrandKitBuildIn(BaseModel):
    company_id: str
    model: str = "claude-sonnet-4-6"
    wait: bool = False  # True = build synchrone (await + renvoie le résultat/erreur). Sinon async.


class BrandKitBuildOut(BaseModel):
    company_id: str
    status: str  # accepted (réponse async) ; le build de fond produit ok|needs_review|
    #              skipped_already_reviewed|company_not_found|error (loggé, pas renvoyé)
    fields_filled: list[str] = []
    confidence: dict[str, Any] = {}
    error_text: str | None = None


async def _run_brandkit_build(company_id: str, model: str) -> None:
    log = logging.getLogger("brand-kit")
    try:
        result = await brand_kit_tools.build_brand_kit(company_id, model=model)
        # build_brand_kit ne lève pas pour company_not_found / skipped → tracer le no-op.
        if result.get("status") not in ("ok", "needs_review"):
            log.warning("build_brand_kit no-op pour %s: %s", company_id, result.get("status"))
    except Exception:  # noqa: BLE001 — tâche de fond : log only, ne casse pas le worker
        log.exception("build_brand_kit a échoué pour %s", company_id)


@app.post(
    "/research/brand-kit",
    dependencies=[Depends(_require_auth)],
    response_model=BrandKitBuildOut,
)
async def build_company_brand_kit(
    payload: BrandKitBuildIn, background_tasks: BackgroundTasks
) -> BrandKitBuildOut:
    """Lance build_brand_kit en tâche de fond (build long : 30-90 s) et renvoie tout de
    suite. Le kit (status ok|needs_review) est écrit quand le build finit ; la démo
    no-store le reflète en direct. Idempotent (garde anti-clobber via companies.brand_kit_status)."""
    if payload.wait:
        # Build synchrone (diagnostic / usage manuel) : on attend et on renvoie le
        # vrai résultat ou l'erreur, au lieu de l'avaler dans la tâche de fond.
        try:
            result = await brand_kit_tools.build_brand_kit(payload.company_id, model=payload.model)
        except Exception as e:  # noqa: BLE001
            return BrandKitBuildOut(
                company_id=payload.company_id, status="error", error_text=repr(e)[:1000]
            )
        return BrandKitBuildOut(
            company_id=payload.company_id,
            status=result.get("status", "unknown"),
            fields_filled=result.get("fields_filled", []),
            confidence=result.get("confidence", {}),
        )
    background_tasks.add_task(_run_brandkit_build, payload.company_id, payload.model)
    return BrandKitBuildOut(company_id=payload.company_id, status="accepted")


class RunWf3In(BaseModel):
    """Pass complet WF-3 : prend N companies sans research_json, les traite en parallèle borné.

    `concurrency` : nb de companies recherchées en parallèle (sémaphore bornée). Garde
    l'appel `/wf3/run` court — 10 en série ≈ 270-300s déclenchait un 502 edge Railway
    (timeout ~300s). En parallèle borné à 4, un lot de 10 tient en ~80-100s. Le retry
    interne de `_call_llm` (tenacity backoff) absorbe les 529 Anthropic transitoires ;
    plus besoin d'espacer les appels manuellement.

    `inter_company_sleep_seconds` : conservé pour rétro-compat de l'API, mais ignoré
    depuis le passage en parallèle (le sémaphore borne déjà la pression sur Anthropic).
    """
    limit: int = 10
    model: str = "claude-sonnet-4-6"
    require_website: bool = True
    concurrency: int = 4
    inter_company_sleep_seconds: float = 3.0  # déprécié — ignoré (cf. docstring)
    track: str = "agence-ia"  # track live ; OPT retiré (legacy) — isole le backlog research par track


class RunWf3Item(BaseModel):
    company_id: str
    name: str | None = None
    status: str
    duration_ms: int | None = None
    error_text: str | None = None


class RunWf3Out(BaseModel):
    processed: int
    succeeded: int
    failed: int
    skipped: int
    items: list[RunWf3Item]


@app.post("/wf3/run", dependencies=[Depends(_require_auth)], response_model=RunWf3Out)
async def run_wf3(payload: RunWf3In) -> RunWf3Out:
    import asyncio

    backlog = await db_tools.list_companies_to_research(
        limit=payload.limit, require_website=payload.require_website, track=payload.track
    )

    sem = asyncio.Semaphore(max(1, payload.concurrency))

    async def _research_one(
        co: dict[str, Any],
    ) -> tuple[dict[str, Any], "ResearchCompanyByIdOut | None", str | None]:
        async with sem:
            try:
                res = await research_company_by_id(
                    ResearchCompanyByIdIn(
                        company_id=co["id"], model=payload.model, track=payload.track
                    )
                )
                return co, res, None
            except Exception as e:  # noqa: BLE001
                return co, None, repr(e)

    # Recherche les companies en parallèle (borné par `concurrency`) — garde l'appel
    # HTTP n8n unique ET court, vs ~270-300s en série qui déclenchait un 502 edge Railway.
    results = await asyncio.gather(*(_research_one(co) for co in backlog))

    items: list[RunWf3Item] = []
    succeeded = failed = skipped = 0
    for co, res, err in results:
        if res is None:
            failed += 1
            items.append(RunWf3Item(
                company_id=co["id"], name=co.get("name"),
                status="error", error_text=err,
            ))
            continue
        if res.status == "ok":
            succeeded += 1
        elif res.status.startswith("skipped"):
            skipped += 1
        else:
            failed += 1
        items.append(RunWf3Item(
            company_id=co["id"], name=co.get("name"),
            status=res.status, duration_ms=res.duration_ms,
            error_text=res.error_text,
        ))

    return RunWf3Out(
        processed=len(items), succeeded=succeeded, failed=failed,
        skipped=skipped, items=items,
    )


# ---------------- REACTI discovery (WF-reacti-2 — PME sans site web) ----------------

# Une entreprise dont la réponse LLM tronque systématiquement ne doit ni boucler
# ni être condamnée en silence : au-delà de ce nombre de passes vides, on tranche
# ET on écrit que la cause est technique, pour pouvoir la rejuger plus tard.
DISCOVERY_TRUNCATION_CAP = 3


def _patch_no_web_presence(*, motif: str) -> dict[str, str]:
    """Le statut ET sa raison. La raison était calculée puis jetée — d'où 88
    lignes 'no_web_presence' sans un mot d'explication en base."""
    return {"status": "no_web_presence", "disqualified_reason": f"discovery:{motif}"}


class ReactiDiscoverIn(BaseModel):
    company_id: str
    model: str = "claude-sonnet-4-6"


class ReactiDiscoverOut(BaseModel):
    """Issue d'UNE passe de découverte.

    `status` — cinq valeurs, mutuellement exclusives :
      - 'found'            : au moins un courriel public retenu (voir contacts_inserted).
      - 'a_reessayer'      : la réponse du modèle a tronqué (stop_reason='max_tokens').
                             RIEN n'a été écrit sur la company, elle reste 'sourced' et
                             repassera. ⚠️ Ce n'est PAS un succès : compter une passe
                             tronquée dans 'found' ferait dire au rapport de lot qu'on a
                             trouvé dix entreprises alors qu'on n'a rien trouvé du tout.
      - 'no_web_presence'  : verdict terminal, avec sa raison en base — vraie absence,
                             ou troncature répétée jusqu'au DISCOVERY_TRUNCATION_CAP.
      - 'error'            : l'appel LLM a levé.
      - 'company_not_found': l'id ne correspond à aucune ligne.
    """
    company_id: str
    status: str
    contacts_inserted: int = 0
    contacts_duplicate: int = 0
    website_backfilled: bool = False
    error_text: str | None = None


@app.post(
    "/reacti/discover-contact",
    dependencies=[Depends(_require_auth)],
    response_model=ReactiDiscoverOut,
)
async def reacti_discover_contact(payload: ReactiDiscoverIn) -> ReactiDiscoverOut:
    """Découverte de contact pour UNE PME REACTI sans site web.

    Web search Anthropic → courriel public ? Si oui → insert_contact (base légale
    honnête) + backfill website. Sinon → status='no_web_presence'. La company
    reste 'sourced' en cas de succès (research la promeut à 'enriched' ensuite).
    """
    import asyncio

    matches = await db_tools.db.select(
        "companies",
        params={
            "select": "id,name,city,address,raw_payload,website,track",
            "id": f"eq.{payload.company_id}",
            "limit": "1",
        },
    )
    if not matches:
        return ReactiDiscoverOut(company_id=payload.company_id, status="company_not_found")
    co = matches[0]

    try:
        llm = await asyncio.to_thread(
            reacti_discover_tools._call_discovery_llm,
            name=co.get("name") or "",
            city=co.get("city"),
            address=co.get("address"),
            phone=(co.get("raw_payload") or {}).get("nationalPhoneNumber"),
            model=payload.model,
        )
    except Exception as e:  # noqa: BLE001
        try:
            await db_tools.record_agent_run(
                db_tools.AgentRunIn(
                    agent="reacti_discover", model=payload.model,
                    company_id=payload.company_id, error_text=repr(e),
                )
            )
        except Exception:  # noqa: BLE001
            pass
        return ReactiDiscoverOut(
            company_id=payload.company_id, status="error", error_text=repr(e),
        )

    actions = reacti_discover_tools.decide_discovery_actions(
        llm.discovery, tronquee=llm.tronquee,
    )

    # Plafond de troncatures. On COMPTE ici, AVANT l'audit : la passe courante n'est
    # pas encore en base, et c'est précisément ce que le `+ 1` représente. Compter
    # après l'audit la compterait deux fois et ferait tomber le plafond à la
    # deuxième troncature. On ne TRANCHE qu'après l'audit (plus bas), pour que la
    # passe qui déclenche le plafond laisse quand même sa trace dans agent_runs.
    plafond_atteint = False
    if actions.motif == "reponse_tronquee":
        vides = await sb.select(
            "agent_runs",
            params={
                "select": "id",
                "agent": "eq.reacti_discover",
                "company_id": f"eq.{payload.company_id}",
                "output_payload->>tronquee": "eq.true",
            },
        )
        plafond_atteint = len(vides) + 1 >= DISCOVERY_TRUNCATION_CAP

    # Audit (non bloquant)
    try:
        await db_tools.record_agent_run(
            db_tools.AgentRunIn(
                agent="reacti_discover", model=llm.model,
                company_id=payload.company_id,
                input_payload={"name": co.get("name"), "city": co.get("city")},
                # `tronquee` dans le payload : c'est LUI que compte le plafond
                # ci-dessus. Sans cette clé le comptage ne trouve jamais rien.
                output_payload={**llm.discovery, "tronquee": llm.tronquee},
                input_tokens=llm.usage.input_tokens,
                output_tokens=llm.usage.output_tokens,
                cache_read_tokens=llm.usage.cache_read_input_tokens,
                cache_creation_tokens=llm.usage.cache_creation_input_tokens,
            )
        )
    except Exception:  # noqa: BLE001
        pass

    if plafond_atteint:
        # La cause reste technique et le verdict le dit : 'reponse_tronquee_x3' se
        # retrouve en base et peut être rejugé, au lieu d'un no_web_presence muet.
        await sb.update(
            "companies",
            _patch_no_web_presence(motif=f"reponse_tronquee_x{DISCOVERY_TRUNCATION_CAP}"),
            filters={"id": f"eq.{payload.company_id}"},
        )
        return ReactiDiscoverOut(
            company_id=payload.company_id, status="no_web_presence",
        )

    if actions.motif == "reponse_tronquee":
        # Sous le plafond : on n'écrit rien, la company repassera. Mais on le DIT,
        # au lieu de tomber dans le 'found' de la fin de fonction — un lot où le
        # modèle a tronqué dix fois rapporterait « dix trouvées » et apprendrait à
        # l'opérateur à faire confiance à un chiffre faux.
        return ReactiDiscoverOut(
            company_id=payload.company_id, status="a_reessayer",
        )

    if actions.new_status == "no_web_presence":
        await db_tools.db.update(
            "companies",
            _patch_no_web_presence(motif=actions.motif or "inconnu"),
            filters={"id": f"eq.{payload.company_id}"},
        )
        return ReactiDiscoverOut(
            company_id=payload.company_id, status="no_web_presence",
        )

    inserted = duplicate = 0
    for c in actions.contacts:
        res = await db_tools.insert_contact(
            db_tools.ContactIn(
                company_id=payload.company_id,
                email=c["email"],
                email_verified=False,
                email_verification_source=c["email_verification_source"],
                source="reacti_discovery",
                raw_payload={"kind": c["kind"], "source_url": c.get("source_url")},
            )
        )
        if res.status == "inserted":
            inserted += 1
        elif res.status == "duplicate":
            duplicate += 1

    website_backfilled = False
    if actions.website_backfill and not co.get("website"):
        await db_tools.db.update(
            "companies",
            {"website": actions.website_backfill},
            filters={"id": f"eq.{payload.company_id}"},
        )
        website_backfilled = True

    return ReactiDiscoverOut(
        company_id=payload.company_id,
        status="found",
        contacts_inserted=inserted,
        contacts_duplicate=duplicate,
        website_backfilled=website_backfilled,
    )


class RunReactiWf2In(BaseModel):
    limit: int = 10
    model: str = "claude-sonnet-4-6"
    concurrency: int = 3


class RunReactiWf2Item(BaseModel):
    company_id: str
    name: str | None = None
    status: str
    error_text: str | None = None


class RunReactiWf2Out(BaseModel):
    processed: int
    found: int
    no_web_presence: int
    # Passes tronquées restées sous le plafond : rien n'a été écrit, ces companies
    # repasseront. Compteur séparé pour que 'found' ne gonfle plus des passes qui
    # n'ont rien trouvé — le rapport de lot doit pouvoir être lu au pied de la lettre.
    a_reessayer: int = 0
    failed: int
    items: list[RunReactiWf2Item]


@app.post(
    "/reacti/wf2/run",
    dependencies=[Depends(_require_auth)],
    response_model=RunReactiWf2Out,
)
async def run_reacti_wf2(payload: RunReactiWf2In) -> RunReactiWf2Out:
    """Pass complet WF-reacti-2 : N companies REACTI à découvrir, en parallèle borné."""
    import asyncio

    backlog = await db_tools.list_companies_to_discover(limit=payload.limit)
    sem = asyncio.Semaphore(max(1, payload.concurrency))

    async def _one(co: dict[str, Any]):
        async with sem:
            try:
                res = await reacti_discover_contact(
                    ReactiDiscoverIn(company_id=co["id"], model=payload.model)
                )
                return co, res, None
            except Exception as e:  # noqa: BLE001
                return co, None, repr(e)

    results = await asyncio.gather(*(_one(co) for co in backlog))

    items: list[RunReactiWf2Item] = []
    found = no_web = a_reessayer = failed = 0
    for co, res, err in results:
        if res is None:
            failed += 1
            items.append(RunReactiWf2Item(
                company_id=co["id"], name=co.get("name"), status="error", error_text=err,
            ))
            continue
        if res.status == "found":
            found += 1
        elif res.status == "no_web_presence":
            no_web += 1
        elif res.status == "a_reessayer":
            a_reessayer += 1
        else:
            failed += 1
        items.append(RunReactiWf2Item(
            company_id=co["id"], name=co.get("name"),
            status=res.status, error_text=res.error_text,
        ))

    return RunReactiWf2Out(
        processed=len(backlog), found=found, no_web_presence=no_web,
        a_reessayer=a_reessayer, failed=failed, items=items,
    )


# ---------------- Personalize (Phase 2 — WF-4) ----------------

import json as _json  # local alias to avoid clashing with model fields

_REFERENCES_PATH = os.environ.get(
    "CLIENT_REFERENCES_PATH",
    str(__file__).replace("http_api.py", "../client_references.json"),
)


def _load_client_references() -> list[dict[str, Any]]:
    """Charge la liste de social_proof. Fichier optionnel — `[]` si absent."""
    from pathlib import Path
    p = Path(_REFERENCES_PATH)
    if not p.exists():
        return []
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
        return data.get("references", [])
    except Exception:  # noqa: BLE001
        return []


def _contact_for_prompt(contact_row: dict[str, Any]) -> dict[str, Any]:
    """Format minimal du contact pour le prompt — uniquement champs utiles.

    `email_source` permet au prompt d'adapter le ton :
    - 'website_scrape' + kind='nominative' : email perso du proprio, salutation prudente.
    - 'website_scrape' + kind='generic' : info@/contact@, ne PAS adresser au nom.
    - 'apollo' : valeur héritée (contacts importés avant le retrait d'Apollo) —
      traiter comme un contact vérifié nominatif.
    """
    raw = contact_row.get("raw_payload") or {}
    return {
        "first_name": contact_row.get("first_name"),
        "last_name": contact_row.get("last_name"),
        "title": contact_row.get("title"),
        "email": contact_row.get("email"),
        "email_source": contact_row.get("email_verification_source"),
        "email_kind": raw.get("kind") if isinstance(raw, dict) else None,
    }


@app.get("/contacts/to-personalize", dependencies=[Depends(_require_auth)])
async def contacts_to_personalize(
    limit: int = 20, max_per_company: int = 1, track: str = "agence-ia",
) -> list[dict[str, Any]]:
    """Backlog WF-4 : contacts avec email + company.research_json + sans draft outbound.

    `max_per_company=1` (défaut) : un seul contact par entreprise, prioritisé.
    """
    return await db_tools.list_contacts_to_personalize(
        limit=limit, max_per_company=max_per_company, track=track,
    )


class PersonalizeContactIn(BaseModel):
    contact_id: str
    template_choice: str = "A"  # "A" ou "B"
    model: str = "claude-sonnet-4-6"
    persist: bool = True  # False → dry-run, retourne juste l'email sans insérer dans messages
    available_slots: list[dict[str, Any]] | None = None  # override (sinon fetch Cal.com)


class PersonalizeContactOut(BaseModel):
    contact_id: str
    status: str  # "ok" | "error" | "skipped_no_email" | "skipped_no_research"
    message_id: str | None = None
    email: dict[str, Any] | None = None
    duration_ms: int | None = None
    template_used: str | None = None
    error_text: str | None = None


def _tombe_sur_le_repli_du_lexique(company_row: dict[str, Any]) -> bool:
    """Cette entreprise recevra-t-elle le lexique GENERIQUE ?

    Vrai quand aucun metier n'est reconnu dans `services_offered`. Le lead part
    quand meme -- c'est le defaut inverse, on n'ecarte jamais sur l'optimisation
    -- mais avec un ouvreur sans metier nomme, donc beaucoup moins ancre.

    ⚠️ Si ce compteur monte, ce n'est pas la copie qui est en cause : c'est WF-3
    qui n'a pas assez creuse. La spec le dit depuis le debut ; il manquait juste
    quelqu'un pour compter.
    """
    from .lib.metiers import resoudre_metiers

    research = company_row.get("research_json") or {}
    return resoudre_metiers(research.get("services_offered"), date.today()).dominant is None


def _bras_ab(template_choice: str, rang: int) -> str:
    """Le bras du test A/B pour le n-ième contact du lot.

    `"AB"` demande l'alternance ; `"A"` ou `"B"` force un bras (mode manuel,
    rejeu d'un lead précis).

    🔴 L'alternance se fait par RANG DANS LE LOT, jamais par une propriété du
    contact. La spec du 2026-08-26 le dit : sans ça, « A part sur les contacts
    les plus anciens et B sur les plus récents, et le test mesure l'ordre de la
    file au lieu du courriel ». La file est triée `created_at.asc`, donc toute
    répartition dérivée du contact serait corrélée à son ancienneté.

    ⚠️ Le rang est celui du lot, pas un compteur global : deux lots consécutifs
    recommencent tous les deux par A. Sur des lots de 10 à 20 c'est sans effet
    sur l'équilibre ; ça le deviendrait sur des lots de 1, cas qui n'existe
    qu'en rejeu manuel — où le bras se force de toute façon.
    """
    if template_choice.strip().upper() != "AB":
        return template_choice
    return "A" if rang % 2 == 0 else "B"


async def _personalize_one(
    contact_row: dict[str, Any],
    company_row: dict[str, Any],
    *,
    template_choice: str,
    model: str,
    persist: bool,
    available_slots: list[dict[str, Any]],
    social_proof: list[dict[str, Any]],
) -> PersonalizeContactOut:
    """Coeur partagé entre /personalize/contact et /wf4/run."""
    contact_id = contact_row["id"]
    if not contact_row.get("email"):
        return PersonalizeContactOut(contact_id=contact_id, status="skipped_no_email")
    research = company_row.get("research_json")
    if not research:
        return PersonalizeContactOut(contact_id=contact_id, status="skipped_no_research")

    try:
        out = await personalize_tools.personalize(
            personalize_tools.PersonalizeIn(
                research_json=research,
                # Filet mort en pratique (`companies.track` est NOT NULL
                # default 'agence-ia', migrations 0003/0020, et les deux
                # appelants sélectionnent la colonne). S'il tirait quand
                # même, `OPT` ferait rédiger un corps VOUVOYÉ, que WF-5
                # relirait ensuite avec le track réel de la base — donc
                # `agence-ia`, donc registre `tu` attendu, donc refusé.
                track=(company_row.get("track") or "agence-ia"),
                company={
                    "name": company_row.get("name"),
                    "website": company_row.get("website"),
                    "city": company_row.get("city"),
                    "icp_segment": company_row.get("icp_segment"),
                    "industry": company_row.get("industry"),
                    # L'ancre factuelle du bloc 2 (AC1b). Absentes ici, le
                    # redacteur n'a aucun chiffre a citer et le bloc saute
                    # 255 fois sur 255 -- ou pire, il en invente un.
                    "google_rating": company_row.get("google_rating"),
                    "google_reviews_count": company_row.get("google_reviews_count"),
                    "google_place_id": company_row.get("google_place_id"),
                },
                contact=_contact_for_prompt(contact_row),
                social_proof=social_proof,
                template_choice=template_choice,
                available_slots=available_slots,
                model=model,
            )
        )
    except Exception as e:  # noqa: BLE001
        # Audit l'échec sans bloquer le batch
        try:
            await db_tools.record_agent_run(
                db_tools.AgentRunIn(
                    agent="personalization",
                    model=model,
                    contact_id=contact_id,
                    company_id=company_row["id"],
                    error_text=repr(e),
                )
            )
        except Exception:  # noqa: BLE001
            pass
        return PersonalizeContactOut(contact_id=contact_id, status="error", error_text=repr(e))

    email = out.email or {}
    subject = email.get("subject") or ""
    body = email.get("body_text") or ""
    warnings = email.get("warnings") or []

    # Audit succès dans agent_runs (avant insert message pour avoir l'id à référencer)
    agent_run_id: str | None = None
    try:
        ar = await db_tools.record_agent_run(
            db_tools.AgentRunIn(
                agent="personalization",
                model=out.model,
                contact_id=contact_id,
                company_id=company_row["id"],
                input_payload={
                    # 🔴 La variante RÉSOLUE, pas le paramètre demandé.
                    #
                    # La route de conformité lit CE champ en premier pour
                    # choisir les bornes de longueur. Avec « AB » ici,
                    # `check_length` cherche les bornes de ('agence-ia', 'AB'),
                    # ne les trouve pas, et refusait un corps de 217 mots.
                    # Le repli par piste (compliance_checks) est la deuxième
                    # ceinture ; celle-ci est la première.
                    "template_choice": (
                        out.template_used
                        if out.template_used in ("A", "B")
                        else template_choice
                    ),
                    # Le paramètre d'entrée reste tracé, séparément, pour qu'on
                    # sache si le choix venait de nous ou du rédacteur.
                    "template_demande": template_choice,
                    "slots_count": sum(len(s.get("times", [])) for s in available_slots),
                    "social_proof_count": len(social_proof),
                },
                output_payload=email,
                duration_ms=out.duration_ms,
                input_tokens=out.usage.input_tokens,
                output_tokens=out.usage.output_tokens,
                cache_read_tokens=out.usage.cache_read_input_tokens,
                cache_creation_tokens=out.usage.cache_creation_input_tokens,
            )
        )
        agent_run_id = ar.get("agent_run_id")
    except Exception:  # noqa: BLE001
        pass

    message_id: str | None = None
    if persist and subject and body:
        # Pivot tri (2026-08-20) : le courriel 1 ne porte AUCUN lien démo. La
        # démo se produit sur réponse positive (session artisanale) et William
        # répond lui-même avec le lien — plus de frappe au draft ni au send.
        notes = "; ".join(warnings) if warnings else None
        try:
            ins = await db_tools.insert_message_draft(
                db_tools.MessageDraftIn(
                    contact_id=contact_id,
                    subject=subject,
                    body_text=body,
                    to_email=contact_row["email"],
                    generated_by_agent_run=agent_run_id,
                    compliance_check_passed=None,  # WF-5 le valide
                    compliance_notes=notes,
                    # La variante RÉELLEMENT écrite (migration 0047). Jamais le
                    # paramètre : avec `template_choice='AB'`, la colonne
                    # porterait « AB » sur 100 % des lignes et il n'y aurait
                    # aucun test A/B — juste deux textes et aucune trace de qui
                    # a reçu quoi. La contrainte de la colonne refuse 'AB'.
                    template_choice=(
                        out.template_used
                        if out.template_used in ("A", "B")
                        else None
                    ),
                    # Les deux relances (migration 0046). Absentes sur OPT.
                    followups=(
                        {
                            "relance_1": email.get("relance_1") or "",
                            "relance_2": email.get("relance_2") or "",
                        }
                        if email.get("relance_1") or email.get("relance_2")
                        else None
                    ),
                )
            )
            message_id = ins.get("message_id")
        except Exception as e:  # noqa: BLE001
            return PersonalizeContactOut(
                contact_id=contact_id, status="error",
                error_text=f"insert_message_draft: {e!r}",
                email=email, template_used=out.template_used,
            )

    return PersonalizeContactOut(
        contact_id=contact_id, status="ok",
        message_id=message_id, email=email,
        duration_ms=out.duration_ms, template_used=out.template_used,
    )


@app.post(
    "/personalize/contact",
    dependencies=[Depends(_require_auth)],
    response_model=PersonalizeContactOut,
)
async def personalize_contact(payload: PersonalizeContactIn) -> PersonalizeContactOut:
    """Personnalisation d'UN contact. Le mode `persist=False` est utile pour
    QA / preview sans polluer la table messages.
    """
    from . import supabase_client as db

    contacts = await db.select(
        "contacts",
        params={
            "select": "id,first_name,last_name,email,title,company_id,email_verification_source,raw_payload",
            "id": f"eq.{payload.contact_id}",
            "limit": "1",
        },
    )
    if not contacts:
        return PersonalizeContactOut(
            contact_id=payload.contact_id, status="error", error_text="contact_not_found",
        )
    contact = contacts[0]

    companies = await db.select(
        "companies",
        params={
            # ⚠️ Le CINQUIÈME point du câblage des avis, oublié par la tâche 7
            # qui n'en nommait que quatre. Cette route appelle le MÊME
            # `_personalize_one` que /wf4/run, et c'est elle que le plan du
            # pivot tri désigne comme la façon de créer un draft de test au
            # go-live. Sans ces colonnes, `bloc_faits_verifies` annonce
            # « aucune note et aucun avis en base » — faux pour 785 des 816
            # entreprises — le draft sort en repli, et il PASSE la conformité
            # (aucun chiffre = rien à vérifier). La dégradation est invisible,
            # et c'est le test de fumée lui-même qui ment.
            "select": (
                "id,name,website,city,icp_segment,industry,research_json,track,"
                "google_rating,google_reviews_count,google_place_id"
            ),
            "id": f"eq.{contact['company_id']}",
            "limit": "1",
        },
    )
    if not companies:
        return PersonalizeContactOut(
            contact_id=payload.contact_id, status="error", error_text="company_not_found",
        )
    company = companies[0]

    # Fetch Cal.com une fois (ou utilise l'override). Si échec : on tombe sur slots=[]
    # et le prompt fallback sur un CTA générique.
    slots: list[dict[str, Any]] = payload.available_slots or []
    if not payload.available_slots:
        import asyncio
        from .lib.calcom import CalcomError, get_available_slots
        try:
            # get_available_slots est synchrone (httpx.get) — wrap via to_thread
            # pour ne pas bloquer l'event loop FastAPI pendant l'appel Cal.com
            # (jusqu'à 10s timeout).
            slots = await asyncio.to_thread(get_available_slots, days_ahead=7)
        except CalcomError:
            slots = []

    return await _personalize_one(
        contact, company,
        template_choice=payload.template_choice,
        model=payload.model,
        persist=payload.persist,
        available_slots=slots,
        social_proof=_load_client_references(),
    )


class RunWf4In(BaseModel):
    """Pass complet WF-4 : prend N contacts à personnaliser, génère drafts.

    Cron-friendly. La sélection des contacts évite ceux qui ont déjà un draft outbound.
    `max_per_company=1` (défaut) garantit qu'on n'envoie pas plusieurs emails à
    la même entreprise dans un même batch (brûlerait la company).
    """
    limit: int = 10
    template_choice: str = "A"
    model: str = "claude-sonnet-4-6"
    persist: bool = True
    max_per_company: int = 1
    # Isole le backlog personalize par piste. Défaut = la piste VIVANTE :
    # `OPT` est gelée depuis le pivot du 2026-06-07, et l'alerte de famine
    # plus bas compte ses restants SUR CE TRACK. Un /wf4/run nu compterait
    # donc sur une file vide et conclurait « fin de liste » — l'alerte se
    # tairait exactement quand elle devrait crier.
    track: str = "agence-ia"


class RunWf4Item(BaseModel):
    contact_id: str
    company_name: str | None = None
    status: str
    message_id: str | None = None
    template_used: str | None = None
    duration_ms: int | None = None
    error_text: str | None = None


class RunWf4Out(BaseModel):
    processed: int
    drafts_created: int
    skipped: int
    failed: int
    slots_available: int  # nb total créneaux Cal.com fetched
    # True/False = l'alerte de famine est partie / s'est perdue. None = il
    # n'y avait rien à annoncer. Lire le retour évite l'alerte qui se croit
    # partie, comme RunWf5Out.alerte_envoyee.
    alerte_famine_envoyee: bool | None = None
    # Combien de leads du lot sont tombes sur le LEXIQUE DE REPLI, faute de
    # metier reconnu. Promis par la tache 5 et par la spec, jamais pose jusqu'au
    # conseil final : sans lui, un lot peut partir massivement en formulations
    # generiques -- exactement ce que la section 3 existe pour eviter -- et
    # /wf4/run rend `drafts_created=10` sans un mot.
    #
    # ⚠️ Ce n'est PAS un compteur de la copie : s'il monte, c'est WF-3 qui n'a
    # pas assez creuse les services de l'entreprise.
    lexique_de_repli: int = 0
    items: list[RunWf4Item]


def _doit_alerter_famine(*, processed: int, envoyables_restants: int) -> bool:
    """Un `/wf4/run` qui ne rédige rien alors qu'il reste des leads est une
    panne, pas une journée calme.

    Ce projet a déjà payé cinq semaines de silence sur un défaut de cette
    famille : la clé Google Places désactivée pour cause de facturation, tout
    le WF-1 en échec, et rien nulle part parce que tout est fail-soft. Un
    pipeline fail-soft DOIT crier quand il ne produit plus rien.

    Le deuxième terme est ce qui distingue les deux « zéro » : zéro sur une
    file vide est une fin de liste (rien à dire), zéro sur une file pleine est
    la famine.
    """
    return processed == 0 and envoyables_restants > 0


async def _compter_envoyables_restants(track: str) -> tuple[int, bool]:
    """Combien de contacts restent à approcher sur ce track. Rend (compte, lu).

    Deux `count()` exacts côté serveur — jamais `len(select(...))` : PostgREST
    plafonne à 1000 lignes sans rien signaler et les agrégats côté serveur sont
    désactivés ici (PGRST123). Un compte tronqué à 1000 dirait « il en reste »
    pour toujours.

      file    = contacts joignables et pas encore écartés (courriel présent,
                statut new/ready) ;
      servis  = messages sortants ENCORE VIVANTS (`status != 'failed'`), la
                même définition qu'utilise l'éligibilité de WF-4 pour décider
                qu'un contact est déjà pris.

    ⚠️ C'est une ESTIMATION, et le message d'alerte l'écrit « ~ ». Elle repose
    sur l'invariant que le pipeline maintient — au plus un message vivant par
    contact (`already_drafted` dans `list_contacts_to_personalize`) — et elle
    penche du côté prudent : les messages adressés à des contacts depuis
    désabonnés sont soustraits d'une file qui ne les contient plus, donc le
    reste est plutôt SOUS-estimé. Un chiffre approché suffit à trancher la
    seule question posée ici : panne ou fin de liste ?

    `lu=False` quand la lecture tombe. L'appelant traite ce cas comme suspect
    plutôt que comme un zéro rassurant.
    """
    from . import supabase_client as sb

    try:
        file_active = await sb.count(
            "contacts",
            params={
                "email": "not.is.null",
                "status": "in.(new,ready)",
                "track": f"eq.{track}",
            },
        )
        servis = await sb.count(
            "messages",
            params={
                "direction": "eq.outbound",
                "status": "not.in.(failed)",
                "track": f"eq.{track}",
            },
        )
    except Exception as e:  # noqa: BLE001
        logging.getLogger("wf4").error("comptage des restants échoué — %r", e)
        return 0, False
    return max(0, file_active - servis), True


async def _alerter_famine_wf4(
    *, track: str, restants: int, compte_lu: bool, limite: int
) -> bool:
    """Crie sur #alertes qu'un lot WF-4 est reparti les mains vides.

    Le message NOMME le nombre de leads restants : sans lui, « 0 draft » ne
    distingue pas une panne d'une fin de liste, et une alerte qu'on ne peut pas
    interpréter finit ignorée.

    Rend le retour de Slack, comme `_alerter_wf5` : une alerte perdue qui se
    croit partie est le pire des deux mondes.
    """
    from .lib import slack as slack_lib

    if compte_lu:
        corps = [
            f"🚨 WF-4 famine — 0 draft rédigé alors qu'il reste ~{restants} "
            f"contact(s) à approcher (track {track}).",
            "Un lot vide sur une file qui ne l'est PAS est une panne, pas une "
            "fin de liste.",
            f"Piste connue : la sélection sur-lit les {limite * 5} plus vieux "
            "contacts, et si tous ont déjà un draft le lot revient vide alors "
            "que la file est pleine (famine WF-4, correctif à part).",
        ]
    else:
        # Le nombre de restants est illisible : on crie quand même. Se taire
        # ici, ce serait faire dépendre l'alerte de la santé de la lecture qui
        # sert à la justifier.
        corps = [
            f"🚨 WF-4 famine — 0 draft rédigé (track {track}) et le nombre de "
            "contacts restants est ILLISIBLE (lecture en échec, voir les logs).",
            "Impossible de dire si c'est une panne ou une fin de liste : "
            "traiter comme une panne.",
        ]

    envoyee = await slack_lib.notify(
        text="\n".join(corps), context="wf4_famine", category="alerts",
    )
    if not envoyee:
        logging.getLogger("wf4").error(
            "alerte famine #alertes NON partie — track=%s restants=%s lu=%s",
            track, restants, compte_lu,
        )
    return envoyee


@app.post("/wf4/run", dependencies=[Depends(_require_auth)], response_model=RunWf4Out)
async def run_wf4(payload: RunWf4In) -> RunWf4Out:
    backlog = await db_tools.list_contacts_to_personalize(
        limit=payload.limit, max_per_company=payload.max_per_company, track=payload.track,
    )

    # 🔴 Cal.com ne sert PLUS la piste `agence-ia`, et le retirer du prompt ne
    # suffisait pas : c'est l'APPEL qu'il faut couper.
    #
    # Le courriel de tri ne propose aucun rendez-vous (règle nº11 : le RDV se
    # propose dans la réponse au oui, jamais dans le froid). Tant que la liste
    # arrivait quand même, `check_cta_slots_real` restait armé en `block` sur
    # une piste où aucun créneau ne doit exister : un ouvreur qui nommerait un
    # jour, une date et une heure serait soit refusé irréversiblement, soit
    # VALIDÉ comme un créneau légitime si l'heure coïncidait.
    # Bénéfice au passage : un appel réseau et un mode de panne en moins par
    # lot, sur un service dont on n'a plus besoin ici.
    slots: list[dict[str, Any]] = []
    if payload.track != "agence-ia":
        # Fetch Cal.com une seule fois pour tout le batch — évite N appels API et
        # garantit que tous les emails du batch piochent dans la même liste.
        import asyncio
        from .lib.calcom import CalcomError, get_available_slots
        try:
            slots = await asyncio.to_thread(get_available_slots, days_ahead=7)
        except CalcomError:
            slots = []
    total_slots = sum(len(s.get("times", [])) for s in slots)

    social_proof = _load_client_references()

    items: list[RunWf4Item] = []
    drafts = skipped = failed = repli_lexique = 0

    for rang, entry in enumerate(backlog):
        contact = entry["contact"]
        company = entry["company"]
        # Compte AVANT la generation : meme si le draft echoue ensuite, le fait
        # que WF-3 n'ait pas trouve de metier reste vrai et doit se voir.
        if _tombe_sur_le_repli_du_lexique(company):
            repli_lexique += 1
        try:
            res = await _personalize_one(
                contact, company,
                template_choice=_bras_ab(payload.template_choice, rang),
                model=payload.model,
                persist=payload.persist,
                available_slots=slots,
                social_proof=social_proof,
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            items.append(RunWf4Item(
                contact_id=contact["id"], company_name=company.get("name"),
                status="error", error_text=repr(e),
            ))
            continue

        if res.status == "ok":
            drafts += 1
        elif res.status.startswith("skipped"):
            skipped += 1
        else:
            failed += 1
        items.append(RunWf4Item(
            contact_id=contact["id"], company_name=company.get("name"),
            status=res.status, message_id=res.message_id,
            template_used=res.template_used, duration_ms=res.duration_ms,
            error_text=res.error_text,
        ))

    # ------------------------------------------------------- Alerte de famine
    # Zéro draft rédigé n'est pas forcément une bonne nouvelle : c'est soit la
    # fin de la liste, soit une panne. On ne paie les deux `count()` que dans ce
    # cas précis — un lot qui tourne n'a rien à demander de plus à la base.
    alerte_famine_envoyee: bool | None = None
    if len(items) == 0:
        restants, compte_lu = await _compter_envoyables_restants(payload.track)
        # `not compte_lu` d'ABORD : si le compte est illisible, on crie quand
        # même. Sans ça, une panne de lecture ferait rendre 0, donc « fin de
        # liste », donc silence — l'alerte se saborderait elle-même exactement
        # au moment où quelque chose ne va pas.
        if not compte_lu or _doit_alerter_famine(
            processed=len(items), envoyables_restants=restants
        ):
            alerte_famine_envoyee = await _alerter_famine_wf4(
                track=payload.track, restants=restants,
                compte_lu=compte_lu, limite=payload.limit,
            )

    return RunWf4Out(
        processed=len(items), drafts_created=drafts,
        skipped=skipped, failed=failed,
        slots_available=total_slots, items=items,
        alerte_famine_envoyee=alerte_famine_envoyee,
        lexique_de_repli=repli_lexique,
    )


# ---------------- Compliance (Phase 2 — WF-5) ----------------

def _patch_verdict_conformite(verdict: str, tentatives_avant: int | None) -> dict[str, Any]:
    """Le patch à écrire sur `messages` après une passe de conformité.

    `non_juge` est le seul verdict qui NE touche PAS `compliance_check_passed` :
    en la laissant NULL, `send.py` (qui exige `is True`) ne part pas, ET la
    requête du lot (qui ne cherche que `is.null`) reprend le draft le lendemain
    sans aucune requête nouvelle. Écrire `false` le figerait à vie — la garde
    anti-boucle des 3 tentatives ne serait jamais atteinte et une panne
    passagère du juge deviendrait un refus définitif.

    `orphelin` prend la route inverse de `non_juge` — `passed = false`, donc le
    message QUITTE le lot. C'est voulu : il n'a pas de quoi être jugé (pas de
    contact rattaché), et ça ne se réparera pas tout seul. `non_juge` attend une
    panne passagère ; l'orphelin attend une correction en base.

    `tentatives_avant` tolère `None` : `compliance_tentatives` absent d'un
    SELECT rend None, et `None + 1` ferait avorter toute la passe.
    """
    # 🔴 `error` ne laisse AUCUNE trace, et c'est le correctif du conseil final.
    #
    # Le layer 0 de conformité (config LCAP incomplète) rend `error` PRÉCISÉMENT
    # pour ne pas marquer le brouillon — la faute est dans l'environnement, pas
    # dans le texte. Mais la route persistait ce verdict comme les autres, et
    # cette fonction écrivait `compliance_check_passed = ("error" == "approved")
    # = False`. **La garde écrite pour empêcher le gel des contacts était
    # exactement ce qui les gelait.**
    #
    # Reproduit par exécution : avec `LCAP_MENTIONS_REDUITES=true` et
    # `INSTANTLY_CAMPAIGN_FOOTER` vide — l'état exact du go-live — chaque
    # brouillon du lot recevait `passed=false`, quittait la requête de
    # `/wf5/run` (qui ne reprend que `is.null`) et gelait son contact à vie.
    # 20 par jour, 255 en deux semaines, zéro courriel, et 1153 tests verts.
    #
    # Sur `main`, aucun `error` ne sortait de l'INTÉRIEUR de `compliance_check`
    # (les seuls venaient des `except` de la route, qui retournent AVANT la
    # persistance). AC1b a introduit le premier, et personne n'avait rouvert la
    # question de la persistance.
    #
    # ⚠️ `compliance_tentatives` ne bouge pas non plus : une configuration
    # absente n'est pas une tentative de jugement. L'incrémenter ferait
    # atteindre le plafond anti-boucle en trois passes, et un problème de
    # variable d'environnement deviendrait un refus définitif.
    if verdict == "error":
        return {"compliance_verdict": verdict}

    patch: dict[str, Any] = {
        "compliance_verdict": verdict,
        "compliance_tentatives": (tentatives_avant or 0) + 1,
    }
    if verdict != "non_juge":
        patch["compliance_check_passed"] = verdict == "approved"
    return patch


def _doit_alerter_wf5(
    *, needs_revision: int, blocked: int, non_juge: int, orphelins: int = 0,
    errors: int = 0,
) -> bool:
    """`non_juge` est dans la condition, et ce n'est pas un détail.

    Sans lui, la panne la plus grave — celle qui laisse passer des courriels
    jamais relus — serait la seule à ne pas crier : elle ne produit ni
    `needs_revision` ni `blocked`.

    `orphelins` y est pour une raison encore plus dure : c'est le seul verdict
    dont le message ne repassera JAMAIS devant la conformité (il en sort avec
    `passed = false`). Hors de cette condition, l'unique occasion de le nommer
    serait manquée et l'anomalie deviendrait invisible.

    🔴 `errors` s'y ajoute le 2026-08-30, sur trouvaille du conseil final, et
    pour la même raison que les deux précédents : depuis le layer 0 de
    conformité, une CONFIGURATION LCAP incomplète rend `error` sur TOUT le lot.
    Hors de cette condition, la seule panne qui arrête l'envoi en entier serait
    aussi la seule totalement muette — le lot rendrait `processed=20,
    approved=0` sans un mot sur `#alertes`, et le résumé du soir n'aurait rien
    à dire non plus. Vérifié : le workflow n8n WF-5 ne porte aucun nœud
    d'alerte, donc le silence serait total, pas seulement côté serveur.
    """
    return (needs_revision + blocked + non_juge + orphelins + errors) > 0


def _regle_qui_a_tranche(out: compliance_tools.ComplianceCheckOut) -> str:
    """Nom court de ce qui a décidé du verdict, pour l'alerte #alertes.

    L'ordre suit la cascade de `compliance_check` : la panne du juge se teste
    en premier parce qu'au plafond des tentatives elle se déguise en
    `needs_revision`, et « length » à la place de « juge injoignable »
    enverrait chercher le défaut dans le mauvais fichier.
    """
    if out.verdict == _VERDICT_ORPHELIN:
        # « orphelin [orphelin] » n'apprendrait rien : c'est le MOTIF qui dit
        # où chercher (le contact rattaché ? la ligne du message ?).
        return out.error_text or _VERDICT_ORPHELIN
    juge = out.llm_judge or {}
    if juge.get("error"):
        return "juge_llm_injoignable"
    if out.deterministic_blockers:
        return str(out.deterministic_blockers[0].get("name") or "layer1")
    if juge.get("send_decision") in ("DO_NOT_SEND", "REVIEW_THEN_SEND"):
        return "juge_llm"
    if out.deterministic_warnings:
        return str(out.deterministic_warnings[0].get("name") or "layer1")
    return out.verdict


_MOTIFS_ORPHELIN = {
    "contact_not_found": "aucun contact rattaché à ce message",
    "message_not_found": "la ligne du message n'existe plus",
}


def _out_orphelin(message_id: str, motif: str) -> compliance_tools.ComplianceCheckOut:
    """Le verdict d'un message qui n'a pas de quoi être jugé.

    Ni `error` (la passe n'a pas planté, elle a très bien fonctionné) ni
    `needs_revision` (la copie n'est pas en cause) : c'est une anomalie de
    DONNÉES, et la nommer autrement enverrait chercher le défaut dans le
    mauvais fichier.
    """
    detail = _MOTIFS_ORPHELIN.get(motif, "le message n'a pas de quoi être jugé")
    return compliance_tools.ComplianceCheckOut(
        message_id=message_id,
        verdict=_VERDICT_ORPHELIN,
        send_decision="DO_NOT_SEND",
        reasoning=(
            f"{motif} : {detail}. Anomalie de DONNÉES, pas de copie — le "
            "re-juger cent fois ne le réparerait jamais. Sorti du lot de "
            "conformité (compliance_check_passed=false) : à réparer en base, "
            "ou à retirer en passant le message en status='failed'."
        ),
        error_text=motif,
    )


async def _persister_verdict_conformite(
    out: compliance_tools.ComplianceCheckOut,
    *,
    message_id: str,
    tentatives_avant: int | None,
) -> compliance_tools.ComplianceCheckOut:
    """Écrit le verdict sur `messages`, et RAPPORTE l'échec dans `out.error_text`.

    Le retour de l'écriture se LIT. L'ancien `except: pass` rendait un verdict
    qui se croyait persisté alors que rien n'avait bougé en base — le mode
    d'échec exact que 1bfb918 et 57edcaf ont déjà eu à refermer côté WF-7.
    Ici c'est fail-safe côté envoi (une écriture ratée laisse `passed` NULL,
    donc rien ne part), mais l'invisible reste invisible : le `message_id`
    doit finir dans le journal de la passe, que `run_wf5` recopie dans ses
    items et dans l'alerte.
    """
    from . import supabase_client as db

    echec_persist: str | None = None
    try:
        patch = _patch_verdict_conformite(out.verdict, tentatives_avant)
        patch["compliance_notes"] = compliance_tools.format_compliance_notes(out)
        lignes = await db.update(
            "messages", patch, filters={"id": f"eq.{message_id}"},
        )
        # PostgREST rend [] quand AUCUNE ligne n'a matché : pas d'exception,
        # pas d'erreur, mais rien d'écrit non plus.
        if not lignes:
            echec_persist = "aucune ligne touchée"
    except Exception as e:  # noqa: BLE001
        echec_persist = repr(e)

    if echec_persist:
        logging.getLogger("wf5").error(
            "persist_failed message_id=%s verdict=%s — %s",
            message_id, out.verdict, echec_persist,
        )
        marqueur = f"persist_failed: {echec_persist}"
        out.error_text = f"{out.error_text} · {marqueur}" if out.error_text else marqueur
    return out


class ComplianceCheckIn(BaseModel):
    """Lance les 2 layers de compliance sur un draft.

    Si `persist=True` (défaut), met à jour `messages.compliance_check_passed`
    et `messages.compliance_notes` avec le verdict. `persist=False` = dry-run
    (utile pour QA, ne touche pas la DB).
    """
    message_id: str
    skip_llm: bool = False
    model: str = "claude-sonnet-4-6"
    persist: bool = True


@app.post(
    "/compliance/check",
    dependencies=[Depends(_require_auth)],
    response_model=compliance_tools.ComplianceCheckOut,
)
async def compliance_check(payload: ComplianceCheckIn) -> compliance_tools.ComplianceCheckOut:
    """Compliance d'UN draft. Pratique pour n8n traitement individuel."""
    from . import supabase_client as db

    # 1) Fetch le message + contact + company + agent_run (pour available_slots)
    msgs = await db.select(
        "messages",
        params={
            # `compliance_tentatives` alimente la garde anti-boucle du juge :
            # sans elle dans le SELECT, elle arrive à None et la 3e tentative
            # n'arrive jamais (voir migration 0045).
            "select": (
                "id,subject,body_text,contact_id,generated_by_agent_run,"
                "compliance_check_passed,compliance_tentatives,followups"
            ),
            "id": f"eq.{payload.message_id}",
            "limit": "1",
        },
    )
    if not msgs:
        # Rien à marquer : la ligne n'existe plus, et un `update` sur zéro ligne
        # n'ajouterait qu'un faux « persist_failed » au journal. Le verdict est
        # quand même `orphelin` (et pas `error`) pour que la passe le NOMME dans
        # son alerte — c'est la seule trace possible quand il n'y a plus de
        # ligne où en laisser une.
        logging.getLogger("wf5").error(
            "orphelin message_id=%s — message_not_found", payload.message_id,
        )
        return _out_orphelin(payload.message_id, "message_not_found")
    msg = msgs[0]

    contact_id = msg.get("contact_id")
    contact_rows = await db.select(
        "contacts",
        params={
            "select": "id,company_id,first_name,last_name,title,email_verification_source",
            "id": f"eq.{contact_id}",
            "limit": "1",
        },
    ) if contact_id else []
    if not contact_rows:
        # LA BOUCLE QUE ÇA REFERME. Avant, on sortait ici avec `verdict="error"`
        # SANS RIEN ÉCRIRE : `compliance_tentatives` n'était jamais incrémenté et
        # `compliance_check_passed` restait NULL. Or la requête du lot ne cherche
        # QUE `is.null` — le message revenait donc tous les jours, indéfiniment,
        # en consommant une place du lot quotidien à chaque passe, sans jamais
        # atteindre le plafond des 3 tentatives censé l'en sortir, et sans que
        # rien nulle part ne le dise.
        #
        # On le sort tout de suite au lieu de le re-juger trois fois : un
        # orphelin est une anomalie de DONNÉES, et trois passes n'en répareront
        # aucune. Mais le sortir SANS LE DIRE le rendrait invisible, d'où la
        # trace en trois endroits qui se relisent tous : `compliance_notes` +
        # `compliance_verdict` en base, le `message_id` dans l'alerte #alertes
        # de /wf5/run, et le compteur 🧩 du résumé quotidien — le seul des trois
        # qui reparle le lendemain, et le seul filet si le ping se perd.
        logging.getLogger("wf5").error(
            "orphelin message_id=%s — contact_not_found (sorti du lot)",
            payload.message_id,
        )
        out = _out_orphelin(payload.message_id, "contact_not_found")
        if payload.persist:
            await _persister_verdict_conformite(
                out,
                message_id=payload.message_id,
                tentatives_avant=msg.get("compliance_tentatives"),
            )
        return out
    company_id = contact_rows[0].get("company_id")
    # Destinataire vérifié = source de vérité de l'identité (prénom/titre), distincte du
    # research_json (scrape du site/page équipe). Sans ça, le juge LLM flagge à tort un
    # contact absent de la page équipe comme "inventé". Track-agnostic :
    # email_source = website_scrape (source live) | apollo (héritage). Voir compliance.md §7.
    contact = {
        "first_name": contact_rows[0].get("first_name"),
        "last_name": contact_rows[0].get("last_name"),
        "title": contact_rows[0].get("title"),
        "email_source": contact_rows[0].get("email_verification_source"),
    }

    company_rows = await db.select(
        "companies",
        params={
            # google_rating / google_reviews_count : le juge et
            # `check_avis_conformes` en ont besoin pour savoir si un chiffre
            # annonce dans le corps est vrai. Sans elles ici, ils arrivent a
            # None et TOUT corps portant une note est bloque -- fail-closed,
            # mais aucun courriel ne part.
            "select": "research_json,track,google_rating,google_reviews_count",
            "id": f"eq.{company_id}",
            "limit": "1",
        },
    ) if company_id else []
    research_json = (company_rows[0].get("research_json") if company_rows else None) or {}
    # Le track sélectionne le registre attendu par le layer 1 : `agence-ia`
    # tutoie, `OPT` vouvoie. AUCUN défaut de piste ici (contrairement à WF-4, où
    # le `or` est un défaut de GÉNÉRATION) : forcer une piste ferait attendre un
    # registre que le corps n'a peut-être pas. `None` laisse `check_registre`
    # sur son défaut historique (`vous`), ce qui est fail-closed.
    track = (company_rows[0].get("track") if company_rows else None)
    google_rating = (company_rows[0].get("google_rating") if company_rows else None)
    google_reviews_count = (
        company_rows[0].get("google_reviews_count") if company_rows else None
    )

    # 2) Charger le contexte du draft (template + slots) depuis agent_runs
    template_used: str | None = None
    available_slots: list[dict[str, Any]] = []
    social_proof: list[dict[str, Any]] = _load_client_references()
    agent_run_id = msg.get("generated_by_agent_run")
    if agent_run_id:
        runs = await db.select(
            "agent_runs",
            params={
                "select": "input_payload,output_payload",
                "id": f"eq.{agent_run_id}",
                "limit": "1",
            },
        )
        if runs:
            inp = runs[0].get("input_payload") or {}
            outp = runs[0].get("output_payload") or {}
            # 🔴 La SORTIE d'abord, l'entrée seulement en repli.
            #
            # L'ordre inverse était une mine : `input_payload.template_choice`
            # peut valoir « AB » (le paramètre qui demande au rédacteur de
            # choisir), et `check_length` retombait alors sur les bornes de la
            # piste OPT — 60 à 95 mots — pour refuser un corps de 217. Mesuré :
            #   template=A  → passed=True   217 mots (cible 180-270)
            #   template=AB → passed=False  217 mots (cible 60-95)
            # Soit 100 % des brouillons en `needs_revision`, sortis du lot pour
            # toujours, contacts gelés à vie.
            template_used = (outp.get("template_used")
                             or inp.get("template_choice"))
            if (template_used or "").upper() == "AB":
                # Ceinture de sécurité pour les agent_runs ÉCRITS AVANT ce
                # correctif : ils portent « AB » des deux côtés. On préfère un
                # gabarit approximatif de la bonne piste à un refus certain.
                template_used = "A"
            # available_slots peut être stocké dans input_payload mais on a juste un count
            # → on re-fetch Cal.com pour avoir la liste actuelle (acceptable car compliance
            # se fait peu après personalize, slots quasi identiques).

    # Même raison qu'à la génération : sur `agence-ia`, aucun créneau ne doit
    # exister dans le corps, donc en fournir une liste n'arme qu'un faux
    # positif possible. `check_cta_slots_real` passe sur liste vide.
    if not available_slots and track != "agence-ia":
        try:
            import asyncio
            from .lib.calcom import CalcomError, get_available_slots
            # Wrap sync httpx.get dans to_thread (event loop non bloqué).
            available_slots = await asyncio.to_thread(
                get_available_slots, days_ahead=14
            )
        except Exception:  # noqa: BLE001
            available_slots = []

    # 3) Run compliance
    try:
        out = await compliance_tools.compliance_check(
            message_id=payload.message_id,
            body=msg.get("body_text") or "",
            subject=msg.get("subject") or "",
            template_used=template_used,
            research_json=research_json,
            contact=contact,
            social_proof=social_proof,
            available_slots=available_slots,
            skip_llm=payload.skip_llm,
            model=payload.model,
            track=track,
            tentatives=msg.get("compliance_tentatives"),
            google_rating=google_rating,
            google_reviews_count=google_reviews_count,
            # Le TRIPLET, pas le seul corps de tri. Sans ca, deux tiers du
            # contenu partent sans avoir ete inspectes par personne.
            followups=msg.get("followups") or None,
        )
    except Exception as e:  # noqa: BLE001
        return compliance_tools.ComplianceCheckOut(
            message_id=payload.message_id, verdict="error",
            send_decision="DO_NOT_SEND",
            error_text=repr(e),
        )

    # 4) Persist verdict — même chemin d'écriture que la sortie « orphelin »
    # plus haut, pour qu'aucune des deux ne puisse dériver sans l'autre.
    if payload.persist:
        await _persister_verdict_conformite(
            out,
            message_id=payload.message_id,
            tentatives_avant=msg.get("compliance_tentatives"),
        )

    return out


class RunWf5In(BaseModel):
    """Passe de conformité sur les drafts jamais jugés.

    La requête ne sélectionne que `compliance_check_passed is.null` (parmi les
    messages `direction='outbound'` et `status='draft'`). Ça inclut les drafts
    jamais tentés ET ceux dont le juge est tombé : `non_juge` laisse
    `compliance_check_passed` à NULL exprès, donc le draft revient de lui-même
    dans le lot du lendemain, sans requête nouvelle. À la 3e tentative il
    devient un vrai refus et sort du lot.

    Le message ORPHELIN prend l'autre porte : sans contact rattaché, il n'y a
    rien à juger, et le re-juger trois fois ne réparerait pas la donnée. Il
    reçoit `compliance_check_passed = false` dès la première passe et quitte le
    lot immédiatement — sinon il y revenait tous les jours pour toujours, en
    consommant une place à chaque fois, sans jamais atteindre le plafond des
    3 tentatives.

    `concurrency` : nb de drafts jugés en parallèle (sémaphore bornée). Garde
    l'appel `/wf5/run` court — 20 en série ≈ 130s déclenchait un 502 edge Railway.
    `inter_message_sleep_seconds` : conservé pour rétro-compat, ignoré (la
    sémaphore régule désormais la charge Anthropic).
    """
    limit: int = 20
    skip_llm: bool = False
    model: str = "claude-sonnet-4-6"
    concurrency: int = 4
    inter_message_sleep_seconds: float = 2.0


class RunWf5Item(BaseModel):
    message_id: str
    subject: str | None = None
    verdict: str
    send_decision: str
    duration_ms: int | None = None
    error_text: str | None = None
    # Ce qui a tranché (nom du check déterministe, `juge_llm`,
    # `juge_llm_injoignable`). Repris tel quel dans l'alerte #alertes.
    regle: str | None = None


class RunWf5Out(BaseModel):
    processed: int
    approved: int
    needs_revision: int
    blocked: int
    # Juge LLM injoignable : le corps n'a PAS été inspecté. Comptait dans
    # `errors` avant, où il se confondait avec une panne de la passe elle-même.
    non_juge: int = 0
    # Message sans contact rattaché (ou dont la ligne a disparu) : il n'y avait
    # rien à juger. Compté à part de `errors` — la passe n'a pas planté — et de
    # `blocked` — la copie n'est pas en cause.
    orphelins: int = 0
    errors: int
    # True/False = l'alerte #alertes est partie / s'est perdue. None = il n'y
    # avait rien à annoncer. Lire le retour évite l'alerte qui se croit partie.
    alerte_envoyee: bool | None = None
    items: list[RunWf5Item]


_WF5_MAX_ITEMS_ALERTE = 5


async def _alerter_wf5(
    *,
    processed: int,
    needs_revision: int,
    blocked: int,
    non_juge: int,
    orphelins: int,
    items: list[RunWf5Item],
) -> bool:
    """Crie sur #alertes quand un lot de conformité n'est pas tout vert.

    Avant, `/wf5/run` calculait ses compteurs et les jetait : le lot entier
    pouvait mourir sans qu'une seule ligne l'annonce nulle part. Rend le retour
    de Slack — une alerte perdue qui se croit partie est le même mode d'échec
    que le verdict non persisté d'à côté.
    """
    from .lib import slack as slack_lib

    fautifs = [
        i for i in items
        if i.verdict in ("needs_revision", "blocked", "non_juge", _VERDICT_ORPHELIN)
    ]
    lignes = [
        f"• `{i.message_id}` — {i.verdict} [{i.regle or '?'}]"
        for i in fautifs[:_WF5_MAX_ITEMS_ALERTE]
    ]
    reste = len(fautifs) - len(lignes)
    if reste > 0:
        lignes.append(f"… et {reste} de plus (voir la réponse de /wf5/run).")

    corps = [
        f"🚨 WF-5 conformité — {len(fautifs)} draft(s) non envoyable(s) "
        f"sur {processed} jugé(s)",
        f"needs_revision : {needs_revision} · blocked : {blocked} · "
        f"non_juge : {non_juge} · orphelin : {orphelins}",
    ]
    if orphelins:
        corps.append(
            "🧩 orphelin = le message n'avait pas de quoi être jugé (contact "
            "rattaché absent, ou ligne disparue). Anomalie de DONNÉES, pas de "
            "copie : relire le courriel n'y changera rien. "
            "`compliance_check_passed=false` — il SORT du lot pour de bon, donc "
            "c'est la dernière fois qu'il est nommé ici. Réparer en base, ou "
            "passer le message en status='failed'."
        )
    if non_juge:
        corps.append(
            "⚠️ non_juge = le juge LLM n'a pas répondu, le corps n'a PAS été "
            "inspecté. `compliance_check_passed` reste NULL : rien ne part, et "
            "le draft revient de lui-même dans le lot de demain (3 tentatives "
            "max, puis refus)."
        )
    corps.extend(lignes)

    envoyee = await slack_lib.notify(
        text="\n".join(corps), context="wf5_lot", category="alerts",
    )
    if not envoyee:
        logging.getLogger("wf5").error(
            "alerte #alertes NON partie — needs_revision=%s blocked=%s non_juge=%s "
            "orphelins=%s ids=%s",
            needs_revision, blocked, non_juge, orphelins,
            ",".join(i.message_id for i in fautifs[:_WF5_MAX_ITEMS_ALERTE]),
        )
    return envoyee


@app.post("/wf5/run", dependencies=[Depends(_require_auth)], response_model=RunWf5Out)
async def run_wf5(payload: RunWf5In) -> RunWf5Out:
    """Batch compliance sur tous les drafts non encore checked."""
    import asyncio
    from . import supabase_client as db

    # Fetch drafts pending compliance
    drafts = await db.select(
        "messages",
        params={
            "select": "id,subject",
            "direction": "eq.outbound",
            "status": "eq.draft",
            "compliance_check_passed": "is.null",
            "order": "created_at.asc",
            "limit": str(payload.limit),
        },
    )

    sem = asyncio.Semaphore(max(1, payload.concurrency))

    async def _judge_one(
        draft: dict[str, Any],
    ) -> tuple[dict[str, Any], compliance_tools.ComplianceCheckOut | None, str | None]:
        async with sem:
            try:
                res = await compliance_check(
                    ComplianceCheckIn(
                        message_id=draft["id"],
                        skip_llm=payload.skip_llm,
                        model=payload.model,
                        persist=True,
                    )
                )
                return draft, res, None
            except Exception as e:  # noqa: BLE001
                return draft, None, repr(e)

    # Juge les drafts en parallèle (borné par `concurrency`) — garde l'appel HTTP
    # n8n unique ET court, vs ~130s en série qui déclenchait un 502 edge Railway.
    results = await asyncio.gather(*(_judge_one(d) for d in drafts))

    items: list[RunWf5Item] = []
    approved = needs_revision = blocked = non_juge = orphelins = errors = 0
    for draft, res, err in results:
        if res is None:
            errors += 1
            items.append(RunWf5Item(
                message_id=draft["id"], subject=draft.get("subject"),
                verdict="error", send_decision="DO_NOT_SEND",
                error_text=err, regle="exception_passe",
            ))
            continue
        if res.verdict == "approved":
            approved += 1
        elif res.verdict == "needs_revision":
            needs_revision += 1
        elif res.verdict == "blocked":
            blocked += 1
        elif res.verdict == "non_juge":
            # Compté à part : un corps NON INSPECTÉ n'est ni un refus ni une
            # panne de la passe. Noyé dans `errors`, il disparaissait dans le
            # bruit des exceptions réseau.
            non_juge += 1
        elif res.verdict == _VERDICT_ORPHELIN:
            # Même raison, à l'envers : la donnée manque, pas la relecture.
            # Et contrairement à `non_juge`, celui-là ne repassera jamais.
            orphelins += 1
        else:
            errors += 1
        items.append(RunWf5Item(
            message_id=draft["id"], subject=draft.get("subject"),
            verdict=res.verdict, send_decision=res.send_decision,
            duration_ms=res.duration_ms, error_text=res.error_text,
            regle=_regle_qui_a_tranche(res),
        ))

    # L'alerte vit ICI, côté serveur, et pas dans le workflow n8n : l'abonnement
    # est en pause et un WF modifié doit être ré-importé pour prendre effet —
    # une alerte qui dépend d'un ré-import est une alerte qu'on oubliera.
    alerte_envoyee: bool | None = None
    if _doit_alerter_wf5(
        needs_revision=needs_revision, blocked=blocked, non_juge=non_juge,
        orphelins=orphelins, errors=errors,
    ):
        alerte_envoyee = await _alerter_wf5(
            processed=len(items), needs_revision=needs_revision,
            blocked=blocked, non_juge=non_juge, orphelins=orphelins, items=items,
        )

    return RunWf5Out(
        processed=len(items), approved=approved,
        needs_revision=needs_revision, blocked=blocked,
        non_juge=non_juge, orphelins=orphelins, errors=errors,
        alerte_envoyee=alerte_envoyee, items=items,
    )


# ---------------- Send (Phase 2 — WF-6) ----------------

@app.post(
    "/send/message",
    dependencies=[Depends(_require_auth)],
    response_model=send_tools.SendMessageOut,
)
async def send_message(payload: send_tools.SendMessageIn) -> send_tools.SendMessageOut:
    """Push UN draft approuvé à Instantly. Idempotent : si status != 'draft',
    skip. Defense in depth : revérifie warmup gate + suppression list même
    si WF-5 a déjà approuvé.
    """
    return await send_tools.send_one_message(payload)


@app.post(
    "/wf6/run",
    dependencies=[Depends(_require_auth)],
    response_model=send_tools.RunWf6Out,
)
async def run_wf6(payload: send_tools.RunWf6In) -> send_tools.RunWf6Out:
    """Pass complet WF-6 : pousse jusqu'à `limit` drafts approuvés à Instantly,
    en respectant le daily cap (compté sur fenêtre America/Toronto).

    `dry_run=true` : simule le push sans appel Instantly (pour tester la
    sélection des drafts pendant le warmup).
    """
    return await send_tools.run_wf6(payload)


@app.get("/send/healthcheck", dependencies=[Depends(_require_auth)])
async def send_healthcheck() -> dict[str, Any]:
    """Vérifie que l'API Instantly est joignable et que la campagne existe.
    Utilisable comme smoke test avant d'activer le cron WF-6.

    Retourne toujours 200 — `ok=false` + `error=<msg>` si problème. Évite
    qu'un 500 cache le vrai diagnostic (env var manquante, réseau, etc.).
    """
    from .lib import instantly as instantly_lib
    try:
        camp = await instantly_lib.get_campaign()
        return {"ok": True, "campaign_id": camp.get("id"), "name": camp.get("name")}
    except Exception as e:  # noqa: BLE001 — endpoint diag, on veut tout voir
        return {"ok": False, "error_type": type(e).__name__, "error": str(e)[:500]}


@app.post(
    "/wf6/sync-status",
    dependencies=[Depends(_require_auth)],
    response_model=send_status_tools.SyncStatusOut,
)
async def wf6_sync_status(
    payload: send_status_tools.SyncStatusIn,
) -> send_status_tools.SyncStatusOut:
    """Réconcilie le statut d'envoi des messages 'queued' avec Instantly (audit #5).

    Pour chaque message outbound encore 'queued', interroge le lead Instantly
    (par l'id stocké dans provider_message_id) et flippe le statut :
    sent / bounced / replied. Sur hard bounce → ajoute l'email à suppression_list
    (reason='hard_bounce') ; sur unsubscribe → suppression (opt_out) + contact
    opted_out. Idempotent (ne touche que les 'queued').

    `dry_run=true` : retourne les outcomes sans écrire en DB (QA / 1ère validation
    du mapping des champs Instantly). Cron-friendly : à appeler ~toutes les 15 min
    pendant les fenêtres d'envoi.
    """
    return await send_status_tools.sync_send_status(payload)


# ---------------- Reply (Phase 2 — WF-7) ----------------

# Le webhook public utilise un secret en QUERY PARAM (pas Bearer) car Instantly
# ne sait pas envoyer de header custom standardisé sur tous les events. n8n nous
# relaie typiquement la requête, donc on garde la même convention en cas d'accès
# direct depuis Instantly (Phase 3 bypass n8n).
#
# Le secret est dans l'env WF7_WEBHOOK_SECRET. URL ressemble à :
#   POST /wf7/instantly-webhook?secret=<long_random>
#
# Choisir un secret >= 32 chars, non-deviné. À rotater régulièrement.

def _wf7_webhook_secret() -> str | None:
    return os.environ.get("WF7_WEBHOOK_SECRET", "").strip() or None


def _require_wf7_webhook_secret(secret: str | None) -> None:
    expected = _wf7_webhook_secret()
    if not expected:
        # Refuse en prod si pas configuré — éviter qu'un webhook public traîne
        # sans auth si l'env var est oubliée.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WF7_WEBHOOK_SECRET non défini côté serveur",
        )
    if not secret or not secrets.compare_digest(secret, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook secret")


@app.post("/wf7/instantly-webhook", response_model=reply_tools.HandleReplyOut)
async def wf7_instantly_webhook(
    payload: dict[str, Any],
    secret: str | None = None,
) -> reply_tools.HandleReplyOut:
    """Endpoint public — reçoit le webhook brut d'Instantly (via n8n relais ou
    direct). Auth via query param `?secret=<WF7_WEBHOOK_SECRET>`.

    Pipeline :
      1. Valide le secret
      2. Extrait les champs du payload Instantly (event_type, lead_email, body,
         provider IDs, etc.)
      3. Si pas un `reply_received` → retourne ok=ignored sans crash
      4. Délègue à `reply_tools.handle_reply` pour le LLM + actions DB
    """
    _require_wf7_webhook_secret(secret)

    extracted = reply_tools.extract_from_instantly_webhook(payload or {})
    if extracted is None:
        # Pas un reply event — on accepte le webhook mais on ne fait rien.
        return reply_tools.HandleReplyOut(
            status="ok",
            actions_taken=["event_ignored_not_reply"],
        )
    return await reply_tools.handle_reply(extracted)


@app.post(
    "/wf7/handle-reply",
    dependencies=[Depends(_require_auth)],
    response_model=reply_tools.HandleReplyOut,
)
async def wf7_handle_reply(payload: reply_tools.HandleReplyIn) -> reply_tools.HandleReplyOut:
    """Endpoint interne (Bearer) pour replay manuel d'un reply ou test.

    Permet de re-processer un reply en passant directement les champs normalisés
    (sans le payload Instantly brut). Utile pour QA, debug, ou pour re-classer
    avec un modèle différent.
    """
    return await reply_tools.handle_reply(payload)


@app.get("/wf7/hot-leads", dependencies=[Depends(_require_auth)])
async def wf7_hot_leads(limit: int = 50) -> list[dict[str, Any]]:
    """Liste les contacts hot (status='replied', conversation.state='hot').
    Dashboard manuel — utile si Slack pas configuré ou pour audit.
    """
    from . import supabase_client as db
    # Approximation simple : on liste les contacts récemment passés 'replied'.
    # Une vue SQL dédiée serait plus rigoureuse, suffit pour MVP.
    rows = await db.select(
        "contacts",
        params={
            "select": "id,first_name,last_name,email,company_id,status,updated_at",
            "status": "eq.replied",
            "order": "updated_at.desc",
            "limit": str(min(limit, 200)),
        },
    )
    # Enrichir avec company name
    out: list[dict[str, Any]] = []
    for r in rows:
        company_name: str | None = None
        cid = r.get("company_id")
        if cid:
            co_rows = await db.select(
                "companies",
                params={"select": "name,city", "id": f"eq.{cid}", "limit": "1"},
            )
            if co_rows:
                company_name = co_rows[0].get("name")
        out.append({
            "contact_id": r["id"],
            "name": f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip(),
            "email": r.get("email"),
            "company": company_name,
            "replied_at": r.get("updated_at"),
        })
    return out


@app.post(
    "/wf7/poll-replies",
    dependencies=[Depends(_require_auth)],
    response_model=reply_tools.PollRepliesOut,
)
async def wf7_poll_replies(payload: reply_tools.PollRepliesIn) -> reply_tools.PollRepliesOut:
    """Poll les N derniers emails received d'Instantly et traite ceux non encore
    processés (idempotent via provider_message_id). Alternative au webhook pour
    les plans Instantly sans webhook.

    Cron-friendly. Recommandé toutes les 5-10 min via n8n.
    """
    return await reply_tools.poll_and_process_replies(payload)


@app.get("/wf7/webhook-healthcheck")
async def wf7_webhook_healthcheck(secret: str | None = None) -> dict[str, Any]:
    """Vérifie que le secret webhook est bien configuré et que Slack répond.
    Public (auth via secret) — utile pour valider la config Railway sans
    déclencher de vrai reply.
    """
    _require_wf7_webhook_secret(secret)
    from .lib import slack as slack_lib
    # Le canal réellement utilisé par WF-7 est #leads : on interroge la MÊME
    # résolution que `notify(category="leads")` (SLACK_WEBHOOK_LEADS, sinon
    # fallback SLACK_WEBHOOK_URL). Tester SLACK_WEBHOOK_URL seul mentait dans
    # les deux sens : vert avec #leads absent, rouge avec #leads bien configuré.
    slack_leads_configured = slack_lib.is_configured("leads")
    sender = os.environ.get("INSTANTLY_SENDER_EMAIL", "").strip() or None
    return {
        "ok": True,
        "wf7_secret_configured": True,
        "slack_leads_configured": slack_leads_configured,
        "instantly_sender_configured": bool(sender),
        # pivot tri 2026-08-20 : plus d'auto-reply — le seuil de confidence et
        # l'URL Cal.com du composer n'existent plus (chaîne retirée de tools/reply.py).
    }


# ---------------- Booking (Phase 2 — WF-8) ----------------

# Cal.com webhook signe le raw body (HMAC-SHA256) avec un secret partagé.
# Le secret est dans `CALCOM_WEBHOOK_SECRET` (env). Le header envoyé par
# Cal.com est `X-Cal-Signature-256` (signature hex sans préfixe).
#
# Différence vs WF-7 webhook (Instantly) : Instantly utilise un secret query
# param. Cal.com supporte HMAC natif — on l'utilise.

def _calcom_webhook_secret() -> str | None:
    return os.environ.get("CALCOM_WEBHOOK_SECRET", "").strip() or None


@app.post(
    "/wf8/calcom-webhook",
    response_model=booking_tools.HandleBookingOut,
)
async def wf8_calcom_webhook(request: Request) -> booking_tools.HandleBookingOut:
    """Endpoint public Cal.com webhook — BOOKING_CREATED / RESCHEDULED /
    CANCELLED / MEETING_ENDED.

    Pipeline:
      1. Valide HMAC-SHA256 du raw body via `X-Cal-Signature-256`
      2. Parse JSON et extrait les champs Cal.com normalisés
      3. Persiste dans `booking_events`, update `conversations.state`
      4. Slack ping (build_booked_blocks pour CREATED, texte simple pour autres)
    """
    expected_secret = _calcom_webhook_secret()
    if not expected_secret:
        # Refuse en prod si pas configuré — éviter qu'un webhook public
        # accepte n'importe quoi si l'env var est oubliée.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CALCOM_WEBHOOK_SECRET non défini côté serveur",
        )

    raw_body = await request.body()
    signature = request.headers.get("X-Cal-Signature-256") or request.headers.get(
        "x-cal-signature-256"
    )
    if not booking_tools.verify_calcom_signature(raw_body, signature, expected_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook signature")

    import json as _json
    try:
        body = _json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except _json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid json body")

    extracted = booking_tools.extract_from_calcom_webhook(body or {})
    if extracted is None:
        return booking_tools.HandleBookingOut(
            status="ignored_unsupported_trigger",
            actions_taken=["payload_not_extractable"],
        )
    return await booking_tools.handle_calcom_booking(extracted)


class HandleBookingReplayIn(BaseModel):
    """Payload pour replay manuel (Bearer auth, pas de HMAC). Sert au QA et
    au re-processing d'un webhook capturé.
    """
    body: dict[str, Any]


@app.post(
    "/wf8/handle-booking",
    dependencies=[Depends(_require_auth)],
    response_model=booking_tools.HandleBookingOut,
)
async def wf8_handle_booking(payload: HandleBookingReplayIn) -> booking_tools.HandleBookingOut:
    """Replay manuel d'un webhook Cal.com (Bearer auth, bypass HMAC).

    Utile pour QA / debug / re-processer un event capturé. Le payload doit
    avoir le shape Cal.com brut (triggerEvent + payload).
    """
    extracted = booking_tools.extract_from_calcom_webhook(payload.body or {})
    if extracted is None:
        return booking_tools.HandleBookingOut(
            status="ignored_unsupported_trigger",
            actions_taken=["payload_not_extractable"],
        )
    return await booking_tools.handle_calcom_booking(extracted)


@app.get("/wf8/webhook-healthcheck")
async def wf8_webhook_healthcheck() -> dict[str, Any]:
    """Vérifie config WF-8. Public (pas d'auth — pas de secret à divulguer)."""
    from .lib import slack as slack_lib
    # Même correctif que WF-7 : WF-8 pingue `category="bookings"`, donc on
    # interroge la MÊME résolution que `notify` (SLACK_WEBHOOK_BOOKINGS, sinon
    # fallback SLACK_WEBHOOK_URL). Tester SLACK_WEBHOOK_URL seul mentait dans
    # les deux sens : vert avec #bookings absent, rouge avec #bookings posé.
    return {
        "ok": True,
        "wf8_secret_configured": bool(_calcom_webhook_secret()),
        "slack_bookings_configured": slack_lib.is_configured("bookings"),
    }


# ---------------- Meeting report (Phase 2 — WF-9, auto Granola) ----------------

# Pipeline auto post-RDV : n8n cron (toutes les 10 min) appelle
# `GET /wf9/pending-bookings` pour lister les booking_events finis sans rapport,
# puis pour chaque ID il appelle `POST /wf9/process-booking`. Le serveur fetch
# la note Granola correspondante (matching attendee email + window temporelle),
# appelle `meeting.analyze_meeting`, persiste le rapport et ping Slack.
#
# Granola enregistre LOCALEMENT sur la machine de William → si Granola ne
# tournait pas (ou pas de note pour ce booking), `process-booking` retourne
# `no_match_yet`. Le compteur `meeting_fetch_attempts` cap à 10 essais (~100 min)
# avant d'arrêter de re-tenter automatiquement.

MAX_FETCH_ATTEMPTS = 10


class Wf9PendingOut(BaseModel):
    booking_event_ids: list[str]
    count: int


@app.get(
    "/wf9/pending-bookings",
    dependencies=[Depends(_require_auth)],
    response_model=Wf9PendingOut,
)
async def wf9_pending_bookings(limit: int = 20) -> Wf9PendingOut:
    """Liste les booking_events finis (`meeting_outcome=held`) sans rapport encore
    généré (`meeting_analyzed_at IS NULL`) et qui n'ont pas dépassé le cap de
    re-tentatives Granola. Triés du plus ancien au plus récent.

    n8n cron toutes les 10 min : GET cette liste, puis POST /wf9/process-booking
    pour chaque ID.
    """
    from . import supabase_client as db_low

    rows = await db_low.select(
        "booking_events",
        params={
            "select": "id",
            "meeting_outcome": "eq.held",
            "meeting_analyzed_at": "is.null",
            "meeting_fetch_attempts": f"lt.{MAX_FETCH_ATTEMPTS}",
            "order": "meeting_scheduled_for.asc.nullsfirst",
            "limit": str(max(1, min(limit, 100))),
        },
    )
    ids = [r["id"] for r in rows if r.get("id")]
    return Wf9PendingOut(booking_event_ids=ids, count=len(ids))


class Wf9ProcessIn(BaseModel):
    booking_event_id: str


class Wf9ProcessOut(BaseModel):
    status: str  # "ok" | "no_match_yet" | "note_not_ready" | "max_attempts" | "skipped_no_attendee" | "error"
    booking_event_id: str
    note_id: str | None = None
    match_score: int | None = None
    fit_score: str | None = None
    attempts: int | None = None
    duration_ms: int | None = None
    error_text: str | None = None


@app.post(
    "/wf9/process-booking",
    dependencies=[Depends(_require_auth)],
    response_model=Wf9ProcessOut,
)
async def wf9_process_booking(payload: Wf9ProcessIn) -> Wf9ProcessOut:
    """Traite UN booking_event : fetch Granola note + analyse + persiste + Slack.

    Retours possibles :
      - `ok`              : note trouvée, rapport généré et persisté
      - `no_match_yet`    : aucune note Granola matche (ré-essai au prochain cron)
      - `note_not_ready`  : note trouvée mais summary IA pas encore prête (re-try)
      - `max_attempts`    : on a déjà tenté MAX_FETCH_ATTEMPTS fois → on lâche
      - `skipped_no_attendee` : booking sans email → impossible de matcher
      - `error`           : exception inattendue (Granola down, Anthropic, etc.)
    """
    import time
    from datetime import datetime, timedelta, timezone

    from . import supabase_client as db_low
    from .lib import granola as granola_lib
    from .lib import slack as slack_lib
    from .tools import meeting as meeting_tools

    started = time.monotonic()
    bid = payload.booking_event_id

    # 1) Charge le booking_event
    rows = await db_low.select(
        "booking_events",
        params={
            "select": "id,contact_id,external_event_id,meeting_scheduled_for,"
                      "meeting_outcome,meeting_analyzed_at,meeting_fetch_attempts",
            "id": f"eq.{bid}",
            "limit": "1",
        },
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"booking_event {bid} introuvable")
    be = rows[0]

    attempts = int(be.get("meeting_fetch_attempts") or 0)
    if attempts >= MAX_FETCH_ATTEMPTS:
        return Wf9ProcessOut(
            status="max_attempts", booking_event_id=bid, attempts=attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    if be.get("meeting_analyzed_at"):
        # Race avec un autre run : déjà traité.
        return Wf9ProcessOut(
            status="ok", booking_event_id=bid, attempts=attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 2) Charge contact + company (pour email de matching et contexte LLM)
    contact = None
    company = None
    if be.get("contact_id"):
        c_rows = await db_low.select(
            "contacts",
            params={
                "select": "id,company_id,first_name,last_name,email",
                "id": f"eq.{be['contact_id']}",
                "limit": "1",
            },
        )
        contact = c_rows[0] if c_rows else None
        if contact and contact.get("company_id"):
            co_rows = await db_low.select(
                "companies",
                params={
                    "select": "id,name,city,icp_segment,industry,research_json",
                    "id": f"eq.{contact['company_id']}",
                    "limit": "1",
                },
            )
            company = co_rows[0] if co_rows else None

    attendee_email = (contact or {}).get("email")
    if not attendee_email:
        # Pas d'email → matching impossible. On incrémente quand même pour
        # éviter une boucle infinie ; max_attempts y mettra fin.
        await db_low.update(
            "booking_events",
            {"meeting_fetch_attempts": attempts + 1},
            filters={"id": f"eq.{bid}"},
        )
        return Wf9ProcessOut(
            status="skipped_no_attendee", booking_event_id=bid, attempts=attempts + 1,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 3) Fetch Granola — window = meeting_start − 1h pour rattraper les notes
    # créées légèrement avant (timezone slop) ou tout de suite après.
    meeting_start = None
    if be.get("meeting_scheduled_for"):
        try:
            meeting_start = datetime.fromisoformat(
                str(be["meeting_scheduled_for"]).replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            meeting_start = None
    created_after = (meeting_start or datetime.now(timezone.utc)) - timedelta(hours=1)

    try:
        notes = await granola_lib.list_notes_paginated(
            created_after=created_after, max_pages=3,
        )
    except granola_lib.GranolaError as e:
        # Auth/clé manquante ou erreur permanente → on retourne error, on
        # n'incrémente PAS attempts (le problème est côté serveur, pas Granola
        # qui n'a pas encore généré la note).
        return Wf9ProcessOut(
            status="error", booking_event_id=bid, attempts=attempts,
            error_text=f"granola_list_failed: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    contact_name = None
    if contact:
        contact_name = (
            f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}"
        ).strip() or None

    matched, score = meeting_tools.match_granola_note(
        notes,
        attendee_email=attendee_email,
        meeting_start_iso=be.get("meeting_scheduled_for"),
        contact_name=contact_name,
        company_name=(company or {}).get("name"),
    )

    if matched is None:
        # Pas de match — incrémente attempts pour qu'on arrête après MAX_FETCH_ATTEMPTS
        await db_low.update(
            "booking_events",
            {"meeting_fetch_attempts": attempts + 1},
            filters={"id": f"eq.{bid}"},
        )
        return Wf9ProcessOut(
            status="no_match_yet", booking_event_id=bid, match_score=score,
            attempts=attempts + 1,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 4) Fetch transcript complet de la note matchée
    note_id = matched.get("id")
    try:
        full_note = await granola_lib.get_note(note_id, include_transcript=True) if note_id else matched
    except granola_lib.GranolaNoteNotReady:
        # Note trouvée mais summary IA pas encore générée. Re-try plus tard.
        await db_low.update(
            "booking_events",
            {"meeting_fetch_attempts": attempts + 1},
            filters={"id": f"eq.{bid}"},
        )
        return Wf9ProcessOut(
            status="note_not_ready", booking_event_id=bid, note_id=note_id,
            match_score=score, attempts=attempts + 1,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except granola_lib.GranolaError as e:
        return Wf9ProcessOut(
            status="error", booking_event_id=bid, note_id=note_id, attempts=attempts,
            error_text=f"granola_get_failed: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    transcript_blob = meeting_tools.granola_note_to_text(full_note)
    if not transcript_blob.strip():
        return Wf9ProcessOut(
            status="error", booking_event_id=bid, note_id=note_id, attempts=attempts,
            error_text="granola note vide après flattening",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 5) Analyse LLM
    context = meeting_tools.format_company_context(company, contact)
    try:
        out = await meeting_tools.analyze_meeting(transcript_blob, company_context=context)
    except Exception as e:  # noqa: BLE001
        return Wf9ProcessOut(
            status="error", booking_event_id=bid, note_id=note_id, attempts=attempts,
            error_text=f"analyze_failed: {type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 6) Persiste — meeting_source='granola' distingue du CLI manuel
    await db_low.update(
        "booking_events",
        {
            "meeting_report_json": out.report,
            "meeting_analyzed_at": datetime.now(timezone.utc).isoformat(),
            "meeting_source": "granola",
            "meeting_fetch_attempts": attempts + 1,
        },
        filters={"id": f"eq.{bid}"},
    )

    # 7) Slack ping #bookings — résumé court
    fit = out.report.get("fit_score") or "?"
    company_name = (company or {}).get("name") or ""
    summary_line = (out.report.get("resume_executif") or "").strip().replace("\n", " ")
    if len(summary_line) > 280:
        summary_line = summary_line[:279] + "…"
    top_opp = ""
    opps = out.report.get("opportunites_automatisation")
    if isinstance(opps, list) and opps:
        first = opps[0] if isinstance(opps[0], dict) else None
        if first and first.get("processus"):
            top_opp = f"\n*Top opportunité :* {first.get('processus')} → {first.get('solution', '')}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📝 Rapport post-RDV prêt — fit {fit}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{contact_name or attendee_email}*" + (f" @ *{company_name}*" if company_name else "")}},
    ]
    if summary_line:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": summary_line}})
    if top_opp:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": top_opp}})
    await slack_lib.notify(
        text=f"📝 Rapport post-RDV prêt — {contact_name or attendee_email} (fit {fit})",
        blocks=blocks, context="wf9_report_ready", category="bookings",
    )

    return Wf9ProcessOut(
        status="ok", booking_event_id=bid, note_id=note_id, match_score=score,
        fit_score=str(fit), attempts=attempts + 1,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


@app.get("/wf9/healthcheck")
async def wf9_healthcheck() -> dict[str, Any]:
    """Vérifie config WF-9. Public (pas de secret divulgué)."""
    from .lib import granola as granola_lib
    from .lib import slack as slack_lib
    return {
        "ok": True,
        "granola_key_configured": bool(os.environ.get(granola_lib.GRANOLA_API_KEY_ENV)),
        "anthropic_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "slack_bookings_configured": bool(
            os.environ.get("SLACK_WEBHOOK_BOOKINGS")
            or os.environ.get(slack_lib.SLACK_WEBHOOK_ENV)
        ),
        "max_fetch_attempts": MAX_FETCH_ATTEMPTS,
    }
