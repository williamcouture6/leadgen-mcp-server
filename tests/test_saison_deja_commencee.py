"""L'ouvreur de C et D affirme une date — elle doit être vraie le jour de l'envoi.

Les gabarits C et D ouvrent sur une phrase qui SITUE la saison. Il y a trois
situations possibles, donc trois phrases, donc trois façons de se tromper :

    saison à venir            « La saison approche »
    commencée depuis < 1 mois « C'est le début de la saison »
    commencée depuis ≥ 1 mois la formulation de pleine saison

Ce que ça corrige, mesuré le 2026-09-04 sur la file réelle de 325 contacts :
avec un seul ouvreur, la phrase aurait été fausse pour la quasi-totalité du lot
dans les mois concernés — 275 contacts sur 278 en mai et juin, 138 sur 141 en
décembre, 94 sur 97 en juillet.

⚠️ Aucun réglage de fenêtre ne répare ça : une fenêtre qui reste ouverte après
le début de la saison contient, par construction, des jours où « la saison
approche » est faux. La correction vit dans la COPIE, et le juge la COMPTE sans
tuer le brouillon.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.lib.compliance_checks import (
    TOURNURES_INTERDITES,
    TOURNURES_PAR_MOMENT,
    check_saison_au_bon_temps,
    run_all,
)
from src.lib.metiers import (
    MOMENT_A_VENIR,
    MOMENT_DEBUT,
    MOMENT_EN_COURS,
    SAISONS,
    fenetre_mois,
    moment_de_la_saison,
    resoudre_metiers,
)
from src.tools.personalize import bloc_metiers_resolus

# ============================================================== 1. LE FAIT ===


@pytest.mark.parametrize(
    "metier,jour,attendu",
    [
        # Le déneigement démarre le 15 novembre, fenêtre août→déc.
        ("déneigement", date(2026, 9, 10), MOMENT_A_VENIR),
        ("déneigement", date(2026, 11, 14), MOMENT_A_VENIR),
        ("déneigement", date(2026, 11, 15), MOMENT_DEBUT),  # le jour même compte
        # 🔴 Le cas qui a imposé de compter en JOURS. Le calendrier dirait « un
        # mois » (novembre → décembre) alors qu'il n'y a que 25 jours de neige :
        # « c'est le début de la saison » est encore parfaitement vrai. Et
        # décembre, c'est 138 des 141 joignables de l'hiver.
        ("déneigement", date(2026, 12, 10), MOMENT_DEBUT),
        ("déneigement", date(2026, 12, 20), MOMENT_EN_COURS),
        # Janvier : il neige, mais janvier est HORS fenêtre — on ne lui écrit
        # pas, donc il n'y a aucune phrase à choisir.
        ("déneigement", date(2027, 1, 10), None),
        ("déneigement", date(2026, 5, 10), None),
        # Le paysagement démarre le 15 avril, fenêtre janv→juin.
        ("paysagement", date(2026, 4, 14), MOMENT_A_VENIR),
        ("paysagement", date(2026, 4, 20), MOMENT_DEBUT),
        ("paysagement", date(2026, 6, 10), MOMENT_EN_COURS),
        # La tonte démarre le 1er mai, fenêtre janv→juil.
        ("tonte", date(2026, 1, 10), MOMENT_A_VENIR),
        ("tonte", date(2026, 5, 10), MOMENT_DEBUT),
        ("tonte", date(2026, 7, 31), MOMENT_EN_COURS),  # 91 jours : le pire cas
    ],
)
def test_le_moment_de_la_saison(metier: str, jour: date, attendu: str | None) -> None:
    assert moment_de_la_saison(metier, jour) is attendu


@pytest.mark.parametrize("metier", sorted(SAISONS))
def test_la_bascule_tombe_un_mois_apres_le_debut(metier: str) -> None:
    """La règle de William, énoncée le 2026-09-07 : « la 3e formule, on peut
    commencer à l'envoyer 1 mois après le début de la saison ».

    Ce test la vérifie sur les six métiers plutôt que sur un exemple, parce que
    la bascule dépend de la fenêtre de chacun et qu'un métier pourrait être
    oublié en silence.
    """
    mois, jour = SAISONS[metier]
    for annee in (2026, 2027):
        debut = date(annee, mois, jour)
        if debut.month not in fenetre_mois(metier):
            continue
        assert moment_de_la_saison(metier, debut) == MOMENT_DEBUT
        assert moment_de_la_saison(metier, debut + timedelta(days=30)) == MOMENT_DEBUT
        trente_et_un = moment_de_la_saison(metier, debut + timedelta(days=31))
        # Au 31ᵉ jour : soit on est passé en pleine saison, soit on est déjà
        # sorti de la fenêtre (déneigement). Jamais « début ».
        assert trente_et_un in (MOMENT_EN_COURS, None)


def test_un_metier_sans_saison_ne_situe_rien() -> None:
    """`pavage` n'a pas de saison documentée. On ne sait pas, donc on n'affirme
    rien — et surtout on ne prétend pas qu'elle commence."""
    assert "pavage" not in SAISONS
    assert moment_de_la_saison("pavage", date(2026, 6, 10)) is None


def test_hors_fenetre_aucune_phrase_n_est_choisie() -> None:
    """Le lien entre le moment et la fenêtre, énoncé plutôt que supposé.

    Contrôle négatif : si la borne redevenait « X jours après le début », un
    déneigeur de mai ou de janvier se verrait attribuer un moment alors qu'on
    ne lui écrit pas du tout.
    """
    for jour in (date(2026, 5, 10), date(2027, 1, 10), date(2027, 2, 10)):
        assert jour.month not in fenetre_mois("déneigement")
        assert moment_de_la_saison("déneigement", jour) is None


def test_la_scene_porte_le_moment() -> None:
    """Le moment suit la SCÈNE, pas le dominant : c'est la scène qui fournit
    l'ouvreur, donc c'est sa saison à elle que la phrase date."""
    r = resoudre_metiers(["déneigement résidentiel"], date(2026, 12, 10))
    assert r.scene == "déneigement"
    assert r.scene_moment_saison == MOMENT_DEBUT

    r = resoudre_metiers(["déneigement résidentiel"], date(2026, 9, 10))
    assert r.scene == "déneigement"
    assert r.scene_moment_saison == MOMENT_A_VENIR


def test_sans_scene_aucun_moment() -> None:
    """Aucun métier reconnu → ouvreur générique, aucune date affirmée."""
    r = resoudre_metiers([], date(2026, 12, 10))
    assert r.scene is None
    assert r.scene_moment_saison is None


# ============================================================ 2. LE CHECK ===
#
# Les trois corps, formatés en colonnes COMME LE VRAI GABARIT le fait. Ce
# détail a de l'importance : la phrase se coupe entre deux mots, et un motif
# écrit avec une espace littérale ne matcherait rien sur un vrai courriel.

CORPS = {
    MOMENT_A_VENIR: """Bonjour,

J'ai vu que tu fais du déneigement dans la région de Laval. La saison
approche, pis je me disais que je pourrais te contacter.""",
    MOMENT_DEBUT: """Bonjour,

J'ai vu que tu fais du déneigement dans la région de Laval. C'est le début
de la saison, pis je me disais que je pourrais te contacter.""",
    MOMENT_EN_COURS: """Bonjour,

J'ai vu que tu fais du déneigement dans la région de Laval. Je sais que t'es
dans le gros de la saison, pis je me disais que je pourrais te contacter.""",
}


@pytest.mark.parametrize("moment_reel", sorted(CORPS))
@pytest.mark.parametrize("moment_ecrit", sorted(CORPS))
def test_la_diagonale(moment_ecrit: str, moment_reel: str) -> None:
    """🔴 LE TEST CENTRAL — les neuf cases, pas seulement les trois bonnes.

    Un corps n'est accepté qu'au moment qu'il décrit, et refusé aux deux
    autres. Écrire les trois cas passants sans les six refus laisserait passer
    un check qui dit toujours oui ; écrire les refus sans les cas passants
    laisserait passer un check qui dit toujours non.

    C'est cette matrice qui a manqué à la première version : elle ne savait
    refuser qu'un sens, et un conseil adversarial a reproduit l'autre.
    """
    r = check_saison_au_bon_temps(CORPS[moment_ecrit], moment_saison=moment_reel)
    assert r.passed is (moment_ecrit == moment_reel), (
        f"corps « {moment_ecrit} » jugé au moment « {moment_reel} » : "
        f"attendu {'accepté' if moment_ecrit == moment_reel else 'refusé'}"
    )


def test_l_interdit_est_bien_l_union_des_autres() -> None:
    """La symétrie est CALCULÉE, pas recopiée — ce test le vérifie sur la
    structure elle-même, pour qu'un quatrième moment ajouté demain ne puisse
    pas créer de trou."""
    for moment, interdites in TOURNURES_INTERDITES.items():
        attendu = {
            motif
            for autre, tournures in TOURNURES_PAR_MOMENT.items()
            if autre != moment
            for motif in tournures
        }
        assert set(interdites) == attendu
        # Et surtout : ses PROPRES tournures ne sont jamais dans son interdit.
        assert not (set(TOURNURES_PAR_MOMENT[moment]) & set(interdites))


def test_moment_inconnu_ne_reproche_rien() -> None:
    """None = aucune date à affirmer (pas de scène, pas de saison documentée,
    ou hors fenêtre). Le check ne date jamais une phrase qu'il ne peut pas
    dater."""
    for corps in CORPS.values():
        r = check_saison_au_bon_temps(corps, moment_saison=None)
        assert r.passed is True
        assert not r.matches


def test_le_message_ne_contredit_jamais_le_corps() -> None:
    """Une version antérieure figeait le message sur un seul sens et pouvait
    affirmer l'inverse du corps qu'elle venait de lire. Une note de conformité
    qui ment sur ce qu'elle a vu est pire qu'une note absente."""
    assert "À VENIR" in check_saison_au_bon_temps(
        CORPS[MOMENT_DEBUT], moment_saison=MOMENT_A_VENIR
    ).message
    assert "BIEN ENTAMÉE" in check_saison_au_bon_temps(
        CORPS[MOMENT_DEBUT], moment_saison=MOMENT_EN_COURS
    ).message


def test_le_check_ne_tue_jamais_un_brouillon() -> None:
    """🔴 Sévérité `info`, et c'est la garde la plus importante du fichier.

    Règle William du 2026-08-31 : seul ce que le prospect peut vérifier ET qui
    l'induit en erreur sur un fait tue un brouillon. Passer ce check à `block`
    ou `warn` brûlerait À VIE les 138 déneigeurs joignables en décembre — un
    brouillon refusé quitte le lot pour toujours et son contact reste gelé.
    """
    for moment in list(CORPS) + [None]:
        for corps in CORPS.values():
            assert (
                check_saison_au_bon_temps(corps, moment_saison=moment).severity == "info"
            )


def test_le_check_est_branche_dans_run_all() -> None:
    """Troisième fois cette semaine qu'un contrôle existe sans être appelé.
    Ce test lit la SORTIE de `run_all`, pas le code source — et il vérifie que
    le moment est bien transmis, pas seulement que le check est présent."""
    resultats = run_all(
        CORPS[MOMENT_DEBUT], social_proof_count=0, moment_saison=MOMENT_A_VENIR
    )
    faute = [r for r in resultats if r.name == "saison_au_bon_temps"]
    assert faute, "le check n'est pas dans run_all"
    assert faute[0].passed is False, (
        "run_all doit transmettre `moment_saison` au check — s'il le laisse "
        "tomber, le check passe toujours et la garde est morte"
    )


# ================================== 3. LA CONSIGNE AU GÉNÉRATEUR ============


def _consignes(services: list[str], jour: date) -> str:
    """Le bloc « Métiers résolus » que le prompt reçoit.

    On passe par la VRAIE fonction du pipeline, pas par une reconstruction :
    un garde-fou testé en isolation mais branché nulle part, c'est le défaut
    qui est revenu trois fois cette semaine.
    """
    return bloc_metiers_resolus(services, jour)


@pytest.mark.parametrize(
    "jour,attendu,interdits",
    [
        (date(2026, 9, 10), "La saison approche », est exact", ("VIENT DE COMMENCER",)),
        (date(2026, 12, 10), "VIENT DE COMMENCER", ("La saison approche », est exact",)),
        (
            date(2026, 12, 20),
            "EST BIEN ENTAMÉE",
            ("VIENT DE COMMENCER", "La saison approche », est exact"),
        ),
    ],
)
def test_la_consigne_suit_le_moment(
    jour: date, attendu: str, interdits: tuple[str, ...]
) -> None:
    txt = _consignes(["déneigement résidentiel"], jour)
    assert attendu in txt
    for mauvais in interdits:
        assert mauvais not in txt, f"le {jour} la consigne dit aussi « {mauvais} »"


def test_hors_fenetre_la_consigne_n_affirme_aucune_date() -> None:
    """Le défaut trouvé en relisant le chemin hors-saison : la consigne
    certifiait « La saison approche est exact » juste avant d'annoncer
    qu'aucune fenêtre n'était ouverte. Et c'était la certification qui avait
    tort — hors fenêtre, la saison est à six mois ou plus."""
    txt = _consignes(["déneigement résidentiel"], date(2026, 5, 10))
    assert "fenêtres saisonnières n'est ouverte" in txt
    assert "La saison approche », est exact" not in txt
    assert "VIENT DE COMMENCER" not in txt
    assert "EST BIEN ENTAMÉE" not in txt


def test_les_trois_ouvreurs_existent_dans_le_gabarit() -> None:
    """Le check ne fait que COMPTER : c'est le gabarit qui produit la bonne
    phrase. Si une version disparaît, le modèle n'a plus quoi recopier et le
    check se contenterait de noter l'échec tous les mois."""
    prompt = (
        Path(__file__).parent.parent / "src/prompts/reacti/personalize.md"
    ).read_text(encoding="utf-8")
    assert "La saison" + chr(10) + "approche" in prompt
    assert "C'est le début" + chr(10) + "de la saison" in prompt
    assert "dans le gros de la saison" in prompt
