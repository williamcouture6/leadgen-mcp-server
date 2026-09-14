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

from collections.abc import Mapping
from typing import Any

from .avis import bloc_avis_autorise
from .metiers import classer_services

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

    2. ~~**Citation d'avis refusée ET un seul service**~~ → **LEVÉE le
       2026-09-14.** Ce refus existait parce que la version de repli de
       `{ANCRE_CD}` demande « je vois que tu fais {ENUMERATION_SERVICES} » sous
       la forme « autant X que Y pis Z » : avec un seul libellé la forme est
       impossible et « On comprend que tu en couvres beaucoup! » est faux.

       On refusait donc le GABARIT faute de PHRASE. William a écrit la phrase
       manquante — une 3ᵉ version du bloc 2, bâtie sur une supposition :
       « J'imagine qu'à la première bordée, ça rentre pas mal tout en même
       temps! » (`lexique_metiers.phrase_du_rush`, une par métier). Le motif du
       refus a disparu, le refus avec.

    A et B n'ont pas ce problème : leur ouvreur est GÉNÉRÉ, il s'adapte.

    ⚠️ **NE PAS REJOUER LE RATTRAPAGE DE `messages.bras_eligibles` APRÈS CE
    COMMIT.** Il se sert de cette fonction pour reconstituer un tirage PASSÉ ;
    il est déjà appliqué (156/156 lignes le 2026-09-14, dont 4 en `AB`). Le
    rejouer maintenant retirerait ces 4 lignes de leur vérité : au moment du
    tirage, C et D leur étaient bel et bien refusés.
    """
    # 🔴 `citation_autorisee` et `nb_services` NE DÉCIDENT PLUS RIEN, et les
    # paramètres restent pour que les deux appelants et le rattrapage gardent
    # leur signature. Ne pas les retirer sans relire la note ci-dessus.
    del citation_autorisee, nb_services
    return bool(metiers_reconnus)


def tete_fixe_servable_pour_entreprise(company: Mapping[str, Any]) -> bool:
    """La même règle, mais lue depuis une FICHE `companies`.

    🔴 POURQUOI ELLE EST ICI ET PAS DANS `http_api`. Le calcul des trois
    booléens vivait dans `http_api._tete_fixe_servable`, donc atteignable
    seulement par une route HTTP. Le rattrapage de `messages.bras_eligibles`
    (2026-09-13) a besoin d'EXACTEMENT ce calcul pour reconstituer, sur un
    brouillon déjà écrit, l'ensemble des bras qui étaient réellement en jeu.
    Le recopier dans un script aurait fabriqué une seconde vérité, et elle
    aurait divergé sur les cas limites — avis sous le plancher, un seul service
    — c'est-à-dire précisément sur les lignes que le rattrapage existe pour
    étiqueter correctement. `http_api._tete_fixe_servable` délègue désormais
    ici, et un test tient l'égalité des deux.

    🔴 AUCUNE DATE N'ENTRE DANS CE CALCUL, et c'est ce qui rend le rattrapage
    légitime. `bras_eligibles` décrit un tirage qui a EU LIEU : si le résultat
    dépendait du jour où on le rejoue, le backfill n'étiquetterait pas le
    passé, il en écrirait un autre — en silence, puisque rien ne compare.
    D'où l'appel à `classer_services` (sans calendrier depuis AC1c·A) plutôt
    qu'à `resoudre_metiers(services, date.today())`, dont la signature laisse
    croire l'inverse. Les deux rendent le même ensemble de métiers — la date ne
    choisit que la SCÈNE du courriel — mais passer par la version datée
    inviterait la prochaine session à croire qu'elle compte.
    """
    research = company.get("research_json") or {}
    services = research.get("services_offered") or []
    return tete_fixe_servable(
        # 🔴 `industry` DEPUIS LE 2026-09-14. Sans lui, une fiche dont le
        # métier ne vient QUE du secteur (« Entreprises Mobile », 0 service,
        # secteur « entrepreneur en déneigement ») était jugée démarchable
        # par `fenetre_saisonniere_ouverte` — qui, elle, le passait — et
        # privée de C et D ici, pour un métier pourtant connu.
        metiers_reconnus=bool(
            classer_services(services, company.get("industry")).metiers
        ),
        citation_autorisee=bloc_avis_autorise(
            company.get("google_rating"), company.get("google_reviews_count")
        ),
        nb_services=len(services),
    )


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

    🔴 L'ORDRE EST CANONIQUE, ET C'EST UNE OBLIGATION DE LA BASE. La contrainte
    `messages_bras_eligibles_domaine` (0055) exige `^A?B?C?D?$`, donc les
    lettres dans l'ordre de `GABARITS`. Or `bras_demandes` PRÉSERVE l'ordre de
    l'appelant — `dict.fromkeys` dédoublonne, il ne trie pas. Une consigne
    `"DC"`, parfaitement licite au regard de la docstring de ce module qui
    présente le paramètre comme une LISTE de bras, produisait donc `'DC'` :
    refusé à l'insert, pour CHAQUE contact du lot, tous les jours. Et l'alerte
    de famine aurait crié « la file est bouchée » au lieu de « la consigne est
    dans le mauvais ordre ».

    ⚠️ On trie ICI et nulle part ailleurs. `bras_eligibles` garde l'ordre de
    l'appelant parce que c'est lui qui décide quel bras sort au rang 0 :
    « CD » sert C en premier, « DC » sert D. Canoniser le tirage changerait les
    courriels envoyés ; canoniser le texte rangé ne change que sa forme.
    """
    eligibles = bras_eligibles(template_choice, metier_connu=metier_connu)
    return "".join(b for b in GABARITS if b in eligibles) or None


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
