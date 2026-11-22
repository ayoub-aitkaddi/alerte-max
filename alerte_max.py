#!/usr/bin/env python3
"""Alerte MAX Jeune : surveille les places à 0 € sur un trajet TGV récurrent.

Le script interroge le jeu de données ouvert SNCF « TGVMAX » (disponibilité
à 30 jours des places MAX Jeune / MAX Senior), garde les trains qui collent
aux créneaux du week-end définis dans ``config.json``, écarte ceux qui
tombent sur un événement d’agenda, puis prévient sur Telegram uniquement
quand quelque chose change.

L’état (les places déjà signalées) est stocké dans un message épinglé de la
conversation Telegram : aucune base de données, aucun fichier à conserver
entre deux exécutions, et l’utilisateur voit l’état courant d’un coup d’œil.

Uniquement la bibliothèque standard Python (3.9+).

Usage :
    export TELEGRAM_TOKEN=...  TELEGRAM_CHAT_ID=...
    python alerte_max.py [--config config.json] [--busy busy.json] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

API_SNCF = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/records"
API_TELEGRAM = "https://api.telegram.org/bot{token}/{method}"
JOURS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
DECALAGE = {"vendredi": 0, "samedi": 1, "dimanche": 2}  # par rapport au vendredi
ENTETE_ETAT = "📌 Places MAX Jeune suivies"
LIGNE_ETAT = re.compile(r"(\d{2})/(\d{2}) · (.+?) → (.+?) · (\d{2}:\d{2}) → (\d{2}:\d{2})")


# --------------------------------------------------------------------------- #
# Modèle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, order=True)
class Train:
    """Un train disponible. Deux trains au même horaire sont fusionnés."""

    jour: date
    origine: str
    destination: str
    depart: str  # "HH:MM"
    arrivee: str

    def label(self) -> str:
        return (f"{JOURS[self.jour.weekday()]} {self.jour:%d/%m} · "
                f"{self.origine} → {self.destination} · {self.depart} → {self.arrivee}")

    def dt_depart(self) -> datetime:
        return datetime.combine(self.jour, datetime.strptime(self.depart, "%H:%M").time())

    def dt_arrivee(self) -> datetime:
        return datetime.combine(self.jour, datetime.strptime(self.arrivee, "%H:%M").time())


@dataclass(frozen=True)
class Evenement:
    titre: str
    debut: datetime
    fin: datetime


# --------------------------------------------------------------------------- #
# Logique pure (testée dans tests/)
# --------------------------------------------------------------------------- #
def prochains_weekends(aujourdhui: date, n: int) -> list[date]:
    """Vendredis des ``n`` prochains week-ends (fenêtre glissante).

    Le week-end en cours compte encore le vendredi et le samedi ; le dimanche,
    on passe au suivant puisque le retour du jour est déjà presque joué.
    """
    vendredi = aujourdhui - timedelta(days=(aujourdhui.weekday() - 4) % 7)
    if vendredi + timedelta(days=2) <= aujourdhui:
        vendredi += timedelta(days=7)
    return [vendredi + timedelta(weeks=i) for i in range(n)]


def filtrer(enregistrements: Iterable[dict], sens: dict, noms: dict,
            maintenant: datetime, agenda: list[Evenement], marge: timedelta) -> set[Train]:
    """Garde les trains MAX disponibles qui respectent créneaux et agenda."""
    heure_min = {DECALAGE[j] + 4: h for j, h in sens["jours"].items()}  # weekday -> heure mini
    trains = set()
    for r in enregistrements:
        if r.get("od_happy_card") != "OUI":
            continue
        t = Train(date.fromisoformat(r["date"]), noms.get(r["origine"], r["origine"]),
                  noms.get(r["destination"], r["destination"]), r["heure_depart"], r["heure_arrivee"])
        mini = heure_min.get(t.jour.weekday())
        if mini is None or t.depart < mini or t.dt_depart() <= maintenant:
            continue
        if any(e.debut < t.dt_arrivee() + marge and e.fin > t.dt_depart() - marge for e in agenda):
            continue
        trains.add(t)
    return trains


def lire_etat(texte: str | None, maintenant: datetime) -> set[Train] | None:
    """Relit le message épinglé. ``None`` si aucun état n’existe encore."""
    if not texte or not texte.startswith(ENTETE_ETAT):
        return None
    etat = set()
    for m in LIGNE_ETAT.finditer(texte):
        jj, mm, origine, destination, dep, arr = m.groups()
        annee = maintenant.year + (1 if int(mm) < maintenant.month else 0)  # passage de l’an
        t = Train(date(annee, int(mm), int(jj)), origine, destination, dep, arr)
        if t.dt_depart() > maintenant:
            etat.add(t)
    return etat


def ecrire_etat(trains: set[Train], maintenant: datetime) -> str:
    lignes = "\n".join(f"• {t.label()}" for t in sorted(trains)) or "Aucune pour l’instant."
    return f"{ENTETE_ETAT} (maj {maintenant:%d/%m %H:%M})\n{lignes}"


def recommandation(trains: set[Train], weekends: list[date], jours_aller: set[int],
                   jours_retour: set[int]) -> tuple[Train, Train] | None:
    """Premier week-end complet : aller le plus tôt, retour le plus tard."""
    for vendredi in weekends:
        aller = [t for t in trains if (t.jour - vendredi).days in jours_aller]
        retour = [t for t in trains if (t.jour - vendredi).days in jours_retour]
        if aller and retour:
            return min(aller), max(retour, key=lambda t: (t.jour, t.depart))
    return None


def composer_message(actuel: set[Train], precedent: set[Train] | None, reco, repli: set[int]) -> str | None:
    """Texte de l’alerte, ou ``None`` s’il n’y a rien de nouveau."""
    premier = precedent is None
    nouveaux = sorted(actuel - (precedent or set()))
    partis = sorted((precedent or set()) - actuel)
    if not premier and not nouveaux and not partis:
        return None

    def puce(t: Train) -> str:
        return f"• {t.label()}" + ("  (en repli)" if t.jour.weekday() in repli else "")

    if premier:
        lignes = [f"🚄 Surveillance MAX Jeune lancée : {len(actuel)} train(s) conviennent."]
        if actuel:
            lignes += ["", "Disponibles :", *map(puce, sorted(actuel))]
    else:
        lignes = [f"🚄 Du nouveau : {len(nouveaux)} place(s) apparue(s), {len(partis)} disparue(s)."]
        if nouveaux:
            lignes += ["", "🆕 Disponibles :", *map(puce, nouveaux)]
        if partis:
            lignes += ["", "❌ Plus disponibles :", *(f"• {t.label()}" for t in partis)]
    if reco:
        lignes += ["", f"👉 Recommandé : aller {reco[0].label()}, retour {reco[1].label()}."]
    lignes += ["", "ℹ️ Disponibilité indicative (données mises à jour une fois par jour) : "
                   "vérifie sur SNCF Connect avant de réserver."]
    return "\n".join(lignes)


# --------------------------------------------------------------------------- #
# Entrées / sorties
# --------------------------------------------------------------------------- #
def http_json(url: str, data: bytes | None = None, essais: int = 6) -> dict:
    """GET/POST JSON avec nouvelles tentatives (le serveur SNCF coupe parfois)."""
    pauses = [5, 10, 20, 30, 60]
    for i in range(essais):
        try:
            with urllib.request.urlopen(url, data, timeout=60) as rep:
                return json.load(rep)
        except Exception as ex:  # noqa: BLE001 — on journalise et on réessaie
            print(f"[réseau] essai {i + 1}/{essais} : {ex!r}", file=sys.stderr)
            if i == essais - 1:
                raise
            time.sleep(pauses[min(i, len(pauses) - 1)])
    raise RuntimeError("inaccessible")


def requete_sncf(sens: dict, jours: list[date]) -> list[dict]:
    def liste(champ: str, valeurs: list[str]) -> str:
        return "(" + " or ".join(f'{champ}="{v}"' for v in valeurs) + ")"

    where = " and ".join([
        liste("origine", sens["origines"]),
        liste("destination", sens["destinations"]),
        "(" + " or ".join(f"date=date'{j.isoformat()}'" for j in jours) + ")",
    ])
    params = urllib.parse.urlencode({"where": where, "limit": 100, "order_by": "date,heure_depart"})
    return http_json(f"{API_SNCF}?{params}")["results"]


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    def appel(self, methode: str, **params) -> dict:
        url = API_TELEGRAM.format(token=self.token, method=methode)
        data = urllib.parse.urlencode({"chat_id": self.chat_id, **params}).encode()
        return http_json(url, data, essais=3)["result"]

    def message_epingle(self) -> dict | None:
        return self.appel("getChat").get("pinned_message")

    def envoyer(self, texte: str, silencieux: bool = False) -> dict:
        return self.appel("sendMessage", text=texte, disable_notification=str(silencieux).lower())

    def epingler(self, message_id: int) -> None:
        self.appel("pinChatMessage", message_id=message_id, disable_notification="true")

    def modifier(self, message_id: int, texte: str) -> None:
        self.appel("editMessageText", message_id=message_id, text=texte)


def charger_agenda(chemin: str | None) -> list[Evenement]:
    if not chemin:
        return []
    return [Evenement(e["titre"], datetime.fromisoformat(e["debut"]), datetime.fromisoformat(e["fin"]))
            for e in json.loads(Path(chemin).read_text(encoding="utf-8"))]


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default=Path(__file__).with_name("config.json"))
    p.add_argument("--busy", help="JSON des événements d’agenda qui bloquent un trajet")
    p.add_argument("--dry-run", action="store_true", help="affiche le message sans rien envoyer")
    args = p.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    maintenant = datetime.now(ZoneInfo(cfg.get("fuseau", "Europe/Paris"))).replace(tzinfo=None)
    weekends = prochains_weekends(maintenant.date(), cfg["fenetre_weekends"])
    agenda = charger_agenda(args.busy)
    marge = timedelta(minutes=cfg.get("marge_agenda_minutes", 60))

    actuel: set[Train] = set()
    for sens in (cfg["aller"], cfg["retour"]):
        jours = [v + timedelta(days=DECALAGE[j]) for v in weekends for j in sens["jours"]]
        actuel |= filtrer(requete_sncf(sens, jours), sens, cfg["noms_gares"], maintenant, agenda, marge)

    def decalages(sens: dict) -> set[int]:
        return {DECALAGE[j] for j in sens["jours"]}

    reco = recommandation(actuel, weekends, decalages(cfg["aller"]), decalages(cfg["retour"]))
    repli = {DECALAGE[j] + 4 for j in cfg.get("jours_repli", [])}

    if args.dry_run:
        print(composer_message(actuel, None, reco, repli))
        return 0

    tg = Telegram(os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    epingle = tg.message_epingle()
    precedent = lire_etat(epingle and epingle.get("text"), maintenant)
    message = composer_message(actuel, precedent, reco, repli)
    if message:
        tg.envoyer(message)

    etat = ecrire_etat(actuel, maintenant)
    if precedent is None:
        tg.epingler(tg.envoyer(etat, silencieux=True)["message_id"])
    elif epingle.get("text") != etat:
        try:
            tg.modifier(epingle["message_id"], etat)
        except Exception as ex:  # noqa: BLE001 — l’état sera réécrit au prochain passage
            print(f"[telegram] état non mis à jour : {ex!r}", file=sys.stderr)
    print(f"OK : {len(actuel)} train(s) disponible(s), message envoyé : {bool(message)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
