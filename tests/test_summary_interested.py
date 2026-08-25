"""PT1 — la ligne « intéressés en attente de site » du résumé quotidien.
N = contacts.interested_at non nul ET aucune ligne agence.demo_sites pour ce
contact. La frappe du jeton (PT2) fait redescendre N sans écriture dédiée."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _il_y_a(jours: int) -> str:
    """Horodatage UTC ISO situé à N jours d'ici, ancré à MIDI America/Toronto.

    Midi et pas minuit : le résumé rend les dates avec `slack.jour()`, qui
    découpe l'ISO UTC. À midi heure de Toronto, la date UTC et la date locale
    sont les mêmes — le test ne bascule donc pas de jour selon l'heure à
    laquelle il tourne. La fenêtre « depuis 7 jours », elle, s'ancre sur minuit
    America/Toronto côté code, comme le reste du résumé."""
    midi = datetime.now(ZoneInfo("America/Toronto")).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    return (midi - timedelta(days=jours)).astimezone(timezone.utc).isoformat()


def _jour_il_y_a(jours: int) -> str:
    """La date telle qu'elle sera RENDUE pour `_il_y_a(jours)`."""
    return _il_y_a(jours)[:10]


def _lignes_suppression(supprimes) -> list[dict]:
    """Normalise le raccourci des tests en vraies lignes `suppression_list`.

    Trois formes acceptées, de la plus courte à la plus complète :
      - itérable de courriels          → motif `opt_out`, sans date
      - dict courriel -> motif         → ce motif, sans date
      - dict courriel -> (motif, date) → motif + `created_at`

    « sans date » n'est pas un caprice de test : c'est le cas dégradé réel où
    seul `contacts.status` porte le désabonnement, sans ligne de suppression
    correspondante — le résumé doit alors écrire « (date inconnue) »."""
    paires = supprimes.items() if isinstance(supprimes, dict) else [
        (c, "opt_out") for c in supprimes
    ]
    lignes = []
    for courriel, valeur in paires:
        motif, quand = (valeur, None) if isinstance(valeur, str) else valeur
        lignes.append({"email": courriel, "reason": motif, "created_at": quand})
    return lignes


def _socle(monkeypatch, *, interesses, demo_par_contact, captured=None, supprimes=(),
           lectures_suppression=None):
    """demo_par_contact : dict contact_id -> lignes demo_sites à retourner.
    captured : liste optionnelle où empiler les `params` de chaque select_all
    sur "contacts" — sert au test qui pin l'ABSENCE de filtre de statut dans la
    requête PostgREST (le tri se fait en Python, cf. plus bas : le remettre en
    SQL viderait le compteur des désabonnés en silence).
    supprimes : contenu de `suppression_list` (voir `_lignes_suppression`), lu
    d'un bloc — le chemin du clic sur le lien du footer ne garantit PAS que
    contacts.status ait basculé, d'où le croisement par courriel.
    lectures_suppression : liste optionnelle où empiler les `params` de chaque
    select_all sur "suppression_list" — sert au garde-fou anti-N+1.

    ⚠️ summary_daily importe `sb` et `slack_lib` LOCALEMENT dans la fonction
    (`from . import supabase_client as sb` / `from .lib import slack as
    slack_lib`) : patcher les attributs des MODULES SOURCE, pas http_api."""
    from src import http_api
    from src import supabase_client as sb
    from src.lib import slack as slack_mod

    lignes_sup = _lignes_suppression(supprimes)

    async def fake_count(table, params=None):
        return 0

    async def fake_select_all(table, order=None, params=None, **kw):
        if table == "contacts" and "interested_at" in (params or {}):
            if captured is not None:
                captured.append(params or {})
            return interesses
        if table == "suppression_list":
            if lectures_suppression is not None:
                lectures_suppression.append(params or {})
            # La table entière, MOTIF ET DATE compris : `hard_bounce` (adresse
            # morte), `manual`/`competitor`/`dncl` (nos décisions) y vivent avec
            # les vrais retraits de consentement, et c'est l'appelant qui trie.
            return lignes_sup
        return []  # dont la vue v_pourquoi_pas_de_courriel : vide suffit ici

    async def fake_select(table, params=None, schema=None, **kw):
        assert table != "suppression_list", (
            "suppression_list doit se lire d'UN bloc via select_all — un select() "
            "par intéressé serait le retour du N+1 (et du croisement sensible à "
            "la casse qui laissait un désabonné dans « en attente de site »)"
        )
        assert table == "demo_sites" and schema == "agence"
        cid = (params or {}).get("contact_id", "").removeprefix("eq.")
        return demo_par_contact.get(cid, [])

    async def fake_notify(**kw):
        return True

    monkeypatch.setattr(sb, "count", fake_count)
    monkeypatch.setattr(sb, "select_all", fake_select_all)
    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    return http_api


async def test_compte_les_interesses_sans_demo(monkeypatch):
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1"}, {"id": "ct-2"}, {"id": "ct-3"}],
        demo_par_contact={"ct-2": [{"id": "d-1"}]},  # ct-2 est servi → sort du compteur
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 2
    # PT3 (2026-08-25) : le compteur RESTE calculé et exposé dans totals — c'est
    # un contrat d'API lu par le cron — mais son affichage a cédé la place au
    # bloc nominatif « Tes leads chauds ». On pin donc la valeur, plus le texte.
    assert "intéressés en attente de site" not in out["text"]


async def test_zero_interesse_pas_de_ligne(monkeypatch):
    http_api = _socle(monkeypatch, interesses=[], demo_par_contact={})
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0
    assert "en attente de site" not in out["text"]


async def test_les_impasses_sortent_du_compteur(monkeypatch):
    """Un intéressé qui bascule opted_out/disqualified/bounced ne doit pas rester
    compté « en attente de site » à vie — interested_at ne redescend jamais seul.

    L'exclusion vivait dans le filtre SQL ; depuis la ligne « intéressés
    désabonnés » (2026-08-23) la requête doit VOIR les opted_out, donc le tri se
    fait en Python. Ce test pin le comportement observable, pas la mécanique."""
    captured: list[dict] = []
    http_api = _socle(
        monkeypatch,
        interesses=[
            {"id": "ct-vivant", "email": "v@x.ca", "status": "replied"},
            {"id": "ct-parti", "email": "p@x.ca", "status": "opted_out"},
            {"id": "ct-dq", "email": "d@x.ca", "status": "disqualified"},
            {"id": "ct-bounce", "email": "b@x.ca", "status": "bounced"},
        ],
        demo_par_contact={},  # aucun n'a de démo
        captured=captured,
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert len(captured) == 1
    # Garde de non-régression : le filtre de statut ne doit JAMAIS revenir dans
    # la requête PostgREST. Le geste est tentant (le N+1 est juste en dessous),
    # mais il rendrait les opted_out invisibles à la lecture — le chemin
    # majoritaire du compteur des désabonnés tomberait à zéro, suite verte.
    assert "status" not in captured[0]
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 1


# =====================================================================
# La ligne « intéressés désabonnés » (2026-08-23) — les DEUX chemins
# =====================================================================

async def test_un_interesse_passe_opted_out_est_compte(monkeypatch):
    """Chemin réponse « désabonnez-moi » (WF-7) ET chemin normal du clic sur le
    lien du footer : les deux posent contacts.status='opted_out'."""
    http_api = _socle(
        monkeypatch,
        interesses=[
            {"id": "ct-1", "email": "a@x.ca", "status": "opted_out"},
            {"id": "ct-2", "email": "b@x.ca", "status": "replied"},
        ],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 1
    assert "intéressés désabonnés 1" in out["text"]
    # complémentaire, pas redondant : le désabonné sort de l'autre compteur
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 1


async def test_un_interesse_sur_la_liste_de_suppression_est_compte(monkeypatch):
    """Chemin du clic sur le lien du footer en mode dégradé : l'Edge Function
    écrit TOUJOURS suppression_list mais ne pose contacts.status qu'au mieux
    (erreur de lecture/écriture journalisée puis ignorée). Sans le croisement
    par courriel, ce désabonnement resterait invisible — pire, le contact
    resterait dans la file « en attente de site »."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "a@x.ca", "status": "replied"}],
        demo_par_contact={},
        supprimes={"a@x.ca"},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 1
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0


async def test_une_adresse_morte_nest_pas_un_desabonnement(monkeypatch):
    """`hard_bounce` (posé par WF-6b) vit dans la MÊME table que les opt-outs.
    Le compter comme un désabonnement mentirait deux fois : le chiffre serait
    faux, et le garde-fou LCAP serait appliqué à quelqu'un qui n'a jamais retiré
    son consentement — son adresse est simplement morte."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "mort@x.ca", "status": "bounced"}],
        demo_par_contact={},
        supprimes={"mort@x.ca": "hard_bounce"},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 0
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0


async def test_un_interesse_sain_nest_pas_compte_comme_desabonne(monkeypatch):
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "a@x.ca", "status": "replied"}],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 0
    assert "intéressés désabonnés" not in out["text"]


async def test_zero_desabonne_pas_de_ligne(monkeypatch):
    http_api = _socle(monkeypatch, interesses=[], demo_par_contact={})
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 0
    assert "désabonnés" not in out["text"]


# =====================================================================
# La ligne doit être ACTIONNABLE (2026-08-23) — un nombre nu ne décide rien
# =====================================================================

def _desabonne(n, *, prenom, nom, courriel, oui):
    return {"id": f"ct-{n}", "email": courriel, "first_name": prenom,
            "last_name": nom, "interested_at": oui, "status": "opted_out"}


async def test_la_ligne_nomme_les_leads_et_porte_linterdit_lcap(monkeypatch):
    """Le rendu EXACT. Un « ⚠️ intéressés désabonnés 3 » nu n'aide pas William à
    décider : il lui faut QUI, QUAND il avait dit oui, QUAND il s'est désabonné,
    et l'interdit — le réflexe naturel devant un chiffre inexpliqué étant
    justement d'aller relancer par courriel, ce que la LCAP interdit.

    Les deux cas de figure cohabitent ici : date de désabonnement connue (ligne
    `suppression_list` avec un motif de retrait) et inconnue (statut posé sans
    ligne correspondante). Dates volontairement anciennes pour que le libellé
    reste `(cumul)` nu — la fenêtre « depuis 7 jours » a son propre test."""
    from src.lib import slack

    http_api = _socle(
        monkeypatch,
        interesses=[
            _desabonne(1, prenom="Jean", nom="Roy", courriel="jean@plomberiex.ca",
                       oui="2026-07-02T09:00:00+00:00"),
            _desabonne(2, prenom="Marie", nom="Tremblay", courriel="info@toiturey.ca",
                       oui="2026-06-09T14:30:00+00:00"),
        ],
        demo_par_contact={},
        supprimes={"jean@plomberiex.ca": ("opt_out", "2026-07-14T11:20:00+00:00")},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert (
        "  ⚠️ intéressés désabonnés 2 (cumul) — "
        "Jean Roy <jean@plomberiex.ca> oui le 2026-07-02, désabonné le 2026-07-14 · "
        "Marie Tremblay <info@toiturey.ca> oui le 2026-06-09, désabonné (date inconnue)\n"
        f"    {slack.GARDE_LCAP_APRES_DESABONNEMENT}"
    ) in out["text"]


async def test_le_mot_cumul_est_dans_le_libelle(monkeypatch):
    """Ce compteur ne redescend JAMAIS (interested_at est un journal, opted_out
    ne revient pas en arrière) : on ne peut pas lui donner une sortie honnête,
    alors on le NOMME, plutôt que de laisser croire à une file de travail qui ne
    se vide pas. La date de désabonnement ne change pas ça — elle sert à TRIER
    et à annoncer les récents, pas à faire redescendre le cumul."""
    http_api = _socle(
        monkeypatch,
        interesses=[_desabonne(1, prenom="Jean", nom="Roy", courriel="j@x.ca",
                               oui="2026-08-12T09:00:00+00:00")],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "intéressés désabonnés 1 (cumul)" in out["text"]
    # la file de travail, elle, garde son libellé sans « cumul » : elle a une sortie
    assert "en attente de site" not in out["text"]


async def test_les_desabonnements_les_plus_recents_passent_en_tete(monkeypatch):
    """LE tri qui compte : par date de DÉSABONNEMENT, pas par date du « oui ».

    Le besoin est de savoir QUAND un lead se désabonne après avoir dit oui. Un
    « oui » de mai retiré hier est le cas actionnable du jour ; trié par date du
    oui il se retrouvait enterré derrière « … +N », donc invisible au moment
    précis où il fallait le voir."""
    http_api = _socle(
        monkeypatch,
        interesses=[
            _desabonne(1, prenom="Oui", nom="Recent", courriel="r@x.ca",
                       oui="2026-08-19T12:00:00+00:00"),
            _desabonne(2, prenom="Oui", nom="Vieux", courriel="v@x.ca",
                       oui="2026-05-01T12:00:00+00:00"),
        ],
        demo_par_contact={},
        supprimes={
            "r@x.ca": ("opt_out", _il_y_a(30)),  # oui récent, parti il y a un mois
            "v@x.ca": ("opt_out", _il_y_a(1)),   # vieux oui, parti hier
        },
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    txt = out["text"]
    assert txt.index("Oui Vieux") < txt.index("Oui Recent")
    assert f"Oui Vieux <v@x.ca> oui le 2026-05-01, désabonné le {_jour_il_y_a(1)}" in txt


async def test_le_libelle_annonce_les_desabonnements_recents(monkeypatch):
    """« dont N depuis 7 jours » : le cumul reste honnête, le nouveau saute aux
    yeux. La fenêtre s'ancre sur minuit America/Toronto (aujourd'hui + les 6
    jours précédents) comme le reste du résumé — le lead parti il y a 7 jours
    est donc DEHORS, et le test pin cette borne."""
    http_api = _socle(
        monkeypatch,
        interesses=[
            _desabonne(1, prenom="A", nom="Aujourdhui", courriel="a@x.ca",
                       oui="2026-05-01T12:00:00+00:00"),
            _desabonne(2, prenom="B", nom="SixJours", courriel="b@x.ca",
                       oui="2026-05-02T12:00:00+00:00"),
            _desabonne(3, prenom="C", nom="SeptJours", courriel="c@x.ca",
                       oui="2026-05-03T12:00:00+00:00"),
            _desabonne(4, prenom="D", nom="UnMois", courriel="d@x.ca",
                       oui="2026-05-04T12:00:00+00:00"),
        ],
        demo_par_contact={},
        supprimes={
            "a@x.ca": ("opt_out", _il_y_a(0)),
            "b@x.ca": ("spam_complaint", _il_y_a(6)),
            "c@x.ca": ("opt_out", _il_y_a(7)),   # juste hors fenêtre
            "d@x.ca": ("opt_out", _il_y_a(40)),
        },
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "intéressés désabonnés 4 (cumul, dont 2 depuis 7 jours)" in out["text"]
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 4
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed_recent"] == 2


async def test_sans_desabonnement_recent_le_libelle_reste_nu(monkeypatch):
    """« dont 0 depuis 7 jours » serait du bruit quotidien : la mention ne
    s'affiche que lorsqu'elle porte une nouvelle."""
    http_api = _socle(
        monkeypatch,
        interesses=[_desabonne(1, prenom="Vieux", nom="Cas", courriel="v@x.ca",
                               oui="2026-05-01T12:00:00+00:00")],
        demo_par_contact={},
        supprimes={"v@x.ca": ("opt_out", _il_y_a(40))},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "intéressés désabonnés 1 (cumul)" in out["text"]
    assert "depuis 7 jours" not in out["text"]
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed_recent"] == 0


async def test_une_date_de_desabonnement_inconnue_est_dite_et_reléguee(monkeypatch):
    """Cas dégradé : `status='opted_out'` posé sans ligne de suppression (donc
    sans `created_at`). On n'invente pas la date — on l'écrit « inconnue » — et
    ces cas passent en FIN de liste, même quand leur « oui » est le plus frais
    de tous : les places du haut appartiennent aux désabonnements datés."""
    http_api = _socle(
        monkeypatch,
        interesses=[
            _desabonne(1, prenom="Sans", nom="Date", courriel="s@x.ca",
                       oui="2026-08-19T12:00:00+00:00"),  # le « oui » le plus frais
            _desabonne(2, prenom="Avec", nom="Date", courriel="a@x.ca",
                       oui="2026-05-01T12:00:00+00:00"),
        ],
        demo_par_contact={},
        supprimes={"a@x.ca": ("opt_out", _il_y_a(20))},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    txt = out["text"]
    assert "Sans Date <s@x.ca> oui le 2026-08-19, désabonné (date inconnue)" in txt
    assert txt.index("Avec Date") < txt.index("Sans Date")


async def test_au_dela_de_cinq_noms_le_reste_est_replie(monkeypatch):
    """Le total reste affiché ; seule la liste est plafonnée — sinon la ligne
    devient un mur illisible dans Slack."""
    http_api = _socle(
        monkeypatch,
        interesses=[
            _desabonne(i, prenom="Lead", nom=f"N{i}", courriel=f"l{i}@x.ca",
                       oui=f"2026-08-{20 - i:02d}T09:00:00+00:00")
            for i in range(1, 8)
        ],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 7
    assert "intéressés désabonnés 7 (cumul)" in out["text"]
    assert ("Lead N5 <l5@x.ca> oui le 2026-08-15, désabonné (date inconnue) · … +2"
            in out["text"])
    assert "Lead N6" not in out["text"] and "Lead N7" not in out["text"]


async def test_un_contact_sans_nom_retombe_sur_le_courriel(monkeypatch):
    """Un « <vide> <info@x.ca> » ferait douter de la donnée elle-même."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "info@toiturey.ca", "first_name": None,
                     "last_name": "", "interested_at": "2026-08-12T09:00:00+00:00",
                     "status": "opted_out"}],
        demo_par_contact={},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert "— info@toiturey.ca oui le 2026-08-12" in out["text"]
    assert "<" not in out["text"].split("désabonnés 1 (cumul)")[1].split("\n")[0]


# =====================================================================
# La lecture de suppression_list : UNE requête, insensible à la casse
# =====================================================================

async def test_lappariement_du_courriel_ignore_la_casse(monkeypatch):
    """`email=eq.` en SQL est SENSIBLE à la casse. Conséquence concrète : un
    lead saisi `Jean@X.ca` chez nous et désabonné sous `jean@x.ca` restait
    « en attente de site » — le tableau de bord envoyait donc William bâtir un
    site pour quelqu'un qui venait de retirer son consentement.

    `ilike` aurait l'air d'être la réponse ; c'en est un piège (`_` et `%` y
    sont des jokers, et `_` est fréquent dans une adresse). L'appariement se
    fait donc en Python sur `strip().lower()` des deux bords."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": " Jean@X.ca ", "status": "replied",
                     "first_name": "Jean", "last_name": "Roy",
                     "interested_at": "2026-08-12T09:00:00+00:00"}],
        demo_par_contact={},
        supprimes={"jean@x.ca": ("opt_out", _il_y_a(1))},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 1
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0
    assert f"désabonné le {_jour_il_y_a(1)}" in out["text"]


async def test_une_seule_lecture_de_suppression_quelle_que_soit_la_liste(monkeypatch):
    """Le garde-fou anti-N+1. 40 intéressés sur 2 tracks : la table de
    suppression se lit UNE fois, pas 40 ni 80. (Le retour au `select()` par
    contact est bloqué en plus par `fake_select`, qui lève.)"""
    lectures: list[dict] = []
    http_api = _socle(
        monkeypatch,
        interesses=[
            {"id": f"ct-{i}", "email": f"l{i}@x.ca", "status": "replied",
             "interested_at": "2026-08-12T09:00:00+00:00"}
            for i in range(40)
        ],
        demo_par_contact={},
        supprimes={f"l{i}@x.ca": ("opt_out", _il_y_a(2)) for i in range(0, 40, 2)},
        lectures_suppression=lectures,
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["OPT", "agence-ia"], post=False)
    )
    assert len(lectures) == 1, "une lecture par track serait déjà une régression"
    # `select_all` (paginé) et non `select` : le plafond PostgREST de 1000 lignes
    # couperait la liste en silence, et les désabonnés au-delà seraient invisibles.
    assert lectures[0]["select"] == "email,reason,created_at"
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 20


# =====================================================================
# Le troisième état : supprimé pour une AUTRE raison (2026-08-23)
# =====================================================================

@pytest.mark.parametrize("motif", ["manual", "competitor", "dncl", "hard_bounce"])
async def test_un_supprime_pour_autre_motif_sort_de_la_file_en_silence(
    monkeypatch, motif
):
    """Le tableau de bord ne doit pas envoyer William produire un site pour un
    prospect que NOUS avons écarté (ou dont l'adresse est morte). Il sort de
    « en attente de site » sans pour autant devenir un désabonné : personne n'a
    retiré de consentement, donc pas d'annonce et surtout pas de garde-fou LCAP
    appliqué à tort."""
    http_api = _socle(
        monkeypatch,
        interesses=[{"id": "ct-1", "email": "a@x.ca", "status": "replied",
                     "interested_at": "2026-08-12T09:00:00+00:00"}],
        demo_par_contact={},
        supprimes={"a@x.ca": motif},
    )
    out = await http_api.summary_daily(
        http_api.DailySummaryIn(tracks=["agence-ia"], post=False)
    )
    assert out["totals"]["agence-ia"]["interested_waiting_site"] == 0
    assert out["totals"]["agence-ia"]["interested_then_unsubscribed"] == 0
    assert "désabonnés" not in out["text"]
    assert "en attente de site" not in out["text"]
