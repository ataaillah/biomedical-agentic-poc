import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from laml_poc import ROOT, NS, run, patients_xlsx


class PocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Toute tentative de connexion réseau pendant le graphe fait échouer le test.
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('Réseau interdit')):
            cls.normal = run()

    def test_normal_and_patient_deduplication(self):
        state = self.normal
        self.assertEqual(state['trace'], ['planificateur', 'analyste', 'documentaliste', 'verificateur'])
        self.assertEqual(state['verification']['status'], 'VERIFIE')
        a = state['analysis']
        self.assertEqual((a['numerator'], a['denominator'], a['percentage'], a['mutation_rows']), (54, 200, 27, 55))
        self.assertEqual(a['patients'], a['control_patients'])
        self.assertIn('TCGA-AB-2915', a['patients'])
        self.assertEqual(a['patients'].count('TCGA-AB-2802'), 1)

    def test_disagreement_is_not_hidden(self):
        state = run(scenario='desaccord')
        self.assertEqual(state['analysis']['numerator'], 54)
        self.assertEqual(state['documentation']['numerator'], 55)
        self.assertEqual(state['verification']['status'], 'NON_VERIFIE')
        self.assertTrue(any('DÉSACCORD' in x for x in state['verification']['issues']))

    def test_missing_pdf(self):
        state = run(pdf=ROOT/'references/absent.pdf')
        self.assertEqual(state['analysis']['numerator'], 54)
        self.assertEqual(state['verification']['status'], 'NON_VERIFIE')
        self.assertTrue(any('Veuillez fournir' in x for x in state['verification']['issues']))

    def test_changed_total_is_never_decoded(self):
        original = ROOT/'data/raw/SuppTable01.xlsx'
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'changed.xlsx'
            with zipfile.ZipFile(original) as src, zipfile.ZipFile(target, 'w') as dst:
                for entry in src.infolist():
                    data = src.read(entry.filename)
                    if entry.filename == 'xl/worksheets/sheet1.xml':
                        root = ET.fromstring(data)
                        cell = root.find('.//m:c[@r="AQ202"]', NS)
                        cell.set('t', 's')
                        cell.find('m:v', NS).text = '999999999'  # invalide si décodé
                        data = ET.tostring(root)
                    dst.writestr(entry, data)
            self.assertEqual(patients_xlsx(original), patients_xlsx(target))

    def test_unsupported_question(self):
        state = run('Combien de patients FLT3 ?')
        self.assertNotIn('numerator', state['analysis'])
        self.assertEqual(state['verification']['status'], 'NON_VERIFIE')


if __name__ == '__main__':
    unittest.main()
