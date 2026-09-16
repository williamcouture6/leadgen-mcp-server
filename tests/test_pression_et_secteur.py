"""Deux corrections du dictionnaire, décidées par William le 2026-09-15.

🔴 ELLES VIENNENT D'UNE MESURE, pas d'une relecture. Premier passage de WF-5 sur
la copie AC1 : 9 refus sur 20, dont **3 blocages**. Les trois s'expliquent
entièrement par le dictionnaire, pas par le rédacteur — qui utilisait
fidèlement une résolution de métiers fausse.

    Aménagement GF · Terrassement S.H. → « déneigement » venait du SECTEUR
    Pavage Gadbois                     → « lavage de vitres » venait de « pression »

⚠️ Le piège à ne pas refaire : la première idée était de resserrer la consigne
du RÉDACTEUR pour qu'il cesse d'inventer. Il n'inventait rien. Un contrôle
construit sur la résolution existante a été mesuré contre les 20 brouillons
déjà jugés — **0 détection sur 9 refus** — parce qu'il comparait le courriel à
la source qui était elle-même fausse. Vérifier une traduction avec le
dictionnaire qui a servi à la faire ne trouve jamais rien.
"""
from __future__ import annotations

from datetime import date

from src.lib.metiers import classer_services, metiers_nommes, resoudre_metiers


class TestPressionSeule:
    """« pression » SEUL ne compte pas — décision William du 2026-09-15.

    La racine avait été posée le 2026-08-30 pour le *lavage à pression* : même
    client, même saison, même équipement. Le raisonnement tient. Ce qui ne
    tenait pas, c'est le mot tout seul.
    """

    def test_le_vrai_lavage_a_pression_compte_toujours(self) -> None:
        """Les 51 libellés légitimes mesurés en base contiennent tous
        « lavage » ou « nettoyage ». Les formes varient ; le sens non."""
        for libelle in (
            "Lavage à pression",
            "Lavage à la pression",
            "Lavage à haute pression",
            "Lavage haute pression",
            "Lavage sous pression (pressure washing)",
            "Nettoyage haute pression",
            "Nettoyage à haute pression",
            "Nettoyage à pression",
            "Lavage extérieur à pression",
            "Nettoyage à pression ou doux",
            "Lavage à pression (patios, entrées, façades)",
        ):
            assert "lavage de vitres" in metiers_nommes(libelle), libelle

    def test_pression_sans_lavage_ne_compte_plus(self) -> None:
        """🔴 LES DEUX CAS RÉELS QUI ONT CAUSÉ UN BLOCAGE.

        « Application de scellant sous pression » est un service d'asphalte.
        Il faisait de Pavage Gadbois une laveuse de vitres — donc le rédacteur
        lui parlait de vitres, et le juge bloquait pour fait inventé.
        """
        assert "lavage de vitres" not in metiers_nommes(
            "Application de scellant sous pression"
        )
        assert "lavage de vitres" not in metiers_nommes("Test de pression")

    def test_repression_ne_matche_pas_et_n_a_jamais_matche(self) -> None:
        """La frontière de mot protégeait déjà ce cas — on vérifie qu'on ne
        l'a pas cassé en changeant le motif."""
        assert "lavage de vitres" not in metiers_nommes(
            "Contrôle et répression d'insectes et de rongeurs"
        )

    def test_les_vitres_et_fenetres_restent_intactes(self) -> None:
        assert "lavage de vitres" in metiers_nommes("Lavage de vitres résidentiel")
        assert "lavage de vitres" in metiers_nommes("Nettoyage de fenêtres")


class TestSecteurEnDernierRecours:
    """Le mot-clé de sourcing ne compte QUE si les services ne disent rien.

    `industry` n'est pas ce que l'entreprise vend : c'est le terme tapé pour la
    trouver sur Google Maps. Il a été branché le 2026-09-14 pour un vrai
    besoin — une entreprise dont aucun service ne porte de métier reconnu
    n'avait aucune fenêtre saisonnière. Ce besoin demeure ; ce qui change,
    c'est qu'il ne prend plus la parole quand les services, eux, ont parlé.
    """

    def test_le_secteur_sert_quand_rien_d_autre_ne_parle(self) -> None:
        """Le cas pour lequel le mécanisme a été créé. Mesuré : 3 entreprises
        sur 506 sont dans ce cas — sans lui, elles n'ont aucun métier."""
        c = classer_services(
            ["Répulsifs anti-oiseaux sur mesure", "Dispositifs anti-oiseaux"],
            "entreprise d'extermination",
        )
        assert "extermination" in c.metiers

    def test_le_secteur_se_tait_quand_une_saison_est_deja_connue(self) -> None:
        """🔴 LE CAS TERRASSEMENT S.H., qui a produit un blocage.

        Ses services : terrassement, aménagement paysager. Aucun déneigement.
        Elle avait été trouvée en cherchant « entrepreneur en déneigement », et
        ce mot-clé lui ajoutait le métier — donc le courriel lui parlait de
        déneigement, et le juge bloquait.

        Le critère retenu est la SAISON, pas la simple présence d'un métier :
        `paysagement` en a une, donc la fenêtre est déjà décidée et le secteur
        n'a rien à ajouter. Voir le test suivant pour le cas inverse.
        """
        c = classer_services(
            ["Terrassement", "Aménagement paysager", "Soumissions de travaux extérieurs"],
            "entrepreneur en déneigement",
        )
        assert "déneigement" not in c.metiers
        assert {"excavation", "paysagement"} <= set(c.metiers)

    def test_le_secteur_parle_encore_quand_aucun_metier_n_a_de_saison(self) -> None:
        """🔴 LE CAS NIWA — et la raison pour laquelle le critère est la saison.

        Ses libellés ne donnent que `pavage`, un métier sans saison, qui n'ouvre
        aucune fenêtre : sans le secteur elle serait écartée pour toujours. La
        première version de cette règle — « le secteur se tait dès que les
        services parlent » — la rendait injoignable à vie, ainsi que Nettoyeur
        de la Cité. Mesuré avant de trancher.
        """
        c = classer_services(
            ["Pavage", "Trottoirs", "Terrasses sur mesure"], "paysagiste"
        )
        assert "paysagement" in c.metiers
        assert c.metiers[0] == "pavage", "le secteur ne doit pas voler le dominant"

    def test_le_secteur_ne_change_rien_quand_il_est_deja_dans_les_services(self) -> None:
        """Une entreprise qui déclare vraiment le métier du mot-clé n'est pas
        touchée — c'est le cas de la grande majorité (462 sur 478 mesurées)."""
        c = classer_services(["Déneigement résidentiel"], "entrepreneur en déneigement")
        assert "déneigement" in c.metiers

    def test_resoudre_metiers_herite_de_la_regle(self) -> None:
        """La règle vit dans `classer_services` ; `resoudre_metiers` en dérive.
        Deux chemins concurrents divergeraient."""
        r = resoudre_metiers(
            ["Terrassement", "Aménagement paysager"], date(2026, 11, 15),
            "entrepreneur en déneigement",
        )
        assert "déneigement" not in r.metiers
