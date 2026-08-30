"""Les deux corps du courriel de tri `agence-ia`, tels que la spec les fixe.

Ce sont des FIXTURES, pas des exemples : plusieurs checks ont été mesurés
faux-verts sur eux (le CTA passait grâce au bloc service et à la ligne de
renvoi, pas grâce au CTA). Tout check qui les concerne doit être testé ici,
et tout changement de copie oblige à remesurer dans le même commit.
"""

SIGNATURE = """---
William Couture
Faisant affaire sous le nom de Couture IA
193 rue de l'Anse, app. 102, Lévis (QC) G6K 1C9
Tu reçois ce courriel parce que ton adresse est publiée en lien avec ton
rôle professionnel.
Questions confidentialité : william@couture-ia.com
Politique de confidentialité : https://couture-ia.com/confidentialite
Pour te désabonner : https://couture-ia.com/unsubscribe ou réponds « STOP »"""

CORPS_A = """Bonjour,

Ça doit t'arriver souvent de pas pouvoir répondre au téléphone. La tonte
pis l'aménagement, ça se fait pas les mains libres. Le client qui tombe
sur ta boîte vocale, lui, il sait pas ça.

Paysagement Rivard a 4,8 étoiles sur 47 avis. Du monde qui te cherche,
t'en as. La question c'est combien tu en échappes dans une semaine.

Ce que je monte : un système qui répond à tout ce qui rentre en moins de
60 secondes. Un appel que tu peux pas prendre, un texto, un message sur
ton site ou sur Facebook. Il demande l'adresse, la grandeur du terrain,
ce que le client veut faire faire. Toi, t'as un texto avec tout dedans.
Le soir, la fin de semaine, quand t'es fermé.

Je travaille tout seul. Ça fait que si de quoi marche pas, c'est moi que
t'appelles, y'a pas de centre d'appel entre nous deux.

En regardant ton site, je me suis dit que je pourrais t'en faire une
version rafraîchie. Je te charge rien pour ça, c'est pour que tu voies
comment je travaille avant qu'on parle du reste.

Dis-moi juste si tu veux le voir.

Si c'est pas toi qui gères ça, tu peux-tu me pointer la bonne personne?
""" + SIGNATURE

CORPS_B = """Bonjour,

Quand quelqu'un cherche un entrepreneur pour son terrain, il en appelle
pas juste un. Il en appelle deux, trois, pis souvent c'est le premier qui
rappelle qui l'a.

Pis toi t'es sur un terrain, la tondeuse dans les oreilles. Tu rappelles
à six heures le soir, pis le gars a déjà donné son contrat à un autre.

Paysagement Rivard a 4,8 étoiles sur 47 avis. Si tu perds des contrats,
c'est probablement pas parce que le monde t'aime pas. C'est parce que
t'as pas pu répondre à temps.

Ce que je monte : un système qui répond à tout ce qui rentre en moins de
60 secondes. Un appel que tu peux pas prendre, un texto, un message sur
ton site ou sur Facebook. Il demande l'adresse, la grandeur du terrain,
ce que le client veut faire faire. Quand tu le rappelles à six heures,
t'es pas le troisième. T'es celui qui lui a déjà répondu.

Je travaille tout seul. Ça fait que si de quoi marche pas, c'est moi que
t'appelles, y'a pas de centre d'appel entre nous deux.

En regardant ton site, je me suis dit que je pourrais t'en faire une
version rafraîchie. Je te charge rien pour ça.

Dis-moi juste si tu veux le voir.

Si c'est pas toi qui gères ça, tu peux-tu me pointer la bonne personne?
""" + SIGNATURE

def _sans(corps: str, cible: str) -> str:
    """Retire `cible` de `corps`, une seule fois — échoue si `cible` est absente
    ou dupliquée.

    str.replace() ne lève jamais d'erreur si `cible` est introuvable : il rend
    la chaîne inchangée. Sans cette garde, un futur edit de CORPS_A qui touche
    `cible` (une virgule, un accent, un retour de ligne) rendrait la variante
    silencieusement identique à l'original, et le test qui s'en sert passerait
    sans plus rien prouver.
    """
    n = corps.count(cible)
    if n != 1:
        raise ValueError(f"cible absente ou dupliquée ({n}x) dans CORPS_A : {cible!r}")
    return corps.replace(cible, "", 1)


# Variantes servant à prouver qu'un check regarde la BONNE phrase.
CORPS_A_SANS_CTA = _sans(CORPS_A, "Dis-moi juste si tu veux le voir.\n\n")
CORPS_A_SANS_RENVOI = _sans(
    CORPS_A, "Si c'est pas toi qui gères ça, tu peux-tu me pointer la bonne personne?\n"
)
