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


# Les gabarits dont le PREMIER PARAGRAPHE EST FIXE et nomme un métier.
#
# 🔴 C'est la raison pour laquelle `bras_du_lot` doit parfois refuser un bras.
# Les trois têtes de C et D disent « J'ai vu que tu fais du {METIER}… » puis
# situent la saison. Une entreprise dont AUCUN métier n'est reconnu n'a rien à
# mettre dans ce trou : le rédacteur laisserait un blanc, ou piocherait un
# service au hasard dans `services_offered` — une affirmation inventée en
# première ligne, ce que tout le reste du système existe pour empêcher.
#
# A et B, eux, ont un ouvreur GÉNÉRÉ : ils savent écrire sans nommer de métier.
GABARITS_A_TETE_FIXE: frozenset[str] = frozenset({"C", "D"})


def tete_fixe_servable(
    *, metiers_reconnus: bool, citation_autorisee: bool, nb_services: int
) -> bool:
    """C et D peuvent-ils être servis à cette entreprise ?

    Leur premier paragraphe est FIXE : il ne s'adapte pas. Deux choses lui
    manquent parfois, et dans les deux cas le rédacteur devrait inventer.

    1. **Aucun métier reconnu** → `{METIER}` n'a rien à recevoir. Le rédacteur
       laisserait un blanc, ou piocherait un service au hasard.

    2. **Citation d'avis refusée ET un seul service** → la version de repli de
       `{ANCRE_CD}` demande « je vois que tu fais {ENUMERATION_SERVICES} » sous
       la forme « autant X que Y pis Z », puis « On comprend que tu en couvres
       beaucoup! ». Avec un seul libellé, la forme est impossible et la phrase
       est fausse. Mesuré le 2026-09-07 : **6 entreprises** sur les 474
       joignables ce jour-là.

    A et B n'ont pas ce problème : leur ouvreur est GÉNÉRÉ, il s'adapte.

    ⚠️ Ça coûte quelque chose, et c'est écrit dans le test : ces entreprises ne
    peuvent plus tirer C ni D, donc la population des quatre bras n'est pas
    exactement la même. Le biais est petit (~2 %), connu, et il évite un
    courriel que le prospect verrait faux.
    """
    if not metiers_reconnus:
        return False
    return citation_autorisee or nb_services >= 2


def bras_du_lot(template_choice: str, rang: int, *, metier_connu: bool = True) -> str:
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
    if not metier_connu:
        # Trouvé par un conseil de relecture le 2026-09-07. Ces entreprises
        # restent joignables toute l'année — c'est le garde-fou nº2, « on
        # inclut dans le doute » — et le bras se tirait au RANG dans le lot,
        # sans jamais regarder si un métier avait été reconnu. Une sur deux
        # tombait donc sur un gabarit dont la tête EXIGE un métier.
        sans_tete_fixe = tuple(b for b in bras if b not in GABARITS_A_TETE_FIXE)
        # Si l'appelant n'a demandé QUE des gabarits à tête fixe, on ne peut
        # rien inventer : on rend le tirage normal plutôt que de renvoyer un
        # bras qu'il n'a pas demandé. Le cas n'existe pas en production
        # (`template_choice="ABCD"`), et le masquer serait pire que le laisser
        # visible.
        if sans_tete_fixe:
            return sans_tete_fixe[rang % len(sans_tete_fixe)]
    return bras[rang % len(bras)]


def est_un_gabarit(valeur: str | None) -> bool:
    """Un seul gabarit nommé — donc PAS une consigne d'alternance.

    Sert partout où l'on décide si `template_used` est traçable : « AB » et
    « ABCD » sont des demandes, pas des réponses, et les écrire dans
    `messages.template_choice` mettrait la même valeur sur toutes les lignes.
    """
    return (valeur or "").strip().upper() in GABARITS
