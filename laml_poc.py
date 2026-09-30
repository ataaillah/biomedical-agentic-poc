"""POC local : quatre rôles déterministes orchestrés par LangGraph."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from typing import TypedDict

# Désactive explicitement les exports de traces, même si configurés dans le shell.
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
from langgraph.graph import StateGraph, START, END

ROOT = Path(__file__).resolve().parent
QUESTION = 'Dans la cohorte TCGA-LAML de l’article NEJM 2013, combien de patients présentent une mutation NPM1 et quelle est la fréquence ?'
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
CONSEQUENCES = {'frame_shift_ins', 'missense'}


def provenance(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def patients_xlsx(path):
    """Lecture en flux ; ne décode AQ qu'après validation de l'identifiant patient."""
    patients = {}
    with zipfile.ZipFile(path) as z:
        strings = [''.join(e.itertext()) for e in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', NS)]
        def value(cell):
            if cell is None:
                return ''
            v = cell.findtext('m:v', '', NS)
            return (strings[int(v)] if cell.get('t') == 's' else v).strip()
        with z.open('xl/worksheets/sheet1.xml') as stream:
            for _, row in ET.iterparse(stream, events=('end',)):
                if row.tag != '{' + NS['m'] + '}row':
                    continue
                cells = {re.sub(r'\d', '', c.get('r')): c for c in row}
                if row.get('r') == '1':
                    if value(cells.get('B')) != 'TCGA Patient ID' or value(cells.get('AQ')) != 'NPM1':
                        raise ValueError('Schéma table 01 inattendu')
                else:
                    patient = value(cells.get('B'))
                    if re.fullmatch(r'\d{4}', patient):
                        key = 'TCGA-AB-' + patient
                        if key in patients:
                            raise ValueError('Patient répété table 01 : ' + key)
                        patients[key] = {'upn': value(cells.get('A')), 'npm1': value(cells.get('AQ'))}
                    elif patient:
                        raise ValueError('Identifiant table 01 invalide : ' + patient)
                    # Ligne de total : B vide, AQ jamais décodée.
                row.clear()
    return patients


def calculate(raw):
    paths = [raw / name for name in ('SuppTable01.xlsx', 'stdFreezeList.tsv', 'SupplementalTable06.tsv')]
    sources = [provenance(p) for p in paths]
    patients = patients_xlsx(paths[0])
    cohort = set(patients)
    with paths[1].open() as handle:
        freeze = {r['PATIENT_BARCODE'] for r in csv.DictReader(handle, delimiter='\t')}
    issues = []
    if len(cohort) != 200:
        issues.append(f'Cohorte inattendue : {len(cohort)} patients au lieu de 200')
    if cohort != freeze:
        issues.append(f'Désaccord freeze/table 01 : absents freeze={sorted(cohort-freeze)}, supplémentaires freeze={sorted(freeze-cohort)}')
    selected, present, outside, mapping = set(), set(), set(), set()
    events = []
    excluded = []
    row_count = 0
    with paths[2].open() as handle:
        for line, r in enumerate(csv.DictReader(handle, delimiter='\t'), 2):
            row_count += 1
            patient = r['TCGA_id']
            present.add(patient)
            if patient not in cohort:
                outside.add(patient)
                continue
            if r['UPN'] != patients[patient]['upn']:
                mapping.add(patient)
            if r['gene_name'] != 'NPM1':
                continue
            if r['tier'] != 'tier1' or r['trv_type'] not in CONSEQUENCES:
                excluded.append({'line': line, 'patient': patient, 'tier': r['tier'], 'consequence': r['trv_type']})
                continue
            selected.add(patient)
            events.append({'line': line, 'patient': patient, 'consequence': r['trv_type'], 'protein': r['amino_acid_change']})
    if outside:
        issues.append('Patients table 06 hors cohorte : ' + ', '.join(sorted(outside)))
    if mapping:
        issues.append('Correspondance UPN incohérente : ' + ', '.join(sorted(mapping)))
    if excluded:
        issues.append('Annotations NPM1 non prises en charge : révision des filtres nécessaire')
    control = {k for k, r in patients.items() if r['npm1']}
    if control != selected:
        issues.append(f'Désaccord NPM1 table 01/06 : seulement 01={sorted(control-selected)}, seulement 06={sorted(selected-control)}')
    if not cohort:
        raise ValueError('Cohorte vide')
    return {'numerator': len(selected), 'denominator': len(cohort), 'percentage': 100*len(selected)/len(cohort),
            'patients': sorted(selected), 'cohort': sorted(cohort), 'control_patients': sorted(control),
            'control_numerator': len(control), 'mutation_rows': len(events), 'events': events,
            'table06_rows': row_count, 'table06_patients': len(present), 'absent_table06': sorted(cohort-present),
            'excluded_npm1': excluded, 'issues': issues, 'provenance': sources,
            'filters': {'gene_name': 'NPM1', 'tier': 'tier1', 'trv_type': sorted(CONSEQUENCES), 'deduplicate': 'TCGA_id'}}


def document(evidence_path, pdf):
    if not pdf.exists():
        return {'issues': [f'Source manquante : {pdf}. Veuillez fournir le PDF de l’article.']}
    if not evidence_path.exists():
        return {'issues': [f'Source documentaire locale manquante : {evidence_path}']}
    evidence = json.loads(evidence_path.read_text())
    issues = []
    if provenance(pdf)['sha256'] != evidence['pdf_sha256']:
        issues.append('Empreinte PDF différente de celle de la preuve locale')
    text = subprocess.check_output(['pdftotext', '-f', str(evidence['pdf_page']), '-l', str(evidence['pdf_page']), '-layout', str(pdf), '-'], text=True)
    normalized = ' '.join(text.split())
    if ' '.join(evidence['quote'].split()) not in normalized:
        issues.append('Extrait documentaire non retrouvé dans la page PDF annoncée')
    if 'Table 1.' not in text or str(evidence['printed_page']) not in text:
        issues.append('Repère tableau/page non retrouvé')
    match = re.search(r'NPM1\s+(\d+)/(\d+)\s+\((\d+(?:\.\d+)?)\)', evidence['quote'])
    if not match:
        raise ValueError('Extrait NPM1 illisible')
    n, d, pct = match.groups()
    return {'numerator': int(n), 'denominator': int(d), 'percentage': float(pct), 'reference': evidence['reference'],
            'quote': evidence['quote'], 'pdf_page': evidence['pdf_page'], 'printed_page': evidence['printed_page'],
            'issues': issues, 'provenance': [provenance(pdf), provenance(evidence_path)]}


class State(TypedDict, total=False):
    question: str
    raw: str
    pdf: str
    evidence: str
    scenario: str
    supported: bool
    plan: list[str]
    analysis: dict
    documentation: dict
    verification: dict
    trace: list[str]


def planificateur(state):
    # Périmètre volontairement fermé, pas de compréhension générale du langage.
    q = ' '.join(state['question'].strip().rstrip('?').split()).casefold().replace('’', "'")
    expected = ' '.join(QUESTION.rstrip('?').split()).casefold().replace('’', "'")
    return {'supported': q == expected, 'plan': ['Calcul patient table 06', 'Contrôle table 01 et freeze', 'Preuve PDF locale', 'Comparaison'], 'trace': ['planificateur']}


def analyste(state):
    try:
        result = calculate(Path(state['raw'])) if state['supported'] else {'issues': ['Question hors périmètre : utilisez la question de démonstration exacte']}
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        result = {'issues': [f'Calcul impossible : {exc}']}
    return {'analysis': result, 'trace': state['trace'] + ['analyste']}


def documentaliste(state):
    try:
        result = document(Path(state['evidence']), Path(state['pdf'])) if state['supported'] else {'issues': []}
        if state['scenario'] == 'desaccord' and 'numerator' in result:
            # Injection de test EN MÉMOIRE : jamais présentée comme une citation du PDF.
            result['authentic_result'] = {k: result[k] for k in ('numerator', 'denominator', 'percentage')}
            result['numerator'] += 1
            result['percentage'] = 100 * result['numerator'] / result['denominator']
            result['synthetic'] = True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result = {'issues': [f'Preuve documentaire indisponible : {exc}']}
    return {'documentation': result, 'trace': state['trace'] + ['documentaliste']}


def verificateur(state):
    a, d = state['analysis'], state['documentation']
    issues = a['issues'] + d['issues']
    if 'numerator' in a and 'numerator' in d:
        for key in ('numerator', 'denominator', 'percentage'):
            if a[key] != d[key]:
                issues.append(f'DÉSACCORD {key} : calcul={a[key]}, documentaire={d[key]}')
    elif state['supported']:
        issues.append('Comparaison calcul/document impossible : source ou calcul manquant')
    return {'verification': {'status': 'VERIFIE' if not issues else 'NON_VERIFIE', 'issues': issues}, 'trace': state['trace'] + ['verificateur']}


def build_graph():
    graph = StateGraph(State)
    for name, node in [('planificateur', planificateur), ('analyste', analyste), ('documentaliste', documentaliste), ('verificateur', verificateur)]:
        graph.add_node(name, node)
    for start, end in [(START, 'planificateur'), ('planificateur', 'analyste'), ('analyste', 'documentaliste'), ('documentaliste', 'verificateur'), ('verificateur', END)]:
        graph.add_edge(start, end)
    return graph.compile()


def run(question=QUESTION, raw=ROOT/'data/raw', pdf=ROOT/'references/NEJMoa1301689.pdf', evidence=ROOT/'references/npm1_table1.json', scenario='normal'):
    return build_graph().invoke({'question': question, 'raw': str(raw), 'pdf': str(pdf), 'evidence': str(evidence), 'scenario': scenario}, config={'callbacks': []})


def render(state):
    a, d, v = state['analysis'], state['documentation'], state['verification']
    lines = [v['status'], 'Graphe : ' + ' → '.join(state['trace'])]
    if 'numerator' in a:
        lines += [f"Calcul : {a['numerator']}/{a['denominator']} patients NPM1 mutés ({a['percentage']:g} %).",
                  f"{a['mutation_rows']} lignes NPM1 ; contrôle table 01 : {a['control_numerator']} patients.",
                  f"Patients absents de la table 06, conservés au dénominateur : {', '.join(a['absent_table06'])}."]
    if 'reference' in d:
        lines += ['Référence : ' + d['reference'], f"Extrait : {d['quote']} (page PDF {d['pdf_page']}, page imprimée {d['printed_page']})."]
    if d.get('synthetic'):
        lines += [f"TEST SYNTHÉTIQUE : résultat documentaire injecté {d['numerator']}/{d['denominator']} ({d['percentage']:g} %), distinct de l’extrait authentique."]
    lines += ['ALERTE : ' + issue for issue in v['issues']]
    lines += ['Provenance locale (SHA-256) :']
    lines += [f"- {p['path']} : {p['sha256']}" for p in a.get('provenance', []) + d.get('provenance', [])]
    lines += ['Limites : annotations des auteurs, sans réanalyse des lectures ; version table 01 compatible avec le manuscrit mais non certifiée. Absence dans la table 06 ≠ génotype sauvage démontré.',
              'Orchestration de quatre rôles par LangGraph ; règles et outils entièrement déterministes, aucun agent autonome à LLM, aucun appel modèle/cloud.']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question', nargs='?', default=QUESTION)
    parser.add_argument('--scenario', choices=['normal', 'desaccord'], default='normal')
    parser.add_argument('--pdf', type=Path, default=ROOT/'references/NEJMoa1301689.pdf')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--mode', choices=['deterministe', 'agentique'], default='deterministe')
    parser.add_argument('--model', default=os.environ.get('OLLAMA_MODEL', 'qwen3:0.6b'))
    parser.add_argument('--diagnostic', type=Path, nargs='?',
                        const=ROOT/'notes/agent_diagnostic.json',
                        help='Mode agentique : enregistrer requêtes, réponses brutes et décisions validées')
    args = parser.parse_args()
    if args.diagnostic and args.mode != 'agentique':
        parser.error('--diagnostic nécessite --mode agentique')
    if args.mode == 'agentique':
        from laml_agent import run_agent, render_agent
        state = run_agent(args.question, model=args.model, pdf=args.pdf, scenario=args.scenario,
                          diagnostic=args.diagnostic)
        output = render_agent
    else:
        state = run(args.question, pdf=args.pdf, scenario=args.scenario)
        output = render
    print(json.dumps(state, ensure_ascii=False, indent=2) if args.json else output(state))
    return 0 if state['verification']['status'] == 'VERIFIE' else 2


if __name__ == '__main__':
    raise SystemExit(main())
