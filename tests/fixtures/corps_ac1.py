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
# ⚠️ La ligne « Couture IA — Automatisation IA pour PME » de la signature
# actuelle est à RETIRER côté Instantly avant le premier envoi : le mot « IA »
# est interdit dans tout ce qu'un prospect lit (règle du 2026-08-25), et il
# partirait sur les 255 courriels.
SIGNATURE_COMPTE_INSTANTLY = """William Couture
Couture IA
couture-ia.com
Pour te désabonner : https://couture-ia.com/unsubscribe?email={{email}}"""


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
# Relances — en fil, jour 3 et jour 7
# ----------------------------------------------------------------------
# Réécrites au cadrage du 2026-08-30. Ce qui a changé par rapport à la
# version de la spec du 26, et pourquoi :
#   · « Deux minutes après » retiré — le courriel 1 promet 60 secondes ;
#     deux chiffres dans le même fil se contredisent.
#   · « ton site web au goût du jour » retiré — présume que son site est
#     démodé (jamais regardé), et ment aux 97 boîtes sans site.
#   · « tu veux que je te l'envoie? » retiré — laisse croire que le site
#     EXISTE déjà. Il est fait à la main APRÈS le oui. C'est la dette
#     d'honnêteté refermée le 2026-08-26.
#   · tiret cadratin dans la phrase retiré (règle nº1) ; séparateur `---`
#     devenu sans objet, la signature ayant quitté le corps.

RELANCE_1 = """Bonjour,

Ça doit t'arriver de recevoir des textos que tu peux pas répondre tout de
suite. La tonte, ça se fait pas le téléphone à la main, pis le client,
lui, il attend une réponse.

Pour te donner une idée de quoi ça a l'air : quand tu réponds pas, ça
prend le relais, ça pose les questions, et ça fixe le rendez-vous dans
ton agenda. Après, t'as un texto avec le nom, le numéro, l'adresse et ce
que le client veut. Tu te présentes, c'est tout.

Pour le site, l'offre tient toujours. Juste à me dire.
"""

RELANCE_2 = """Bonjour,

Ça doit t'arriver que du monde appelle quand t'es fermé. Un terrain à
tondre, ça attend pas au lendemain matin, pis celui qui tombe sur ta
boîte vocale, il appelle probablement la compétition.

Ce que ça change dans une semaine normale : t'as plus à choisir entre
finir ta job et répondre au téléphone. Les deux se font. Le soir, t'as
une liste de vrais clients à rappeler au lieu d'une boîte vocale pleine.

Et la version rafraîchie de ton site, ça tient toujours. Je te charge
rien. Tu veux-tu la voir? T'as juste à me dire.
"""


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

# La relance 1 ne bascule PAS : « Pour le site, l'offre tient toujours » ne
# dit rien de l'état du site, donc la même phrase sert aux deux cas. Une
# variante de moins à maintenir.
RELANCE_2_SANS_SITE = _sans(
    RELANCE_2,
    "Et la version rafraîchie de ton site, ça tient toujours. Je te charge\n"
    "rien. Tu veux-tu la voir? T'as juste à me dire.",
    "Et le site, l'offre tient toujours. Je pourrais t'en monter un, je te\n"
    "charge rien. Ça t'intéresse-tu? T'as juste à me dire.",
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
    "CORPS_A_REPLI_AVIS": CORPS_A_REPLI_AVIS,
    "CORPS_B_REPLI_AVIS": CORPS_B_REPLI_AVIS,
    "CORPS_A_SANS_SITE": CORPS_A_SANS_SITE,
    "CORPS_B_SANS_SITE": CORPS_B_SANS_SITE,
    "RELANCE_1": RELANCE_1,
    "RELANCE_2": RELANCE_2,
    "RELANCE_2_SANS_SITE": RELANCE_2_SANS_SITE,
}

# Le gabarit à passer à `check_length` pour chacun.
GABARIT: dict[str, str] = {
    "CORPS_A": "A",
    "CORPS_B": "B",
    "CORPS_A_REPLI_AVIS": "A",
    "CORPS_B_REPLI_AVIS": "B",
    "CORPS_A_SANS_SITE": "A",
    "CORPS_B_SANS_SITE": "B",
    "RELANCE_1": "RELANCE",
    "RELANCE_2": "RELANCE",
    "RELANCE_2_SANS_SITE": "RELANCE",
}
