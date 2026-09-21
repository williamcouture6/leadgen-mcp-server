"""Le nom d'entreprise : UNE valeur, résolue au même endroit pour les deux.

🔴 LE DÉFAUT QUE CE FICHIER FERME — DEUX SOURCES DE VÉRITÉ POUR UN SEUL NOM.

Mesuré le 2026-09-17 sur les 343 entreprises ayant un contact joignable :
**67 (20 %)** portaient un nom que le RÉDACTEUR imprimait différemment de ce
que le JUGE lisait.

  · le rédacteur recevait `companies.name` (Google Places), coupé au premier
    séparateur par `nom_commercial` ;
  · le juge ne recevait AUCUN nom — il déduisait le vrai du
    `research_json.company_summary`, écrit par un modèle qui lisait le site.

Rien ne les reliait. Trois brouillons refusés le même soir, dont un BLOQUÉ :
« Vitres & Gouttières - 123Entretien » → le courriel a attribué la note Google
à « Vitres & Gouttières », et le juge a crié au fait inventé. Les deux avaient
raison chacun de son côté.

Depuis la migration 0072 : `companies.nom_usage` porte le nom lu sur le site,
`nom_usage_fiable` le filtre, `nom_a_imprimer` le résout, et le bloc
« Faits vérifiés » — servi aux DEUX — le transporte.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.lib.avis import (
    bloc_faits_verifies,
    nom_a_imprimer,
    nom_commercial,
    nom_usage_fiable,
)
from src.tools.research import sans_diagnostic
from tests.fixtures.corps_ac1 import (
    CORPS_A_REPLI_AVIS,
    SIGNATURE_COMPTE_INSTANTLY,
)


# ── La garde : ce qu'elle accepte ───────────────────────────────────────────

@pytest.mark.parametrize(
    ("cas", "candidat", "nom_brut", "attendu"),
    [
        # 🔴 LE CAS QUI A BLOQUÉ UN BROUILLON. Le vrai nom était à DROITE du
        # séparateur — `nom_commercial` prenait la gauche et fabriquait un nom.
        ("vrai nom à droite", "123Entretien",
         "Vitres & Gouttières - 123Entretien", "123Entretien"),
        # Bourrage À L'INTÉRIEUR : rien à couper, 14 des 20 pires cas.
        ("bourrage interne", "Paysagement Gagné",
         "Paysagement Déneigement Gagné", "Paysagement Gagné"),
        # Séparateur que `nom_commercial` ne connaît pas.
        ("puce ronde", "Jolie Québec",
         "lavage des vitres Entretien Ménager •Jolie Québec", "Jolie Québec"),
        # Noms courts légitimes : ils ne doivent pas être pris pour du bruit.
        ("nom court", "ELIT", "ELIT", "ELIT"),
        ("nom avec mot-outil", "À Point", "À Point", "À Point"),
        # Le contre-exemple qui prouve qu'aucune règle de CÔTÉ ne marche : ici
        # le bon nom est à droite, alors que « Vitres Royal - Lavages de
        # Vitres » a le sien à gauche.
        ("l'autre côté", "Morin Extermination",
         "Exterminateur Laval - Morin Extermination Inc.", "Morin Extermination"),
        # Espace en plus côté site, collé côté Google.
        ("forme collée", "123 Entretien",
         "Vitres & Gouttières - 123Entretien", "123 Entretien"),
    ],
)
def test_la_garde_accepte_un_nom_prouvable(
    cas: str, candidat: str, nom_brut: str, attendu: str
) -> None:
    assert nom_usage_fiable(candidat, nom_brut) == attendu, cas


@pytest.mark.parametrize(
    ("cas", "candidat", "nom_brut"),
    [
        # 🔴 MESURÉ EN BASE : le modèle a vraiment rendu « Les » comme nom.
        ("aucun mot significatif", "Les",
         "Lavage de vitres Services Aqua-Verre inc."),
        ("que des mots d'enveloppe", "Services",
         "Services de Piscine AquanNord Inc."),
        # 🔴 MESURÉ EN BASE aussi. Le token « n » est justement ce qui prouve
        # que le candidat dit autre chose — d'où le choix de GARDER les tokens
        # d'une lettre, contrairement au tokeniseur de `research.py`.
        ("nom de personne", "N. Théorêt", "Déneigement Théoret"),
        # Le modèle recopie le libellé Google bourré, qu'il a sous les yeux.
        # Sans la règle du séparateur, l'inclusion l'accepterait — et on
        # imprimerait le bourrage ENTIER, pire qu'avant.
        ("le libellé recopié", "Vitres & Gouttières - 123Entretien",
         "Vitres & Gouttières - 123Entretien"),
        # Vrai peut-être, mais improuvable : on ne l'imprime pas.
        ("rien en commun", "AquaVerre", "Lavage de vitres Montréal"),
        ("vide", "", "ELIT"),
        ("absent", None, "ELIT"),
    ],
)
def test_la_garde_refuse_ce_qui_ne_se_prouve_pas(
    cas: str, candidat: str | None, nom_brut: str
) -> None:
    assert nom_usage_fiable(candidat, nom_brut) is None, cas


@pytest.mark.parametrize(
    ("cas", "candidat", "nom_brut", "attendu"),
    [
        # 🔴 LES DEUX NETTOYAGES TROUVÉS PAR UN GALOP D'ESSAI SUR 18 FICHES
        # RÉELLES, avant tout déploiement. La garde acceptait des noms exacts
        # mais imprononçables dans un courriel.
        ("la glose entre parenthèses", "Symetric (Cèdres Gatineau)",
         "Cèdres Gatineau Symetric | Entretien haie de cèdres", "Symetric"),
        ("le suffixe légal", "Les Entreprises J.S. Lauzon Inc.",
         "Les Entreprises J.S. Lauzon | Excavation, Fosse septique",
         "Les Entreprises J.S. Lauzon"),
    ],
)
def test_la_garde_nettoie_ce_qui_ne_se_dit_pas(
    cas: str, candidat: str, nom_brut: str, attendu: str
) -> None:
    assert nom_usage_fiable(candidat, nom_brut) == attendu, cas


# ── La résolution : un seul point, pour les deux acteurs ────────────────────

# Les valeurs que le découpage rendait AVANT la 0072, relevées sur des fiches
# réelles. Écrites en dur : comparer `nom_a_imprimer` à `nom_commercial`
# reviendrait à réciter la ligne de code, et resterait vert le jour où
# `nom_commercial` change — ce que le conseil de relecture a relevé.
REPLI_ATTENDU = {
    "Vitres Ultra Nettes -lavage de vitres résidentiel": "Vitres Ultra Nettes",
    "Net-Pro | Lavage de vitres et nettoyage de gouttières": "Net-Pro",
    "Piscines Élégance, Québec inc.": "Piscines Élégance",
    "Chasse-Neige Express": "Chasse-Neige Express",
    "Les Entreprises J.S. Lauzon | Excavation, Fosse septique":
        "Les Entreprises J.S. Lauzon",
}


@pytest.mark.parametrize(("brut", "attendu"), sorted(REPLI_ATTENDU.items()))
def test_le_repli_est_exactement_le_comportement_d_avant(
    brut: str, attendu: str
) -> None:
    """🔴 LE POINT QUI REND LE DÉPLOIEMENT SÛR.

    Au jour 1, `nom_usage` est NULL sur les 1132 fiches. Si le repli n'était pas
    identique au découpage d'avant, la 0072 changerait le nom imprimé à TOUTES
    les entreprises d'un coup, sans que personne l'ait décidé.

    ⚠️ Les valeurs sont écrites en dur, pas dérivées de `nom_commercial` : une
    première version comparait les deux fonctions, ce qui récitait la
    délégation au lieu de la vérifier. Le conseil de relecture l'a relevé.
    """
    assert nom_commercial(brut) == attendu, "le découpage lui-même a changé"
    # Les trois portes de sortie vers le repli : clé absente, None, blancs.
    assert nom_a_imprimer({"name": brut}) == attendu
    assert nom_a_imprimer({"name": brut, "nom_usage": None}) == attendu
    assert nom_a_imprimer({"name": brut, "nom_usage": "  "}) == attendu


def test_la_colonne_gagne_quand_elle_est_remplie() -> None:
    assert nom_a_imprimer({
        "name": "Vitres & Gouttières - 123Entretien",
        "nom_usage": "123Entretien",
    }) == "123Entretien"


# ── Le transport : le bloc servi aux DEUX ───────────────────────────────────

def test_le_bloc_porte_le_nom_et_dit_qu_il_fait_foi() -> None:
    bloc = bloc_faits_verifies(4.8, 47, nom_entreprise="123Entretien")
    assert "Nom de l'entreprise : **123Entretien**" in bloc
    # La phrase qui désamorce le faux positif : le juge voyait un autre nom
    # dans le research_json et criait au fait inventé.
    assert "même s'il diffère" in bloc


def test_sans_le_parametre_le_bloc_est_INCHANGE() -> None:
    """La compatibilité ascendante, vérifiée et pas supposée.

    ⚠️ Une première version se contentait de `"Nom de l'entreprise" not in …`.
    Le conseil de relecture a montré qu'elle passait AUSSI contre la version
    d'hier — la chaîne n'existait nulle part — et que n'importe quel AUTRE
    changement du bloc serait passé inaperçu. On compare le texte entier.
    """
    sans = bloc_faits_verifies(4.8, 47)
    assert sans == (
        "## Faits vérifiés (valeurs de colonne — à recopier au mot près, "
        "jamais à arrondir ni à embellir)\n"
        "- Note Google : 4,8\n"
        "- Nombre d'avis : 47\n"
        "  ✅ Tu PEUX citer : « 4,8 étoiles sur 47 avis Google ».\n"
        "  Ces deux chiffres se recopient exactement, sans les modifier."
    ), "le bloc servi sans nom a changé — deux tests existants en dépendent"


def test_le_candidat_brut_ne_sort_JAMAIS_vers_les_prompts() -> None:
    """🔴 Sinon on recrée la divergence qu'on vient de fermer.

    `research_json["nom_usage"]` est le candidat AVANT la garde — il reste en
    base comme trace mesurable, mais le servir aux prompts donnerait aux deux
    acteurs un nom peut-être refusé, à côté du bloc qui en dit un autre.
    """
    assert sans_diagnostic({"nom_usage": "Les", "company_summary": "x"}) == {
        "company_summary": "x"
    }


# ── Les deux listes de mots vides ne doivent pas diverger ───────────────────

def test_les_mots_vides_restent_d_accord_avec_research() -> None:
    """🔴 `lib/avis` RECOPIE les listes de `tools/research` — `lib` ne peut pas
    dépendre de `tools` sans inverser la dépendance. Une copie qui dérive est
    une copie qui ment ; ce test est le seul lien entre les deux.

    ⚠️ `_NOM_MOTS_GEO` est volontairement ABSENTE de la copie : `research`
    cherche le radical de MARQUE (la géographie y est du bruit), `avis` cherche
    l'ÉGALITÉ entre deux écritures du même nom — et « Jolie Québec » perdrait
    la moitié du sien.
    """
    from src.lib.avis import _MOTS_NON_SIGNIFIANTS
    from src.tools.research import (
        _NOM_MOTS_ENVELOPPE,
        _NOM_MOTS_GEO,
        _NOM_MOTS_OUTILS,
        _NOM_SUFFIXES_LEGAUX,
    )

    attendu = _NOM_SUFFIXES_LEGAUX | _NOM_MOTS_OUTILS | _NOM_MOTS_ENVELOPPE
    assert _MOTS_NON_SIGNIFIANTS == attendu, (
        "les listes ont divergé : mets à jour la copie de lib/avis.py"
    )
    assert not (_MOTS_NON_SIGNIFIANTS & _NOM_MOTS_GEO), (
        "la géographie est entrée dans la copie — « Jolie Québec » va perdre "
        "la moitié de son nom"
    )


# ── Le câblage : les colonnes qu'un SELECT oublié rend invisibles ───────────

def test_les_trois_select_et_le_dict_ramenent_la_colonne() -> None:
    """🔴 LA CLASSE DE DÉFAUT LA PLUS SILENCIEUSE DU DÉPÔT.

    Une colonne absente d'un `select` PostgREST n'existe pas dans la ligne
    rendue — sans erreur, sans log. `nom_a_imprimer` retomberait sur le repli
    POUR TOUJOURS et tout aurait l'air de marcher.

    Le câblage des avis a déjà produit ce défaut deux fois (voir
    `test_avis_cablage.py`). Ici on lit le texte des quatre endroits.
    """
    racine = Path(__file__).resolve().parent.parent
    db = (racine / "src/tools/db.py").read_text(encoding="utf-8")
    api = (racine / "src/http_api.py").read_text(encoding="utf-8")

    # ⚠️ ON CHERCHE LES COLONNES, PAS UNE CHAÎNE EXACTE. Une première version
    # comparait le `select` au caractère près : ajouter `status` à la même liste
    # l'a fait tomber alors que rien n'était cassé. Un test qui rougit sur un
    # réordonnancement inoffensif finit par être contourné au lieu d'être lu.
    def _colonnes_du_select(texte: str, ancre: str) -> set[str]:
        i = texte.index(ancre)
        fin = texte.index("),", i)
        brut = texte[i:fin].replace('"', "").replace("\n", "").replace(" ", "")
        return {c for c in brut.split(",") if c}

    wf4 = _colonnes_du_select(db, '"id,name,nom_usage')
    assert {"name", "nom_usage", "status"} <= wf4, (
        f"le SELECT de WF-4 a perdu une colonne : {sorted(wf4)}. Sans `status`, "
        "la garde des entreprises disqualifiées ne peut pas décider."
    )
    contact = _colonnes_du_select(api, '"id,name,nom_usage,status')
    assert {"name", "nom_usage", "status", "recontact_manuel"} <= contact, (
        f"le SELECT de /personalize/contact a perdu une colonne : {sorted(contact)}. "
        "Sans `status` ni `recontact_manuel`, cette route peut écrire un "
        "brouillon pour une fiche désabonnée — une infraction LCAP."
    )

    juge = _colonnes_du_select(api, '"name,nom_usage,research_json')
    assert {"name", "nom_usage"} <= juge, (
        "le SELECT du juge a perdu name ou nom_usage — sans `name`, "
        "`nom_a_imprimer` n'a même pas de repli"
    )
    assert '"nom_usage": company_row.get("nom_usage")' in api, (
        "le dict `company` de _personalize_one est reconstruit à la main : "
        "une colonne ajoutée au SELECT n'y arrive pas toute seule"
    )


# ── Le maillon positionnel : le seul du lot sans filet ──────────────────────


@pytest.mark.asyncio
async def test_le_juge_recoit_VRAIMENT_le_nom(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 LE TROU QUE LE CONSEIL DE RELECTURE A TROUVÉ.

    Rien ne vérifiait que le nom arrive au juge — alors que le chemin passe par
    **deux appels POSITIONNELS** : `compliance_check` appelle `_llm_judge` avec
    12 arguments dans l'ordre (via `asyncio.to_thread`), qui appelle
    `_message_utilisateur_juge` avec 10. Un argument inséré au milieu décalerait
    tout, en silence : `industry` deviendrait le nom, le nom deviendrait autre
    chose, et le juge recevrait un bloc faux sans qu'aucune erreur ne soit levée.

    Le conseil l'a prouvé aligné le 2026-09-17. Ce test le tient demain.

    ⚠️ On ne compare pas les arguments par position — ce serait refaire la même
    erreur à l'envers. On les **lie à la signature réelle** : le test survit à
    un passage en arguments nommés, et tombe sur un décalage.
    """
    import inspect

    from src.tools import compliance as comp

    captures: dict[str, object] = {}

    def _juge_espion(*args: object, **kwargs: object) -> dict[str, object]:
        lie = inspect.signature(_VRAI_LLM_JUDGE).bind(*args, **kwargs)
        lie.apply_defaults()
        captures.update(lie.arguments)
        return {"verdict": "approved", "violations": []}

    _VRAI_LLM_JUDGE = comp._llm_judge
    monkeypatch.setattr(comp, "_llm_judge", _juge_espion)
    for cle, val in (
        ("WARMUP_DISABLED", "true"),
        ("LEGAL_COMPANY_NAME", "Couture IA"),
        ("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse"),
        ("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe"),
        ("LCAP_MENTIONS_REDUITES", "true"),
        ("INSTANTLY_CAMPAIGN_FOOTER", SIGNATURE_COMPTE_INSTANTLY),
    ):
        monkeypatch.setenv(cle, val)

    # ⚠️ Le corps de référence des tests de conformité, pas une phrase inventée :
    # un corps qui déclenche un contrôle BLOQUANT n'atteint jamais le juge, et
    # le test passerait au vert sans rien avoir vérifié. Première version de ce
    # test : `captures` est resté vide.
    sortie = await comp.compliance_check(
        message_id="msg-1",
        body=CORPS_A_REPLI_AVIS,
        subject="Une question",
        template_used="A",
        track="agence-ia",
        research_json={},
        social_proof=[],
        available_slots=[],
        nom_entreprise="123Entretien",
    )
    assert captures, (
        f"le juge n'a jamais été appelé (verdict {sortie.verdict}) : un "
        "contrôle déterministe a bloqué avant, ce test ne prouve rien"
    )

    assert captures.get("nom_entreprise") == "123Entretien", (
        "le nom n'arrive pas au juge : l'appel positionnel de "
        "`compliance_check` vers `_llm_judge` a été décalé. Le juge recevrait "
        f"un bloc faux sans aucune erreur. Ce qu'il a reçu : {captures!r}"
    )
    # Le témoin : si la liaison avait tout mis dans le mauvais tiroir, ces
    # deux-là seraient faux aussi et l'assertion du dessus ne prouverait rien.
    assert captures.get("subject") == "Une question"
    assert captures.get("max_tokens") == 2500


def test_le_second_maillon_positionnel_est_aligne() -> None:
    """La deuxième moitié du chemin : `_llm_judge` → `_message_utilisateur_juge`.

    Elle ne se teste pas en appelant `_llm_judge` (il lui faudrait une clé
    Anthropic), alors on lie les arguments de l'appel à la signature de la
    fonction appelée — ce qui attrape exactement la même faute : un décalage.
    """
    import ast
    import inspect
    from pathlib import Path

    from src.tools import compliance as comp

    source = (
        Path(__file__).resolve().parent.parent / "src/tools/compliance.py"
    ).read_text(encoding="utf-8")
    arbre = ast.parse(source)

    appels = [
        n for n in ast.walk(arbre)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_message_utilisateur_juge"
    ]
    assert len(appels) == 1, f"{len(appels)} appels trouvés, attendu 1"

    noms = inspect.signature(comp._message_utilisateur_juge).parameters
    appel = appels[0]
    positions = [a.id if isinstance(a, ast.Name) else "?" for a in appel.args]
    assert len(positions) == len(noms), (
        f"{len(positions)} arguments pour {len(noms)} paramètres : "
        "l'appel positionnel est décalé"
    )
    # Le paramètre `nom_entreprise` doit recevoir la variable du même nom.
    rang = list(noms).index("nom_entreprise")
    assert positions[rang] == "nom_entreprise", (
        f"le paramètre `nom_entreprise` reçoit `{positions[rang]}` — décalage"
    )


# ── WF-4 ne rédige pas pour une entreprise sortie du circuit ────────────────


def test_wf4_ecarte_les_entreprises_disqualifiees_et_desabonnees() -> None:
    """🔴 DÉFAUT TROUVÉ PAR UN CONSEIL DE RELECTURE LE 2026-09-17, quelques
    heures après le commit intitulé « une disqualification sort la fiche du
    circuit ».

    Elle sortait du circuit de RECHERCHE — `list_companies_to_research` filtre
    `not.in.(disqualified,no_web_presence)` — mais PAS de celui de RÉDACTION.
    La boucle d'éligibilité de `list_contacts_to_personalize` testait le
    brouillon déjà écrit, l'entreprise déjà servie, la recherche, le site et la
    saison. Jamais le statut.

    📏 Le cas vivant au moment de la correction : *Strathmore Commercial
    Landscape Management*, passée `disqualified` le soir même (« 25 employés ou
    plus / répartiteur en place — entreprise nationale avec 250+ camions »),
    TROIS contacts en `new`, aucun message. Rien ne l'empêchait de sortir dans
    le lot du lendemain midi.

    ⚠️ `suppressed` compte plus lourd encore : c'est le statut d'un
    désabonnement. Lui écrire n'est pas une maladresse, c'est une infraction
    LCAP.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "src/tools/db.py"
    ).read_text(encoding="utf-8")

    deb = src.index("def _retenir(")
    fin = src.index("\ndef ", deb + 10)
    boucle = src[deb:fin]

    assert 'company.get("status") in ("disqualified", "suppressed")' in boucle, (
        "la garde de statut a disparu de la boucle d'éligibilité de WF-4 : "
        "une entreprise disqualifiée ou désabonnée peut recevoir un brouillon"
    )


def test_la_route_manuelle_refuse_ce_que_le_lot_refuse() -> None:
    """🔴 `/personalize/contact` CONTOURNAIT ENTIÈREMENT LA GARDE DU LOT.

    `list_contacts_to_personalize` écarte `disqualified`, `suppressed` et les
    décisions humaines. Cette route ne passe pas par elle : elle lit le contact
    et l'entreprise directement, et `persist=True` est le défaut.

    ⚠️ `suppressed` est le cas qui compte : c'est le statut d'un désabonnement,
    et cette route est justement celle que la checklist de go-live désigne comme
    « la façon de créer un draft de test ». Un test de fumée sur le mauvais
    contact suffisait à écrire à quelqu'un qui s'est désabonné.

    Trouvé par un conseil de relecture quelques heures après le commit qui
    fermait le même trou côté lot.
    """
    from pathlib import Path

    api = (
        Path(__file__).resolve().parent.parent / "src/http_api.py"
    ).read_text(encoding="utf-8")

    assert 'if company.get("status") in ("disqualified", "suppressed"):' in api, (
        "la garde de statut a disparu de /personalize/contact"
    )
    assert 'company.get("recontact_manuel") in ("hors_cible", "jamais")' in api, (
        "la garde de décision humaine a disparu de /personalize/contact"
    )


def test_la_decision_humaine_ecarte_vraiment_la_fiche() -> None:
    """🔴 La migration 0032 s'intitule « La décision humaine doit avoir un
    endroit où vivre, ET ELLE DOIT L'EMPORTER ». Mesuré le 2026-09-17 :
    `recontact_manuel` n'était lu par AUCUN code Python — seulement par une vue.

    ⚠️ `a_juger` ne bloque PAS, délibérément : il veut dire « un humain doit
    regarder », pas « ne la contacte pas ».
    """
    from src.tools.db import _sans_decision_humaine_contraire as filtre

    lot = [
        {"name": "normale", "recontact_manuel": None},
        {"name": "Terminix", "recontact_manuel": "hors_cible"},
        {"name": "plainte", "recontact_manuel": "jamais"},
        {"name": "à regarder", "recontact_manuel": "a_juger"},
        {"name": "colonne absente"},
    ]
    gardees = [r["name"] for r in filtre(lot)]
    assert gardees == ["normale", "à regarder", "colonne absente"], gardees


def test_le_filtre_de_decision_reste_en_PYTHON_et_pas_dans_la_requete() -> None:
    """🔴 DEUX PIÈGES MESURÉS AVANT DE DÉPLOYER, chacun silencieux.

    1. `recontact_manuel=not.in.(hors_cible,jamais)` laisse passer **ZÉRO fiche
       sur 723** : en SQL, `NULL not in (...)` ne vaut pas vrai mais NULL, donc
       la ligne est ÉCARTÉE — et 721 fiches ont la colonne à NULL. WF-3 se
       serait tu entièrement, sans une seule erreur : un lot vide ne plante pas.
    2. Les paramètres portent DÉJÀ une clé `"or"`, celle qui distingue « jamais
       recherchée » de « à reprendre après 90 jours ». Une seconde clé `"or"`
       dans le même littéral l'ÉCRASE en silence — Python ne prévient pas.

    Ce test fige les deux : le filtre reste en Python, et il n'y a qu'un `"or"`.
    """
    from pathlib import Path

    db = (
        Path(__file__).resolve().parent.parent / "src/tools/db.py"
    ).read_text(encoding="utf-8")

    deb = db.index("async def list_companies_to_research(")
    fin = db.index(chr(10) + "async def ", deb + 10)
    corps = db[deb:fin]

    assert corps.count('"or":') == 1, (
        "une seconde clé `or` est apparue dans les paramètres : elle écrase la "
        "première EN SILENCE et détruit la sélection de WF-3"
    )
    # ⚠️ On interdit la colonne comme CLE DE FILTRE, pas dans le `select` — elle
    # doit y être, sinon le filtre Python ne la voit pas. Première version
    # de ce test interdisait le mot PARTOUT, et rougissait sur le `select`
    # lui-même.
    assert '"recontact_manuel":' not in corps, (
        "le filtre de décision humaine est revenu dans la requête : sur une "
        "colonne à 721 NULL, il écarterait TOUTES les fiches"
    )
    assert "recontact_manuel" in corps, (
        "la colonne a disparu du select : le filtre Python lirait None partout "
        "et n'écarterait plus personne"
    )
    # ⚠️ On vérifie que le filtre est APPELÉ, pas la forme exacte de son
    # argument. Il recevait `rows` ; depuis le 2026-09-20 il reçoit directement
    # le résultat du `select`, parce que les post-filtres doivent tourner AVANT
    # la troncature à `limit` — sinon chaque fiche écartée rétrécit le lot en
    # silence (mesuré : des lots de 6 à 10 au lieu de 10). Figer la forme de
    # l'argument ferait rougir ce test sur un refactor qui ne touche pas à ce
    # qu'il protège.
    assert "_sans_decision_humaine_contraire(" in corps, (
        "le filtre de décision humaine a disparu de la sélection"
    )
