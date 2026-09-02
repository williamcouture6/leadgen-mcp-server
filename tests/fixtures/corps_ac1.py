"""Les corps du courriel de tri `agence-ia`, tels que la spec les fixe.

Ce sont des FIXTURES, pas des exemples : plusieurs checks ont été mesurés
faux-verts sur eux (le CTA passait grâce au bloc service et à la ligne de
renvoi, pas grâce au CTA). Tout check qui les concerne doit être testé ici,
et tout changement de copie oblige à remesurer dans le même commit.

🔴 AUCUN CORPS NE PORTE DE SIGNATURE — décision William du 2026-08-30.
Les mentions vivent dans la signature du COMPTE d'envoi Instantly, que l'ESP
colle à chaque message (donc aux trois étapes). Voir
`compliance_checks._mentions_reduites`. Un corps qui porterait une signature
remettrait l'adresse postale dans le texte et compterait ses mots deux fois.

⚠️ `_body_without_signature` ne trouve donc plus de séparateur `---` et rend
le corps entier. C'est CORRECT : les bornes 180-250 ont été mesurées sur le
corps SANS signature, les comptes ne bougent pas.
"""

# Ce qu'Instantly ajoute réellement au courriel reçu (signature du compte
# d'envoi + lien de désabonnement). Sert d'`appended_footer` aux checks.
#
# 🔴 CE TEXTE DOIT ÊTRE IDENTIQUE À TROIS ENDROITS, et rien ne le vérifie :
#   1. la signature du compte d'envoi, dans Instantly ;
#   2. la variable `INSTANTLY_CAMPAIGN_FOOTER` sur Railway ;
#   3. ici.
#
# Aucun code ne lit la vraie signature d'Instantly. `INSTANTLY_CAMPAIGN_FOOTER`
# n'est qu'une DÉCLARATION de ce qu'elle contient — et cette fixture est la
# copie sur laquelle les tests mesurent. Si les trois divergent, les contrôles
# valident un pied de page qui n'existe plus, et les mentions légales
# manquent aux courriels réellement partis sans que personne puisse le voir.
#
# ⚠️ La conséquence est asymétrique. Une déclaration TROP PAUVRE fait échouer
# la configuration : verdict `error`, rien n'est envoyé, rien n'est gelé, tout
# repart dès la correction. Une déclaration TROP RICHE — qui promet des
# mentions que la vraie signature n'a pas — passe au vert et laisse partir
# 255 courriels sans désabonnement valide. C'est le seul sens dangereux.
#
# 🔧 Mise à jour du 2026-09-02, signature choisie par William :
#   · « Automatisation IA pour PME » → « Systèmes de réponse pour PME ».
#     Le mot « IA » est interdit dans tout ce qu'un prospect lit (règle du
#     2026-08-25) et « automatisation » est sur la liste des mots bannis. Ni
#     l'un ni l'autre n'était détecté ici : les checks de mots bannis lisent le
#     CORPS, jamais le pied de page.
#   · le tiret cadratin devient un point médian « · » — le cadratin est le tell
#     nº1 d'un texte écrit par une machine (règle de rédaction nº1).
#   · « Couture IA » RESTE : c'est le nom de l'entreprise et le domaine. Le
#     contrôle des mots bannis fait lui-même cette exception.
#   · le lien porte `?email={{email}}`. Sans le paramètre la page fonctionne
#     quand même — elle demande l'adresse — mais une faute de frappe
#     enregistrerait le désabonnement d'une adresse inexistante pendant que la
#     vraie continue de recevoir.
SIGNATURE_COMPTE_INSTANTLY = """William Couture
Couture IA · Systèmes de réponse pour PME
couture-ia.com

https://couture-ia.com/unsubscribe?email={{email}}"""


# ----------------------------------------------------------------------
# Courriel 1 — jour 0, les deux angles
# ----------------------------------------------------------------------
# Exemple de rendu : Paysagement Rivard, tonte + aménagement paysager,
# 4,8 ⭐ sur 47 avis, a un site, contacté en février.
#   · 1er paragraphe = GÉNÉRÉ (ouvreur ancré + phrase des autres métiers)
#   · le reste = FIXE

CORPS_A = """Bonjour,

Ça doit t'arriver souvent de pas pouvoir répondre au téléphone.
L'aménagement, ça se fait pas les mains libres. Le client qui tombe sur
ta boîte vocale, lui, il sait pas ça. Tu fais de la tonte aussi.

Paysagement Rivard a 4,8 étoiles sur 47 avis. Du monde qui te cherche,
t'en as. La question c'est combien tu en échappes dans une semaine.

Moi c'est William, et je fais en sorte de régler ces problèmes-là. Ce que
je propose aux entreprises de services résidentiels, c'est de créer un
système qui répond à tout ce qui rentre en moins de 60 secondes. Un appel
que tu peux pas prendre, un texto, un message sur ton site ou sur
Facebook. Il demande l'adresse, la grandeur du terrain, ce que le client
veut faire faire. Le système reste actif 24/7, le soir, la fin de semaine,
quand t'es pas disponible. Toi, tu reçois un texto avec tout dedans qui te
simplifie la vie.

En regardant ton site, je me suis aussi dit que je pourrais t'en faire
une version rafraîchie. Je te charge rien pour ça, c'est pour que tu voies
comment je travaille avant qu'on parle du reste.

Dis-moi juste si tu veux le voir.

Si c'est pas toi qui gères ça, tu peux-tu me pointer la bonne personne?
"""

CORPS_B = """Bonjour,

Quand quelqu'un cherche un entrepreneur pour son terrain, il en appelle
pas juste un. Il en appelle deux, trois, pis souvent c'est le premier qui
rappelle qui l'a.

Pis toi t'es sur un terrain, la tondeuse dans les oreilles. Tu rappelles
à six heures le soir, pis le gars a déjà donné son contrat à un autre.
Pour le reste de l'année, tu fais du déneigement.

Paysagement Rivard a 4,8 étoiles sur 47 avis. Si tu perds des contrats,
c'est probablement pas parce que le monde t'aime pas. C'est parce que
t'as pas pu répondre à temps.

Moi c'est William, et je fais en sorte de régler ces problèmes-là. Ce que
je propose aux entreprises de services résidentiels, c'est de créer un
système qui répond à tout ce qui rentre en moins de 60 secondes. Un appel
que tu peux pas prendre, un texto, un message sur ton site ou sur
Facebook. Il demande l'adresse, la grandeur du terrain, ce que le client
veut faire faire. Avec ce système tu réponds plus vite au client, et ça
t'évite de le rappeler à 18h pour qu'il te dise qu'il a déjà appelé une
autre compagnie.

En regardant ton site, je me suis aussi dit que je pourrais t'en faire
une version rafraîchie. Je te charge rien pour ça.

Dis-moi juste si tu veux le voir.

Si c'est pas toi qui gères ça, tu peux-tu me pointer la bonne personne?
"""


# ----------------------------------------------------------------------
# Relances — le fichier ne les DÉFINIT plus, il les IMPORTE
# ----------------------------------------------------------------------
# 🔴 Elles vivaient ici en copie, et elles avaient DÉJÀ divergé le 2026-09-01,
# le jour même où William les a réécrites. Les tests mesuraient donc des textes
# qui ne partaient plus : forme, longueur, budget de « pis » — tout était vert
# sur les anciennes versions.
#
# C'est le défaut exact que `lib/relances.py` a supprimé côté production le
# matin même (six endroits codaient « il y a deux relances »), reproduit côté
# tests l'après-midi. Une copie ne diverge pas parce qu'on est négligent : elle
# diverge parce que rien ne l'oblige à suivre.
#
# Les fixtures ré-exportent donc la source de vérité. Un texte, un endroit.
from src.lib.relances import CORPS_RELANCES  # noqa: E402

RELANCE_1 = CORPS_RELANCES["relance_1"] + "\n"
RELANCE_2 = CORPS_RELANCES["relance_2"] + "\n"
RELANCE_3 = CORPS_RELANCES["relance_3"] + "\n"

# ⚠️ `RELANCE_2_SANS_SITE` a été SUPPRIMÉE, pas oubliée. La relance 2 réécrite
# ne parle plus du site du tout : il n'y a donc plus de bascule à faire, et une
# variante vide aurait été une fixture qui teste une différence inexistante.
# La contradiction qu'elle portait — le courriel disant le site fait, la relance
# le disant à faire — disparaît avec elle.


def _sans(corps: str, cible: str, remplacement: str = "") -> str:
    """Remplace `cible` dans `corps`, une seule fois — échoue si `cible` est
    absente ou dupliquée.

    str.replace() ne lève jamais d'erreur si `cible` est introuvable : il rend
    la chaîne inchangée. Sans cette garde, un futur edit d'un corps qui touche
    `cible` (une virgule, un accent, un retour de ligne) rendrait la variante
    silencieusement identique à l'original, et le test qui s'en sert passerait
    sans plus rien prouver.
    """
    n = corps.count(cible)
    if n != 1:
        raise ValueError(f"cible absente ou dupliquée ({n}x) : {cible!r}")
    return corps.replace(cible, remplacement, 1)


# ----------------------------------------------------------------------
# Le repli du bloc 2 — la CITATION saute, pas le paragraphe
# ----------------------------------------------------------------------
# 89 boîtes sur 255 (35 %) sont sous le plancher (moins de 10 avis OU moins
# de 4,0). Sans lui, un tiers de la liste lit sa propre mauvaise note en
# pleine face, et le paragraphe B (« c'est probablement pas parce que le
# monde t'aime pas ») se lit comme du sarcasme.
# La v3 faisait sauter le bloc ENTIER, ce qui amputait ~25 mots et laissait
# le corps à un mot de la borne basse.

CORPS_A_REPLI_AVIS = _sans(
    CORPS_A,
    "Paysagement Rivard a 4,8 étoiles sur 47 avis. Du monde qui te cherche,\n"
    "t'en as. La question c'est combien tu en échappes dans une semaine.",
    "Du monde qui te cherche, t'en as. La question c'est plutôt combien tu\n"
    "en échappes dans une semaine.",
)

CORPS_B_REPLI_AVIS = _sans(
    CORPS_B, "Paysagement Rivard a 4,8 étoiles sur 47 avis. Si tu perds", "Si tu perds"
)


# ----------------------------------------------------------------------
# La bascule « sans site » — 97 boîtes sur 255
# ----------------------------------------------------------------------
# Elle est plus VRAIE que la version standard : l'absence de site est
# vérifiable dans `companies.website`. Sans elle, les deux gabarits et le
# repli disent tous « ton site » — l'implicite produit le mensonge par défaut.

CORPS_A_SANS_SITE = _sans(
    CORPS_A,
    "En regardant ton site, je me suis aussi dit que je pourrais t'en faire\n"
    "une version rafraîchie. Je te charge rien pour ça, c'est pour que tu voies\n"
    "comment je travaille avant qu'on parle du reste.",
    "En regardant ton entreprise, je me suis aussi dit que je pourrais te\n"
    "créer un site, parce que je pense que t'en as pas. Je te charge rien\n"
    "pour ça, c'est pour que tu voies comment je travaille avant qu'on parle\n"
    "du reste.",
)

CORPS_B_SANS_SITE = _sans(
    CORPS_B,
    "En regardant ton site, je me suis aussi dit que je pourrais t'en faire\n"
    "une version rafraîchie. Je te charge rien pour ça.",
    "En regardant ton entreprise, je me suis aussi dit que je pourrais te\n"
    "créer un site, parce que je pense que t'en as pas. Je te charge rien\n"
    "pour ça.",
)





# ----------------------------------------------------------------------
# Courriel 1, gabarits C et D — jour 0, l'angle de la saison
# ----------------------------------------------------------------------
# Écrits par William les 2026-08-31 et 2026-09-01, gelés dans
# `docs/superpowers/specs/../plans/2026-08-30-ac1b-copie.md`.
#
# Même entreprise d'exemple que A et B pour que les quatre se comparent :
# Paysagement Rivard, tonte + aménagement paysager, 4,8 ⭐ sur 47 avis, a un
# site, contacté en février.
#
# 🔴 CE QUI LES DISTINGUE DE A ET B, ET QUI COMPTE POUR LES TESTS :
# le 1er paragraphe est FIXE. Il ne porte que le métier et la ville, tous deux
# fournis. Il n'y a pas d'ouvreur généré, donc pas de longueur variable, donc
# le compte de mots ci-dessous est le compte RÉEL — pas une estimation avec
# une marge pour ce que le modèle ajoutera.
#
# C et D partagent leurs 1er, 2e, 6e et 7e paragraphes. Seuls les 4e et 5e
# changent : C décrit le service en vague, D met la mécanique en vitrine.

_CD_TETE = """Bonjour,

J'ai vu que tu fais du paysagement dans la région de Laval. La saison
approche, pis je me disais que je pourrais te contacter pour te parler de
quelque chose qui pourrait t'intéresser.

Avec ton entreprise, je vois que tu as 4,8 étoiles sur 47 avis. On
comprend que tes clients aiment ton travail! Pourtant je suis certain
qu'il serait possible de te simplifier la vie avec la gestion de tes
clients et t'en amener plus en même temps."""

_CD_PIED = """Je te pousse à rien, mais si ça t'intéresse t'as juste à me le dire et je
pourrais t'expliquer un peu plus!

J'en ai aussi profité pour te refaire un site web au goût du jour. Je
pourrais te montrer ça aussi si t'es intéressé.
"""

CORPS_C = f"""{_CD_TETE}

Je veux pas donner l'impression de trop pousser, mais je me présente. Moi
c'est William, et j'aide les PME de paysagement à se simplifier la vie,
gagner du temps et des clients. Ce que je fais, c'est construire un
système, pour ton entreprise, de réponse automatique aux appels, textos,
formulaires ou courriels que tu pourrais avoir manqués, ou juste pas eu le
temps de répondre à temps.

Les avantages d'avoir un système comme ça, c'est d'être le plus vite à
répondre à un prospect qui autrement irait chez ta compétition. Ça
augmente aussi la satisfaction de tes clients et te sauve du temps au
passage.

{_CD_PIED}"""

CORPS_D = f"""{_CD_TETE}

Je veux pas donner l'impression de trop pousser, mais je me présente. Moi
c'est William, et je monte des systèmes qui répondent en moins de 60
secondes à tout ce qui rentre : les appels manqués, les textos, les
formulaires, les courriels. Le système pose les questions qu'il faut pour
savoir ce que le client cherche, il t'envoie un résumé de la demande, et
il peut même fixer le rendez-vous.

Pourquoi ça compte : tes clients trouvent ça pas mal plus agréable, pis le
premier qui rappelle est bien souvent celui qui décroche le contrat, même
quand il est pas le moins cher.

{_CD_PIED}"""


# ----------------------------------------------------------------------
# C et D — le repli du bloc 2
# ----------------------------------------------------------------------
# 🔴 Le repli de C et D ne se contente pas de RETIRER le chiffre, contrairement
# à celui de A et B : il met les SERVICES RÉELS à la place. Exigence de William
# du 2026-08-31 — « il faudrait modifier le deuxième paragraphe pour qu'il soit
# personnalisé même si on enlève les infos de Google ». Un repli générique
# aurait vidé le seul paragraphe qui parle de l'entreprise.
#
# La phrase du milieu change aussi, et c'est voulu : « tes clients aiment ton
# travail » découle d'une note, « tu en couvres beaucoup » découle d'une liste.
# La chute est identique dans les deux versions.

_REPLI_ANCRE = (
    "Avec ton entreprise, je vois que tu fais autant la tonte que\n"
    "l'aménagement paysager pis les plates-bandes. On comprend que tu en\n"
    "couvres beaucoup! Pourtant je suis certain"
)
_NOTE_ANCRE = (
    "Avec ton entreprise, je vois que tu as 4,8 étoiles sur 47 avis. On\n"
    "comprend que tes clients aiment ton travail! Pourtant je suis certain"
)

CORPS_C_REPLI_AVIS = _sans(CORPS_C, _NOTE_ANCRE, _REPLI_ANCRE)
CORPS_D_REPLI_AVIS = _sans(CORPS_D, _NOTE_ANCRE, _REPLI_ANCRE)


# ----------------------------------------------------------------------
# C et D — la bascule « sans site »
# ----------------------------------------------------------------------
# Un seul mot change, et c'est le bon : on ne « refait » pas un site qui
# n'existe pas. La formulation dit le site DÉJÀ fait dans les deux cas —
# décision William du 2026-08-31, réaffirmée après avertissement. Le contrôle
# `site_au_conditionnel` la détecte toujours et l'écrit dans les notes ; il ne
# bloque plus.

_SITE_REFAIT = "J'en ai aussi profité pour te refaire un site web"
_SITE_FAIT = "J'en ai aussi profité pour te faire un site web"

CORPS_C_SANS_SITE = _sans(CORPS_C, _SITE_REFAIT, _SITE_FAIT)
CORPS_D_SANS_SITE = _sans(CORPS_D, _SITE_REFAIT, _SITE_FAIT)

# La QUATRIÈME combinaison : sous le plancher d'avis ET sans site.
#
# 🔧 Corrigé le 2026-09-01. Elle n'existait d'abord que pour C et D, au motif
# que « chez A et B les deux bascules se composent sans surprise, chez C et D
# le repli réécrit tout le paragraphe 2 ». C'était FAUX, et mesuré depuis :
# dans les QUATRE gabarits, le repli touche le paragraphe de l'ancre et la
# bascule sans-site celui du site — deux paragraphes différents, aucune
# interaction. C et D n'avaient rien de spécial.
#
# Le motif de les écrire toutes les quatre n'est donc pas structurel, il est
# factuel : une entreprise sous le plancher d'avis ET sans site est un cas réel
# de la liste, et rien ne la mesurait.
CORPS_C_REPLI_SANS_SITE = _sans(CORPS_C_REPLI_AVIS, _SITE_REFAIT, _SITE_FAIT)
CORPS_D_REPLI_SANS_SITE = _sans(CORPS_D_REPLI_AVIS, _SITE_REFAIT, _SITE_FAIT)

# A et B composent leurs deux bascules exactement pareil. Leur bloc du site
# n'est pas celui de C et D — ils restent au conditionnel — d'où des
# remplacements propres plutôt que `_SITE_FAIT`.
CORPS_A_REPLI_SANS_SITE = _sans(
    CORPS_A_REPLI_AVIS,
    "En regardant ton site, je me suis aussi dit que je pourrais t'en faire\n"
    "une version rafraîchie. Je te charge rien pour ça, c'est pour que tu voies\n"
    "comment je travaille avant qu'on parle du reste.",
    "En regardant ton entreprise, je me suis aussi dit que je pourrais te\n"
    "créer un site, parce que je pense que t'en as pas. Je te charge rien\n"
    "pour ça, c'est pour que tu voies comment je travaille avant qu'on parle\n"
    "du reste.",
)

CORPS_B_REPLI_SANS_SITE = _sans(
    CORPS_B_REPLI_AVIS,
    "En regardant ton site, je me suis aussi dit que je pourrais t'en faire\n"
    "une version rafraîchie. Je te charge rien pour ça.",
    "En regardant ton entreprise, je me suis aussi dit que je pourrais te\n"
    "créer un site, parce que je pense que t'en as pas. Je te charge rien\n"
    "pour ça.",
)


# ----------------------------------------------------------------------
# Variantes servant à prouver qu'un check regarde la BONNE phrase
# ----------------------------------------------------------------------

CORPS_A_SANS_CTA = _sans(CORPS_A, "Dis-moi juste si tu veux le voir.\n\n")
CORPS_A_SANS_RENVOI = _sans(
    CORPS_A, "Si c'est pas toi qui gères ça, tu peux-tu me pointer la bonne personne?\n"
)


# Tout ce qui doit respecter les invariants de forme. Un corps ajouté ici
# est automatiquement soumis aux tests de la famille (pas de signature, pas
# de tiret cadratin, pas de lien).
TOUS_LES_CORPS: dict[str, str] = {
    "CORPS_A": CORPS_A,
    "CORPS_B": CORPS_B,
    "CORPS_C": CORPS_C,
    "CORPS_D": CORPS_D,
    "CORPS_A_REPLI_AVIS": CORPS_A_REPLI_AVIS,
    "CORPS_B_REPLI_AVIS": CORPS_B_REPLI_AVIS,
    "CORPS_C_REPLI_AVIS": CORPS_C_REPLI_AVIS,
    "CORPS_D_REPLI_AVIS": CORPS_D_REPLI_AVIS,
    "CORPS_A_SANS_SITE": CORPS_A_SANS_SITE,
    "CORPS_B_SANS_SITE": CORPS_B_SANS_SITE,
    "CORPS_C_SANS_SITE": CORPS_C_SANS_SITE,
    "CORPS_D_SANS_SITE": CORPS_D_SANS_SITE,
    "CORPS_A_REPLI_SANS_SITE": CORPS_A_REPLI_SANS_SITE,
    "CORPS_B_REPLI_SANS_SITE": CORPS_B_REPLI_SANS_SITE,
    "CORPS_C_REPLI_SANS_SITE": CORPS_C_REPLI_SANS_SITE,
    "CORPS_D_REPLI_SANS_SITE": CORPS_D_REPLI_SANS_SITE,
    "RELANCE_1": RELANCE_1,
    "RELANCE_2": RELANCE_2,
    "RELANCE_3": RELANCE_3,
}

# Le gabarit à passer à `check_length` pour chacun.
GABARIT: dict[str, str] = {
    "CORPS_A": "A",
    "CORPS_B": "B",
    "CORPS_C": "C",
    "CORPS_D": "D",
    "CORPS_A_REPLI_AVIS": "A",
    "CORPS_B_REPLI_AVIS": "B",
    "CORPS_C_REPLI_AVIS": "C",
    "CORPS_D_REPLI_AVIS": "D",
    "CORPS_A_SANS_SITE": "A",
    "CORPS_B_SANS_SITE": "B",
    "CORPS_C_SANS_SITE": "C",
    "CORPS_D_SANS_SITE": "D",
    "CORPS_A_REPLI_SANS_SITE": "A",
    "CORPS_B_REPLI_SANS_SITE": "B",
    "CORPS_C_REPLI_SANS_SITE": "C",
    "CORPS_D_REPLI_SANS_SITE": "D",
    "RELANCE_1": "RELANCE",
    "RELANCE_2": "RELANCE",
    "RELANCE_3": "RELANCE",
}
