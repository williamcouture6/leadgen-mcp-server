"""L'alerte de famine de l'inventaire de sourcing.

Exigée par William le 2026-09-15, DANS le même lot que la table 0070 : « il
faut faire en sorte de mettre une alerte slack de famine quand wf-1 va manquer
de leads dans la nouvelle table ». Au futur — l'alerte doit PRÉVENIR, pas
constater. Le jour où la file est vide, les jours sont déjà perdus.

🔴 CE QUE CES TESTS PROTÈGENT, dans l'ordre d'importance :

1. LE TROISIÈME ZÉRO. `_doit_alerter_famine` (WF-4) distingue déjà deux zéros :
   zéro sur une file vide est une fin de liste, zéro sur une file pleine est une
   panne. L'inventaire en a un TROISIÈME, et c'est celui d'aujourd'hui — vide
   parce que le balayeur n'existe pas encore. Crier là-dessus chaque matin
   apprendrait à ignorer l'alerte avant même qu'elle serve.

2. UN COMPTEUR GLOBAL MENT. 5 800 entreprises en inventaire dont 5 700 hors
   saison, c'est une famine qui s'affiche en pleine forme. Le chiffre qui
   compte est « piochable MAINTENANT ». C'est le défaut que PT3 a déjà corrigé
   ailleurs (le compteur aveugle des leads chauds) et que la 0065 a corrigé
   dans la vue des refus (190 entreprises hors saison annoncées « rien ne s'y
   oppose »).

3. LE FILTRE DE SECTEUR DOIT VRAIMENT MATCHER. Nos secteurs contiennent des
   espaces. Un littéral de tableau Postgres sans guillemets fait lire quatre
   éléments à `entrepreneur en déneigement`, le filtre ne matche rien, et
   l'alerte annonce une famine parfaitement fausse — la pire des sorties, parce
   qu'elle est crédible.

4. UNE PANNE NE RESSEMBLE PAS À UN CALME PLAT. Lecture tombée et passe de
   balayage morte en vol ont chacune leur message.
"""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# ---------------------------------------------------------------- la saison

def test_septembre_ne_prepare_que_le_deneigement() -> None:
    """CLAUDE.md : « de septembre à décembre, seul le déneigement est ouvert »."""
    from src.tools.db import secteurs_a_preparer

    assert secteurs_a_preparer(date(2026, 9, 16)) == ["entrepreneur en déneigement"]


def test_decembre_ouvre_le_printemps_grace_au_mois_d_avance() -> None:
    """Les fenêtres de printemps s'ouvrent en janvier ; le mois d'avance les
    attrape en décembre. C'est TOUTE la raison d'être de
    `MOIS_AVANCE_PREPARATION` : WF-3 traite ~20 fiches/jour, donc préparer un
    métier le mois de son ouverture revient à le manquer."""
    from src.tools.db import secteurs_a_preparer

    secteurs = secteurs_a_preparer(date(2026, 12, 15))
    assert "paysagiste" in secteurs
    assert "entrepreneur en déneigement" in secteurs


def test_un_secteur_sans_metier_reconnu_est_ecarte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Et PAS inclus « au cas où ». L'inclure ferait préparer toute l'année un
    secteur dont on ne sait rien — c'est la décision William du 2026-09-14 qui
    a écarté `metier_inconnu` de la démarchabilité.

    ⚠️ `monkeypatch.setitem` et pas une affectation avec try/finally : le
    catalogue est un état de MODULE, partagé par tout le processus de test. Un
    `finally` qui ne s'exécute pas — interruption, erreur dans le corps — le
    laisserait cassé pour tous les tests suivants, et la panne se manifesterait
    ailleurs que là où elle a été causée.
    """
    from src.tools import db as dbt

    monkeypatch.setitem(
        dbt._CATALOGS,
        "agence-ia",
        {"commerce_local": ["quelque chose qui n'est pas un métier"]},
    )
    assert dbt.secteurs_a_preparer(date(2026, 9, 16)) == []


# -------------------------------------------------- le littéral de tableau

def test_le_litteral_protege_les_secteurs_a_espaces() -> None:
    """Sans guillemets, Postgres lit quatre éléments au lieu d'un, le filtre ne
    matche rien, et l'alerte annonce une famine fausse."""
    from src.http_api import _litteral_tableau_pg

    rendu = _litteral_tableau_pg(["entrepreneur en déneigement", "tonte de gazon"])
    assert rendu == '{"entrepreneur en déneigement","tonte de gazon"}'


def test_le_litteral_echappe_les_guillemets() -> None:
    from src.http_api import _litteral_tableau_pg

    assert _litteral_tableau_pg(['a"b']) == '{"a\\"b"}'


# ------------------------------------------------------------- le verdict

def _etat(**kw):
    from src.http_api import EtatInventaire

    return EtatInventaire(**kw)


def test_inventaire_vide_et_jamais_balaye_nest_PAS_une_famine() -> None:
    """LE TEST LE PLUS IMPORTANT DU FICHIER. C'est l'état du 2026-09-16 : la
    table existe, le balayeur non. Une alerte quotidienne ici s'userait avant
    d'avoir jamais servi."""
    from src.http_api import _verdict_inventaire

    assert _verdict_inventaire(_etat(total=0, dernier_balayage=None)) == "jamais_balaye"


def test_plus_rien_a_piocher_apres_un_balayage_EST_une_famine() -> None:
    """Le même zéro, mais après qu'un balayage ait tourné : là, c'est une vraie
    fin de file et il faut le dire."""
    from src.http_api import _verdict_inventaire

    etat = _etat(total=800, piochable=0, dernier_balayage="2026-09-10T10:00:00Z")
    assert _verdict_inventaire(etat) == "vide"


def test_un_gros_stock_hors_saison_ne_sauve_pas_du_verdict() -> None:
    """5 800 connues dont 0 piochable reste une famine. Un compteur global
    dirait que tout va bien."""
    from src.http_api import _verdict_inventaire

    etat = _etat(total=5800, piochable=0, dernier_balayage="2026-09-10T10:00:00Z")
    assert _verdict_inventaire(etat) == "vide"


@pytest.mark.parametrize(
    ("piochable", "rythme", "attendu"),
    [
        (20, 20.0, "urgence"),   # 1 jour
        (100, 20.0, "famine"),   # 5 jours
        (300, 20.0, "ok"),       # 15 jours
    ],
)
def test_le_verdict_suit_les_jours_de_file(piochable, rythme, attendu) -> None:
    from src.http_api import _verdict_inventaire

    etat = _etat(
        total=piochable, piochable=piochable, rythme_par_jour=rythme,
        jours_de_file=piochable / rythme, dernier_balayage="2026-09-10T10:00:00Z",
    )
    assert _verdict_inventaire(etat) == attendu


def test_une_passe_morte_en_vol_prime_sur_le_reste() -> None:
    """Une passe 'running' depuis des heures est une panne, et elle doit se dire
    même quand la file est confortable — c'est le mode d'échec qui a laissé le
    sourcing mort cinq semaines à l'été 2026."""
    from src.http_api import _verdict_inventaire

    etat = _etat(
        total=800, piochable=800, rythme_par_jour=20.0, jours_de_file=40.0,
        balayages_orphelins=1, dernier_balayage="2026-09-16T08:00:00Z",
    )
    assert _verdict_inventaire(etat) == "balayage_orphelin"


def test_une_lecture_tombee_ne_ressemble_pas_a_un_calme_plat() -> None:
    """Se taire ici ferait dépendre l'alerte de la santé de la lecture qui sert
    à la justifier — l'argument est déjà écrit dans `_alerter_famine_wf4`."""
    from src.http_api import _verdict_inventaire

    assert _verdict_inventaire(_etat(lu=False, total=0)) == "illisible"


# ------------------------------------------------------------- le message

def test_du_stock_sans_rythme_mesurable_ne_declenche_PAS_d_alerte() -> None:
    """🔴 DÉFAUT RÉEL, ATTRAPÉ EN PRODUCTION LE 2026-09-16. Juste après le
    premier balayage — 591 entreprises piochables, aucune hydratation encore
    faite donc aucun rythme mesurable — la première version rendait « stock
    faible » et aurait crié tous les matins sur une file abondante.

    Sans rythme, on ne peut pas parler en jours. Alors on n'en parle pas : la
    ligne du résumé porte le chiffre, #alertes reste silencieux."""
    from src.http_api import _verdict_inventaire

    etat = _etat(
        total=842, piochable=591, rythme_par_jour=None, jours_de_file=None,
        dernier_balayage="2026-09-16T01:00:00Z",
    )
    assert _verdict_inventaire(etat) == "sans_rythme"


def test_un_stock_maigre_crie_meme_sans_rythme() -> None:
    """La contrepartie : 12 fiches sont maigres à n'importe quel rythme
    plausible. Se taire là serait le défaut inverse."""
    from src.http_api import _verdict_inventaire

    etat = _etat(
        total=800, piochable=12, rythme_par_jour=None, jours_de_file=None,
        dernier_balayage="2026-09-16T01:00:00Z",
    )
    assert _verdict_inventaire(etat) == "rythme_inconnu"


@pytest.mark.anyio
async def test_aucune_alerte_quand_tout_va_bien_ni_avant_le_premier_balayage() -> None:
    from src.http_api import _alerter_famine_inventaire

    for verdict in ("ok", "jamais_balaye", "sans_rythme"):
        assert await _alerter_famine_inventaire(_etat(), verdict) is None


@pytest.mark.anyio
async def test_le_message_nomme_les_chiffres_et_l_action(monkeypatch) -> None:
    """Une alerte qui ne dit pas quoi faire se fait ignorer. Celle-ci doit
    porter : combien il reste, combien de jours, quels secteurs comptent, et
    quelles régions balayer."""
    from src import http_api
    from src.lib import slack as slack_lib

    captures: list[str] = []

    async def _faux_notify(*, text, blocks=None, context=None, category=None):
        captures.append(text)
        assert category == "alerts", "la famine doit réveiller #alertes"
        return True

    monkeypatch.setattr(slack_lib, "notify", _faux_notify)

    etat = _etat(
        total=5800, piochable=48, rythme_par_jour=20.0, jours_de_file=48 / 20,
        secteurs_a_preparer=["entrepreneur en déneigement"],
        secteurs_a_balayer={"entrepreneur en déneigement": ["Longueuil", "Sherbrooke"]},
        dernier_balayage="2026-09-10T10:00:00Z",
    )
    assert await http_api._alerter_famine_inventaire(etat, "famine") is True

    msg = captures[0]
    assert "48" in msg
    assert "2.4" in msg
    assert "entrepreneur en déneigement" in msg
    # 🔴 L'ACTION DOIT NOMMER LE SECTEUR, PAS SEULEMENT LA RÉGION. Simulé sur
    # décembre le 2026-09-20 : l'alerte criait bien « plus rien à piocher » mais
    # concluait « toutes les régions ont été balayées, rebalayer plus finement »
    # — un conseil FAUX. Les dix régions l'avaient été pour le DÉNEIGEMENT ; ce
    # qu'il fallait, c'était balayer les six métiers dont la fenêtre venait de
    # s'ouvrir. Une alerte qui dit la mauvaise action se paie trois mois plus
    # tard, quand le contexte est oublié.
    assert "Longueuil" in msg
    assert "balayage.py" in msg, "l'alerte doit donner la commande a lancer"
    assert "--secteur" in msg
    # LE point 2 : le total ne doit jamais s'afficher seul, sans le hors-saison.
    assert "DORMENT" in msg  # le total ne s'affiche jamais seul


@pytest.mark.anyio
async def test_le_message_dit_quoi_faire_meme_quand_tout_est_balaye(monkeypatch) -> None:
    """« À faire : rien » serait pire que pas d'alerte du tout."""
    from src import http_api
    from src.lib import slack as slack_lib

    captures: list[str] = []

    async def _faux_notify(*, text, blocks=None, context=None, category=None):
        captures.append(text)
        return True

    monkeypatch.setattr(slack_lib, "notify", _faux_notify)

    etat = _etat(
        total=800, piochable=3, rythme_par_jour=20.0, jours_de_file=0.15,
        secteurs_a_preparer=["entrepreneur en déneigement"],
        regions_jamais_balayees=[], dernier_balayage="2026-09-10T10:00:00Z",
    )
    await http_api._alerter_famine_inventaire(etat, "urgence")
    assert "rebalayer plus finement" in captures[0]


@pytest.mark.anyio
async def test_une_alerte_perdue_se_signale(monkeypatch) -> None:
    """Une alerte perdue qui se croit partie est le pire des deux mondes —
    même règle que `_alerter_wf5` et `_alerter_famine_wf4`."""
    from src import http_api
    from src.lib import slack as slack_lib

    async def _faux_notify(**_kw):
        return False

    monkeypatch.setattr(slack_lib, "notify", _faux_notify)
    etat = _etat(total=10, piochable=0, dernier_balayage="2026-09-10T10:00:00Z")
    assert await http_api._alerter_famine_inventaire(etat, "vide") is False


# ------------------------------------------------- la ligne du résumé

def test_la_ligne_du_resume_dit_l_etat_d_aujourd_hui_sans_alarmer() -> None:
    from src.http_api import _ligne_resume_inventaire

    ligne = _ligne_resume_inventaire(_etat(total=0), "jamais_balaye")
    assert "aucun balayage" in ligne
    assert "🚨" not in ligne


def test_la_ligne_du_resume_separe_le_stock_du_piochable() -> None:
    from src.http_api import _ligne_resume_inventaire

    etat = _etat(
        total=5800, piochable=47, rythme_par_jour=20.0, jours_de_file=2.35,
        par_secteur={"entrepreneur en déneigement": 47},
    )
    ligne = _ligne_resume_inventaire(etat, "famine")
    assert "5800 connues" in ligne
    assert "47 piochables" in ligne
    assert "5753 en attente de leur saison" in ligne
