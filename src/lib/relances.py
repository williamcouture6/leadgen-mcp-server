"""La séquence de relances, définie à UN seul endroit.

🔴 POURQUOI CE MODULE EXISTE.

Avant le 2026-09-01, « il y a exactement deux relances » était écrit en dur à
**six** endroits indépendants :

    http_api.py     construction du dict depuis la sortie du rédacteur
    instantly.py    les variables `followup_N_body` du push
    compliance.py   le message au juge (deux fois : le rendu et la couche 1)
    personalize.py  les avertissements sur une relance vide
    send.py         la garde qui refuse de pousser une relance manquante

Ajouter une troisième relance demandait donc six modifications, et en oublier
une ne cassait rien de visible : le corps aurait été écrit, jugé et stocké,
puis le lead serait parti sans lui — exactement le défaut que cinq lentilles du
conseil de revue de la spec avaient trouvé SÉPARÉMENT pour les deux premières
relances, quand rien ne les transportait.

Une constante unique rend la prochaine étape triviale et l'oubli impossible :
tout ce qui parcourt les relances parcourt CETTE liste.

⚠️ CE MODULE NE CRÉE PAS L'ÉTAPE CÔTÉ INSTANTLY. Une relance de plus ici veut
dire une étape de plus dans la campagne, dont le gabarit doit être
`{{followup_N_body}}`. Sans elle, la variable arrive chez un destinataire qui
ne la lit pas. C'est une ligne de `docs/go-live-checklist.md`.

⚠️ LE DÉLAI N'EST PAS ICI NON PLUS. Les jours entre deux envois se règlent dans
Instantly. Les libellés ci-dessous les mentionnent pour que le juge sache à
quel rythme le prospect reçoit la séquence — ils ne les IMPOSENT pas.
"""
from __future__ import annotations

# (clé en base, variable Instantly, libellé lisible)
#
# La clé est celle de `messages.followups` (jsonb) et de la sortie du
# rédacteur. La variable est celle que la campagne Instantly interpole. Le
# libellé sert au juge et aux notes de conformité.
RELANCES: tuple[tuple[str, str, str], ...] = (
    ("relance_1", "followup_1_body", "Relance 1 (jour 3)"),
    ("relance_2", "followup_2_body", "Relance 2 (jour 7)"),
    # Ajoutée le 2026-09-01. C'est un courriel d'ADIEU : il annonce qu'on
    # n'écrira plus. Ça a deux conséquences qu'il faut garder en tête.
    #
    # Côté conformité, c'est un point FAVORABLE : la LCAP apprécie une sortie
    # explicite, et le prospect qui la lit sait que la séquence s'arrête sans
    # avoir à se désabonner. Ça ne remplace PAS le lien de désabonnement, qui
    # reste dans la signature du compte.
    #
    # Côté honnêteté, la promesse « je ne vais plus t'écrire » engage. Toute
    # relance ajoutée APRÈS celle-ci ferait de cette phrase un mensonge — et
    # d'un genre que le prospect constate lui-même, donc le pire. Si une
    # relance 4 apparaît un jour, c'est le texte de la 3 qu'il faut changer
    # d'abord.
    ("relance_3", "followup_3_body", "Relance 3 (dernier contact — annonce la fin de la séquence)"),
)

# Les clés seules, pour les parcours simples.
CLES_RELANCES: tuple[str, ...] = tuple(cle for cle, _, _ in RELANCES)

# Le nombre de corps qu'un envoi complet transporte : le courriel + ses relances.
NB_CORPS_PAR_ENVOI: int = 1 + len(RELANCES)


# 🔴 LES CORPS SONT DES CONSTANTES, PAS DE LA GÉNÉRATION. Décision du
# 2026-09-01 : « les 3 relances seront les mêmes pour tous les templates ».
#
# Ils n'ont AUCUN trou — ni métier, ni ville, ni note Google, ni ouvreur généré.
# Trois textes identiques pour les 255 leads et pour les quatre gabarits. Faire
# réécrire par un modèle un texte qui ne varie pas, c'est payer des jetons pour
# fabriquer du risque :
#
#   - la DÉRIVE. C'est ce qui a rendu `check_statistiques_conformes` nécessaire :
#     un 21 qui devient 210 est un mensonge que le prospect vérifie. Un corps
#     constant ne dérive pas.
#   - la TRONCATURE. `skipped_followups_manquants` existe parce qu'un modèle
#     peut s'arrêter au milieu. Une constante ne se tronque pas.
#   - ~280 mots générés par lead, pour un résultat qu'on connaît d'avance.
#
# Les gardes restent en place et ne deviennent pas inutiles : elles surveillent
# désormais LA CONSTANTE. Une modification maladroite de ce fichier se fait
# attraper par `check_statistiques_conformes` et par
# `test_aucune_ancre_ne_doit_etre_morte`.
#
# ⚠️ CE QUI CHANGE POUR QUI ÉDITE CES TEXTES : ils partent tels quels, à tout
# le monde. Il n'y a plus de modèle entre ce fichier et la boîte du prospect.
CORPS_RELANCES: dict[str, str] = {
    "relance_1": """Bonjour,

Je te réécris juste pour remettre mon courriel sur le dessus, des fois que tu l'aies manqué.

Pour te rappeler, je voudrais regarder avec toi pour te créer un système de réponse automatique aux appels, textos, formulaires et même les courriels. Ça répond, les qualifie et ensuite ça t'envoie un résumé pour que tu les rappelles en sachant déjà de quoi ils ont besoin et que tu leur proposes une soumission direct.

Pour le site, l'offre tient toujours.

J'espère pouvoir t'en parler un peu plus!""",
    # Les quatre chiffres de ce corps sont gardés par
    # `check_statistiques_conformes`. Ce qu'ils valent réellement — le 78 % n'a
    # pas de source primaire, le 21 fois mesure la qualification — est écrit
    # au-dessus de `STATISTIQUES_APPROUVEES`. Décision William, prise en
    # connaissance de cause le 2026-09-01.
    "relance_2": """Salut!

J'espère que mon courriel d'avant s'est pas encore perdu à travers les autres.

Je te réécris juste pour te dire que si jamais t'as des questions à propos du système de réponse automatique que je te propose, t'as juste à me les demander, y'a aucun problème.

Je pense vraiment qu'une entreprise comme la tienne pourrait profiter d'un système comme ça.

Juste pour te dire, les entreprises qui ont un système similaire sont capables de retenir en moyenne 21 fois plus de clients si elles répondent en moins de 5 minutes comparé à celles qui répondent en 30 minutes. Sans compter qu'en moyenne 78 % des clients signent avec la première compagnie qui répond.

Bref, si t'as des questions hésite pas""",
    "relance_3": """Re-bonjour,

Je crois avoir compris que ça ne t'intéresse pas d'avoir le système et un site web au goût du jour. Bref, je voulais juste te dire que si un jour tu venais à vouloir avoir plus de clients et gagner plus de temps, je reste toujours disponible pour te répondre.

Je ne vais plus t'écrire, donc si tu veux en savoir plus contacte-moi sur le même courriel.

Au plaisir de pouvoir te parler!""",
}
