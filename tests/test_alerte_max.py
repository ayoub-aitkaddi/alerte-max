"""Tests de la logique pure (aucun appel réseau). Lancer : python -m unittest"""
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alerte_max as am  # noqa: E402

NOMS = {"PARIS (intramuros)": "Paris-Est", "METZ VILLE": "Metz", "FORBACH": "Forbach"}
ALLER = {"jours": {"vendredi": "15:00", "samedi": "00:00"}}
RETOUR = {"jours": {"dimanche": "00:00"}}


def rec(jour, dep, arr, dispo="OUI", o="PARIS (intramuros)", d="METZ VILLE"):
    return {"date": jour, "origine": o, "destination": d,
            "heure_depart": dep, "heure_arrivee": arr, "od_happy_card": dispo}


class TestWeekends(unittest.TestCase):
    def test_un_mercredi_donne_le_vendredi_suivant(self):
        self.assertEqual(am.prochains_weekends(date(2026, 11, 25), 3),
                         [date(2026, 11, 27), date(2026, 12, 4), date(2026, 12, 11)])

    def test_le_samedi_garde_le_weekend_en_cours(self):
        self.assertEqual(am.prochains_weekends(date(2026, 11, 28), 1), [date(2026, 11, 27)])

    def test_le_dimanche_passe_au_suivant(self):
        self.assertEqual(am.prochains_weekends(date(2026, 11, 29), 1), [date(2026, 12, 4)])


class TestFiltre(unittest.TestCase):
    MAINTENANT = datetime(2026, 11, 25, 8, 0)

    def filtrer(self, enregistrements, sens=ALLER, agenda=()):
        return am.filtrer(enregistrements, sens, NOMS, self.MAINTENANT, list(agenda), timedelta(hours=1))

    def test_ignore_les_trains_sans_place_max(self):
        self.assertEqual(self.filtrer([rec("2026-11-27", "17:40", "19:04", dispo="NON")]), set())

    def test_vendredi_avant_15h_exclu(self):
        self.assertEqual(self.filtrer([rec("2026-11-27", "13:39", "15:03")]), set())

    def test_vendredi_soir_garde(self):
        (t,) = self.filtrer([rec("2026-11-27", "17:40", "19:04")])
        self.assertEqual((t.origine, t.destination, t.depart), ("Paris-Est", "Metz", "17:40"))

    def test_jour_hors_creneau_exclu(self):
        self.assertEqual(self.filtrer([rec("2026-11-29", "17:40", "19:04")]), set())

    def test_trains_au_meme_horaire_fusionnes(self):
        self.assertEqual(len(self.filtrer([rec("2026-11-27", "17:40", "19:04")] * 2)), 1)

    def test_conflit_agenda_avec_marge(self):
        reunion = am.Evenement("Réunion", datetime(2026, 11, 27, 19, 30), datetime(2026, 11, 27, 21, 0))
        self.assertEqual(self.filtrer([rec("2026-11-27", "17:40", "19:04")], agenda=[reunion]), set())

    def test_train_deja_parti_exclu(self):
        tard = datetime(2026, 11, 27, 18, 0)
        res = am.filtrer([rec("2026-11-27", "17:40", "19:04")], ALLER, NOMS, tard, [], timedelta(0))
        self.assertEqual(res, set())


class TestEtat(unittest.TestCase):
    MAINTENANT = datetime(2026, 11, 25, 8, 0)
    T1 = am.Train(date(2026, 11, 27), "Paris-Est", "Metz", "17:40", "19:04")
    T2 = am.Train(date(2026, 11, 29), "Metz", "Paris-Est", "08:49", "10:13")

    def test_aller_retour_texte(self):
        texte = am.ecrire_etat({self.T1, self.T2}, self.MAINTENANT)
        self.assertEqual(am.lire_etat(texte, self.MAINTENANT), {self.T1, self.T2})

    def test_etat_absent(self):
        self.assertIsNone(am.lire_etat(None, self.MAINTENANT))
        self.assertIsNone(am.lire_etat("un autre message épinglé", self.MAINTENANT))

    def test_etat_vide(self):
        self.assertEqual(am.lire_etat(am.ecrire_etat(set(), self.MAINTENANT), self.MAINTENANT), set())

    def test_passage_a_la_nouvelle_annee(self):
        fin_dec = datetime(2026, 12, 30, 8, 0)
        t = am.Train(date(2027, 1, 2), "Paris-Est", "Metz", "17:40", "19:04")
        self.assertEqual(am.lire_etat(am.ecrire_etat({t}, fin_dec), fin_dec), {t})


class TestMessage(unittest.TestCase):
    T1 = TestEtat.T1
    T2 = TestEtat.T2

    def test_rien_de_nouveau_pas_de_message(self):
        self.assertIsNone(am.composer_message({self.T1}, {self.T1}, None, set()))

    def test_place_apparue_et_disparue(self):
        msg = am.composer_message({self.T2}, {self.T1}, None, set())
        self.assertIn("1 place(s) apparue(s), 1 disparue(s)", msg)
        self.assertIn("🆕", msg)
        self.assertIn("❌", msg)

    def test_premier_passage_toujours_un_message(self):
        self.assertIn("lancée : 0 train", am.composer_message(set(), None, None, set()))

    def test_recommandation_aller_tot_retour_tard(self):
        tard = am.Train(date(2026, 11, 29), "Metz", "Paris-Est", "19:42", "21:13")
        reco = am.recommandation({self.T1, self.T2, tard}, [date(2026, 11, 27)], {0, 1}, {2})
        self.assertEqual(reco, (self.T1, tard))

    def test_pas_de_recommandation_sans_retour(self):
        self.assertIsNone(am.recommandation({self.T1}, [date(2026, 11, 27)], {0, 1}, {2}))


if __name__ == "__main__":
    unittest.main()
