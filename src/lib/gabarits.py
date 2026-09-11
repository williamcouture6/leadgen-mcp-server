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


def bras_eligibles(
    template_choice: str | None, *, metier_connu: bool = True
) -> tuple[str, ...]:
    """Les bras qui pouvaient RÉELLEMENT être servis à ce contact.

    🔴 CE N'EST PAS LE PARAMÈTRE DU LOT, et la nuance est tout l'intérêt de la
    fonction. Un lot tiré en « ABCD » sur une entreprise dont aucun métier n'est
    reconnu n'a que A et B en jeu : C et D exigent un métier à nommer en
    première ligne. Enregistrer « ABCD » sur ce lead laisserait croire qu'il
    était un témoin pour C et D — le biais resterait, en ayant l'air corrigé,
    ce qui est pire que de le laisser visible.

    ⚠️ `bras_du_lot` en dérive : c'est la MÊME liste qui décide et qui se range
    en base. Deux fonctions parallèles auraient fini par diverger, et elles
    auraient divergé exactement sur les cas limites — le rejeu forcé, le métier
    absent — que la trace existe pour éclairer.
    """
    bras = bras_demandes(template_choice)
    if not bras:
        return ()
    if not metier_connu:
        sans_tete_fixe = tuple(b for b in bras if b not in GABARITS_A_TETE_FIXE)
        # Si l'appelant n'a demandé QUE des gabarits à tête fixe, il ne reste
        # rien vers quoi basculer : ils restent éligibles, et la trace le dit.
        # C'est ce qui rend le trou de « CD » seul MESURABLE plutôt que caché.
        if sans_tete_fixe:
            return sans_tete_fixe
    return bras


def bras_eligibles_texte(
    template_choice: str | None, *, metier_connu: bool = True
) -> str | None:
    """La forme rangée dans `messages.bras_eligibles` : « ABCD », « AB », « C ».

    NULL — et non une chaîne vide — quand la consigne est inintelligible : une
    chaîne vide se lirait comme « aucun bras éligible », qui est une réponse ;
    NULL dit « on ne sait pas », qui est la vérité.
    """
    eligibles = bras_eligibles(template_choice, metier_connu=metier_connu)
    return "".join(eligibles) or None


def bras_du_lot(template_choice: str, rang: int, *, metier_connu: bool = True) -> str:
    """Le bras du n-ième contact du lot.

    🔴 L'alternance se fait par RANG DANS LE LOT, jamais par une propriété du
    contact. La spec du 2026-08-26 le dit : sans ça, « A part sur les contacts
    les plus anciens et B sur les plus récents, et le test mesure l'ordre de la
    file au lieu du courriel ». La file est triée `created_at.asc`, donc toute
    répartition dérivée du contact serait corrélée à son ancienneté.

    ⚠️ LE RANG EST UN COMPTEUR GLOBAL, plus celui du lot (corrigé le
    2026-09-10). L'appelant le lit en base — le nombre de brouillons portant un
    bras déjà écrits sur cette piste — et le fait avancer d'une unité PAR
    BROUILLON ÉCRIT. Deux raisons, toutes deux mesurées :

      · un lot de 10 ne se divise pas par 4 (A=3, B=3, C=2, D=2), donc deux
        lots quotidiens repartant de zéro servaient 30/30/20/20 à perpétuité ;
      · un reste de fin de journée retombe toujours sur les premiers bras si le
        compteur se remet à zéro à minuit. En comptant depuis toujours, l'écart
        maximal entre bras reste borné à 1 sur toute la durée du test.

    Cette fonction, elle, ne sait rien de tout ça : elle applique un modulo au
    rang qu'on lui donne. C'est l'appelant qui décide ce que « rang » veut dire.
    """
    eligibles = bras_eligibles(template_choice, metier_connu=metier_connu)
    if not eligibles:
        # Valeur inconnue : on la rend telle quelle. Elle sera visible dans
        # `template_demande` et refusée plus loin plutôt que devinée ici.
        return template_choice
    return eligibles[rang % len(eligibles)]


def est_un_gabarit(valeur: str | None) -> bool:
    """Un seul gabarit nommé — donc PAS une consigne d'alternance.

    Sert partout où l'on décide si `template_used` est traçable : « AB » et
    « ABCD » sont des demandes, pas des réponses, et les écrire dans
    `messages.template_choice` mettrait la même valeur sur toutes les lignes.
    """
    return (valeur or "").strip().upper() in GABARITS
