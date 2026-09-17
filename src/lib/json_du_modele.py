"""Lire l'objet JSON qu'un modèle vient de rendre, quoi qu'il ait mis autour.

🔴 UNE SEULE IMPLÉMENTATION, CINQ APPELANTS. C'est le point de ce module.

Le 2026-09-17 à 12 h 33, WF-4 a perdu un brouillon sur dix :
`JSONDecodeError('Extra data: line 18 column 1 (char 2443)')` — le modèle avait
rendu son objet PUIS autre chose. Le correctif a d'abord été écrit dans
`tools/personalize.py` seulement. Un conseil de relecture a montré le jour même
que **le même défaut existait à l'identique dans quatre autres modules**, copiés
mot pour mot :

    tools/research.py · tools/meeting.py · tools/reply.py · tools/compliance.py

Autrement dit : la même réponse qui a fait tomber le rédacteur faisait tomber le
juge de conformité et le classement des réponses. Corriger la cinquième copie
aurait laissé le même piège prêt à se refermer ailleurs — c'est la duplication
qui est le défaut, pas le motif.

## Ce qui ne marchait pas, et pourquoi c'était invisible

L'ancienne version tentait `json.loads` sur tout le texte, puis se rabattait sur
« tout ce qu'il y a entre la première et la dernière accolade ». Ce repli :

  · marchait très bien quand le modèle ajoutait de la prose SANS accolade —
    d'où des mois sans incident, et personne pour le soupçonner ;
  · avalait DEUX objets d'un coup quand le modèle répondait deux fois, et deux
    objets collés ne forment pas un objet valide non plus ;
  · rappelait `json.loads` **hors de tout `try`** : l'erreur s'échappait.

`raw_decode` lit un objet complet et rend l'indice où il s'arrête. Ce qui traîne
derrière ne le regarde pas.

⚠️ **Ne pas « simplifier » en revenant à une expression régulière.** Aucune ne
sait où se ferme un objet JSON : il faut compter les accolades ET savoir
lesquelles sont à l'intérieur d'une chaîne de caractères. C'est le travail d'un
analyseur, pas d'un motif.
"""
from __future__ import annotations

import json
import re
from typing import Any

_BALISE_OUVRANTE = re.compile(r"^```(?:json)?\s*")
_BALISE_FERMANTE = re.compile(r"\s*```$")


def objet_json_du_modele(texte: str, *, source: str = "response") -> dict[str, Any]:
    """Le premier objet JSON complet du texte. Lève `ValueError` sinon.

    `source` n'apparaît que dans le message d'erreur, pour qu'on sache lequel
    des cinq appelants a reçu une réponse illisible.

    🔴 ON ESSAIE CHAQUE ACCOLADE OUVRANTE, PAS SEULEMENT LA PREMIÈRE. Le premier
    correctif ne tentait que `texte.find("{")`, et ça reperdait un brouillon dès
    que le modèle écrivait une accolade avant son JSON. Ce n'est pas théorique :
    le gabarit du rédacteur est bâti sur des jetons à accolades — `{OUVREUR_A}`,
    `{VILLE}`, `{ANCRE_A}`, `{NOTE}` — que le modèle manipule pendant toute sa
    réponse. Une phrase comme « Gabarit A, {OUVREUR_A} rempli : » avant l'objet
    suffisait.
    """
    texte = texte.strip()
    texte = _BALISE_OUVRANTE.sub("", texte)
    texte = _BALISE_FERMANTE.sub("", texte)

    decodeur = json.JSONDecoder()
    depart = texte.find("{")
    while depart != -1:
        try:
            objet, _fin = decodeur.raw_decode(texte, depart)
        except json.JSONDecodeError:
            objet = None
        # ⚠️ On n'accepte QUE les objets, et on continue de chercher sinon.
        # Un modèle qui rend `[...]` ou `"texte"` a mal répondu : les appelants
        # lisent tous des clés (`.get("subject")`, `.get("verdict")`), donc
        # rendre autre chose déplacerait la panne plus loin, là où elle serait
        # bien plus dure à rattacher à sa cause.
        if isinstance(objet, dict):
            return objet
        depart = texte.find("{", depart + 1)

    raise ValueError(f"No JSON object found in {source}: {texte[:300]}")
