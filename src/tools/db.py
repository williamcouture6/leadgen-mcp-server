"""Tool `db` — accès Supabase pour le pipeline.

🔴 CONVENTION D'ÉCRITURE — décision William du 2026-09-04.

**Tout chiffre mesuré écrit dans un commentaire porte SA DATE.** « 8 entreprises
concernées » se lit comme l'état du jour et devient faux sans bruit ; « 8 au
2026-09-02 » se lit comme ce qu'il est — une mesure passée qui a justifié une
décision, et qui reste vraie pour toujours.

Le déclencheur : trois jeux de chiffres se contredisaient déjà dans ce fichier,
et l'ajout d'une seule date de saison a fait passer « 3 fiches » à 2 en une
heure. La règle qui suit :

  · un chiffre qui JUSTIFIE une décision → on le garde, daté ;
  · un chiffre qui décrit l'ÉTAT COURANT → il n'a rien à faire ici. Il va dans
    le résumé quotidien, qui le recalcule.

Phase 1 (sourcing) :
- next_sourcing_target : trouve le prochain (city, sector) à scraper (cooldown 30j)
- start_sourcing_run : crée une trace de pass (status=running)
- complete_sourcing_run : marque completed/failed avec métriques
- insert_company : insert avec dédup 2 clés (google_place_id, dedup_key)
- list_recent_companies : pour vérif manuelle après WF-1
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import supabase_client as db
from ..lib.lead_scoring import MARQUEUR_TETE_DE_FILE, calculer_score, est_tete_de_file
from ..lib.metiers import (
    SAISONS,
    colonnes_metiers,
    fenetre_mois,
    metier_depuis_industry,
    resoudre_metiers,
)
from ..lib.owner_match import summarize_company_decideur
from ..lib.pricing import estimated_cost_usd

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Catalogue de cibles (city, sector) — ordre de priorité
# ----------------------------------------------------------------------
# Aligné avec docs/icp-playbooks.md. Pour MVP on reste sur 3 segments × top villes.
# Le sector correspond à un keyword Google Places (`type` ou `keyword`).

# Les régions de sourcing, avec le rectangle que le balayeur interroge.
#
# 🔴 LA CLÉ EST LE NOM DE RÉGION, ET C'EST LA MÊME CHAÎNE PARTOUT :
# `companies.city` quand Google ne rend pas de locality, `sourcing_runs.city`,
# `sourcing_inventaire.regions` et `sourcing_balayages.region`. Rien ne les
# accorde mécaniquement côté base — une coquille rendrait une région invisible
# à l'alerte de famine, définitivement. D'où la source unique ici, et
# `DEFAULT_CITIES` qui en DÉRIVE au lieu d'être une seconde liste à maintenir.
#
# Rectangle = (bas_lat, bas_lng, haut_lat, haut_lng), coin sud-ouest puis
# nord-est, comme l'attend `locationRestriction` de Places.
#
# ⚠️ CES BORNES SONT APPROXIMATIVES ET VOLONTAIREMENT GÉNÉREUSES. L'asymétrie
# qui justifie ce choix : un rectangle TROP GRAND ne coûte que des tuiles
# supplémentaires, et une tuile est facturée 0 $ au masque identifiants-seuls ;
# un rectangle TROP PETIT perd des entreprises pour toujours, sans que rien ne
# le signale. Dans le doute, élargir.
#
# ⚠️ Les rectangles se CHEVAUCHENT entre régions voisines (Montréal / Laval /
# Terrebonne / Longueuil). C'est sans conséquence : la dédup se fait sur le
# `google_place_id` et `sourcing_inventaire.regions` est un tableau — une
# entreprise sortie de deux balayages porte les deux régions.
#
# ✅ Vérifié le 2026-09-16 : les bornes de Montréal, Québec, Laval et Gatineau
# contiennent bien la totalité des fiches que nous avons déjà dans ces villes.
# Les six autres régions n'ont AUCUNE fiche en base, donc leurs bornes ne sont
# vérifiées par rien — c'est le premier balayage qui les éprouvera.
REGIONS_BALAYAGE: dict[str, tuple[float, float, float, float]] = {
    "Montréal":       (45.38, -74.02, 45.74, -73.44),
    "Québec":         (46.70, -71.55, 47.00, -71.05),
    "Laval":          (45.50, -73.92, 45.74, -73.56),
    # Étirée d'ouest en est sur ~60 km : la borne est à -75.50 laissait dehors
    # Buckingham, Masson-Angers et L'Ange-Gardien — trois fiches déjà en base,
    # trouvées le 2026-09-16 en vérifiant ce rectangle. La démonstration en
    # miniature de « dans le doute, élargir ».
    "Gatineau":       (45.38, -76.10, 45.66, -75.25),
    "Longueuil":      (45.42, -73.58, 45.60, -73.36),
    "Sherbrooke":     (45.28, -72.08, 45.52, -71.74),
    "Saguenay":       (48.26, -71.40, 48.60, -70.80),
    "Lévis":          (46.64, -71.34, 46.88, -70.90),
    "Trois-Rivières": (46.24, -72.76, 46.50, -72.40),
    "Terrebonne":     (45.62, -73.78, 45.88, -73.46),
}

# ⚠️ DÉRIVÉE, jamais réécrite à la main : deux listes finiraient par diverger,
# et l'ordre compte (il tranche les égalités du classement des cibles).
DEFAULT_CITIES: list[str] = list(REGIONS_BALAYAGE)

# 3 segments ICP × keywords Google Places
SECTOR_CATALOG: dict[str, list[str]] = {
    "commerce_local": [
        # Restauration
        "restaurant",
        # Santé (5-50 empl., privé)
        "clinique dentaire",
        "clinique de physiothérapie",
        "clinique médicale privée",
        # Services auto
        "garage automobile",
        "carrosserie",
        "concessionnaire automobile",
        # Beauté / bien-être
        "spa",
        "salon de coiffure",
        "centre esthétique",
        # Retail local
        "boutique de mode",
        "quincaillerie",
        "animalerie",
        # Services résidentiels (track OPT)
        # NOTE anti double-fichage : les verticales REACTI (réactivation de base)
        # sont EXCLUES de ce catalogue OPT — voir CLAUDE.md + migration 0003.
        # Retirées 2026-05-30 : paysagiste, entrepreneur en déneigement,
        # exterminateur, piscines et spas, lavage de vitres. Elles iront dans un
        # catalogue REACTI dédié (auto-tag track='REACTI' au sourcing).
        # NE PAS les remettre ici.
        "plombier",
        "électricien",
        "entrepreneur CVAC",
        "entretien ménager commercial",
        "couvreur",
        "entrepreneur général en rénovation",
        "peintre résidentiel",
        "inspecteur en bâtiment",
        "pavage",
        "réparation électroménagers",
        "menuisier",
    ],
    "services_pro": [
        # Cabinets comptables / fiscalité
        "cabinet comptable",
        "CPA",
        # Juridique
        "cabinet d'avocats",
        "notaire",
        # RH / recrutement
        "agence de recrutement",
        "firme de consultation RH",
        # Agences créatives
        "agence de marketing",
        "agence web",
        "agence de design",
        # Ingénierie / architecture
        "firme d'ingénierie",
        "cabinet d'architecture",
        # Consultation
        "consultant en gestion",
    ],
    "manufacturier": [
        # Note : Google Places n'est pas idéal pour les manufacturiers
        # (peu de discoverabilité locale) — segment à faible yield WF-3.
        "manufacturier alimentaire",
        "manufacturier de boissons",
        "atelier de machinage",
        "fabricant de produits métalliques",
        "manufacturier de plastique",
        "fabricant d'emballages",
        "grossiste industriel",
        "fabricant d'équipement industriel",
    ],
}

# Catalogue de sourcing agence-ia (track 'agence-ia') — verticales service
# résidentiel récurrent/saisonnier. Retirées du catalogue OPT le 2026-05-30
# (anti double-fichage, voir CLAUDE.md). Tague track='agence-ia' à l'insert.
#
# INVARIANT — 1 secteur réel = 1 seule entrée = 1 seul label `industry`.
# `sector` sert À LA FOIS de mot-clé Google ET de valeur stockée
# (`companies.industry` + `sourcing_runs.sector`) : deux entrées pour la même
# chose re-splittent le label. NE PAS ajouter de synonyme comme entrée séparée.
# Décision 2026-08-05 (voir migration 0026) :
#   - "tonte de pelouse" fusionnée dans "tonte de gazon" (même secteur).
#   - "installation de piscine" + "piscines et spas" retirées : l'offre vise
#     l'ENTRETIEN récurrent, pas l'installation one-shot ni le détaillant.
# lib.sourcing_filters reste le filet anti-junk (spas détente, chaînes retail).
REACTI_SECTOR_CATALOG: dict[str, list[str]] = {
    "commerce_local": [
        "entrepreneur en déneigement",
        "paysagiste",
        # Deux formulations pour la tonte (décision William 2026-08-21) : Google
        # Places ne rend pas les mêmes entreprises selon le mot employé. Les
        # doublons ne coûtent rien — `insert_company` dédupe sur google_place_id,
        # identique pour une même entreprise quelle que soit la requête.
        "tonte de gazon",
        "tonte de pelouse",
        "exterminateur",
        "entretien de piscine",
        "lavage de vitres",
    ],
}

# Sélection du catalogue par track. Défaut OPT = comportement historique.
# Clé 'agence-ia' = offre vivante (pivot 2026-06-07) ; REACTI_SECTOR_CATALOG garde
# son nom de variable legacy (= verticales services résidentiels).
_CATALOGS: dict[str, dict[str, list[str]]] = {
    "OPT": SECTOR_CATALOG,
    "agence-ia": REACTI_SECTOR_CATALOG,
}

COOLDOWN_DAYS = 30

# 🔴 Combien de contacts la sélection WF-4 LIT pour en garder `limit`.
#
# Exporté parce qu'il est cité ailleurs : `_alerter_famine_wf4` explique la
# famine en nommant ce chiffre. Il valait 5 dans le code et 5 dans le message ;
# le code est passé à 12 le 2026-09-02 avec le filtre saisonnier, le message
# est resté à 5 — l'alerte diagnostiquait donc avec un chiffre faux, et c'est
# le conseil qui l'a vu. Une constante partagée les empêche de diverger.
#
# Porté de 5 à 12 avec le filtre saisonnier, qui écarte plus de la moitié des
# fiches en septembre. À 5, un lot de 20 lisait 100 contacts, en gardait ~45
# après la saison, puis perdait encore au dédoublonnage par entreprise.
#
# ⚠️ DIMENSIONNÉ SUR `limit=20`. Les deux crons n8n postent `limit: 10`, donc
# la fenêtre réelle vaut 120 contacts, pas 240. Le conseil du 2026-09-02 a
# mesuré qu'à 120, 39 des 125 déneigeurs joignables en septembre ont plus de
# 120 contacts hors saison DEVANT eux dans l'ordre `created_at.asc` : ils ne
# sont lus aucune fois d'août à décembre. Monter le facteur ne fait que
# déplacer le seuil — le correctif réel est de faire TOURNER la file
# (horodater les contacts lus et écartés, trier « jamais tenté d'abord »).
# Question posée à William le 2026-09-02, en attente de sa réponse.
FACTEUR_SURRECOLTE = 12


def _all_targets(track: str = "OPT") -> list[tuple[str, str, str]]:
    """Liste complète (city, sector, icp_segment) du catalogue du `track`, par priorité."""
    catalog = _CATALOGS.get(track, SECTOR_CATALOG)
    targets: list[tuple[str, str, str]] = []
    for city in DEFAULT_CITIES:
        for icp, sectors in catalog.items():
            for sector in sectors:
                targets.append((city, sector, icp))
    return targets


# Combien de mois d'AVANCE il faut prendre sur l'ouverture d'une fenêtre.
#
# 🔴 CE N'EST PAS UN CONFORT, C'EST LE DÉLAI DU PIPELINE. On ne peut pas hydrater
# et rechercher le jour où la fenêtre s'ouvre : WF-3 traite ~20 fiches/jour, donc
# 186 entreprises demandent NEUF JOURS de recherche, et WF-4 puis WF-5 passent
# après. Préparer un métier le mois où sa fenêtre s'ouvre revient à le manquer.
#
# ⚠️ À UN MOIS, ET PAS PLUS. Prendre trois mois d'avance ferait traiter en
# septembre des paysagistes joignables en avril : on paierait la recherche
# (0,034 $/fiche, mesuré sur 606 runs) six mois avant de s'en servir, et la
# fiche serait périmée au moment de l'envoi.
MOIS_AVANCE_PREPARATION = 1


def secteurs_a_preparer(
    aujourdhui: date | None = None, track: str = "agence-ia"
) -> list[str]:
    """Les secteurs du catalogue qu'il faut avoir traités MAINTENANT.

    Un secteur est à préparer si la fenêtre saisonnière de son métier est
    ouverte ce mois-ci, ou le sera dans `MOIS_AVANCE_PREPARATION` mois.

    🔴 LE PONT SECTEUR → MÉTIER PASSE PAR `metier_depuis_industry`, ET C'EST
    VOLONTAIRE. `SAISONS` est clé par MÉTIER (`déneigement`, `tonte`) alors que
    le catalogue porte des SECTEURS (`entrepreneur en déneigement`,
    `tonte de gazon`). Recopier la correspondance ici — en SQL ou en dur —
    créerait une QUATRIÈME classification concurrente après `classer_services`,
    `colonnes_metiers` et `metier_depuis_industry`, contre la doctrine « un seul
    dictionnaire, deux lecteurs » de `lib/metiers.py`. C'est aussi pourquoi ce
    calcul ne peut PAS vivre dans une vue Postgres.

    ⚠️ Un secteur dont le métier n'est pas reconnu est ÉCARTÉ, pas inclus « au
    cas où » : l'inclure ferait préparer toute l'année un secteur dont on ne
    sait rien, et c'est exactement ce que la décision William du 2026-09-14 a
    tranché en écartant `metier_inconnu` de la démarchabilité.

    ⚠️ `tonte de gazon` et `tonte de pelouse` rendent tous les deux `tonte` :
    la liste porte donc DEUX libellés pour un seul marché. C'est voulu — ce sont
    les deux mots-clés réellement présents dans `trouve_par` — mais ne jamais
    lire `len()` de cette liste comme un nombre de métiers.
    """
    jour = aujourdhui or date.today()
    mois_vises = {
        ((jour.month - 1 + n) % 12) + 1
        for n in range(MOIS_AVANCE_PREPARATION + 1)
    }
    catalog = _CATALOGS.get(track, SECTOR_CATALOG)
    retenus: list[str] = []
    for secteurs in catalog.values():
        for secteur in secteurs:
            metier = metier_depuis_industry(secteur)
            if metier is None:
                continue
            if fenetre_mois(metier) & mois_vises:
                retenus.append(secteur)
    return sorted(set(retenus))


# ----------------------------------------------------------------------
# Schémas Pydantic (input/output des tools)
# ----------------------------------------------------------------------

class NextTargetOut(BaseModel):
    city: str
    sector: str
    icp_segment: str
    reason: Literal["never_scraped", "cooldown_expired"]


class StartRunIn(BaseModel):
    city: str
    sector: str
    icp_segment: str
    search_query: str | None = None


class StartRunOut(BaseModel):
    run_id: str
    started_at: str


class CompleteRunIn(BaseModel):
    run_id: str
    status: Literal["completed", "failed"]
    next_page_token: str | None = None
    results_count: int = 0
    new_companies_count: int = 0
    duplicates_count: int = 0
    error_text: str | None = None


class CompanyIn(BaseModel):
    name: str
    google_place_id: str | None = None
    address: str | None = None
    city: str | None = None
    region: str = "QC"
    postal_code: str | None = None
    country: str = "CA"
    latitude: float | None = None
    longitude: float | None = None
    website: str | None = None
    domain: str | None = None
    icp_segment: str | None = None
    industry: str | None = None
    google_types: list[str] = Field(default_factory=list)
    google_rating: float | None = None
    google_reviews_count: int | None = None
    source: str = "google_places"
    track: str = "OPT"  # OPT | REACTI — taggé au sourcing (anti double-fichage)
    raw_payload: dict[str, Any] | None = None


class InsertCompanyOut(BaseModel):
    status: Literal["inserted", "duplicate"]
    company_id: str | None = None
    dedup_reason: str | None = None


# ----------------------------------------------------------------------
# Logique
# ----------------------------------------------------------------------

async def next_sourcing_target(track: str = "agence-ia") -> NextTargetOut | None:
    """Retourne la prochaine cible (city, sector) du catalogue `track`, ou None.

    Le défaut vise la piste VIVANTE, comme `RunWf1In.track` et
    `/sourcing/next-target`. Il valait `'OPT'` jusqu'au 2026-09-15 : deux
    appelants nus — le tool MCP et `scripts/run_sourcing_pass.py` — sourçaient
    donc le catalogue GELÉ depuis le pivot du 2026-06-07, et aucun des deux
    n'offrait le moyen de le corriger. Un défaut qui vise la piste morte
    n'attend qu'un appelant distrait.

    Stratégie — la plus affamée d'abord :
    1. On écarte tout ce qui a été scrapé il y a moins de `COOLDOWN_DAYS`.
    2. Parmi le reste, une cible JAMAIS scrapée passe avant tout.
    3. Entre deux cibles déjà scrapées, la plus ancienne passe la première.
    4. À égalité, l'ordre de priorité du catalogue tranche.

    🔴 NE PAS revenir à « la première cible du catalogue hors cooldown ».
    C'était la règle jusqu'au 2026-09-14, et elle rendait la queue du catalogue
    INATTEIGNABLE — pas « servie tard » : jamais. Avec un run par jour et un
    cooldown de 30 jours, le curseur avance d'un cran par jour mais retombe à
    zéro dès que la tête ressort de la fenêtre : il ne dépasse jamais l'indice
    `COOLDOWN_DAYS`. Donc `COOLDOWN_DAYS + 1` entrées tournent en rond — 31 ici
    — et TOUT ce qui suit meurt, soit 39 des 70 cibles du catalogue actuel.
    Mesuré en prod ce jour-là : 41 des 70 cibles `agence-ia` jamais scrapées
    une seule fois, dont six villes entières (Longueuil, Sherbrooke, Saguenay,
    Lévis, Trois-Rivières, Terrebonne), pendant que WF-1 re-mâchait Montréal
    chaque matin. Le coût se chiffre : sur l'historique complet, un PREMIER
    passage sur une cible rend 90,5 % de fiches neuves, un re-passage 9,4 %.
    Régression couverte par `tests/test_sourcing_famine_de_cible.py`.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS)

    # Historique COMPLET, pas seulement la fenêtre de cooldown : classer par
    # ancienneté demande de savoir quand chaque cible a été vue la dernière
    # fois, y compris il y a six mois. `select_all` pagine — un `select` nu se
    # ferait couper à 1000 lignes le jour où l'historique les dépasse.
    # `order` est un argument NOMMÉ OBLIGATOIRE de select_all, et il doit
    # porter sur une colonne UNIQUE : la pagination par offset saute ou double
    # des lignes si deux d'entre elles se valent. D'où `id` et non
    # `created_at.desc` — l'ordre de lecture n'a de toute façon aucune
    # importance ici, le maximum par cible étant recalculé plus bas.
    runs = await db.select_all(
        "sourcing_runs",
        order="id",
        params={"select": "city,sector,created_at"},
    )
    dernier: dict[tuple[str, str], datetime] = {}
    for r in runs:
        cle = (r["city"], r["sector"])
        vu = _en_datetime(r.get("created_at"))
        if vu is None:
            continue
        if cle not in dernier or vu > dernier[cle]:
            dernier[cle] = vu

    meilleure: tuple[str, str, str] | None = None
    meilleur_rang: tuple[int, float] | None = None

    for city, sector, icp in _all_targets(track):
        vu = dernier.get((city, sector))
        if vu is not None and vu >= cutoff:
            continue  # cooldown encore actif
        # (0, …) = jamais scrapée, passe avant toute cible déjà vue.
        rang = (0, 0.0) if vu is None else (1, vu.timestamp())
        if meilleur_rang is None or rang < meilleur_rang:
            meilleur_rang, meilleure = rang, (city, sector, icp)

    if meilleure is None:
        return None
    city, sector, icp = meilleure
    reason: Literal["never_scraped", "cooldown_expired"] = (
        "never_scraped" if (city, sector) not in dernier else "cooldown_expired"
    )
    return NextTargetOut(city=city, sector=sector, icp_segment=icp, reason=reason)


def _en_datetime(valeur: Any) -> datetime | None:
    """`created_at` PostgREST → datetime aware. Rend None sur une valeur illisible.

    Une ligne d'historique illisible ne doit pas faire tomber le sourcing du
    jour : au pire elle est ignorée, la cible repasse pour « jamais scrapée »
    et coûte un doublon — jamais une exception dans le cron de 10 h.
    """
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=timezone.utc)
    if not isinstance(valeur, str) or not valeur:
        return None
    try:
        d = datetime.fromisoformat(valeur.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# L'inventaire — la file du piocheur (WF-1 nouvelle manière)
# ----------------------------------------------------------------------

# Au-delà, la fiche a échoué trop souvent pour qu'on continue de la servir.
# 🔴 SANS CE PLAFOND, LA REMISE EN FILE DES ÉCHECS TRANSITOIRES ROUVRE LA
# FAMINE. Une fiche qui échoue pour une raison structurelle reviendrait en tête
# chaque matin — le défaut de `next_sourcing_target` corrigé le 2026-09-14, et
# celui que `list_companies_to_research` et la file d'envoi évitent déjà par le
# même moyen. Le dictionnaire de la 0070 annonçait ce lecteur ; il n'existait
# pas, et c'est ce qui rendait le correctif de `echec` dangereux.
MAX_TENTATIVES_INVENTAIRE = 5


async def list_inventaire_a_piocher(
    limit: int = 20, *, track: str = "agence-ia",
    secteurs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Les entreprises à hydrater MAINTENANT, les plus affamées d'abord.

    🔴 TROIS RÈGLES, ET CHACUNE A UNE HISTOIRE.

    1. `etat = 'a_traiter'` EN LITTÉRAL. C'est ce qu'exige le prédicat de
       l'index GIN partiel de la 0070. Passé en paramètre lié, le plan générique
       perd la preuve du prédicat et l'index est ignoré — en silence.

    2. `trouve_par` CROISÉ AVEC LA SAISON. On n'hydrate que ce qu'on pourra
       démarcher dans le mois : payer 0,02 $ de détails plus 0,034 $ de
       recherche six mois avant de s'en servir, c'est payer pour une fiche qui
       sera périmée le jour de l'envoi. `secteurs_a_preparer` prend déjà un mois
       d'avance sur l'ouverture de la fenêtre.

    3. `derniere_tentative` NULLS FIRST. Jamais tentée d'abord, puis la plus
       anciennement tentée. Sans cet ordre, une fiche dont l'hydratation échoue
       en boucle reste en tête de file et mange le budget tous les matins —
       c'est la forme exacte du défaut de `next_sourcing_target` corrigé le
       2026-09-14, et les deux autres files du dépôt portent déjà ce remède.

    ⚠️ `select` nu et pas `select_all` : on veut `limit` lignes, pas la table.
    Le plafond PostgREST de 1000 ne mord donc jamais ici.
    """
    # ⚠️ La liste est passée par l'appelant quand il l'a déjà calculée. La
    # recalculer ici ferait diverger les deux le jour où la requête chevauche
    # minuit du dernier jour du mois : on piocherait sur l'ancienne saison et on
    # rapporterait la nouvelle.
    secteurs = secteurs_a_preparer(track=track) if secteurs is None else secteurs
    if not secteurs:
        # Aucun métier en fenêtre : il n'y a rien à préparer, et c'est un état
        # normal, pas une panne. L'alerte de famine le dit à sa façon.
        return []
    # 5. ON LIT TOUTE LA FILE ÉLIGIBLE, PUIS ON RÉPARTIT EN PYTHON.
    #
    # 🔴 LA SUR-LECTURE PARTIELLE NE MARCHE PAS ICI, ET C'EST MESURÉ. Un premier
    # correctif lisait `limit × 6` lignes avant de répartir : sur les 663
    # déneigeurs en file le 2026-09-16, les 120 premières par `created_at`
    # appartenaient toutes à Montréal, Laval et Longueuil — le lot ne touchait
    # QUE TROIS régions sur dix. Un facteur ne règle rien, il déplace le seuil :
    # Montréal porte à elle seule 243 entrées, donc il faudrait lire presque
    # toute la table pour atteindre Trois-Rivières. Autant l'assumer.
    #
    # ⚠️ `select_all` ET PAS `select` : PostgREST tronque à 1000 lignes SANS
    # erreur ni en-tête, et une troncature muette ramènerait exactement le
    # défaut qu'on vient de corriger — les dernières régions disparaîtraient
    # de la file sans que rien ne le dise.
    #
    # ⚠️ L'ORDRE DE LECTURE EST `google_place_id`, PAS l'ordre de famine. La
    # pagination par offset de `select_all` exige une colonne UNIQUE, sinon elle
    # saute ou double des lignes en silence. L'ordre de famine est rétabli en
    # Python juste après, sur la totalité — ce qui donne le même résultat, sans
    # le piège.
    lignes = await db.select_all(
        "sourcing_inventaire",
        order="google_place_id",
        params={
            "select": (
                "google_place_id,trouve_par,regions,tentatives,"
                "derniere_tentative,created_at"
            ),
            "track": f"eq.{track}",
            "etat": "eq.a_traiter",
            "trouve_par": "ov." + db.litteral_tableau(secteurs),
            # 4. ON CESSE DE SERVIR CE QUI ÉCHOUE TOUJOURS. Voir
            # `MAX_TENTATIVES_INVENTAIRE` : c'est ce plafond qui rend sûre la
            # remise en file des échecs transitoires.
            "tentatives": f"lt.{MAX_TENTATIVES_INVENTAIRE}",
        },
    )
    lignes.sort(key=_rang_de_famine)
    return _repartir_par_region(lignes, limit)


def _rang_de_famine(ligne: dict[str, Any]) -> tuple[int, str, str]:
    """`derniere_tentative` nulls first, puis `created_at`.

    C'est l'ordre que PostgREST appliquait avant qu'on lise toute la file : on
    le rétablit ici, à l'identique. `(0, "")` fait passer les jamais tentées
    devant, et la comparaison de chaînes ISO équivaut à la comparaison de dates
    — toutes sont écrites par `datetime.isoformat()` en UTC.
    """
    tentee = ligne.get("derniere_tentative")
    return (1, tentee, ligne.get("created_at") or "") if tentee else (
        0, "", ligne.get("created_at") or ""
    )


# ⚠️ `FACTEUR_EQUITE_REGIONALE` A EXISTÉ ICI ET A ÉTÉ RETIRÉ LE 2026-09-16.
# Il valait 6 : on lisait `limit × 6` lignes avant de répartir par région. Ça ne
# marchait pas — les 120 premières lignes par `created_at` venaient toutes des
# trois régions balayées en premier. Un facteur ne règle pas ce problème, il
# déplace le seuil. Ne pas le réintroduire : la file entière se lit d'un coup,
# voir le commentaire dans `list_inventaire_a_piocher`.


def _repartir_par_region(
    lignes: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Sert les régions à tour de rôle, en gardant l'ordre de famine DANS chacune.

    🔴 SANS ÇA, LE TRI PAR `created_at` REJOUE LA FAMINE GÉOGRAPHIQUE QUE
    L'INVENTAIRE DEVAIT GUÉRIR. `created_at` est l'ordre de DÉCOUVERTE, donc
    l'ordre dans lequel le balayeur a parcouru les régions. Mesuré le 2026-09-16
    sur les 663 déneigeurs en file : Montréal était servie dès le jour 1,
    Saguenay au jour 22 et Trois-Rivières au jour 25 — en pleine ouverture de
    saison, et pour la seule raison qu'elles avaient été balayées en dernier.

    C'est exactement le défaut que `next_sourcing_target` a corrigé le
    2026-09-14 — « six villes entières jamais atteintes » — reproduit un étage
    plus bas par un `order by` qui paraissait inoffensif.

    ⚠️ L'ORDRE DE FAMINE EST PRÉSERVÉ À L'INTÉRIEUR DE CHAQUE RÉGION : les
    lignes arrivent déjà triées `derniere_tentative nulls first, created_at`, et
    on les consomme dans cet ordre. On ALTERNE entre les files, on ne mélange
    pas — une fiche déjà tentée reste derrière les vierges de sa région.

    ⚠️ Une entreprise portant plusieurs régions est rangée sous la PREMIÈRE de
    son tableau. Le trigger de fusion les trie alphabétiquement, donc ce choix
    est arbitraire mais STABLE : elle apparaît dans une seule file, jamais deux.
    """
    files: dict[str, list[dict[str, Any]]] = {}
    for ligne in lignes:
        regions = ligne.get("regions") or [""]
        files.setdefault(regions[0], []).append(ligne)

    sortie: list[dict[str, Any]] = []
    while len(sortie) < limit and any(files.values()):
        for region in sorted(files):
            if len(sortie) >= limit:
                break
            if files[region]:
                sortie.append(files[region].pop(0))
    return sortie


async def journaliser_pioche(
    *, track: str, secteurs: list[str], compteurs: dict[str, int],
    restant_apres: int | None, duree_ms: int, error_text: str | None,
) -> None:
    """Écrit UNE ligne par exécution du piocheur, même quand elle n'a rien pioché.

    🔴 C'EST CE « MÊME QUAND ELLE N'A RIEN PIOCHÉ » QUI PORTE TOUT. Sans ligne,
    « le piocheur n'a pas tourné » et « il a tourné et la file était vide »
    laissent exactement la même trace : aucune. Le verdict `piocheur_muet`
    lisait `sourcing_inventaire.derniere_tentative`, qui ne bouge QUE si une
    fiche a été touchée — donc un matin hors saison aurait déclenché une fausse
    alerte au bout de 48 h, sur un système parfaitement sain.

    ⚠️ NE LÈVE JAMAIS. Une panne de journalisation ne doit pas faire échouer un
    lot qui a réussi : au pire on perd une ligne d'historique, on ne perd pas le
    travail. Même principe que `slack.notify`.
    """
    try:
        await db.insert(
            "sourcing_pioches",
            {
                "track": track,
                "secteurs": secteurs,
                "pioches": compteurs.get("pioches", 0),
                "inserees": compteurs.get("inserees", 0),
                "doublons": compteurs.get("doublons", 0),
                "junk": compteurs.get("junk", 0),
                "introuvables": compteurs.get("introuvables", 0),
                "echecs": compteurs.get("echecs", 0),
                "restant_apres": restant_apres,
                "duree_ms": duree_ms,
                "error_text": error_text,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.error("journal de pioche non ecrit — %r", e)


async def compter_restant_a_piocher(
    *, track: str = "agence-ia", secteurs: list[str] | None = None
) -> int | None:
    """Combien d'entrées restent piochables. None si la lecture tombe.

    ⚠️ `db.count` et pas `len(select(...))` : PostgREST tronque à 1000 lignes
    sans rien signaler, et un compte tronqué dirait « il en reste » pour
    toujours. Même règle que `_compter_envoyables_restants`.
    """
    secteurs = secteurs_a_preparer(track=track) if secteurs is None else secteurs
    if not secteurs:
        return 0
    try:
        return await db.count(
            "sourcing_inventaire",
            params={
                "track": f"eq.{track}",
                "etat": "eq.a_traiter",
                "trouve_par": "ov." + db.litteral_tableau(secteurs),
                "tentatives": f"lt.{MAX_TENTATIVES_INVENTAIRE}",
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.error("comptage du restant a piocher echoue — %r", e)
        return None


async def marquer_inventaire(
    google_place_id: str,
    *,
    etat: Literal["traitee", "ecartee", "echec"],
    company_id: str | None = None,
    motif: str | None = None,
    tentatives: int = 0,
) -> None:
    """Referme une ligne d'inventaire après une tentative d'hydratation.

    ⚠️ `derniere_tentative` est écrit DANS TOUS LES CAS, succès compris. C'est
    la colonne d'ordre : si elle n'était posée qu'en cas d'échec, une fiche
    traitée resterait éternellement « jamais tentée » pour le tri.

    ⚠️ On n'envoie JAMAIS `trouve_par` ni `regions` ici. Le trigger de fusion
    les protégerait, mais s'en remettre à lui pour une faute qu'on peut ne pas
    commettre, c'est user la garde pour rien.
    """
    patch: dict[str, Any] = {
        "etat": etat,
        "derniere_tentative": datetime.now(timezone.utc).isoformat(),
        "tentatives": tentatives + 1,
    }
    if company_id:
        patch["company_id"] = company_id
    if motif:
        patch["ecartee_motif"] = motif
    await db.update(
        "sourcing_inventaire", patch,
        filters={"google_place_id": f"eq.{google_place_id}"},
    )


async def start_sourcing_run(payload: StartRunIn) -> StartRunOut:
    now = datetime.now(timezone.utc).isoformat()
    rows = await db.insert(
        "sourcing_runs",
        {
            "city": payload.city,
            "sector": payload.sector,
            "icp_segment": payload.icp_segment,
            "search_query": payload.search_query
            or f"{payload.sector} in {payload.city}",
            "status": "running",
            "started_at": now,
        },
    )
    row = rows[0]
    return StartRunOut(run_id=row["id"], started_at=row["started_at"])


async def complete_sourcing_run(payload: CompleteRunIn) -> dict[str, Any]:
    patch: dict[str, Any] = {
        "status": payload.status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "results_count": payload.results_count,
        "new_companies_count": payload.new_companies_count,
        "duplicates_count": payload.duplicates_count,
    }
    if payload.next_page_token is not None:
        patch["next_page_token"] = payload.next_page_token
    if payload.error_text:
        patch["error_text"] = payload.error_text
    rows = await db.update("sourcing_runs", patch, filters={"id": f"eq.{payload.run_id}"})
    return {"updated": len(rows)}


async def insert_company(payload: CompanyIn) -> InsertCompanyOut:
    """Insert avec dédup. Retourne 'duplicate' si une clé conflit."""
    # Pré-check sur la clé business google_place_id (fallback dedup_key en INSERT).
    if payload.google_place_id:
        existing = await db.select(
            "companies",
            params={
                "select": "id",
                "google_place_id": f"eq.{payload.google_place_id}",
                "limit": "1",
            },
        )
        if existing:
            return InsertCompanyOut(
                status="duplicate",
                company_id=existing[0]["id"],
                dedup_reason="google_place_id",
            )

    row = payload.model_dump(exclude_none=False)
    # status par défaut = 'sourced' (défini dans la migration)
    try:
        rows = await db.insert("companies", row)
    except Exception as e:  # noqa: BLE001
        # PostgREST renvoie 409 sur conflit unique (dedup_key). On l'attrape grossièrement
        # et on relit la company existante via dedup_key.
        msg = str(e)
        if "23505" in msg or "duplicate key" in msg.lower() or "409" in msg:
            # Re-fetch par nom + ville + postal pour récupérer l'id existant
            existing = await db.select(
                "companies",
                params={
                    "select": "id",
                    "name": f"eq.{payload.name}",
                    "city": f"eq.{payload.city or ''}",
                    "limit": "1",
                },
            )
            if existing:
                return InsertCompanyOut(
                    status="duplicate",
                    company_id=existing[0]["id"],
                    dedup_reason="dedup_key",
                )
        raise
    return InsertCompanyOut(status="inserted", company_id=rows[0]["id"])


async def list_recent_companies(limit: int = 20) -> list[dict[str, Any]]:
    return await db.select(
        "companies",
        params={
            "select": "id,name,city,icp_segment,status,created_at,google_rating",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )


# ----------------------------------------------------------------------
# Contacts (insérés par WF-3 à partir des emails scrapés du site officiel)
# ----------------------------------------------------------------------

class ContactIn(BaseModel):
    company_id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    email_verified: bool = False
    email_verification_source: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    title: str | None = None
    seniority: str | None = None
    is_decision_maker: bool = False
    owner_confidence: str | None = None   # 'confirmed' | 'potential' | 'unknown'
    potential_owner: dict[str, Any] | None = None
    source: str = "website"
    raw_payload: dict[str, Any] | None = None


class InsertContactOut(BaseModel):
    status: Literal["inserted", "duplicate", "skipped_no_email"]
    contact_id: str | None = None


def _consent_basis_for_contact(
    *, source: str | None, email_verification_source: str | None,
) -> str:
    """Base légale LCAP/Loi 25 inférée de la provenance de l'email.

    - Email publié manifestement sur le site public de l'entreprise (scraping WF-3)
      → `implied_conspicuous` (CASL 10(9)(b) : publication conspicue + courriel
      pertinent au rôle). On stocke l'URL source comme preuve.
    - Email trouvé sur la page propre de l'entreprise via découverte web (WF-reacti-2,
      source `reacti_discovery_own_page`) → même base `implied_conspicuous` : le
      courriel était publié publiquement par l'entreprise elle-même (page Facebook/GMB
      confirmée comme page officielle), même logique que le scraping WF-3.
    - Email d'une autre provenance (fournisseur tiers, import manuel, legacy Apollo,
      annuaire tiers `reacti_discovery_directory`)
      → `legitimate_interest` : on n'a PAS de preuve de publication conspicue, donc
      on enregistre honnêtement une base plus faible, à confirmer/upgrader plus tard.
    """
    src = (email_verification_source or source or "").lower()
    if "website" in src or "scrape" in src or "own_page" in src:
        return "implied_conspicuous"
    return "legitimate_interest"


async def _record_consent(
    contact_id: str,
    *,
    source: str | None,
    email_verification_source: str | None,
    raw_payload: dict[str, Any] | None,
    recorded_by: str = "auto_insert_contact",
) -> None:
    """Journalise la base légale d'un contact dans `consent_registry` (Loi 25/LCAP).

    Idempotent : `consent_registry` a un unique (contact_id, basis) ; on vérifie
    l'existence avant insert. NON bloquant : toute erreur est avalée pour ne
    jamais empêcher la création du contact (le log de consentement est un
    complément de preuve, pas un prérequis au pipeline d'acquisition).
    """
    try:
        basis = _consent_basis_for_contact(
            source=source, email_verification_source=email_verification_source,
        )
        existing = await db.select(
            "consent_registry",
            params={
                "select": "id",
                "contact_id": f"eq.{contact_id}",
                "basis": f"eq.{basis}",
                "limit": "1",
            },
        )
        if existing:
            return
        raw = raw_payload or {}
        source_url = raw.get("source_url") if isinstance(raw, dict) else None
        if basis == "implied_conspicuous":
            desc = "Courriel publié publiquement sur le site de l'entreprise (scraping WF-3)."
        else:
            desc = (
                f"Courriel professionnel obtenu via {source or 'fournisseur tiers'} "
                "(B2B) — pas de preuve de publication conspicue ; base à confirmer."
            )
        await db.insert(
            "consent_registry",
            {
                "contact_id": contact_id,
                "basis": basis,
                "source_url": source_url,
                "source_description": desc,
                "evidence_json": {"source": source, "email_source": email_verification_source},
                "recorded_by": recorded_by,
            },
        )
    except Exception:  # noqa: BLE001 — jamais bloquant
        pass


async def insert_contact(payload: ContactIn) -> InsertContactOut:
    """Insert contact, dédup sur le COURRIEL SEUL (insensible à la casse).

    🔴 PAS sur le couple (company_id, email) — c'était le cas jusqu'au
    2026-09-08, et ça laissait passer les vrais doublons.

    Mesuré la veille du premier envoi : **9 adresses portaient 2 ou 3 contacts**,
    tous sur des entreprises DIFFÉRENTES. Le gars de cVert aurait reçu TROIS
    courriels quasi identiques, possiblement sur trois gabarits différents.

    La cause est en amont : les doublons viennent de fiches Google Places en
    double. « cVert », « cVert - Entretien de Pelouse Ville », « … Laval » sont
    trois inscriptions du même commerce, donc trois `companies` distinctes — et
    la dédup par couple les voyait comme trois cas légitimes.

    Le bon critère est l'ADRESSE, parce que c'est elle qui reçoit : une boîte
    de réception, un courriel. Peu importe combien de fiches Google la
    désignent.

    ⚠️ `lower()` des deux côtés : `Info@X.ca` et `info@x.ca` sont la même boîte,
    et les fiches Google ne s'accordent pas sur la casse.

    ⚠️ Cette vérification peut se faire doubler par deux WF-3 concurrents. La
    garde réelle est l'index `contacts_email_actif_unique` (migration 0049) ;
    celle-ci évite juste d'aller au bout d'une insertion vouée à l'échec, et
    rend un `duplicate` propre plutôt qu'une erreur.
    """
    if not payload.email:
        return InsertContactOut(status="skipped_no_email")

    existing = await db.select(
        "contacts",
        params={
            "select": "id",
            "email": f"ilike.{payload.email.strip()}",
            "status": "neq.disqualified",
            "limit": "1",
        },
    )
    if existing:
        return InsertContactOut(status="duplicate", contact_id=existing[0]["id"])

    row = payload.model_dump(exclude_none=False)
    rows = await db.insert("contacts", row)
    contact_id = rows[0]["id"]

    # Loi 25/LCAP : journaliser la base légale dès la création (compliance by design).
    await _record_consent(
        contact_id,
        source=payload.source,
        email_verification_source=payload.email_verification_source,
        raw_payload=payload.raw_payload,
    )

    return InsertContactOut(status="inserted", contact_id=contact_id)


async def add_to_suppression(
    *,
    email: str | None = None,
    domain: str | None = None,
    reason: str = "manual",
    source: str | None = None,
    notes: str | None = None,
) -> bool:
    """Ajoute une adresse/domaine à `suppression_list` (idempotent sur email).

    `suppression_list` est le point d'enforcement anti-renvoi : `send.py` vérifie
    email + domaine contre cette table avant chaque push Instantly. `reason` doit
    être une valeur de l'enum `suppression_reason` (opt_out, hard_bounce,
    spam_complaint, dncl, manual, competitor).

    NON bloquant : avale toute erreur (la suppression est un garde-fou, ne doit
    jamais faire crasher le flow appelant). Retourne True si l'insert a réussi.
    """
    if not email and not domain:
        return False
    row: dict[str, Any] = {"reason": reason}
    if email:
        row["email"] = email
    if domain:
        row["domain"] = domain
    if source:
        row["source"] = source
    if notes:
        row["notes"] = notes
    try:
        if email:
            # Idempotent : unique sur email → ré-insert silencieux.
            await db.insert(
                "suppression_list", row, on_conflict="email", ignore_duplicates=True
            )
        else:
            await db.insert("suppression_list", row)
        return True
    except Exception:  # noqa: BLE001 — jamais bloquant
        return False


async def mark_company_disqualified(company_id: str, reason: str) -> dict[str, Any]:
    """Marque une company comme disqualifiée (échec research répété, hors-ICP, etc.).

    Le backlog WF-3 (`list_companies_to_research`) exclut `status='disqualified'`,
    donc une company disqualifiée sort automatiquement du backlog.
    """
    return {
        "updated": len(
            await db.update(
                "companies",
                {
                    "status": "disqualified",
                    "disqualified_reason": reason,
                    "last_enriched_at": datetime.now(timezone.utc).isoformat(),
                },
                filters={"id": f"eq.{company_id}"},
            )
        )
    }


# ----------------------------------------------------------------------
# Personalize (Phase 2 — WF-4)
# ----------------------------------------------------------------------

# Combien de pages la sélection accepte de lire avant d'abandonner.
#
# Borne de COÛT, pas de logique : 10 pages de 200 couvrent 2000 contacts, très
# au-delà de la file actuelle (345 au 2026-09-09). L'atteindre signifie que la
# file est majoritairement bouchée — l'alerte de famine le dira, puisque le lot
# reviendra court.
MAX_PAGES_SELECTION = 10


def _contact_priority_score(contact: dict[str, Any]) -> int:
    """Score de priorité (plus bas = meilleur) pour choisir 1 contact par company.

    scrape nominative same-domain > scrape nominative personal-domain > scrape
    generic > other. Les valeurs `apollo` (héritage : contacts importés avant le
    retrait d'Apollo) restent prioritaires comme contacts vérifiés.

    Évite d'envoyer plusieurs emails à la même entreprise (brûle la company).
    Le contact retenu est celui qui a la plus forte probabilité de joindre un
    décideur réel.
    """
    src = contact.get("email_verification_source")
    raw = contact.get("raw_payload") or {}
    kind = raw.get("kind") if isinstance(raw, dict) else None
    email_dom = (contact.get("email") or "").rsplit("@", 1)[-1].lower()
    # Domaines persos (gmail, hotmail, etc.) — on déclasse vs same-domain.
    PERSONAL_DOMS = {
        "gmail.com", "hotmail.com", "hotmail.ca", "hotmail.fr",
        "outlook.com", "outlook.fr", "live.com", "live.ca",
        "yahoo.com", "yahoo.ca", "yahoo.fr",
        "icloud.com", "me.com", "videotron.ca", "sympatico.ca",
        "bellnet.ca", "rogers.com",
    }
    is_personal = email_dom in PERSONAL_DOMS

    if src == "apollo" and contact.get("email_verified"):
        return 1
    if src == "apollo":
        return 2
    if src == "website_scrape" and kind == "nominative" and not is_personal:
        return 3
    if src == "website_scrape" and kind == "nominative" and is_personal:
        return 4
    if src == "website_scrape" and kind == "generic":
        return 5
    return 9


# Champs lus pour ordonner la file d'envoi, jamais transmis à l'agent de
# personnalisation — voir le commentaire dans `list_contacts_to_personalize`.
CHAMPS_INTERNES = ("lead_potential_score", "lead_potential_reason")


def _rang_de_priorite(company: dict[str, Any]) -> tuple[int, int]:
    """Clé de tri d'une company dans la file d'envoi. Plus petit = plus tôt.

    1. Tête de file : un avis dit qu'on n'arrive pas à joindre l'entreprise.
    2. Potentiel décroissant ; un score absent passe en dernier, jamais devant
       un lead mesuré.
    """
    raison = company.get("lead_potential_reason") or ""
    tete = 0 if raison.startswith(MARQUEUR_TETE_DE_FILE) else 1
    score = company.get("lead_potential_score")
    rang_score = -score if isinstance(score, int) and not isinstance(score, bool) else 1
    return (tete, rang_score)


# Les entreprises dont la colonne `fenetre_mois` a été comparée au calcul, et
# celles où les deux divergent.
#
# 🔴 OBSERVATION SEULE — AC1c·A. Le verdict rendu reste TOUJOURS celui du calcul :
# la colonne est neuve, le backfill peut avoir été rattrapé par WF-3, et 119 tests
# du dépôt construisent des fiches sans cette clé.
#
# 🔴 DEUX compteurs, pas un. `divergences=0` seul est ambigu : il vaut 0 quand
# tout concorde ET quand il n'y avait rien à comparer (colonne NULL parce que
# l'écrivain a échoué en silence). `comparaisons=0` distingue les deux.
DIVERGENCES_FENETRE: set[str] = set()
COMPARAISONS_FENETRE: set[str] = set()


def fenetre_saisonniere_ouverte(
    company: dict[str, Any], *, track: str, aujourdhui: date | None = None
) -> bool:
    """L'entreprise est-elle joignable CE MOIS-CI ?

    🔴 LA RÈGLE, ET ELLE VIENT DE LOIN. Spec du 2026-08-27 §3, décision William
    du 2026-08-29 : « La fenêtre d'un métier s'ouvre 3 mois avant le début de sa
    saison et se ferme 2 mois après », et — mot pour mot — « une entreprise
    mono-métier hors saison **n'est pas contactée** : elle attend son ouverture,
    et sera contactée à la bonne période ».

    La règle était implémentée dans `lib/metiers.fenetre_mois` depuis AC1b, mais
    elle ne servait qu'à choisir DE QUEL MÉTIER le courriel parle. Le morceau
    qui décide À QUI on écrit avait été différé en AC1c, avec l'avertissement
    écrit dans le plan AC1b : « rien n'empêche mécaniquement d'écrire à un
    tondeur en octobre ». Il est posé ici le 2026-09-02, sans attendre le reste
    d'AC1c (la vue, les colonnes dérivées, les index) : c'est le seul morceau
    dont le premier envoi a besoin.

    Mesuré le 2026-09-02, jour où il est posé, sur les 403 fiches d'alors :
    **154 joignables en
    septembre**, contre 363 sans le filtre. Les 209 écartés sont surtout des
    paysagistes dont la saison s'est terminée cet été et dont la prochaine
    fenêtre ouvre le 15 janvier.

    🔴 RENVERSÉ LE 2026-09-14 : UNE ENTREPRISE SANS MÉTIER RECONNU N'EST PLUS
    DÉMARCHÉE. Décision William. Cette fonction rendait `True` ici — défaut
    inversé volontaire de la spec du 2026-08-27 §3, « on inclut dans le doute ».

    Ce qui l'a fait tomber, mesuré le 2026-09-14 sur les 457 fiches joignables :
    ces entreprises portent `fenetre_mois = [1..12]`, donc elles étaient les
    SEULES joignables douze mois sur douze pendant que toutes les autres
    attendaient leur saison. En septembre, où seul le déneigement est ouvert,
    elles passaient quand même. Le défaut inversé n'était pas neutre : il
    PRIVILÉGIAIT les fiches qu'on ne sait pas classer.

    Le cas concret : « Conception Perma-Nourricière » a reçu un courriel froid.
    Onze services — design en permaculture, agroforesterie, parcs comestibles
    municipaux, jardins pédagogiques, aide aux subventions — et aucun qui soit
    un métier du catalogue. Ce n'est pas un contracteur de services résidentiels.

    C'est la MÊME règle que celle de Niwa (2026-09-02), étendue au cas qu'elle
    avait laissé ouvert :
        aucun métier reconnu  → ÉCARTÉE (depuis 2026-09-14)
        un métier sans saison → ÉCARTÉE (depuis 2026-09-02)
    Dans les deux cas on écrirait sans savoir à qui — soit faute de donnée, soit
    sur une donnée fausse. La correction est en amont, dans WF-3 et le
    dictionnaire de `lib/metiers`, jamais en devinant ici.

    ⚠️ LE DÉFAUT INVERSÉ DE `lib/metiers` RESTE EN PLACE, et ce n'est pas une
    incohérence. `fenetre_mois` répond à « QUAND est le bon moment ? » et rend
    toujours les douze mois sur l'inconnu ; cette fonction-ci répond à « A-T-ON
    LE DROIT d'écrire ? ». La spec distinguait déjà les deux — permission
    contre optimisation — et c'est la permission qui change de réponse.
    Renverser AUSSI la colonne casserait son sens.

    ⚠️ L'OBJECTION DE LA SPEC RESTE VALABLE : « le silence serait invisible ».
    Elle n'est pas écartée, elle est déplacée — ces entreprises doivent se voir
    dans `agence.v_pourquoi_pas_de_courriel`. Un refus muet redeviendrait le
    défaut que la spec craignait.

    ⚠️ Ne s'applique QU'À la piste `agence-ia`. OPT est gelée et ses métiers
    (dentiste, physio) n'ont pas de saison ; y appliquer la fenêtre écarterait
    tout le monde en silence.
    """
    if (track or "").strip() != "agence-ia":
        return True

    services = ((company.get("research_json") or {}).get("services_offered")) or []
    resolus = resoudre_metiers(
        services, aujourdhui or date.today(), industry=company.get("industry")
    )
    if not resolus.metiers:
        # 🔴 `False` depuis le 2026-09-14 — voir le docstring. C'était `True`,
        # et l'inverser relève d'une décision de William, pas d'un nettoyage :
        # ne pas « corriger » ce retour en croyant restaurer le garde-fou nº2
        # de la spec. Le garde-fou vit toujours, mais dans `lib/metiers`, sur la
        # colonne `fenetre_mois` — pas sur la permission d'écrire.
        #
        # ⚠️ Comme les deux autres sorties anticipées, celle-ci court-circuite
        # l'observation de la ceinture plus bas, et c'est VOULU : la colonne
        # porte `[1..12]` sur ces fiches (défaut inversé, inchangé), donc elle
        # divergerait systématiquement du verdict. Ce serait du bruit, pas un
        # signal — la colonne et le verdict ne répondent plus à la même
        # question sur ce cas précis.
        return False

    # 🔴 UN MÉTIER 12 MOIS SUR 12 N'ENCLENCHE PAS LA SÉQUENCE — règle William du
    # 2026-09-02, et c'est la MÊME que celle du choix de la scène.
    #
    # Sa formulation : « si les entreprises ont un métier secondaire qui est
    # 12 mois sur 12, il ne peut pas enclencher la séquence de contact. Il peut
    # seulement être référencé plus loin dans le courriel. »
    #
    # Ce que ça corrigeait, mesuré le 2026-09-02 : 43 leads passaient le
    # filtre sur `pavage`
    # ou `excavation` alors que leur vraie saison — paysagement, lavage de
    # vitres — était fermée jusqu'en janvier. Le courriel leur disait « la
    # saison approche » quatre mois trop tôt. Vérifié : ce sont 28 paysagistes
    # et 12 laveurs de vitres, donc la cible exacte, pas du bruit à écarter.
    #
    # ⚠️ Ces métiers ne sont PAS retirés de la fiche : ils restent dans
    # `resolus.metiers` et se font nommer au 2ᵉ temps du courriel (« tu fais
    # aussi du pavage »). Ils ne peuvent simplement pas OUVRIR.
    #
    # 🔴 Une entreprise dont TOUS les métiers sont sans saison est ÉCARTÉE, et
    # elle ne sera JAMAIS contactée tant que sa fiche ne dit pas mieux.
    #
    # ⚠️ Un repli sur `industry` existe (`metiers.metier_depuis_industry`) et
    # la rendrait joignable en lui rendant son métier depuis son mot-clé de
    # sourcing. Il est DÉBRANCHÉ, décision William du 2026-09-02 : si la seule
    # chose qu'on reconnaît d'un paysagiste est « pavage », la donnée est
    # mauvaise, et on ne devine pas. La correction est en amont, dans WF-3.
    #
    # ⚠️ Ce commentaire affirmait l'inverse jusqu'au conseil du 2026-09-02 — il
    # disait que le repli s'appliquait. Une session future l'aurait lu devant
    # une fiche écartée, aurait conclu à un bogue, et aurait « réparé » en
    # rebranchant le repli, c'est-à-dire en annulant la décision.
    verdict = any(m in SAISONS for m in resolus.fenetre_ouverte)

    # Observation : la colonne dit-elle la même chose ? On ne change rien au
    # verdict — voir le commentaire des deux compteurs.
    #
    # ⚠️ Les DEUX `return True` plus haut court-circuitent l'observation, et
    # c'est ASSUMÉ, pas un oubli : la piste ≠ `agence-ia` n'est pas filtrée du
    # tout, et une fiche dont aucun métier n'est reconnu porte `[1..12]` en
    # colonne (défaut inversé des deux côtés) — elle ne PEUT pas diverger.
    colonne = company.get("fenetre_mois")
    # 🔴 `isinstance` ET PAS `is not None`. Sans lui, une colonne d'un type
    # inattendu (une chaine, un entier — impossible depuis la base, qui rend un
    # integer[], mais possible depuis une fiche construite a la main dans un test
    # ou un script) ferait lever `mois in colonne`. Or AUCUN `try` ne protege le
    # chemin `fenetre_saisonniere_ouverte` -> `_retenir` ->
    # `list_contacts_to_personalize` -> `_run_wf4` : l'exception ne refuserait pas
    # UNE entreprise, elle les refuserait TOUTES. Une ceinture qui ne refuse
    # jamais n'a pas le droit de pouvoir lever.
    if isinstance(colonne, (list, tuple, set, frozenset)):
        COMPARAISONS_FENETRE.add(str(company.get("id")))
        mois = (aujourdhui or date.today()).month
        if (mois in colonne) != verdict:
            DIVERGENCES_FENETRE.add(str(company.get("id")))

    return verdict


async def list_contacts_to_personalize(
    limit: int = 20,
    *,
    require_research: bool = True,
    max_per_company: int = 1,
    track: str = "OPT",
) -> list[dict[str, Any]]:
    """Contacts prêts pour personnalisation : email présent, company.research_json
    présent (sinon le prompt n'a rien à se mettre sous la dent), pas encore de
    draft outbound dans messages.

    `max_per_company` (défaut 1) limite le nombre de contacts retournés par
    company, en gardant les meilleurs selon `_contact_priority_score`. Évite
    d'envoyer plusieurs emails séparés à la même entreprise.

    On filtre côté Python plutôt que via une jointure PostgREST compliquée :
    1) On récupère les contacts avec email + status='new' ou 'ready'.
    2) On joint manuellement avec companies.research_json.
    3) On exclut ceux qui ont déjà un message outbound VIVANT — et, depuis le
       2026-09-10, toute ENTREPRISE dont un contact en a un : `max_per_company`
       ne vaut qu'à l'intérieur d'un lot, donc deux lots consécutifs
       repiochaient chez la même boîte (PROGAZON en avait reçu quatre) NON abandonné (tout
       status sauf 'failed' — voir le commentaire sur la requête messages).
    4) On garde les top-N contacts par company selon priorité.
    """
    # 🔴 LA FILE SE LIT PAR PAGES, jusqu'à en avoir assez — plus par une fenêtre
    # fixe. C'est le correctif de la FAMINE, ouverte depuis mai et refermée le
    # 2026-09-09.
    #
    # L'ancienne version lisait `limit * FACTEUR_SURRECOLTE` contacts, une fois,
    # et filtrait ensuite. La fenêtre était donc proportionnelle au LOT, alors
    # que ce qu'il faut franchir est proportionnel à la FILE — le bouchon des
    # contacts déjà rédigés, qui grossit chaque jour.
    #
    # Ce que ça donnait, mesuré le 2026-09-09 avec 58 brouillons déjà écrits :
    #     91 contacts éligibles, mais le premier en 87ᵉ position
    #     limit=20 → lit 240 → 45 éligibles dedans → rend 20   ✅
    #     limit=10 → lit 120 →  2 éligibles dedans → rend  2   ❌
    #
    # Diviser le lot par deux divisait la fenêtre par deux, et la famine
    # revenait — le lot serait tombé à 2 dès le lendemain, puis à 0, en silence.
    #
    # Monter le facteur ne ferait que déplacer le seuil : le bouchon grandit
    # avec chaque envoi. Lire par pages jusqu'à avoir son compte supprime la
    # question, et ne coûte rien quand la file est courte (une page suffit).
    #
    # ⚠️ `MAX_PAGES_SELECTION` borne le coût sur une file énorme. L'atteindre
    # veut dire « la file est majoritairement bouchée » — c'est exactement ce
    # que l'alerte de famine doit dire, et elle se déclenche alors d'elle-même
    # puisque le lot revient court.
    # ⚠️ Borné à 1000 : PostgREST coupe TOUTE réponse à `max-rows=1000`, sans
    # erreur ni en-tête. À `limit=84`, `limit * FACTEUR_SURRECOLTE` demandait
    # 1008 lignes, la page en rendait 1000, et `len(lot) < taille_page` faisait
    # conclure « file épuisée » après une seule lecture. La priorisation
    # dépendant maintenant d'une lecture complète, cette troncature muette
    # aurait décidé de l'ordre d'envoi.
    taille_page = min(max(limit * FACTEUR_SURRECOLTE, 200), 1000)
    contacts: list[dict[str, Any]] = []
    for page in range(MAX_PAGES_SELECTION):
        lot = await db.select(
            "contacts",
            params={
                "select": (
                    "id,first_name,last_name,email,email_verified,title,company_id,"
                    "status,email_verification_source,raw_payload,track,"
                    "owner_confidence,potential_owner"
                ),
                "email": "not.is.null",
                "status": "in.(new,ready)",
                "track": f"eq.{track}",  # filtre track au niveau DB (sinon les
                # contacts d'un track minoritaire sont noyés par l'over-fetch)
                "order": "created_at.asc",
                "limit": str(taille_page),
                "offset": str(page * taille_page),
            },
        )
        if not lot:
            break
        contacts.extend(lot)
        if len(lot) < taille_page:
            break

    # 🔴 On lit TOUTE la file avant de retenir, décision William du 2026-09-09.
    # Avant, la lecture s'arrêtait dès que le lot était plein : le tri par
    # potentiel n'ordonnait alors que la première page, et une entreprise
    # marquée « tête de file » assise en page 2 ne remontait jamais — elle
    # attendait que la file devant elle se vide. À 345 contacts en file (mesuré
    # le 2026-09-09), tout lire coûte 2 pages au lieu d'une : la priorisation
    # devient vraie partout pour une lecture de plus.
    return await _retenir(
        contacts, limit=limit, max_per_company=max_per_company,
        track=track, require_research=require_research,
    )


# Un filtre PostgREST `in.(...)` voyage dans l'URL : 345 identifiants font déjà
# ~13 ko, et la file grossit. Depuis qu'on lit la file ENTIÈRE avant de retenir
# (décision du 2026-09-09), le filtre porte sur tout d'un coup — donc on le
# découpe. Sans ça, le jour où la file passe le seuil du serveur, la requête
# revient en 414 et le lot se vide en silence.
TAILLE_TRANCHE_IN = 120

# PostgREST coupe toute reponse a 1000 lignes, sans erreur ni en-tete.
PLAFOND_POSTGREST = 1000


async def _select_par_tranches(
    table: str,
    *,
    params: dict[str, str],
    ids: list[str],
    cle: str = "id",
    order: str | None = None,
) -> list[dict[str, Any]]:
    """`select` avec un filtre `in.(...)` découpé en tranches, résultats concaténés.

    `order` non nul → chaque tranche est PAGINÉE et ne se fait donc pas couper
    au plafond de 1000 lignes. À utiliser dès qu'UNE TRANCHE peut dépasser ce
    plafond : 120 contacts qui portent chacun une
    poignée de messages y arrivent, et une réponse tronquée ferait disparaître
    des contacts de `already_drafted` — donc un deuxième courriel à quelqu'un
    qui en a déjà reçu un.
    """
    lignes: list[dict[str, Any]] = []
    for debut in range(0, len(ids), TAILLE_TRANCHE_IN):
        tranche = ids[debut:debut + TAILLE_TRANCHE_IN]
        filtre = {**params, cle: f"in.({','.join(tranche)})"}
        if order is None:
            lot = await db.select(table, params=filtre)
            lignes.extend(lot)
            # Une tranche qui revient EXACTEMENT pleine a très probablement été
            # coupée par PostgREST, qui tronque sans erreur ni en-tête. On ne
            # peut pas le distinguer d'un hasard, mais le crier coûte une ligne
            # de journal et évite le mode de panne le plus cher du projet :
            # celui qu'on ne voit pas.
            if len(lot) >= PLAFOND_POSTGREST:
                logging.getLogger("wf4").warning(
                    "%s : une tranche de %d lignes revient au plafond PostgREST "
                    "— la réponse est probablement TRONQUÉE, il manque des lignes",
                    table, len(lot),
                )
            continue
        # Tranche paginée : on reste sur `select`, mais on redemande tant que
        # la page revient pleine. Une page courte dit la fin — donc un faux
        # `select` de test qui ignore `limit`/`offset` sort au premier tour.
        for page in range(MAX_PAGES_SELECTION):
            lot = await db.select(table, params={
                **filtre, "order": order,
                "limit": str(PLAFOND_POSTGREST),
                "offset": str(page * PLAFOND_POSTGREST),
            })
            lignes.extend(lot)
            if len(lot) < PLAFOND_POSTGREST:
                break
        else:
            # Le plafond de pages est atteint et la dernière était pleine : la
            # réponse est TRONQUÉE. Le seuil est passé de 1 000 à 10 000, mais
            # le mode de panne est le même — un contact absent d'
            # `already_drafted` reçoit un deuxième courriel. Ça se crie.
            logging.getLogger("wf4").warning(
                "%s : tranche tronquée à %d lignes (plafond de pages atteint) — "
                "des lignes manquent, un contact déjà rédigé peut repasser",
                table, MAX_PAGES_SELECTION * PLAFOND_POSTGREST,
            )
    return lignes


async def _retenir(
    contacts: list[dict[str, Any]],
    *,
    limit: int,
    max_per_company: int,
    track: str,
    require_research: bool,
) -> list[dict[str, Any]]:
    """Les contacts qui méritent un courriel, dans l'ordre de PRIORITÉ.

    Séparée de la lecture pour que celle-ci puisse tourner en boucle. Prend la
    liste ACCUMULÉE et non la dernière page : les déduplications par entreprise
    et par courriel doivent voir tout ce qui précède, sinon la page 2 pourrait
    resservir une entreprise déjà retenue en page 1.
    """
    if not contacts:
        return []

    company_ids = list({c["company_id"] for c in contacts})
    companies = await _select_par_tranches(
        "companies",
        params={
            # google_rating / google_reviews_count : l'ancre factuelle du bloc 2
            # (AC1b). Sans elles ici, tout le reste du câblage lit None en
            # silence et le bloc saute 255 fois sur 255.
            # google_place_id : la garde « sans site » (une entreprise sans
            # website n'est démarchée que si sa fiche Google est exploitable).
            # lead_potential_* : SERVENT UNIQUEMENT à ordonner le lot ; ils sont
            # retirés avant d'être rendus (voir CHAMPS_INTERNES).
            "select": (
                "id,name,nom_usage,status,recontact_manuel,domain,website,city,icp_segment,industry,research_json,track,"
                "google_rating,google_reviews_count,google_place_id,"
                # lead_potential_* : SERVENT UNIQUEMENT à ordonner le lot ; ils
                # sont retirés avant d'être rendus (voir CHAMPS_INTERNES).
                "lead_potential_score,lead_potential_reason,"
                # 🔴 PROJETÉE POUR ÊTRE COMPARÉE, PAS POUR DÉCIDER (AC1c·A).
                # Sans elle, `company.get("fenetre_mois")` rend None en production,
                # l'observation est court-circuitée, et /wf4/run remonte
                # `comparaisons_fenetre=0` à tous les coups — ce qui se lit, par la
                # convention que cette même conversation installe, comme « l'écrivain
                # de la colonne est en panne ».
                # Ajouter un champ a une PROJECTION ne peut pas changer quelles
                # lignes PostgREST rend : la requete ne filtre que par `id=in.(...)`,
                # sans ressource embarquee ni `!inner`, et l'eligibilite en aval ne
                # lit jamais cette colonne pour decider.
                # ⚠️ CE N'EST PAS LE REJEU SUR INSTANTANE QUI LE PROUVE. Sa fausse
                # base ignore le `select` : elle rend la ligne entiere quoi qu'on
                # demande, donc elle est AVEUGLE a ce changement. Le raisonnement
                # ci-dessus est la preuve ; le rejeu n'en est pas une.

                "fenetre_mois"
            ),
        },
        ids=company_ids,
    )
    # by_id restreint au `track` demandé → un contact dont la company est d'un autre
    # track est ignoré (company=None dans la boucle). Isolation OPT/REACTI.
    by_id = {c["id"]: c for c in companies if (c.get("track") or "OPT") == track}

    # `status=not.in.(failed)` : un message ABANDONNÉ ne gèle plus son contact.
    # 'failed' = « ne partira jamais » (garde d'envoi : blocklist domaine, opt-out,
    # ou draft retiré à la main). Sans cette exclusion, un draft refusé par WF-5
    # (compliance_check_passed=false, jamais re-jugé car WF-5 ne juge que le NULL)
    # gelait son contact À VIE, et la seule sortie était de DELETE le message —
    # ce qui détruit la trace du refus (sujet, corps, verdict, notes). Garder la
    # ligne et la marquer 'failed' est la façon de retirer un draft SANS effacer
    # cette histoire.
    # Seul 'failed' est retiré du jeu bloquant. draft/queued/sent/delivered/replied
    # = message vivant ou déjà remis (contact engagé, surtout pas de 2e courriel).
    # 'bounced' bloque aussi : l'adresse est morte, re-drafter ne ferait que
    # re-bouncer (jetons brûlés + réputation d'envoi) — ça se règle au niveau
    # contact, pas en régénérant un draft.
    # (messages.status est NOT NULL DEFAULT 'draft' → pas de piège NULL avec not.in.)
    # 🔴 L'EXCLUSION PORTE SUR L'ENTREPRISE, PAS SEULEMENT SUR LE CONTACT.
    #
    # Mesuré le 2026-09-10 : 85 leads pour 78 entreprises, et PROGAZON avait
    # reçu QUATRE courriels froids, sur les bras A, C et D. `max_per_company`
    # ne vaut qu'à l'intérieur d'un lot : celui de 12 h retenait le contact 1 et
    # écartait le contact 2, celui de 12 h 30 revoyait le contact 2, ne le
    # trouvait dans aucun message, et le servait.
    #
    # C'est d'abord un problème d'ENVOI — quatre courriels froids à la même
    # boîte, c'est ce qui fait dire « c'est du spam », et ça se voit chez le
    # prospect avant de se voir chez nous. Accessoirement, le bras du deuxième
    # courriel est confondu avec « cette entreprise nous a déjà vus ».
    #
    # ⚠️ ON REMONTE DES ENTREPRISES VERS TOUS LEURS CONTACTS, et pas seulement
    # ceux de la page. `send.py` passe le contact à `status='contacted'` au
    # push : le frère DÉJÀ SERVI a donc quitté le `status in (new, ready)` que
    # lit la sélection. Déduire l'exclusion des contacts de la page raterait
    # exactement le cas dangereux — celui où l'entreprise a déjà reçu son
    # courriel.
    #
    # ⚠️ `select_all` ET PAS `select`. PostgREST coupe TOUTE réponse à 1000
    # lignes sans rien signaler (voir supabase_client), et cette lecture-ci
    # grossit avec la file : les pages sont accumulées, donc jusqu'à
    # MAX_PAGES_SELECTION * taille_page entreprises candidates. Au ratio mesuré
    # de 1,27 contact par entreprise, la coupe mord vers 790 entreprises en
    # file — ~2,5 fois celle d'aujourd'hui.
    #
    # Ce que la troncature ferait, et personne ne le verrait : `company_par_contact`
    # amputé, donc (1) une entreprise déjà démarchée redevient éligible — le
    # défaut même que ce bloc referme — et (2) un contact de la page tombé hors
    # des 1000 n'a plus son propre message interrogé, donc il est RE-RÉDIGÉ.
    # Le second est une régression par rapport à l'ancienne version, qui
    # interrogeait directement les ids de la page.
    freres = await db.select_all(
        "contacts",
        order="id",
        params={
            "select": "id,company_id",
            "company_id": f"in.({','.join(company_ids)})",
        },
    )
    company_par_contact = {f["id"]: f["company_id"] for f in freres}

    # `status=not.in.(failed)` : un message ABANDONNÉ ne gèle ni son contact ni
    # son entreprise. 'failed' est la façon PRÉVUE de retirer un brouillon à la
    # main ; bloquer dessus gèlerait toute la boîte au lieu de la libérer.
    existing_msgs = await db.select_all(
        "messages",
        order="id",
        params={
            "select": "contact_id",
            "contact_id": f"in.({','.join(company_par_contact)})",
            "direction": "eq.outbound",
            "status": "not.in.(failed)",
        },
    ) if company_par_contact else []
    already_drafted = {m["contact_id"] for m in existing_msgs}
    entreprises_engagees = {
        company_par_contact[cid]
        for cid in already_drafted
        if cid in company_par_contact
    }

    # Filtre + groupe par company
    eligible: dict[str, list[dict[str, Any]]] = {}
    for c in contacts:
        if c["id"] in already_drafted:
            continue
        # Une entreprise déjà servie ne revient pas, quel que soit le contact.
        if c["company_id"] in entreprises_engagees:
            continue
        company = by_id.get(c["company_id"])
        if not company:
            continue
        # 🔴 LE STATUT DE L'ENTREPRISE, ET IL MANQUAIT — trouve par un conseil
        # de relecture le 2026-09-17, quelques heures apres le commit qui
        # s'intitulait « une disqualification sort la fiche du circuit ».
        #
        # Elle sortait du circuit de RECHERCHE (`list_companies_to_research`
        # filtre `not.in.(disqualified,no_web_presence)`) mais PAS de celui de
        # REDACTION : cette boucle testait le brouillon deja ecrit, l'entreprise
        # deja servie, la recherche, le site, la saison — jamais le statut.
        #
        # 📏 Le cas vivant au moment de la correction : *Strathmore Commercial
        # Landscape Management*, passee `disqualified` le soir meme par la
        # nouvelle regle (« 25 employes ou plus / repartiteur en place —
        # entreprise nationale avec 250+ camions »), avec TROIS contacts en
        # `new` et aucun message. Rien ne l'empechait de sortir dans le lot du
        # lendemain midi.
        #
        # ⚠️ `suppressed` est dans la liste pour une raison differente et plus
        # lourde : c'est le statut d'un desabonnement. Ecrire a une fiche
        # `suppressed` n'est pas une maladresse, c'est une infraction LCAP.
        if company.get("status") in ("disqualified", "suppressed"):
            continue
        # 🔴 LA DECISION HUMAINE L'EMPORTE — et elle ne l'emportait nulle part.
        #
        # La migration 0032 s'intitule « La decision humaine doit avoir un
        # endroit ou vivre, ET ELLE DOIT L'EMPORTER », et le commentaire de la
        # colonne dit qu'elle « l'emporte TOUJOURS sur le verdict calcule ».
        # Mesure du 2026-09-17 : `recontact_manuel` n'etait lu par AUCUN code
        # Python — seulement par `v_pourquoi_pas_de_courriel`. Une decision de
        # William apparaissait donc au tableau de bord pendant que le pipeline
        # redigeait quand meme.
        #
        # 📏 Le cas qui l'a montre : *Terminix Canada*, marquee `hors_cible` le
        # 2026-09-10 (« multinationale Rentokil/Terminix : hors cible agence-ia
        # (contracteurs QC) — decision William »), balayee le 2026-09-17 par une
        # remise en file de masse. Rien ne l'aurait arretee.
        #
        # ⚠️ `a_juger` n'est PAS bloquant, et c'est un choix : il veut dire « un
        # humain doit regarder », pas « ne la contacte pas ». Le bloquer gelerait
        # des fiches sur une demande d'attention. Si ca doit changer, c'est une
        # decision de William, pas une extension silencieuse de cette liste.
        if company.get("recontact_manuel") in ("hors_cible", "jamais"):
            continue
        if require_research and not company.get("research_json"):
            continue
        if not site_ou_fiche_exploitable(company):
            continue
        if not fenetre_saisonniere_ouverte(company, track=track):
            continue
        eligible.setdefault(c["company_id"], []).append(c)

    retenus: list[dict[str, Any]] = []
    # Préserve l'ordre d'arrivée des companies (created_at.asc du premier contact).
    seen_companies: list[str] = []
    for c in contacts:
        if c["company_id"] in eligible and c["company_id"] not in seen_companies:
            seen_companies.append(c["company_id"])

    # Ordre d'émission. Jusqu'au 2026-09-01, le lot sortait dans l'ordre
    # d'arrivée des contacts (`created_at.asc`) et le score de potentiel ne
    # triait RIEN — il était écrit, jamais lu. Désormais : les leads dont un
    # avis dit qu'on n'arrive pas à joindre l'entreprise passent devant, puis
    # le potentiel décroissant, puis l'ordre d'arrivée (le tri est stable).
    #
    # 🔴 CE TRI NE DOIT JAMAIS SERVIR À TIRER LE GABARIT A/B/C/D. L'alternance
    # se fait au RANG D'ARRIVÉE (`rang_arrivee`, posé plus bas) : le rang de
    # priorité est une fonction du score, donc le bras A recevrait toujours les
    # meilleurs leads et D les plus faibles, et `v_perf_par_bras` mesurerait la
    # qualité des leads au lieu de la copie. Voir `lib/gabarits.bras_du_lot`.
    seen_companies.sort(key=lambda cid: _rang_de_priorite(by_id[cid]))

    # Dédup global sur email : si plusieurs companies pointent vers le même email
    # (cas chaînes où Google Places retourne plusieurs succursales), garder
    # uniquement la première company rencontrée pour ce email. Depuis le tri
    # par potentiel, « première » veut dire LA MIEUX NOTÉE, et non plus la plus
    # ancienne : à courriel partagé, c'est le meilleur lead qui le garde.
    seen_emails: set[str] = set()
    for company_id in seen_companies:
        group = eligible[company_id]
        group.sort(key=_contact_priority_score)
        for c in group[:max_per_company]:
            email_key = (c.get("email") or "").lower()
            if email_key in seen_emails:
                continue
            seen_emails.add(email_key)
            retenus.append({
                "contact": c,
                # Rempli juste avant le retour — voir `_poser_rang_arrivee`.
                "rang_arrivee": 0,
                # Le marqueur de tête de file est INTERNE : écrire « j'ai vu que
                # tes clients disent que tu ne rappelles pas » citerait un tiers
                # au prospect à son sujet et ruinerait le courriel. Il ordonne,
                # il ne parle pas. On le retire donc du dict qui descend vers
                # l'agent de personnalisation.
                "company": {
                    k: v for k, v in by_id[company_id].items()
                    if k not in CHAMPS_INTERNES
                },
            })
            if len(retenus) >= limit:
                return _poser_rang_arrivee(retenus, contacts)
    return _poser_rang_arrivee(retenus, contacts)


def _poser_rang_arrivee(
    retenus: list[dict[str, Any]], contacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Numérote les retenus dans leur ordre d'ARRIVÉE, sans changer leur ordre d'envoi.

    🔴 C'est la clé de tri du PARCOURS de `/wf4/run` : le lot y est traversé
    dans l'ordre d'arrivée, et c'est ce qui décorrèle le gabarit A/B du score.
    Le bras lui-même vient d'ailleurs — `rang_du_bras`, un compteur lu en base
    qui continue d'un lot à l'autre. Les deux sont nécessaires : le compteur
    seul reste corrélé (à 10 brouillons par lot, le décalage vaut 2 modulo 4,
    donc le meilleur lead alternerait entre A et C sans jamais tomber sur B
    ni D), et l'ordre d'arrivée seul ne répartit pas également les quatre bras.

    ⚠️ Ne retire pas le `sorted()` de `/wf4/run` en croyant qu'il ne sert à
    rien : il EST le découplage.

    La liste rendue garde l'ordre de priorité (l'ordre d'ENVOI) ; seule
    l'étiquette `rang_arrivee` change.
    """
    position = {c["id"]: i for i, c in enumerate(contacts)}
    par_arrivee = sorted(
        range(len(retenus)),
        key=lambda i: position.get(retenus[i]["contact"]["id"], 0),
    )
    for rang, i in enumerate(par_arrivee):
        retenus[i]["rang_arrivee"] = rang
    return retenus


# Seuil de la garde « sans site ». C'est UN BOUTON, pas une vérité : la spec le
# dit explicitement. En dessous, il n'y a ni matière pour écrire sur eux ni
# matière pour bâtir un site.
MIN_AVIS_SANS_SITE = 3


def site_ou_fiche_exploitable(company: dict[str, Any]) -> bool:
    """Une entreprise sans site est-elle démarchable ?

    Les entreprises sans `website` (97 au 2026-08-30) reçoivent la variante
    « je pourrais te
    créer un site ». Encore faut-il qu'on ait de quoi écrire : la garde exige
    une fiche Google exploitable (`google_place_id` renseigné et au moins
    quelques avis).

    🔴 Sans cette garde, l'implicite produit le mensonge par défaut : les deux
    gabarits et le repli disent tous « ton site », et le lead sans site ni fiche
    recevrait un courriel qui parle d'un site inexistant à une entreprise dont
    on ne sait rien.

    ⚠️ Le motif du saut n'apparaît PAS encore dans `v_pourquoi_pas_de_courriel`
    — la vue appartient à AC1c, différé. Le lead est donc écarté silencieusement
    pour l'instant, ce qui est assumé et inscrit au plan.
    """
    if (company.get("website") or "").strip():
        return True
    if not (company.get("google_place_id") or "").strip():
        return False
    return (company.get("google_reviews_count") or 0) >= MIN_AVIS_SANS_SITE


class MessageDraftIn(BaseModel):
    contact_id: str
    campaign_id: str | None = None
    sequence_step_id: str | None = None
    subject: str
    body_text: str
    from_email: str | None = None
    to_email: str
    generated_by_agent_run: str | None = None
    compliance_check_passed: bool | None = None
    compliance_notes: str | None = None
    demo_url: str | None = None
    # Le bras du test A/B REELLEMENT ecrit (migration 0047). Jamais 'AB' : la
    # contrainte de la colonne l'interdit, parce que le stocker mettrait la
    # meme valeur sur 100 % des lignes et il n'y aurait aucun test.
    template_choice: str | None = None
    # Les corps des relances, {"relance_1": "...", "relance_2": "..."}
    # (migration 0046). NULL sur la piste OPT, qui n'a pas de relances.
    followups: dict[str, Any] | None = None
    # Les bras qui pouvaient REELLEMENT etre servis a ce contact (migration
    # 0055). PAS le parametre du lot : l'ensemble APRES la garde des tetes
    # fixes. Sans lui, comparer A/B a C/D revient a comparer deux lots.
    bras_eligibles: str | None = None
    # Ce dont ce courriel a parle, FIGE (migration 0058). Jamais relu depuis
    # companies : AC1c ajoute precisement le recalcul qui reclasse, et relire au
    # moment de l'analyse reecrirait l'histoire.
    metiers: list[str] | None = None
    metier_scene: str | None = None


async def insert_message_draft(payload: MessageDraftIn) -> dict[str, Any]:
    """Insert un draft outbound dans messages. Le Compliance Agent (WF-5) le
    validera avant envoi."""
    row = payload.model_dump(exclude_none=True)
    row["direction"] = "outbound"
    row["status"] = "draft"
    rows = await db.insert("messages", row)
    return {"message_id": rows[0]["id"] if rows else None}


# ----------------------------------------------------------------------
# Research (Phase 2 — WF-3)
# ----------------------------------------------------------------------

# 🔴 LA DECISION HUMAINE L'EMPORTE — et elle ne l'emportait NULLE PART.
#
# La migration 0032 s'intitule « La decision humaine doit avoir un endroit ou
# vivre, ET ELLE DOIT L'EMPORTER », et le commentaire de la colonne promet
# qu'elle « l'emporte TOUJOURS sur le verdict calcule ». Mesure du 2026-09-17 :
# `recontact_manuel` n'etait lu par AUCUN code Python — seulement par
# `v_pourquoi_pas_de_courriel`. La decision apparaissait au tableau de bord
# pendant que le pipeline recherchait et redigeait quand meme.
#
# 📏 Le cas : *Terminix Canada*, marquee `hors_cible` le 2026-09-10
# (« multinationale Rentokil/Terminix : hors cible agence-ia — decision
# William »), balayee le 2026-09-17 par une remise en file de masse. Rien ne
# l'aurait arretee.
#
# ⚠️ POURQUOI EN PYTHON ET PAS DANS LA REQUETE — deux pieges mesures avant de
# deployer, chacun silencieux :
#   1. `recontact_manuel=not.in.(hors_cible,jamais)` laisse passer ZERO fiche
#      sur 723. En SQL, `NULL not in (...)` ne vaut pas vrai mais NULL, donc la
#      ligne est ECARTEE — et 721 fiches sur 723 ont la colonne a NULL. WF-3 se
#      serait tu entierement, sans une seule erreur : un lot vide ne plante pas.
#   2. `params` porte DEJA une cle `"or"`, celle qui distingue « jamais
#      recherchee » de « a reprendre apres 90 jours ». Une seconde cle `"or"`
#      dans le meme litteral l'ECRASE en silence — Python ne previent pas.
# Deux lignes de Python valent mieux qu'une syntaxe PostgREST qu'on ne peut pas
# exercer depuis la suite de tests.
_DECISIONS_QUI_ECARTENT = ("hors_cible", "jamais")


def _sans_decision_humaine_contraire(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Retire les fiches que William a mises hors cible ou interdites.

    ⚠️ `a_juger` ne bloque PAS, et c'est un choix : il veut dire « un humain
    doit regarder », pas « ne la contacte pas ». Le bloquer gelerait des fiches
    sur une simple demande d'attention. Si ca doit changer, c'est une decision
    de William, pas un elargissement discret de cette liste.
    """
    return [
        r for r in rows
        if r.get("recontact_manuel") not in _DECISIONS_QUI_ECARTENT
    ]


async def list_companies_to_research(
    limit: int = 20,
    *,
    require_website: bool = True,
    track: str = "agence-ia",  # track live ; OPT retiré = jamais sélectionné sauf track="OPT" explicite
) -> list[dict[str, Any]]:
    """Backlog de recherche : jamais researchée, OU researchée sans contact il y a
    plus de 90 jours.

    OPT : exige un website (`require_website=True`) — pas de site = pas de matière.
    REACTI : peut tourner avec `require_website=False` pour traiter les boîtes sans
    site dont la découverte (WF-reacti-2) a trouvé un contact. Dans ce mode, on
    exige tout de même `website NOT NULL OR a >=1 contact` (anti-gaspillage : une
    boîte sans contact ni site n'a rien à personnaliser en aval).

    Exclut les statuts terminaux (disqualified, no_web_presence).
    """
    # Deux portes vers le backlog : jamais researchée, OU researchée sans contact
    # il y a plus de 90 jours (un site peut publier une adresse entre-temps).
    # 90 jours : repasser tout le parc à chaque cron coûterait cher pour peu de
    # rendement, ne jamais repasser figerait 145 entreprises pour toujours
    # (mesuré le 2026-08-17).
    #
    # ⚠️ `research_json is null` vit DANS la porte 'sourced', jamais en filtre de
    # premier niveau : toute company 'researched_no_contact' a par construction un
    # research_json (c'est la passe de recherche qui pose ce statut), donc un ET
    # global sur research_json annulerait la seconde porte en silence.
    limite_reprise = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    params: dict[str, str] = {
        "select": "id,name,domain,website,city,icp_segment,industry,google_place_id,status,track,recontact_manuel",
        "or": (
            "(and(status.eq.sourced,research_json.is.null),"
            f"and(status.eq.researched_no_contact,last_enriched_at.lt.{limite_reprise}))"
        ),
        "google_place_id": "not.is.null",
        "status": "not.in.(disqualified,no_web_presence)",
        "track": f"eq.{track}",
        # La file TOURNE : jamais recherchée d'abord, puis la moins récemment
        # recherchée, created_at en départage. Même rotation que la file d'envoi
        # (P4.10 / migration 0028, `last_send_attempt_at.asc.nullsfirst`), et pour la
        # même raison : en `created_at.asc` pur, les 145 'researched_no_contact' —
        # créées AVANT la plupart des 'sourced' — front-runneraient à chaque passe
        # 225 entreprises jamais recherchées au 2026-08-17. Une file qui sert les
        # échecs connus
        # avant les pistes neuves priorise le mauvais travail. `last_enriched_at`
        # joue ici le rôle de `last_send_attempt_at` : NULL = jamais recherchée.
        "order": "last_enriched_at.asc.nullsfirst,created_at.asc",
        "limit": str(limit),
    }
    if require_website:
        params["website"] = "not.is.null"
    rows = await db.select("companies", params=params)
    rows = _sans_decision_humaine_contraire(rows)
    if require_website or not rows:
        return rows

    # Mode no-website : garder website NOT NULL OU company avec >=1 contact.
    candidate_ids = [r["id"] for r in rows if not r.get("website")]
    with_contact: set[str] = set()
    if candidate_ids:
        contacts = await db.select(
            "contacts",
            params={
                "select": "company_id",
                "company_id": f"in.({','.join(candidate_ids)})",
            },
        )
        with_contact = {c["company_id"] for c in contacts}
    return [r for r in rows if r.get("website") or r["id"] in with_contact]


async def list_companies_to_discover(
    limit: int = 20,
    track: str = "agence-ia",
) -> list[dict[str, Any]]:
    """Backlog WF-reacti-2 : companies agence-ia encore 'sourced', sans website ni
    research_json. Ce sont les boîtes pour lesquelles tenter une découverte web.
    (track 'agence-ia' = ex-REACTI renommé, pivot 2026-06-07.)
    """
    return await db.select(
        "companies",
        params={
            "select": "id,name,city,address,raw_payload,status,track",
            "track": f"eq.{track}",
            "website": "is.null",
            "research_json": "is.null",
            "status": "eq.sourced",
            "order": "created_at.asc",
            "limit": str(limit),
        },
    )


# Un modèle qui répond « aucune » remplit quand même le tableau. Sans ce
# filtre, `bool([...])` mettait le score à 0 et supprimait la tête de file,
# sans log ni trace.
_MOTS_VIDES_DISQUALIFICATION = frozenset({
    "", "aucune", "aucun", "n/a", "na", "non", "rien", "néant", "neant",
    "aucune disqualification", "pas de disqualification",
})


def _porte_une_disqualification(research_json: dict[str, Any]) -> bool:
    lignes = research_json.get("disqualifications")
    if not isinstance(lignes, list):
        return False
    return any(
        isinstance(x, str)
        and x.strip().rstrip(".").lower() not in _MOTS_VIDES_DISQUALIFICATION
        for x in lignes
    )


def motif_de_disqualification(research_json: Any) -> str | None:
    """Le PREMIER motif de disqualification lisible, ou None.

    `_porte_une_disqualification` repond oui/non pour le score ; celle-ci rend
    le texte, parce que `companies.disqualified_reason` doit dire POURQUOI. Une
    fiche sortie du circuit sans motif est une fiche que personne ne pourra
    rouvrir en connaissance de cause.
    """
    if not isinstance(research_json, dict):
        return None
    lignes = research_json.get("disqualifications")
    if not isinstance(lignes, list):
        return None
    for x in lignes:
        if not isinstance(x, str):
            continue
        propre = x.strip().rstrip(".")
        if propre.lower() not in _MOTS_VIDES_DISQUALIFICATION:
            return propre[:200]
    return None


def extract_lead_potential_patch(research_json: Any) -> dict[str, Any]:
    """Extrait les colonnes flat `lead_potential_*` du research_json.

    Le Research Agent ne rend plus de chiffre : il rend des CONSTATS dans
    `research_json["lead_potential"]["signaux"]`, dont une partie est mesurée
    en Python (avis, horaires, outils). Le score se calcule ici, par
    `lib.lead_scoring.calculer_score`. Un LLM ne tient pas de registre entre
    plusieurs ajustements — mesuré le 2026-09-01 sur les 283 scores de prod :
    27 valeurs distinctes, dont 72 pour un quart de la base.

    Les signaux restent dans `research_json`, donc re-régler les poids est un
    UPDATE SQL sur les lignes déjà recherchées, sans un seul appel LLM.

    Une `disqualification` posée par le research force le score à 0 : sans ça,
    une municipalité pouvait ressortir devant un vrai prospect.

    Retourne un dict à fusionner dans le patch UPDATE. Vide si le score est
    absent ou invalide (on ne touche alors pas les colonnes — elles restent à
    leur valeur précédente / null).
    """
    if not isinstance(research_json, dict):
        return {}
    lp = research_json.get("lead_potential")
    if not isinstance(lp, dict):
        return {}
    signaux = lp.get("signaux")
    disqualifie = _porte_une_disqualification(research_json)
    if isinstance(signaux, dict) and signaux:
        # Forme actuelle : des constats, pondérés par le code.
        score, _ = calculer_score(signaux, disqualifie=disqualifie)
    else:
        # Forme héritée (les 283 lignes recherchées avant le 2026-09-01) : le
        # modèle rendait le chiffre lui-même. On le recopie tel quel — le
        # recalculer est impossible, les signaux n'ont jamais été relevés.
        base = lp.get("score")
        # bool est une sous-classe d'int — on l'exclut explicitement.
        if not isinstance(base, int) or isinstance(base, bool):
            return {}
        if not (0 <= base <= 100):
            return {}
        # La disqualification vaut sur les DEUX formes. Elle ne s'appliquait
        # qu'à la forme actuelle jusqu'au 2026-09-09 : une municipalité déjà
        # en base, notée 72 par l'ancien modèle et disqualifiée par le même
        # research, ressortait à 72 et repassait devant un vrai prospect.
        # ⚠️ PAS de mise à zéro sur la forme héritée (retiré le 2026-09-10).
        # Ces `disqualifications` ont été écrites sous l'ANCIENNE règle, large,
        # que la refonte abroge : « tech-savvy élevé », « agence partenaire
        # visible », « site inactif ». 117 des 404 entreprises recherchées en
        # portent une. Les mettre à 0 appliquerait rétroactivement une règle
        # supprimée, et enterrerait des PME que la décision de William veut
        # garder dans la liste. Ces lignes se règlent par un re-scoring, pas
        # par un jugement rendu sous une loi abrogée.
        score = base
    patch: dict[str, Any] = {"lead_potential_score": score}
    reason = lp.get("reasoning") if isinstance(lp.get("reasoning"), str) else ""
    # 🔴 Sur le chemin des signaux, la raison est TOUJOURS réécrite, même vide.
    # Sans ça le marqueur ne s'effaçait jamais : une entreprise marquée en août
    # puis re-recherchée en octobre sans plainte gardait son « ⚑ » — et comme
    # c'est lui qui ordonne la file d'envoi, elle restait première de tous les
    # lots, indéfiniment. Tant que la colonne n'était lue par personne le
    # résidu était inoffensif ; depuis qu'elle trie, il capture la tête de file.
    # …sauf quand la plainte est INVÉRIFIABLE ce jour-là. `avis_note_min` vient
    # des ≤ 5 avis que Google fait tourner : une passe où la fiche revient sans
    # avis rendrait `est_tete_de_file` faux alors que le modèle maintient la
    # plainte, et effacerait une marque toujours méritée. Absence de mesure ≠
    # absence de plainte — la même doctrine que dans `signaux_mesures`.
    plainte_invérifiable = (
        isinstance(signaux, dict)
        and signaux.get("avis_disent_injoignable") is True
        and signaux.get("avis_plainte_recente") is None
    )
    ecrire_la_raison = (
        isinstance(signaux, dict) and bool(signaux) and not plainte_invérifiable
    )
    # La marque de tête de file vit DANS la justification, pas dans le score :
    # le lead garde la note que le barème lui donne (un 8 reste un 8) et cette
    # phrase dit pourquoi il passe quand même devant. Elle est en tête de
    # chaîne pour rester lisible après la troncature à 500 et pour qu'un
    # `like '⚑%'` suffise à sortir la file prioritaire.
    if est_tete_de_file(signaux, disqualifie=disqualifie):
        reason = f"{MARQUEUR_TETE_DE_FILE} {reason}".strip()
    if reason:
        patch["lead_potential_reason"] = reason[:500]
    elif ecrire_la_raison:
        patch["lead_potential_reason"] = None
    return patch


def extract_metiers_patch(research_json: Any) -> dict[str, Any]:
    """Les colonnes de métiers à écrire, depuis le research_json.

    Calquée sur `extract_lead_potential_patch` pour la forme — mais elle rend
    TOUJOURS un patch, là où sa jumelle rend `{}` quand la donnée manque. C'est
    voulu : « aucun métier reconnu » est une information (le défaut inversé, qui
    ouvre les douze mois), pas une absence. Rendre `{}` laisserait la colonne à
    NULL, ce qu'un prédicat `fenetre_mois @> array[9]` écarte EN SILENCE.
    """
    services = None
    if isinstance(research_json, dict):
        services = research_json.get("services_offered")
    patch = dict(colonnes_metiers(services if isinstance(services, list) else None))
    patch["metiers_calcules_le"] = datetime.now(timezone.utc).isoformat()
    return patch


async def _statut_apres_recherche(
    company_id: str,
    emails_found: list[dict[str, Any]] | None,
) -> str:
    """'enriched' si la passe a un contact à montrer, sinon 'researched_no_contact'.

    Deux sources d'évidence, et il faut les deux :

    1. Les contacts DÉJÀ en base — couvre les re-passes et le backfill (le script
       `backfill_research_columns.py` relit les contacts existants).
    2. Les `emails_found` de la passe courante — indispensable parce que
       `http_api.research_company_by_id` appelle cette fonction AVANT sa boucle
       d'insertion des contacts scrapés. À cet instant la table `contacts` est vide
       même quand le scraping a ramené trois adresses : ne compter que la base ferait
       basculer 100 % des premières passes en 'researched_no_contact', l'inverse exact
       du bug corrigé ici. Un courriel non vide finit toujours contact —
       `insert_contact` ne rejette que l'adresse vide (skipped_no_email), le doublon
       renvoyant une ligne déjà présente.
    """
    if any((e or {}).get("email") for e in (emails_found or [])):
        return "enriched"
    contacts_existants = await db.select(
        "contacts",
        params={"select": "id", "company_id": f"eq.{company_id}", "limit": "1"},
    )
    return "enriched" if contacts_existants else "researched_no_contact"


async def update_company_research(
    company_id: str,
    research_json: dict[str, Any],
    emails_found: list[dict[str, Any]] | None = None,
    *,
    nom_usage: str | None = None,
) -> dict[str, Any]:
    """Patch companies.research_json (+ colonnes flat lead_potential_* et décideur)
    et pose le status selon ce qui a été TROUVÉ.

    Met à jour le payload du Research Agent, le score de potentiel extrait, et le
    décideur résumé (decideur_confirme/decideur_potentiel, mutuellement exclusifs)
    calculé depuis decideur_candidats + les emails nominatifs scrapés.

    `status` passe à 'enriched' s'il y a au moins un contact, sinon à
    'researched_no_contact' (0001_initial_schema.sql définit 'enriched' = « contacts
    trouvés » ; le poser inconditionnellement faisait annoncer des contacts inexistants
    à 145 entreprises au 2026-08-17). `last_enriched_at` est horodaté dans les deux
    cas : il porte la
    ré-éligibilité à 90 jours de `list_companies_to_research`.

    Le filtre `status not.in.(disqualified,suppressed)` protège les boîtes terminales :
    on ne ressuscite jamais un lead écarté, et l'UPDATE entier (research_json inclus)
    est donc sauté pour ces boîtes — voulu, elles sont hors pipeline.
    'researched_no_contact' n'est PAS terminal et reste donc hors de ce filtre.
    """
    confirme, potentiel = summarize_company_decideur(
        (research_json or {}).get("decideur_candidats"), emails_found
    )
    # 🔴 UNE DISQUALIFICATION SORT L'ENTREPRISE DU CIRCUIT — decision William,
    # 2026-09-16.
    #
    # Jusqu'ici elle ne faisait que mettre le score a ZERO
    # (`calculer_score(disqualifie=True)`), ce qui releguait la fiche au fond de
    # la file sans jamais l'en retirer. Le prompt de recherche, lui, parle d'une
    # « liste FERMEE » de cinq motifs terminaux : entite publique, annuaire,
    # cooperative, 25 employes ou plus, commerce ferme. Aucun de ces cinq ne
    # merite d'etre « plus bas dans la file » : ils meritent de sortir.
    #
    # Le cas qui l'a montre : Worry Free Snow Blowing, 25-50 employes avec un
    # centre d'appels, a recu un brouillon lui disant « t'es dans ta machine ».
    #
    # 🔴 ET CA NE VAUT QUE POUR LES RECHERCHES FUTURES, jamais retroactivement.
    # 117 des 404 fiches deja recherchees portent une disqualification ecrite
    # sous l'ANCIENNE regle, large, que la refonte a abrogee (« tech-savvy
    # eleve », « agence partenaire visible », « site inactif »). Les passer en
    # `disqualified` appliquerait une loi supprimee a des jugements rendus sous
    # elle, et enterrerait des PME que William veut garder. C'est exactement le
    # raisonnement qui a fait RETIRER la mise a zero retroactive le 2026-09-10 ;
    # voir `extract_lead_potential_patch`.
    #
    # ⚠️ Le filtre de l'UPDATE porte sur le statut ACTUEL
    # (`not.in.(disqualified,suppressed)`) : il empeche de ressusciter une fiche
    # deja sortie, et n'empeche pas d'en sortir une nouvelle.
    motif_disqualifiant = motif_de_disqualification(research_json)
    patch: dict[str, Any] = {
        "research_json": research_json,
        "decideur_confirme": confirme,
        "decideur_potentiel": potentiel,
        "status": (
            "disqualified" if motif_disqualifiant
            else await _statut_apres_recherche(company_id, emails_found)
        ),
        "last_enriched_at": datetime.now(timezone.utc).isoformat(),
    }
    if motif_disqualifiant:
        patch["disqualified_reason"] = motif_disqualifiant
    # 🔴 LA CLE EST ABSENTE QUAND LA GARDE A REFUSE, jamais posee a None — meme
    # convention que `disqualified_reason` juste au-dessus, et pour la meme
    # raison : une re-recherche a 90 jours dont le candidat serait refuse
    # EFFACERAIT sinon un nom bon, valide par un passage precedent.
    if nom_usage:
        patch["nom_usage"] = nom_usage
    patch.update(extract_lead_potential_patch(research_json))
    rows = await db.update(
        "companies",
        patch,
        filters={
            "id": f"eq.{company_id}",
            "status": "not.in.(disqualified,suppressed)",
        },
    )

    # ── Second UPDATE : les colonnes de métiers, et lui seul ────────────────
    # 🔴 SÉPARÉ, ET DANS CET ORDRE, POUR UNE RAISON MESURÉE. L'anti-clobber ne
    # peut pas vivre dans le filtre du patch ci-dessus : il y sauterait l'UPDATE
    # ENTIER, donc research_json, status et last_enriched_at — et cette dernière
    # porte la ré-éligibilité à 90 jours du backlog de recherche. 41 fiches en
    # sortiraient pour trois mois, en silence (mesuré le 2026-09-10).
    #
    # 🔴 ET IL NE LÈVE JAMAIS. L'appelant (`/research/company`) n'est pas sous
    # `try` : une exception ici perdrait research_json APRÈS un appel LLM de
    # ~35 s, et la boucle qui insère les contacts scrapés ne tournerait pas non
    # plus.
    #
    # 🔴 LE RATTRAPAGE N'EST PAS `metiers_calcules_le IS NULL`. Ce critère ne
    # voit que la PREMIÈRE passe. Si ce second UPDATE échoue sur une fiche déjà
    # calculée — une re-recherche à 90 jours — l'horodatage garde son ancienne
    # valeur non nulle pendant que `research_json` vient d'être remplacé :
    # `metiers`/`fenetre_mois` décrivent alors l'ANCIEN JSON, périmés ET
    # invisibles à un backfill qui ne cherche que les NULL. Le critère juste est
    #     metiers_calcules_le is null or metiers_calcules_le < last_enriched_at
    # (le même est inscrit dans le commentaire SQL de la colonne, migration 0059).
    #
    # ⚠️ COÛT : `db.update` est décoré d'un retry à 3 tentatives avec backoff
    # exponentiel (1→8 s, timeout 30 s par tentative), donc sur un 5xx transitoire
    # ce second appel peut ajouter une quinzaine de secondes PAR FICHE — contre un
    # budget de lot d'environ 300 s avant que Railway coupe.
    #
    # ⚠️ LE FILTRE DES STATUTS TERMINAUX EST RÉPÉTÉ ICI, exprès. Sans lui, une
    # fiche disqualified/suppressed se verrait refuser research_json par le
    # premier UPDATE mais recevrait quand même ses colonnes de métiers — donc un
    # `metiers_calcules_le` non nul, et un statut annonçant « tout va bien » sur
    # le seul chemin où l'écriture principale a été jetée. Sur `suppressed`, qui
    # est un retrait de consentement, écrire « les mois où on peut la démarcher »
    # est un mauvais signal en soi. L'appel part même quand `rows` est vide : la
    # fiche peut être devenue terminale PENDANT l'appel LLM, et c'est ce filtre —
    # pas `rows` — qui tranche au moment de l'écriture.
    statut_metiers = "echec"
    try:
        touchees = await db.update(
            "companies",
            extract_metiers_patch(research_json),
            filters={
                "id": f"eq.{company_id}",
                "status": "not.in.(disqualified,suppressed)",
                "metiers_verifies_a_la_main": "eq.false",
            },
        )
        # 🔧 CINQ VALEURS DEPUIS LE 2026-09-14, décision William : « le 20e qui
        # a été mis comme verrouillé doit être mis comme disqualifié ».
        #
        # Le cas réel : « Groupe AZ Extermination », le 2026-09-14 à 15 h 00. La
        # recherche n'a rien rendu, le PREMIER update l'a donc passée en
        # `disqualified` — et le SECOND, qui exclut les statuts terminaux, l'a
        # écartée. Le code rendait alors `verrouillee`, un mot qui désigne
        # l'anti-clobber (« William a corrigé cette fiche à la main »). Deux
        # causes opposées sous une seule étiquette : l'une est une décision de
        # William qu'on protège, l'autre une fiche que la recherche vient de
        # fermer.
        #
        # ⚠️ AUCUNE DES DEUX N'ALERTE, donc ce n'était pas un faux positif — mais
        # un diagnostic qui ment sur la cause envoie chercher au mauvais endroit
        # le jour où ça compte.
        if not rows:
            statut_metiers = "absente"
        elif touchees:
            statut_metiers = "ecrit"
        elif patch.get("status") in ("disqualified", "suppressed"):
            # 🔴 ON LIT LE PATCH, PAS LA BASE. Le statut terminal vient presque
            # toujours du PREMIER UPDATE de cette même fonction : la recherche
            # n'a rien rendu, `_statut_apres_recherche` a conclu `disqualified`,
            # le premier update l'a écrit — et le second, qui exclut les statuts
            # terminaux, s'est donc exclu lui-même. La réponse est dans `patch`,
            # déjà en mémoire. Un `select` de plus coûterait un aller-retour
            # réseau PAR FICHE pour une valeur qu'on vient d'écrire.
            statut_metiers = "terminale"
        else:
            statut_metiers = "verrouillee"
    except Exception as exc:  # noqa: BLE001 — voir le bloc ci-dessus
        # 🔴 `str(exc)` d'une HTTPStatusError rend « 400 Bad Request » + deux
        # lignes de MDN. Le vrai message PostgREST (« PGRST204: column ... does
        # not exist ») est dans `response.text`, et sur un chemin où l'exception
        # est délibérément avalée c'est le SEUL canal de diagnostic.
        # Idiome repris de src/lib/granola.py.
        reponse = getattr(exc, "response", None)
        corps = (getattr(reponse, "text", "") or "")[:300] if reponse is not None else ""
        logger.warning(
            "colonnes de metiers non ecrites pour %s : %s %s", company_id, exc, corps
        )

    return {
        "updated": len(rows),
        # Conservé pour la rétro-compatibilité — `statut_metiers` porte le détail.
        "metiers_ecrits": statut_metiers == "ecrit",
        # ecrit · verrouillee (anti-clobber) · terminale (fiche disqualifiée ou
        # supprimée) · absente · echec.
        # 🔴 Seuls `echec` et `absente` sont des anomalies : n'alerter que sur
        # eux. `verrouillee` et `terminale` sont deux COMPORTEMENTS CORRECTS, et
        # les distinguer ne sert pas l'alerte — ça sert le diagnostic, qui doit
        # dire « William l'a corrigée à la main » ou « la recherche l'a fermée »,
        # jamais l'un pour l'autre. Une alarme qui sonne au nominal est une
        # alarme morte ; un diagnostic qui ment est pire, il envoie chercher
        # ailleurs.
        "statut_metiers": statut_metiers,
    }


class AgentRunIn(BaseModel):
    agent: Literal["research", "personalization", "qualification", "call_prep", "compliance", "reacti_discover"]
    model: str
    company_id: str | None = None
    contact_id: str | None = None
    campaign_id: str | None = None
    input_payload: dict[str, Any] | None = None
    output_payload: dict[str, Any] | None = None
    error_text: str | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None


async def record_agent_run(payload: AgentRunIn) -> dict[str, Any]:
    """Audit trail — chaque appel d'agent laisse une trace.

    Pas critique pour le pipeline (un échec d'insert ne doit pas bloquer le run).
    L'appelant peut try/except sans risque.
    """
    now = datetime.now(timezone.utc).isoformat()
    row = payload.model_dump(exclude_none=True)
    row["started_at"] = now
    row["finished_at"] = now
    cost = estimated_cost_usd(
        payload.model,
        input_tokens=payload.input_tokens,
        output_tokens=payload.output_tokens,
        cache_read_tokens=payload.cache_read_tokens,
        cache_creation_tokens=payload.cache_creation_tokens,
    )
    if cost is not None:
        row["estimated_cost_usd"] = cost
    rows = await db.insert("agent_runs", row)
    return {"agent_run_id": rows[0]["id"] if rows else None}
