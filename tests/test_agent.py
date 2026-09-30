import unittest
from unittest.mock import patch
from laml_poc import run
from laml_agent import run_agent, validate, PROMPT, SCHEMA, Ollama

QUESTIONS = ['Quel pourcentage de patients porte une mutation NPM1 dans TCGA-LAML ?',
             'Combien de malades NPM1 mutés dans la cohorte de l’article NEJM 2013 ?',
             'Donne les effectifs et la fréquence NPM1 de TCGA-LAML, avec preuve.']

def script(names):
    calls = iter(names)
    return lambda question, observations: {'outil': next(calls), 'arguments': {}}

class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real = run()

    def agent(self, question=QUESTIONS[0], names=None, **kwargs):
        with patch('laml_agent.calculate', return_value=self.real['analysis']):
            return run_agent(question, decide=script(names or ['examiner_cohorte', 'compter_npm1', 'preuve_pdf', 'comparer', 'terminer']), **kwargs)

    def test_french_questions_simulated(self):
        # Ne valide pas la compréhension du modèle réel : contrôleur scripté.
        for q in QUESTIONS:
            with self.subTest(q=q):
                s = self.agent(q)
                self.assertEqual(s['verification']['status'], 'VERIFIE')
                self.assertTrue(s['metrics']['simulated'])

    def test_order_is_llm_choice(self):
        s = self.agent(names=['preuve_pdf', 'compter_npm1', 'comparer', 'terminer'])
        self.assertEqual(s['calls'][0]['outil'], 'preuve_pdf')
        self.assertEqual(s['verification']['status'], 'VERIFIE')

    def test_out_of_scope_simulated(self):
        s = self.agent('Combien de patients FLT3 ?', names=['hors_perimetre'])
        self.assertEqual(s['verification']['status'], 'NON_VERIFIE')
        self.assertNotIn('numerator', s['analysis'])

    def test_missing_pdf(self):
        self.assertEqual(self.agent(pdf='/tmp/npm1-no-such.pdf')['verification']['status'], 'NON_VERIFIE')

    def test_disagreement(self):
        s = self.agent(scenario='desaccord')
        self.assertTrue(any('DÉSACCORD' in x for x in s['verification']['issues']))

    def test_invalid_decisions(self):
        for action in [{'outil': 'shell', 'arguments': {}}, {'outil': 'compter_npm1', 'arguments': {'code': 'x'}}, {'outil': 'terminer'}]:
            with self.assertRaises(ValueError):
                validate(action)
            s = run_agent(decide=lambda q, o: action)
            self.assertEqual(s['verification']['status'], 'NON_VERIFIE')

    def test_early_finish_and_limit(self):
        self.assertEqual(self.agent(names=['terminer'])['verification']['status'], 'NON_VERIFIE')
        with patch('laml_agent.calculate', side_effect=OSError('Source absente')):
            s = run_agent(QUESTIONS[0], decide=script(['compter_npm1'] * 6))
        self.assertEqual(s['decisions'], 6)
        self.assertTrue(any('Limite' in x for x in s['verification']['issues']))


    def test_successful_repeat_reused_then_stopped(self):
        observations = []
        def repeat(q, summary):
            observations.append(summary)
            return {'outil': 'compter_npm1', 'arguments': {}}
        with patch('laml_agent.calculate', return_value=self.real['analysis']) as calc:
            state = run_agent(QUESTIONS[0], decide=repeat)
        self.assertEqual(calc.call_count, 1)
        self.assertEqual(state['decisions'], 3)
        self.assertEqual([c['reused'] for c in state['calls']], [False, True, True])
        self.assertEqual(state['verification']['status'], 'NON_VERIFIE')
        self.assertTrue(any('Répétition persistante' in x for x in state['verification']['issues']))
        self.assertIn('Choisis une autre action utile', observations[2]['rappel'])
        self.assertEqual(observations[1]['dernier_choix']['outil'], 'compter_npm1')
        self.assertEqual(observations[1]['resultats_disponibles']['compter_npm1']['numerator'], 54)
        self.assertIn('preuve_pdf', observations[1]['outils_encore_utiles'])

    def test_wire_messages_and_audit(self):
        import io
        import json
        names = iter(['compter_npm1', 'preuve_pdf', 'comparer', 'terminer'])
        client = Ollama()
        payloads = []
        class Response(io.BytesIO):
            pass
        def capture(request, **kwargs):
            payloads.append(json.loads(request.data))
            return Response(json.dumps({'done': True, 'message': {'content': json.dumps(
                {'outil': next(names), 'arguments': {}})}}).encode())
        with patch('laml_agent.calculate', return_value=self.real['analysis']), \
             patch.object(client.opener, 'open', side_effect=capture):
            state = run_agent(QUESTIONS[0], decide=client)
        second = payloads[1]['messages']
        content = json.loads(second[1]['content'])['bilan']
        self.assertEqual(content['dernier_choix']['outil'], 'compter_npm1')
        self.assertEqual(content['dernier_choix']['resultat']['mutation_rows'], 55)
        self.assertNotIn('sha256', json.dumps(second))
        self.assertNotIn('TCGA-AB-', json.dumps(second))
        self.assertEqual(state['verification']['status'], 'VERIFIE')
        self.assertEqual(state['model_exchanges'][1]['request'], payloads[1])
        self.assertIn('provenance', state['calls'][0]['result'])
        self.assertIn('patients', state['calls'][0]['result'])

    def test_recovery_after_one_repeat(self):
        state = self.agent(names=['compter_npm1', 'compter_npm1', 'preuve_pdf', 'comparer', 'terminer'])
        self.assertEqual(state['verification']['status'], 'VERIFIE')
        self.assertEqual(state['decisions'], 5)

    def test_diagnostic_records_wire_response_and_validated_decision(self):
        import io
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'diagnostic.json'
            client = Ollama(diagnostic=target)
            # Construction sans normaliser le corps renvoyé par le transport.
            raw = '  ' + json.dumps({'done': True, 'message': {'content': '{"outil":"terminer","arguments":{}}'},
                                    'prompt_eval_count': 500, 'eval_count': 20}) + '\n'
            def respond(request, **kwargs):
                pending = json.loads(target.read_text())['decisions'][0]
                self.assertEqual(pending['request_body'].encode(), request.data)
                self.assertEqual(pending['request']['messages'], json.loads(request.data)['messages'])
                self.assertEqual(pending['json_schema']['properties']['outil']['enum'], list(SCHEMA['properties']['outil']['enum']))
                self.assertIsNone(pending['validated_decision'])
                return io.BytesIO(raw.encode())
            with patch.object(client.opener, 'open', side_effect=respond):
                run_agent(QUESTIONS[0], decide=client)
            decision = json.loads(target.read_text())['decisions'][0]
            self.assertEqual(decision['raw_response'], raw)
            self.assertEqual(decision['raw_model_content'], '{"outil":"terminer","arguments":{}}')
            self.assertEqual(decision['validated_decision'], {'outil': 'terminer', 'arguments': {}})
            self.assertIsNone(decision['validation_error'])
            self.assertTrue(decision['context_check']['input_plus_output_within_limit'])

    def test_diagnostic_keeps_invalid_output(self):
        import io
        import json
        import tempfile
        from pathlib import Path
        for content in ['not json', '{"outil":"shell","arguments":{}}']:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                target = Path(folder)/'diagnostic.json'
                client = Ollama(diagnostic=target)
                raw = json.dumps({'done': True, 'message': {'content': content}})
                with patch.object(client.opener, 'open', return_value=io.BytesIO(raw.encode())):
                    state = run_agent(QUESTIONS[0], decide=client)
                record = json.loads(target.read_text())['decisions'][0]
                self.assertEqual(record['raw_response'], raw)
                self.assertEqual(record['raw_model_content'], content)
                self.assertIsNone(record['validated_decision'])
                self.assertTrue(record['validation_error'])
                self.assertEqual(state['verification']['status'], 'NON_VERIFIE')

    def test_schema_remains_open_to_all_tools_after_success(self):
        from laml_agent import TOOLS
        client = Ollama()
        for tool in TOOLS:
            self.assertEqual(validate({'outil': tool, 'arguments': {}})['outil'], tool)
        self.assertEqual(set(client.payload(QUESTIONS[0], {'taches_realisees': ['compter_npm1']})
                             ['format']['properties']['outil']['enum']), set(TOOLS))
        self.assertNotIn('const', str(SCHEMA))
        self.assertNotIn('default', str(SCHEMA))

    def test_no_answer_in_instructions(self):
        self.assertNotIn('54/200', PROMPT + str(SCHEMA))

if __name__ == '__main__':
    unittest.main()
