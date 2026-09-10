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
        # 🔧 RESSERRÉ le 2026-09-07, sur constat d'un conseil de relecture.
        # L'assertion tolérait `None` « au cas où la fenêtre serait déjà
        # fermée » — mais la mesure dit que les SIX métiers sont encore dans
        # leur fenêtre au 31ᵉ jour. Le `None` toléré n'arrivait jamais, et il
        # aurait avalé exactement la régression que ce test existe pour
        # attraper : une fenêtre fermée un jour trop tôt.
        assert moment_de_la_saison(metier, debut + timedelta(days=31)) == MOMENT_EN_COURS


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

    # 🔴 L'ASSERTION SYMÉTRIQUE, ajoutée le 2026-09-07. Sans elle, un `run_all`
    # qui IGNORERAIT son argument et coderait en dur `moment_saison=A_VENIR`
    # passait le test — un conseil de relecture a posé ce mutant et l'a vu
    # survivre. Les deux assertions ensemble le tuent : la première exige un
    # refus sur une combinaison fautive, la seconde un accord sur la bonne.
    bon = [
        r
        for r in run_all(
            CORPS[MOMENT_DEBUT], social_proof_count=0, moment_saison=MOMENT_DEBUT
        )
        if r.name == "saison_au_bon_temps"
    ]
    assert bon and bon[0].passed is True, (
        "run_all rend un verdict indépendant du moment reçu : il ignore son "
        "paramètre"
    )


# ================================== 3. LA CONSIGNE AU GÉNÉRATEUR ============


def _consignes(services: list[str], jour: date) -> str:
    """Le bloc « Métiers résolus » que le prompt reçoit.

    On passe par la VRAIE fonction du pipeline, pas par une reconstruction :
    un garde-fou testé en isolation mais branché nulle part, c'est le défaut
    qui est revenu trois fois cette semaine.
    """
    # `gabarit="C"` : on teste la consigne de l'ouvreur de C et D, donc on
    # appelle comme le vrai chemin appelle. Sans la lettre, la consigne est
    # volontairement tue — voir la docstring de `bloc_metiers_resolus`.
    return bloc_metiers_resolus(services, jour, gabarit="C")


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
    # 🔧 2026-09-07 : deux des trois assertions figeaient la POSITION du retour
    # de ligne, alors que la docstring ne parle que de PRÉSENCE. Un simple
    # re-formatage du gabarit — qui ne change pas un mot de la copie — les
    # aurait cassées. On normalise les espaces avant de chercher.
    plat = " ".join(prompt.split())
    for phrase in (
        "La saison approche",
        "C'est le début de la saison",
        "dans le gros de la saison",
    ):
        assert phrase in plat, f"l'ouvreur « {phrase} » a disparu du gabarit"


# ============================ 4. LE JUGE SÉMANTIQUE =========================


def test_le_juge_connait_les_trois_ouvreurs() -> None:
    """🔴 Trouvé par un conseil de relecture le 2026-09-07 : le mot « saison »
    n'apparaissait PAS UNE SEULE FOIS dans le prompt du juge sémantique.

    Sa règle nº1 exige que toute affirmation soit vérifiable. « Je sais que t'es
    dans le gros de la saison » est une affirmation sur le prospect qu'il ne
    peut recouper avec rien — ni calendrier, ni table de saisons. Verdict
    probable : `blocked`. Et `blocked`, c'est le brouillon qui quitte le lot
    POUR TOUJOURS et le contact gelé À VIE.

    C'est la troisième fois que cette configuration exacte apparaît (le pied de
    page du site, les chiffres de la relance 2), et le correctif est toujours le
    même : **nommer la permission ET la faire figurer dans la liste
    « NE PAS RE-CHECKER »**. L'un sans l'autre ne suffit pas — l'exemple concret
    l'emporte sur la règle abstraite.
    """
    juge = (
        Path(__file__).parent.parent / "src/prompts/compliance.md"
    ).read_text(encoding="utf-8")

    for phrase in (
        "La saison approche",
        "C'est le début de la saison",
        "Je sais que t'es dans le gros de la saison",
    ):
        assert phrase in juge, f"le juge ignore l'ouvreur « {phrase} »"

    assert "1quinquies" in juge, "la permission nommée pour l'ouvreur de saison manque"
    assert "1sexies" in juge, "la permission nommée pour le 2ᵉ temps manque"


def test_le_juge_a_la_permission_ET_l_exemple() -> None:
    """Les deux moitiés du correctif, vérifiées séparément.

    La liste « NE PAS RE-CHECKER » est ce que le juge lit en premier ; la
    permission numérotée est ce qui la justifie. Une permission sans entrée dans
    la liste s'est déjà fait ignorer.
    """
    juge = (
        Path(__file__).parent.parent / "src/prompts/compliance.md"
    ).read_text(encoding="utf-8")
    liste = juge.split("## LÉGITIME")[0]
    assert "ouvreur de saison" in liste
    assert "2ᵉ temps" in liste


def test_le_juge_autorise_le_deuxieme_temps() -> None:
    """« j'ai aussi vu que » concerne 70 % des destinataires. Le juge doit savoir
    que ce n'est pas de la mise en scène de la recherche, sinon il gèle sept
    contacts sur dix."""
    juge = (
        Path(__file__).parent.parent / "src/prompts/compliance.md"
    ).read_text(encoding="utf-8")
    assert "j'ai aussi vu que" in juge.lower()
    assert "Pour le reste de l'année, j'ai aussi vu que tu fais" in juge


# ================= 5. CHAQUE MOTIF EST EXERCÉ AU MOINS UNE FOIS =============
#
# 🔴 Constat d'un conseil de relecture, 2026-09-07 : `test_la_diagonale`
# n'utilise que les trois corps du gabarit, donc UNE tournure par moment. Les
# sept autres motifs n'étaient jamais confrontés à un texte. Une espace
# littérale au lieu de `\s+` — le défaut déjà trouvé une fois dans ce même
# fichier — ou une coquille dans une alternative les aurait rendus muets sans
# qu'un test bronche.
#
# Une phrase d'exemple par motif, écrite comme un vrai corps l'écrirait.

EXEMPLES: dict[str, tuple[str, ...]] = {
    MOMENT_A_VENIR: (
        "la saison approche",
        "la saison qui approche",
        "la saison s'en vient",
        "la saison arrive",
        "la saison qui arrive",
        "avant que la saison commence",
        "avant que ta saison démarre",
    ),
    MOMENT_DEBUT: (
        "c'est le début de la saison",
        "c'est le debut de la saison",
        "le début de la saison",
        "le début de ta saison",
        "la saison vient de commencer",
        "la saison commence à peine",
    ),
    MOMENT_EN_COURS: (
        "t'es en plein dedans",
        "tu es en pleine saison",
        "dans le gros de la saison",
        "dans le gros de ta saison",
        "en pleine saison",
        "la saison est commencée",
        "la saison est déjà commencée",
    ),
}


@pytest.mark.parametrize("moment", sorted(EXEMPLES))
def test_chaque_motif_a_son_exemple(moment: str) -> None:
    """Tout motif déclaré doit être atteint par au moins un exemple.

    C'est le contrôle qui manquait : un motif qu'aucun texte ne touche est
    indistinguable d'un motif cassé.
    """
    import re

    for motif in TOURNURES_PAR_MOMENT[moment]:
        touche = [ex for ex in EXEMPLES[moment] if re.search(motif, ex, re.IGNORECASE)]
        assert touche, f"aucun exemple n'atteint le motif {motif!r} de « {moment} »"


@pytest.mark.parametrize("moment", sorted(EXEMPLES))
def test_chaque_motif_survit_au_retour_de_ligne(moment: str) -> None:
    """🔴 Le contrôle que §5 promettait sans le faire.

    Son en-tête annonce « une espace littérale au lieu de \\s+ les aurait rendus
    muets ». Mais les EXEMPLES sont écrits sur UNE LIGNE, en espaces simples :
    ils ne distinguent pas `\\s+` d'une espace. Dix des onze motifs n'étaient
    donc jamais confrontés au cas qui a réellement mordu — le corps généré est
    formaté en colonnes, et la coupure tombe où elle veut.

    Ici, on coupe à CHAQUE espace de la phrase, tour à tour.
    """
    for exemple in EXEMPLES[moment]:
        mots = exemple.split(" ")
        for i in range(1, len(mots)):
            coupe = " ".join(mots[:i]) + chr(10) + " ".join(mots[i:])
            corps = f"J'ai vu que tu fais du déneigement à Laval. {coupe}, pis bon."
            assert check_saison_au_bon_temps(corps, moment_saison=moment).passed is True
            autre = next(m for m in TOURNURES_PAR_MOMENT if m != moment)
            assert (
                check_saison_au_bon_temps(corps, moment_saison=autre).passed is False
            ), (
                f"« {exemple} » coupé après le mot {i} n'est plus reconnu : le "
                "motif ne tolère pas le retour de ligne"
            )


@pytest.mark.parametrize("moment", sorted(EXEMPLES))
def test_chaque_exemple_est_refuse_aux_deux_autres_moments(moment: str) -> None:
    """Et la contre-épreuve : chaque phrase est acceptée à son moment, refusée
    aux deux autres. Un motif trop large se verrait ici."""
    for exemple in EXEMPLES[moment]:
        corps = f"J'ai vu que tu fais du déneigement à Laval. {exemple}, pis je me disais."
        assert check_saison_au_bon_temps(corps, moment_saison=moment).passed is True, (
            f"« {exemple} » refusé à son propre moment « {moment} »"
        )
        for autre in TOURNURES_PAR_MOMENT:
            if autre == moment:
                continue
            assert check_saison_au_bon_temps(corps, moment_saison=autre).passed is False, (
                f"« {exemple} » passe au moment « {autre} », où il est faux"
            )


# ============ 6. LA GARDE EST ARMÉE SUR LE VRAI CHEMIN (WF-5) ==============


@pytest.fixture
def _env_vert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARMUP_DISABLED", "true")
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture IA")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    monkeypatch.setenv("LCAP_MENTIONS_REDUITES", "true")
    monkeypatch.setenv(
        "INSTANTLY_CAMPAIGN_FOOTER",
        "Couture IA\nPour te désabonner : https://couture-ia.com/unsubscribe",
    )


@pytest.mark.asyncio
async def test_la_garde_saison_est_armee_dans_le_vrai_juge(
    _env_vert: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 MESURÉ PAR MUTATION, et c'est ce qui a rendu ce test nécessaire.

    Un conseil de relecture a supprimé le calcul de `moment_saison` dans
    `compliance.py` : `check_saison_au_bon_temps` recevait alors `None` pour
    100 % des brouillons, sortait par la porte « moment inconnu », et **aucun
    test ne rougissait**. En juin, les 275 contacts en pleine saison auraient
    reçu « La saison approche » sans une seule note de conformité, et
    `pytest -q` aurait affiché tout vert.

    Les tests existants n'exerçaient que `run_all` — la fonction — jamais
    l'ARMEMENT, c'est-à-dire le calcul qui décide quoi lui passer. C'est la
    quatrième fois cette semaine qu'un garde-fou est testé en isolation sans
    l'être sur son chemin de production.
    """
    import src.tools.compliance as comp

    class _Faux(date):
        @classmethod
        def today(cls) -> date:  # type: ignore[override]
            return date(2026, 12, 20)  # déneigement : saison bien entamée

    monkeypatch.setattr(comp, "date", _Faux)

    out = await comp.compliance_check(
        message_id="m-saison",
        body=CORPS[MOMENT_A_VENIR],  # « La saison approche » — faux ce jour-là
        subject="s",
        template_used="C",
        research_json={"services_offered": ["Déneigement résidentiel"]},
        social_proof=[],
        available_slots=[],
        skip_llm=True,
        track="agence-ia",
    )
    # ⚠️ `deterministic_infos`, PAS `warnings` ni `blockers` : la garde est en
    # sévérité `info`, donc elle annote sans tuer le brouillon. Chercher dans
    # les warnings aurait fait échouer ce test pour la mauvaise raison — et
    # laissé croire que le constat du conseil était confirmé alors qu'il ne
    # l'était pas encore.
    notes = {n["name"] for n in (out.deterministic_infos or [])}
    assert "saison_au_bon_temps" in notes, (
        "le juge n'arme pas la garde saison : elle est muette sur le vrai "
        f"chemin. Notes vues : {notes or 'aucune'}"
    )

    # 🔴 L'AUTRE SENS, ajouté le 2026-09-07 : sans lui, un `moment_saison` codé
    # en dur survivait au test — un conseil de relecture a posé le mutant et l'a
    # vu passer. Le même corps, jugé un jour où la saison N'EST PAS commencée,
    # ne doit produire AUCUNE note : c'est la preuve que le moment est bien
    # calculé depuis la date, et pas figé.
    class _Avant(date):
        @classmethod
        def today(cls) -> date:  # type: ignore[override]
            return date(2026, 9, 10)  # déneigement : saison à venir

    monkeypatch.setattr(comp, "date", _Avant)
    avant = await comp.compliance_check(
        message_id="m-saison-2",
        body=CORPS[MOMENT_A_VENIR],  # « La saison approche » — exact ce jour-là
        subject="s",
        template_used="C",
        research_json={"services_offered": ["Déneigement résidentiel"]},
        social_proof=[],
        available_slots=[],
        skip_llm=True,
        track="agence-ia",
    )
    assert "saison_au_bon_temps" not in {
        n["name"] for n in (avant.deterministic_infos or [])
    }, "le moment est figé au lieu d'être calculé depuis la date du jour"
    # Et le brouillon PART quand même : c'est toute la raison du `info`.
    assert out.send_decision != "DO_NOT_SEND", (
        "une remarque de forme sur la saison ne doit jamais tuer un brouillon "
        "— ça gèlerait le contact à vie"
    )


def _notes(out) -> set[str]:
    return {n["name"] for n in (out.deterministic_infos or [])}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gabarit,attendu",
    [("C", True), ("D", True), ("A", False), ("B", False)],
)
async def test_le_controle_de_saison_ne_vise_que_C_et_D(
    _env_vert: None, monkeypatch: pytest.MonkeyPatch, gabarit: str, attendu: bool
) -> None:
    """🔴 La restriction posée le 2026-09-07, et sa contre-épreuve.

    Seuls C et D ont un premier paragraphe FIXE qui situe la saison. L'ouvreur
    de A et B est écrit par le modèle et ne porte aucune des trois formulations.

    Ce test remplace une garde qui ne marchait dans aucun sens : elle comparait
    le métier de la scène au corps ENTIER, donc sur C et D elle ne se
    déclenchait jamais (le 2ᵉ temps énumère justement les autres métiers) et sur
    A et B elle se déclenchait toujours (« paysagement » n'est pas dans
    « aménagement paysager »). Exactement l'inverse de l'intention.
    """
    import src.tools.compliance as comp

    class _Faux(date):
        @classmethod
        def today(cls) -> date:  # type: ignore[override]
            return date(2026, 12, 20)  # déneigement : saison bien entamée

    monkeypatch.setattr(comp, "date", _Faux)
    out = await comp.compliance_check(
        message_id=f"m-{gabarit}",
        body=CORPS[MOMENT_A_VENIR],
        subject="s",
        template_used=gabarit,
        research_json={"services_offered": ["Déneigement résidentiel"]},
        social_proof=[],
        available_slots=[],
        skip_llm=True,
        track="agence-ia",
    )
    assert ("saison_au_bon_temps" in _notes(out)) is attendu, (
        f"gabarit {gabarit} : la garde saison devrait "
        f"{'être armée' if attendu else 'rester muette'}"
    )


def test_tous_les_messages_du_check_sont_assertes() -> None:
    """🔴 Constat d'un conseil : quatre des six messages n'étaient assertés
    nulle part. Une note de conformité peut donc mentir sur ce qu'elle a vu —
    et une note qui ment est pire qu'une note absente, parce qu'on la croit.

    On les vérifie tous les six, dans les deux états (faute / rien à signaler).
    """
    from src.lib.compliance_checks import _FAUTE_PAR_MOMENT, _RIEN_PAR_MOMENT

    attendus_faute = {
        MOMENT_A_VENIR: "À VENIR",
        MOMENT_DEBUT: "QUI VIENT DE COMMENCER",
        MOMENT_EN_COURS: "BIEN ENTAMÉE",
    }
    for moment, fragment in attendus_faute.items():
        assert fragment in _FAUTE_PAR_MOMENT[moment]
        # Et le message SORT vraiment, avec ce fragment.
        autre = next(m for m in CORPS if m != moment)
        r = check_saison_au_bon_temps(CORPS[autre], moment_saison=moment)
        assert r.passed is False and fragment in r.message

    attendus_rien = {
        MOMENT_A_VENIR: "à venir",
        MOMENT_DEBUT: "vient de commencer",
        MOMENT_EN_COURS: "bien entamée",
    }
    for moment, fragment in attendus_rien.items():
        assert fragment in _RIEN_PAR_MOMENT[moment]
        r = check_saison_au_bon_temps(CORPS[moment], moment_saison=moment)
        assert r.passed is True and fragment in r.message


def test_la_consigne_de_saison_ne_part_jamais_vers_A_ni_B() -> None:
    """🔴 Trouvé par un conseil de vérification le 2026-09-08.

    La consigne dit « emploie la 2ᵉ version de l'ouvreur de C et D » — une
    phrase FIXE, qui n'existe pas dans A ni B, dont l'ouvreur est ÉCRIT par le
    modèle. Servie à un rédacteur de A, elle l'invitait à recopier du gabarit
    dans un paragraphe qu'il doit composer.

    Et le même jour, `check_saison_au_bon_temps` avait été restreint à C et D :
    **plus rien ne l'aurait rattrapé.** Deux changements corrects séparément,
    qui ouvraient un trou ensemble.
    """
    for gabarit in ("A", "B"):
        txt = bloc_metiers_resolus(
            ["Déneigement résidentiel"], date(2026, 12, 10), gabarit=gabarit
        )
        assert "ouvreur de C et D" not in txt, (
            f"le gabarit {gabarit} reçoit une consigne sur une phrase qu'il n'a pas"
        )
        assert "VIENT DE COMMENCER" not in txt

    # Contre-épreuve : C et D la reçoivent toujours.
    for gabarit in ("C", "D"):
        txt = bloc_metiers_resolus(
            ["Déneigement résidentiel"], date(2026, 12, 10), gabarit=gabarit
        )
        assert "VIENT DE COMMENCER" in txt


def test_le_metier_est_servi_avec_son_article_aux_gabarits_fixes() -> None:
    """La tête fixe dit « tu fais {METIER} » et le nom de famille est nu dans la
    table. « de la tonte » mais « du paysagement » — l'article n'est pas le même,
    et le rédacteur n'a pas à le deviner dans une phrase qu'on lui demande de
    recopier virgule pour virgule."""
    tonte = bloc_metiers_resolus(["Tonte de pelouse"], date(2026, 5, 10), gabarit="C")
    assert "**de la tonte**" in tonte

    pays = bloc_metiers_resolus(
        ["Aménagement paysager"], date(2026, 5, 10), gabarit="D"
    )
    assert "**du paysagement**" in pays

    # A et B composent leur ouvreur : ils n'ont pas de trou à remplir.
    a = bloc_metiers_resolus(["Tonte de pelouse"], date(2026, 5, 10), gabarit="A")
    assert "recopier TEL QUEL" not in a
