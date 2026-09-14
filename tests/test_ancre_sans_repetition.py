"""L'ancre de C et D ne répète plus le verbe du 1ᵉʳ paragraphe.

🔴 CE QUE WILLIAM A VU, le 2026-09-14, en relisant un courriel monté :

    ...pis je me disais que je pourrais te contacter pour te parler de quelque
    chose qui pourrait t'intéresser. Pour le reste de l'année, J'AI AUSSI VU
    que tu fais du paysagement pis de l'excavation.

    Avec ton entreprise, JE VOIS QUE tu as 4,8 étoiles sur 47 avis. On comprend
    que tes clients aiment ton travail!

Deux fois le même verbe à une ligne d'écart. Ça touche les **70 %** de
destinataires qui ont un 2ᵉ temps.

C'est l'ANCRE qui a bougé, pas le 2ᵉ temps : la formulation de celui-ci est une
décision du 2026-09-09 (« tu fais X » → « j'ai aussi vu que tu fais X ») et la
défaire aurait annulé une décision pour en réparer une autre. Les deux versions
de l'ancre s'ouvrent désormais par « Avec … , on comprend que … ! ».
"""

from __future__ import annotations

from pathlib import Path

from src.lib.avis import bloc_faits_verifies
from src.lib.compliance_checks import check_avis_conformes

GABARIT = (
    Path(__file__).parent.parent / "src/prompts/reacti/personalize.md"
).read_text(encoding="utf-8")
PLAT = " ".join(GABARIT.split())


def test_l_ancre_ne_dit_plus_je_vois_que() -> None:
    """La répétition elle-même, interdite à la source."""
    assert "Avec ton entreprise, je vois que" not in PLAT


def test_les_deux_versions_ouvrent_pareil() -> None:
    """« Avec X, on comprend que Y! » — la structure est commune aux deux, et
    c'est ce qui les fait sonner comme une seule voix."""
    assert (
        "Avec {NOTE} étoiles sur {NB_AVIS} avis Google, on comprend que tes "
        "clients aiment ton travail!" in PLAT
    )
    assert (
        "Avec {ENUMERATION_SERVICES}, on comprend que tu en couvres beaucoup!"
        in PLAT
    )
    # ⚠️ « tu en couvres beaucoup » est la phrase de William, et elle a failli
    # se perdre : mon brouillon de la nouvelle forme disait « à voir tout ce que
    # tu couvres … tu en couvres beaucoup », donc je l'avais remplacée pour
    # éviter la répétition. La refonte a fait sauter le début — et donc la
    # répétition — mais j'avais gardé le remplacement. Vérifié le 2026-09-14 :
    # aucun mot ne se répète dans la phrase telle qu'elle est aujourd'hui.


def test_le_deuxieme_temps_garde_sa_formulation() -> None:
    """🔴 Contrôle négatif de la décision du 2026-09-09. Réparer la répétition
    en retirant « j'ai aussi vu » aurait été le réflexe — et aurait défait un
    choix de William vieux de cinq jours."""
    assert "Pour le reste de l'année, j'ai aussi vu que tu fais {AUTRES}." in PLAT
    assert "J'ai aussi vu que tu fais {AUTRES}." in PLAT


def test_le_gabarit_et_le_bloc_de_faits_disent_la_meme_chose() -> None:
    """🔴 LE VRAI RISQUE de cette modification. Le bloc « Faits vérifiés » est
    ce que le rédacteur RECOPIE ; le gabarit est ce qu'il doit écrire. S'ils se
    contredisent sur « avis » / « avis Google », il tranche au hasard et la
    moitié des courriels porte l'autre forme."""
    assert "avis Google" in PLAT
    consigne = bloc_faits_verifies(4.8, 47)
    assert "sur 47 avis Google" in consigne


def test_le_mot_google_naveugle_pas_le_controle_des_chiffres() -> None:
    """Le contrôle déterministe lit le NOMBRE, pas le mot qui suit. Sans cette
    assertion, ajouter un mot à la phrase pourrait faire passer un chiffre
    inventé — le seul garde-fou qui ne dépend d'aucun LLM."""
    vrai = "Avec 4,8 étoiles sur 47 avis Google, on comprend que tes clients aiment ça!"
    assert check_avis_conformes(vrai, 4.8, 47, track="agence-ia").passed

    faux = "Avec 4,8 étoiles sur 300 avis Google, on comprend que tes clients aiment ça!"
    assert not check_avis_conformes(faux, 4.8, 47, track="agence-ia").passed
