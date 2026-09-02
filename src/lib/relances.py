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
