"""Les gabarits du courriel de tri, et la lecture du paramètre `template_choice`.

🔴 POURQUOI CE MODULE.

« Les gabarits sont A et B » était écrit en dur dans quatre tests d'appartenance
répartis sur deux fichiers, plus une fonction d'alternance qui rendait
littéralement `"A" if rang % 2 == 0 else "B"`. Ajouter C et D demandait de
retrouver les cinq. En rater un ne cassait rien de bruyant : `template_used`
serait simplement retombé sur le paramètre, et la colonne
`messages.template_choice` aurait porté une valeur fausse — donc un test A/B
qui mesure autre chose que ce qu'on croit.

C'est le même défaut que celui des relances, corrigé le même jour, et pour la
même raison : une vérité écrite à cinq endroits n'est pas une vérité, c'est
cinq occasions de diverger.

## Ce que `template_choice` veut dire

Le paramètre **liste les bras à utiliser**. C'est plus utile qu'un simple
interrupteur, et ça garde le sens historique intact :

    "A"      → force le gabarit A (rejeu manuel d'un lead précis)
    "AB"     → alterne entre A et B, et RIEN d'autre
    "ABCD"   → alterne entre les quatre
    "CD"     → alterne entre C et D seulement

⚠️ `"AB"` continue donc de signifier exactement ce qu'il signifiait avant
l'arrivée de C et D. Le JSON n8n de WF-4 qui poste `"AB"` reste correct et
n'a pas à être ré-importé pour ce changement. Pour ouvrir la campagne aux
quatre gabarits, il faut y écrire `"ABCD"` — un choix explicite, jamais un
effet de bord d'une mise à jour de code.

🔴 Une valeur INCONNUE ne devine rien. Elle est rendue telle quelle pour que
l'appelant la voie et la refuse, plutôt que d'envoyer 255 courriels sur un
gabarit choisi par défaut silencieux.
"""
from __future__ import annotations

# L'ordre compte : c'est celui de l'alternance.
#   A — l'angle du manque          B — l'angle de la course
#   C — la saison, service vague   D — la saison, mécanique en vitrine
GABARITS: tuple[str, ...] = ("A", "B", "C", "D")


def bras_demandes(template_choice: str | None) -> tuple[str, ...]:
    """Les bras que `template_choice` demande, dans l'ordre.

    Rend un tuple vide si la valeur n'est pas interprétable — l'appelant décide
    quoi en faire, ce module ne choisit pas à sa place.
    """
    valeur = (template_choice or "").strip().upper()
    if not valeur:
        return ()
    if valeur in GABARITS:
        return (valeur,)
    # `dict.fromkeys` dédoublonne en gardant l'ordre : « ABA » vaut « AB ».
    lettres = tuple(dict.fromkeys(valeur))
    if all(lettre in GABARITS for lettre in lettres):
        return lettres
    return ()


def bras_du_lot(template_choice: str, rang: int) -> str:
    """Le bras du n-ième contact du lot.

    🔴 L'alternance se fait par RANG DANS LE LOT, jamais par une propriété du
    contact. La spec du 2026-08-26 le dit : sans ça, « A part sur les contacts
    les plus anciens et B sur les plus récents, et le test mesure l'ordre de la
    file au lieu du courriel ». La file est triée `created_at.asc`, donc toute
    répartition dérivée du contact serait corrélée à son ancienneté.

    ⚠️ Le rang est celui du lot, pas un compteur global : deux lots consécutifs
    recommencent tous les deux par le premier bras. Sur des lots de 10 à 20
    c'est sans effet sur l'équilibre ; ça le deviendrait sur des lots de 1, cas
    qui n'existe qu'en rejeu manuel — où le bras se force de toute façon.
    Avec quatre bras, l'écart maximal sur un lot de 10 est de 1 courriel entre
    le bras le plus servi et le moins servi.
    """
    bras = bras_demandes(template_choice)
    if not bras:
        # Valeur inconnue : on la rend telle quelle. Elle sera visible dans
        # `template_demande` et refusée plus loin plutôt que devinée ici.
        return template_choice
    return bras[rang % len(bras)]


def est_un_gabarit(valeur: str | None) -> bool:
    """Un seul gabarit nommé — donc PAS une consigne d'alternance.

    Sert partout où l'on décide si `template_used` est traçable : « AB » et
    « ABCD » sont des demandes, pas des réponses, et les écrire dans
    `messages.template_choice` mettrait la même valeur sur toutes les lignes.
    """
    return (valeur or "").strip().upper() in GABARITS
