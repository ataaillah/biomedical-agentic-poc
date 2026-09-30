"""Essais réels séquentiels, aucun remplacement simulé en cas d’échec."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from laml_agent import run_agent
from laml_poc import ROOT
cases = [
    ('effectif', 'Combien de patients NPM1 mutés dans TCGA-LAML NEJM 2013 ?', {}),
    ('frequence', 'Quel pourcentage de la cohorte TCGA-LAML porte une mutation NPM1 ?', {}),
    ('preuve', 'Donne les effectifs NPM1 et leur fréquence dans TCGA-LAML avec la preuve du PDF.', {}),
    ('hors_perimetre', 'Combien de patients FLT3 ?', {}),
    ('pdf_absent', 'Combien de patients NPM1 mutés dans TCGA-LAML ?', {'pdf': ROOT/'references/absent.pdf'}),
    ('desaccord', 'Quelle fréquence NPM1 dans TCGA-LAML ?', {'scenario': 'desaccord'}),
]
for name, question, kwargs in cases:
    result = run_agent(question, **kwargs)
    print(json.dumps({'case': name, 'state': result}, ensure_ascii=False), flush=True)
