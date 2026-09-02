"""Tool `personalize` — Personalization Agent (WF-4).

Génère un cold email personnalisé (Template A ou B) à partir de :
  1. `research_json` de la company (produit par WF-3)
  2. Données contact (scrape WF-3 du site officiel : prénom si dispo, titre, email)
  3. Liste de créneaux Cal.com (source de vérité du CTA — voir [[feedback_cta_real_availability]])
  4. Liste de social_proof (clients référence — voir [[project_zero_client_references]])

Appel Claude Sonnet avec le prompt système `src/prompts/personalize.md`
(cache_control=ephemeral pour réduire le coût input sur appels successifs).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from pydantic import BaseModel

from ..lib.avis import bloc_faits_verifies, nom_commercial
from ..lib.lexique_metiers import lexique_pour
from ..lib.gabarits import est_un_gabarit
from ..lib.relances import CLES_RELANCES, CORPS_RELANCES
from ..lib.metiers import resoudre_metiers
from . import research as research_tools

# ----------------------------------------------------------------------
# Prompt + modèle
# ----------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "personalize.md"
_REACTI_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "reacti" / "personalize.md"
# Sélection du prompt système par track. Défaut OPT = comportement historique.
# Clé 'agence-ia' = offre vivante (pivot 2026-06-07) ; _REACTI_PROMPT_PATH garde
# son nom de variable legacy (= prompt cold email réactivation/services résidentiels).
_PROMPT_PATHS = {"OPT": _PROMPT_PATH, "agence-ia": _REACTI_PROMPT_PATH}
_DEFAULT_MODEL = "claude-sonnet-4-6"


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


# ----------------------------------------------------------------------
# Construction du user message
# ----------------------------------------------------------------------

def bloc_metiers_resolus(
    services_offered: list[str] | None, aujourdhui: date
) -> str:
    """Ce que le rédacteur reçoit sur les métiers. **Il ne classe rien.**

    Tout est décidé par du code déterministe : quel métier fournit la scène
    (celui dont la fenêtre est ouverte et dont la saison arrive le plus tôt),
    lesquels sont les autres, quelle FORMULATION employer pour le 2ᵉ temps, et
    quel lexique gouverne le bloc service.

    🔴 La formulation n'est pas laissée au modèle. « Pour le reste de l'année »
    affirme un contraste temporel qui devient un MENSONGE quand les métiers
    partagent la même saison — la tonte et le paysagement, c'est le même été.
    « Tu fais X aussi » n'affirme rien de temporel et ne peut pas être faux.
    """
    r = resoudre_metiers(services_offered, aujourdhui)

    # 🔴 Le lexique se SÉPARE en deux, et les souder était un défaut trouvé par
    # le conseil final.
    #
    # La spec §3 dit « le lexique du BLOC SERVICE suit le métier dominant ;
    # seule la SCÈNE de l'ouvreur suit le métier saisonnier ». Le bloc service,
    # ce sont les trois QUESTIONS. Le lieu (« où il est »), lui, appartient à
    # l'ouvreur — donc à la scène.
    #
    # Les prendre tous les deux au dominant écrivait, à un laveur de vitres
    # démarché en décembre sur le déneigement :
    #   « Quand quelqu'un cherche un entrepreneur pour déneiger son entrée…
    #     Pis toi t'es EN HAUT D'UNE ÉCHELLE. »
    # Mesuré : scène ≠ dominant sur 27 % des entreprises, lieu divergent sur
    # 25 %. Un courriel sur quatre décrivait le gars au mauvais endroit.
    lex_scene = lexique_pour(r.scene or r.dominant)
    lex_dominant = lexique_pour(r.dominant)

    lignes = ["## Métiers résolus (déjà classés — tu ne recalcules RIEN)"]

    # ⚠️ Deux cas TRÈS différents se cachent derrière `scene is None`, et les
    # confondre coûte un courriel générique à une entreprise dont on connaît
    # parfaitement le métier :
    #   · aucun métier RECONNU  → on n'a rien à nommer, ouvreur générique.
    #   · métiers reconnus mais TOUS hors saison → on sait quoi nommer, c'est
    #     le MOMENT qui est mauvais. La scène retombe sur le dominant, et le
    #     hors-saison se signale.
    hors_saison = r.scene is None and bool(r.metiers)
    scene = r.scene or (r.dominant if hors_saison else None)

    if scene is None:
        lignes += [
            "- **Aucun métier reconnu dans `services_offered`.**",
            "  Écris un ouvreur **générique** : ne nomme aucun métier, garde la",
            "  supposition de l'appel manqué. N'invente surtout pas un métier.",
            "- Pas de 2ᵉ temps.",
        ]
    else:
        lignes.append(f"- **Métier de la scène** (l'ouvreur) : {scene}")
        if hors_saison:
            lignes.append(
                "  ⚠️ **Aucune de ses fenêtres saisonnières n'est ouverte ce mois-ci.** "
                "La scène retombe sur son métier dominant. Ajoute le warning "
                "« hors fenêtre saisonnière »."
            )
        autres_metiers = [m for m in r.metiers if m != scene]
        if autres_metiers:
            autres = _enumerer_metiers(autres_metiers)
            formule = (
                f"Tu fais {autres} aussi."
                if r.meme_saison
                else f"Pour le reste de l'année, tu fais {autres}."
            )
            lignes.append(f"- **Ses autres métiers** : {', '.join(autres_metiers)}")
            lignes.append(
                f"- **2ᵉ temps OBLIGATOIRE**, formulation imposée : « {formule} »"
            )
            if r.scene_est_minoritaire:
                lignes.append(
                    f"  ⚠️ Le métier de la scène pèse ≤ 25 % de ses libellés : le 2ᵉ temps "
                    f"doit NOMMER son métier dominant ({r.dominant}) en premier."
                )
        else:
            lignes.append("- **Entreprise mono-métier : aucun 2ᵉ temps.** Ne l'invente pas.")

    q = lex_dominant.questions
    lignes += [
        "",
        "## Lexique (choisi par une table — recopie-le TEL QUEL)",
        f"- **Où il est**, pour l'OUVREUR (suit le métier de la scène) : **{lex_scene.ou_il_est}**",
        f"- **Les trois questions**, pour le BLOC SERVICE (suivent le métier dominant) :",
        f"  **{q[0]}, {q[1]}, {q[2]}**",
    ]
    if lex_scene.ou_il_est != lex_dominant.ou_il_est:
        lignes.append(
            "  ⚠️ Le lieu et les questions viennent de DEUX métiers différents, "
            "et c'est voulu : l'ouvreur parle de sa saison qui s'en vient, le "
            "bloc service parle de son métier de tous les jours."
        )
    if lex_dominant.est_repli:
        lignes.append(
            "  (lexique de repli : aucun métier reconnu, formulations neutres)"
        )
    return "\n".join(lignes)


# Les métiers féminins du dictionnaire. Les autres sont masculins.
_METIERS_FEMININS = frozenset({"tonte", "toiture", "piscine", "excavation", "extermination"})


def _avec_article(metier: str) -> str:
    """« déneigement » → « du déneigement », « piscine » → « de la piscine »,
    « excavation » → « de l'excavation ».

    🔴 Trois défauts corrigés le 2026-08-30, sur trouvaille du conseil final.
    Ils comptaient parce que le prompt présente cette phrase comme une
    **formulation IMPOSÉE** et interdit au rédacteur de la reformuler « même
    mieux » : le modèle était donc sommé de recopier la faute.

    1. **L'élision passait après le test du féminin**, qui retournait le
       premier : « de la excavation », « de la extermination ».
    2. **`piscine` manquait à la liste** : « du piscine ».
    3. **L'article était posé sur la chaîne DÉJÀ jointe** : « de la excavation
       pis pavage » — un seul article pour deux métiers. C'est pourquoi cette
       fonction ne prend plus qu'UN métier, et que la jointure vient après.

    Mesuré : ~19 % des entreprises ayant un `services_offered` tombaient sur
    l'un des trois.
    """
    # L'élision D'ABORD : elle l'emporte sur le genre. « excavation » est
    # féminin ET commence par une voyelle, et c'est l'élision qui gagne.
    if metier[:1].lower() in "aeiouâàéèêîïôûù":
        return f"de l'{metier}"
    if metier in _METIERS_FEMININS:
        return f"de la {metier}"
    return f"du {metier}"


def _enumerer_metiers(metiers: list[str]) -> str:
    """« du déneigement pis de la tonte » — un article PAR métier.

    ⚠️ La jointure vient APRÈS l'articulation, jamais avant : « de la
    excavation pis pavage » était le résultat de l'ordre inverse.
    """
    avec = [_avec_article(m) for m in metiers]
    if len(avec) == 1:
        return avec[0]
    if len(avec) == 2:
        return f"{avec[0]} pis {avec[1]}"
    return ", ".join(avec[:-1]) + f" pis {avec[-1]}"


def _format_input_for_llm(
    *,
    research: dict[str, Any],
    company: dict[str, Any],
    contact: dict[str, Any] | None,
    social_proof: list[dict[str, Any]],
    template_choice: str,
    slots_block: str,
    track: str = "OPT",
    aujourdhui: date | None = None,
) -> str:
    """Reprend exactement le format du proto CLI (`agents/personalize_agent.py`)."""
    # Coupe au premier separateur : les noms en base sont des fiches Google
    # bourrees de mots-cles, et le nom brut pousse le corps hors des bornes.
    place_name = nom_commercial(company.get("name"))
    website = company.get("website", "") or ""
    # `research_json` porte aussi la télémétrie du scraper d'emails
    # (`diagnostic_courriels`) : compteurs de rejets + adresses tierces jetées.
    # C'est de l'outillage interne — hors du prompt, sinon on changerait l'input
    # du LLM pour chaque company et on lui tendrait des adresses à recopier.
    research = research_tools.sans_diagnostic(research)

    parts = [
        f"## Template à utiliser\n{template_choice}",
        f"\n## Entreprise ciblée\nname: {place_name}\nwebsite: {website}",
    ]

    # ⚠️ Les deux blocs d'AC1b ne servent QUE la piste `agence-ia`.
    #
    # Le prompt OPT ne connaît ni la structure en trois temps, ni le lexique de
    # métier, ni le plancher d'avis. Lui servir ces blocs lui donnerait des
    # instructions qu'il ne sait pas exécuter, et le bloc de faits vérifiés lui
    # ordonnerait de citer une note que son gabarit n'a nulle part où mettre.
    # OPT est gelé mais doit rester INTACT — c'est la règle du repo depuis le
    # pivot, et `check_length` comme `check_registre` la respectent déjà.
    if track == "agence-ia":
        parts += [
            "\n" + bloc_metiers_resolus(
                research.get("services_offered"), aujourdhui or date.today()
            ),
            "\n" + bloc_faits_verifies(
                company.get("google_rating"), company.get("google_reviews_count")
            ),
        ]

    parts.append(
        f"\n## research_json (Research Agent output)\n```json\n{json.dumps(research, ensure_ascii=False, indent=2)}\n```"
    )

    if contact:
        parts.append(
            "\n## contact (fiche contact vérifiée)\n"
            f"```json\n{json.dumps(contact, ensure_ascii=False, indent=2)}\n```"
        )
    else:
        parts.append(
            "\n## contact\n`null` — aucun contact email trouvé "
            "(`owner_confidence` à traiter comme `unknown`). N'invente PAS de nom : "
            "écris une salutation neutre `Bonjour,` et applique le **mode large/routage** "
            "(voir section « Mode d'adresse »). Mets un warning "
            "'Aucun email trouvé — fallback manuel requis (formulaire de contact)'."
        )

    if social_proof:
        parts.append(
            "\n## social_proof (références client réelles, citables uniquement si match secteur/ville)\n"
            f"```json\n{json.dumps(social_proof, ensure_ascii=False, indent=2)}\n```"
        )
    else:
        parts.append(
            "\n## social_proof\n`[]` — Couture IA n'a aucun client référence actuellement. "
            "**INTERDICTION ABSOLUE d'inventer ou de suggérer l'existence de clients passés.** "
            "L'email doit être convaincant sans aucune référence à d'autres clients."
        )

    # 🔴 Cal.com sort du chemin pour `agence-ia`, et le VIDER n'aurait pas
    # suffi : sur liste vide, `format_slots_for_prompt` dit encore « utilise un
    # CTA générique type "15 minutes cette semaine ?" ». Or la règle nº11 du
    # prompt interdit TOUT rendez-vous, tout créneau, toute heure — le
    # rendez-vous se propose dans la réponse au oui, jamais dans le froid.
    # Le tour UTILISATEUR étant plus récent que le système, c'est lui que le
    # modèle suit : le bloc gagnait contre la règle.
    if track != "agence-ia":
        parts.append("\n" + slots_block)
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------

def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {text[:300]}")
    return json.loads(match.group(0))


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

class PersonalizeIn(BaseModel):
    research_json: dict[str, Any]
    company: dict[str, Any]  # nom, website, city, etc. (extrait de la row companies)
    contact: dict[str, Any] | None = None  # fiche contact normalisée (first_name, last_name, email, title)
    social_proof: list[dict[str, Any]] = []
    template_choice: str = "A"  # "A" ou "B"
    available_slots: list[dict[str, Any]] = []  # output de get_available_slots
    model: str = _DEFAULT_MODEL
    track: str = "OPT"  # OPT | REACTI — choisit le prompt système (personalize.md vs reacti/personalize.md)


class PersonalizeOut(BaseModel):
    email: dict[str, Any]  # {subject, body_text, justification, warnings, word_count, template_used}
    template_used: str
    contact_used: bool
    social_proof_count: int
    available_slots_at_generation: list[dict[str, Any]]
    duration_ms: int
    model: str
    usage: LLMUsage


def _call_llm(
    user_message: str,
    model: str,
    max_tokens: int = 2500,
    track: str = "OPT",
) -> tuple[dict[str, Any], LLMUsage]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)

    system_prompt = _PROMPT_PATHS.get(track, _PROMPT_PATH).read_text(encoding="utf-8")

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.4,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = resp.usage
    return (
        _parse_json(text),
        LLMUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
    )


async def personalize(payload: PersonalizeIn) -> PersonalizeOut:
    """Génère un draft email personnalisé. Synchrone à l'intérieur (Anthropic SDK),
    wrappé async avec asyncio.to_thread pour ne pas bloquer FastAPI."""
    import asyncio
    from ..lib.calcom import format_slots_for_prompt

    started = time.monotonic()
    slots_block = format_slots_for_prompt(payload.available_slots)

    user_message = _format_input_for_llm(
        research=payload.research_json,
        company=payload.company,
        contact=payload.contact,
        social_proof=payload.social_proof,
        template_choice=payload.template_choice,
        slots_block=slots_block,
        track=payload.track,
    )

    # 4000 et non 2500 : la piste `agence-ia` rend TROIS corps (le courriel et
    # ses deux relances) au lieu d'un. Une troncature du modèle est silencieuse
    # — elle rendrait une relance vide, refusée au push bien plus tard, sans que
    # rien ne dise pourquoi.
    max_tokens = 4000 if payload.track == "agence-ia" else 2500
    email_json, usage = await asyncio.to_thread(
        _call_llm, user_message, payload.model, max_tokens, payload.track
    )

    # 🔴 La variante RÉELLEMENT écrite, jamais le paramètre. Avec
    # `template_choice="AB"`, le paramètre vaut « AB » : la colonne
    # `messages.template_choice` porterait « AB » sur 100 % des lignes, et il
    # n'y aurait pas de test A/B — juste deux textes et aucune trace de qui a
    # reçu quoi.
    template_used = (email_json.get("template_used") or "").strip().upper()
    if not est_un_gabarit(template_used):
        template_used = payload.template_choice
        if not est_un_gabarit(template_used):
            # Dernier recours : le modèle n'a pas dit sa variante ET le
            # paramètre était « AB ». On refuse de deviner, mais on le DIT.
            email_json.setdefault("warnings", []).append(
                "template_used absent de la sortie et template_choice est une "
                "consigne d'alternance (AB, ABCD…) : la variante envoyée n'est "
                "pas traçable"
            )

    # 🔴 Les relances sont INJECTÉES, pas générées. Décision du 2026-09-01 :
    # les trois sont identiques pour les quatre gabarits et n'ont aucun trou.
    #
    # L'écrasement est VOLONTAIRE et inconditionnel. Si le modèle a quand même
    # produit un `relance_1` — parce qu'un prompt n'a pas été mis à jour, parce
    # qu'il a suivi un exemple — c'est sa version qui est du bruit, pas la
    # nôtre. Fusionner « seulement si absent » laisserait passer exactement le
    # texte dérivé qu'on veut rendre impossible.
    #
    # Ce qui disparaît avec ce bloc : l'avertissement « relance vide ou absente
    # (troncature du modèle ?) ». Il n'a plus d'objet — une constante ne se
    # tronque pas. La garde du push (`skipped_followups_manquants`) reste, elle,
    # parce qu'elle protège aussi les brouillons écrits AVANT ce changement.
    if payload.track == "agence-ia":
        for cle in CLES_RELANCES:
            email_json[cle] = CORPS_RELANCES[cle]

    return PersonalizeOut(
        email=email_json,
        template_used=template_used,
        contact_used=payload.contact is not None,
        social_proof_count=len(payload.social_proof),
        available_slots_at_generation=payload.available_slots,
        duration_ms=int((time.monotonic() - started) * 1000),
        model=payload.model,
        usage=usage,
    )
