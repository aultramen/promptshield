"""Offline behavioral checks for FSD-PROMPTSHIELD-V1 TEST-001."""
import importlib
import json
from pathlib import Path
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / '.scratch/prototypes/promptshield-ui-v1'
sys.path.insert(0, str(PROTOTYPE))
ID = '1' * 32
INSTANCE = '2' * 32


class LocalContractTests(unittest.TestCase):
    def contract(self):
        self.assertTrue((PROTOTYPE / 'control_dto.py').is_file(),
                        'GOAL-001 typed contract consumer is not materialized')
        return importlib.import_module('control_dto').Contract()

    def test_status_accepts_only_fixed_fields(self):
        contract = self.contract()
        request = dict(schema_version=1, id=ID, instance_id=INSTANCE, op='status')
        self.assertEqual(contract.request(json.dumps(request)).op, 'status')
        request['metadata'] = {'value': 'SYNTHETIC_PRIVATE_CANARY'}
        with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$'):
            contract.request(json.dumps(request))

    def provider(self, **options):
        self.assertTrue((ROOT / 'tests/contracts/mock_control.py').is_file(),
                        'GOAL-001 mock provider is not materialized')
        return importlib.import_module('mock_control').MockControl(**options)

    def test_policy_cas_invalidates_pending_but_preserves_sent_snapshot(self):
        from control_dto import Consumer
        provider = self.provider()
        client = Consumer(provider, INSTANCE)
        pending = client.call('list_pending').payload.pending[0]
        receipt = client.call('set_mode', mode='off', expected_policy_version=1)
        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.payload.policy_version, 2)
        stale = client.call('decide', pending_id=pending.pending_id,
                            pending_revision=pending.revision, decision='mask')
        self.assertEqual(stale.payload.code, 'STALE')
        status = client.call('status').payload
        self.assertEqual(status.active_request_mode, 'always-mask')
        self.assertEqual(status.next_mode, 'off')
        self.assertEqual(status.pending_count, 1)
        conflict = client.call('set_mode', mode='always-mask', expected_policy_version=1)
        self.assertEqual(conflict.payload.code, 'STALE')

    def test_strict_wire_abuse(self):
        contract = self.contract()
        request = dict(schema_version=1, id=ID, instance_id=INSTANCE, op='set_verbose',
                       enabled=False, expected_policy_version=1)
        valid = json.dumps(request)
        bad = [valid.replace('"schema_version": 1', '"schema_version": true'),
               valid.replace('"enabled": false', '"enabled": 0'),
               valid.replace('"expected_policy_version": 1', '"expected_policy_version": 0'),
               valid.replace('"expected_policy_version": 1', '"expected_policy_version": 9007199254740992'),
               valid.replace('"enabled": false', '"enabled": NaN'),
               valid.replace('"enabled": false', '"enabled": Infinity'),
               valid.replace('"enabled": false', '"enabled": false, "enabled": true'),
               valid.replace(INSTANCE, 'A' * 32), b'\xff', ' ' * 65537,
               '[' * 1500 + ']' * 1500, 'null', '[]']
        for wire in bad:
            with self.subTest(wire_type=type(wire).__name__):
                with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$'):
                    contract.request(wire)

    def test_response_discriminants_identity_and_no_value_fields(self):
        contract = self.contract()
        request = contract.request(json.dumps(dict(schema_version=1, id=ID,
                                                   instance_id=INSTANCE, op='stop')))
        good = dict(schema_version=1, id=ID, ok=True, result=dict(kind='STOP_RECEIPT', state='STOPPED'))
        self.assertTrue(contract.response(json.dumps(good), request).ok)
        bad = [good | {'error': {'code': 'INTERNAL_ERROR', 'retryable': False}},
               good | {'id': '3' * 32}, good | {'ok': 1},
               good | {'result': {'kind': 'DECISION_RECEIPT', 'state': 'CANCEL'}},
               good | {'result': good['result'] | {'value': 'SYNTHETIC_PRIVATE_CANARY'}},
               dict(schema_version=1, id=ID, ok=False,
                    error=dict(code='INTERNAL_ERROR', retryable=False, message='SYNTHETIC_PRIVATE_CANARY'))]
        for response in bad:
            with self.subTest(response_fields=list(response)):
                with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$'):
                    contract.response(json.dumps(response), request)

    def test_terminal_tool_choices_never_resume_and_replays_are_stale(self):
        from control_dto import Consumer
        for decision, outcome in [('manual', 'MANUAL_HANDOFF'), ('variable', 'VARIABLE_GUIDANCE'), ('cancel', 'CANCEL')]:
            provider = self.provider(action_kind='TOOL_HELD', state='TOOL_HELD')
            client = Consumer(provider, INSTANCE)
            pending = client.call('list_pending').payload.pending[0]
            incompatible = client.call('decide', pending_id=pending.pending_id,
                                       pending_revision=1, decision='mask')
            self.assertFalse(incompatible.ok)
            self.assertEqual(incompatible.payload.code, 'POLICY_INVALID')
            receipt = client.call('decide', pending_id=pending.pending_id,
                                 pending_revision=1, decision=decision)
            self.assertTrue(receipt.ok)
            self.assertEqual(receipt.payload.state, outcome)
            self.assertEqual(client.call('status').payload.pending_count, 0)
            self.assertEqual(client.call('decide', pending_id=pending.pending_id,
                                        pending_revision=1, decision=decision).payload.code, 'STALE')
            self.assertEqual(provider.dispositions[pending.pending_id], outcome)

    def test_expiry_disconnect_owner_stop_and_cancel_are_finite(self):
        from control_dto import Consumer
        now = [100.0]
        provider = self.provider(clock=lambda: now[0])
        client = Consumer(provider, INSTANCE)
        self.assertEqual(client.call('list_pending').payload.pending[0].time_remaining, 120)
        # Disconnection makes no decision; elapsed deadline terminates it.
        now[0] = 220.0
        self.assertEqual(client.call('decide', pending_id='3' * 32,
                                    pending_revision=1, decision='mask').payload.code, 'EXPIRED')
        self.assertEqual(client.call('status').payload.pending_count, 0)
        self.assertEqual(Consumer(self.provider(owner=False), INSTANCE).call('status').payload.code, 'FORBIDDEN')
        self.assertEqual(Consumer(self.provider(), 'f' * 32).call('list_pending').payload.code, 'FORBIDDEN')
        self.assertEqual(Consumer(self.provider(locked=True), INSTANCE).call(
            'set_mode', mode='off', expected_policy_version=1).payload.code, 'FORBIDDEN')
        for _ in range(2):
            cancelled = client.call('cancel_request', request_id='4' * 32)
            self.assertTrue(cancelled.ok)
            self.assertEqual(cancelled.payload.state, 'CANCELLED')
            stopped = client.call('stop')
            self.assertTrue(stopped.ok)
            self.assertEqual(stopped.payload.state, 'STOPPED')
        status = client.call('status').payload
        self.assertEqual(status.token_store_state, 'revoked')
        self.assertIsNone(status.active_request_mode)

    def test_exactly_one_terminal_decision_under_race(self):
        from control_dto import Consumer
        provider = self.provider()
        def decide(_):
            return Consumer(provider, INSTANCE).call('decide', pending_id='3' * 32,
                                                     pending_revision=1, decision='mask')
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(decide, range(2)))
        self.assertEqual(sum(response.ok for response in responses), 1)
        self.assertEqual(len(provider.dispositions), 1)

    def test_fixture_and_terminal_projection_cover_twelve_states(self):
        catalog = ROOT / 'tests/fixtures/synthetic/contracts-v1.json'
        self.assertTrue(catalog.is_file(), 'GOAL-001 deterministic fixtures are not materialized')
        self.assertTrue((PROTOTYPE / 'review.py').is_file(), 'GOAL-001 keyboard prototype is not materialized')
        fixtures = json.loads(catalog.read_text(encoding='utf-8'))
        self.assertEqual(fixtures['contract_version'], self.contract().version)
        builder = importlib.import_module('build_fixtures')
        self.assertEqual(fixtures, builder.build(), 'Fixture catalog drifted from fixed mock/contract')
        for abuse in fixtures['abuse_requests']:
            with self.subTest(abuse=abuse['name']):
                with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$'):
                    self.contract().request(abuse['wire'])
        self.assertEqual({state for fixture in fixtures['scenarios'] for state in fixture['states']},
                         {f'UI-STATE-{i:03d}' for i in range(1, 13)})
        review = importlib.import_module('review')
        report = review.scenario_suite([80, 120])
        self.assertEqual(report['failures'], [])
        self.assertGreaterEqual(report['checks'], 24)

    def test_receipts_must_match_the_accepted_decision_and_policy_increment(self):
        contract = self.contract()
        request = contract.request(json.dumps(dict(schema_version=1, id=ID, instance_id=INSTANCE,
                       op='set_mode', mode='off', expected_policy_version=1)))
        with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$'):
            contract.response(json.dumps(dict(schema_version=1, id=ID, ok=True,
                     result=dict(kind='MODE_RECEIPT', policy_version=1))), request)
        request = contract.request(json.dumps(dict(schema_version=1, id=ID, instance_id=INSTANCE,
                       op='decide', pending_id='3' * 32, pending_revision=1, decision='cancel')))
        with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$'):
            contract.response(json.dumps(dict(schema_version=1, id=ID, ok=True,
                     result=dict(kind='DECISION_RECEIPT', state='MANUAL_HANDOFF'))), request)

    def test_initial_setup_never_selects_a_mode_on_empty_enter(self):
        import io
        from control_dto import Consumer
        review = importlib.import_module('review')
        provider = self.provider(state='NOT_READY', pending=False)
        output = io.StringIO()
        keys = iter(['', 'q'])
        reviewer = review.Reviewer(Consumer(provider, INSTANCE), output=output)
        self.assertTrue(hasattr(reviewer, 'setup'), 'Initial setup requires explicit local mode selection')
        self.assertFalse(reviewer.setup(lambda: next(keys)))
        self.assertEqual(provider.policy_version, 1)
        self.assertNotIn('mode: detect-first', output.getvalue())

    def test_ctrl_c_inside_mode_menu_cancels_selected_pending(self):
        import io
        from control_dto import Consumer
        review = importlib.import_module('review')
        provider = self.provider(state='WAITING_USER')
        keys = iter(['m', 'CTRL_C', 'q'])
        def read():
            key = next(keys)
            if key == 'CTRL_C':
                raise KeyboardInterrupt
            return key
        review.Reviewer(Consumer(provider, INSTANCE), output=io.StringIO()).run('SIMULASI', read)
        self.assertEqual(len(provider.pending), 0)

    def test_cancel_of_completed_request_preserves_terminal_receipt(self):
        from control_dto import Consumer
        client = Consumer(self.provider(state='COMPLETED', pending=False), INSTANCE)
        response = client.call('cancel_request', request_id='4' * 32)
        self.assertTrue(response.ok)
        self.assertEqual(response.payload.state, 'COMPLETED')

    def test_pending_and_status_nested_bounds_and_allowlists(self):
        contract = self.contract()
        provider = self.provider()
        for op in ('list_pending', 'status'):
            raw = json.dumps(dict(schema_version=1, id=ID, instance_id=INSTANCE, op=op))
            request = contract.request(raw)
            response = json.loads(provider(raw))
            mutations = []
            if op == 'list_pending':
                pending = response['result']['pending'][0]
                for patch in [dict(time_remaining=121), dict(revision=True),
                              dict(categories={'UNKNOWN': 1}), dict(categories={'PII': -1}),
                              dict(categories={'PII': True}), dict(permitted_decisions=['manual', 'variable', 'cancel']),
                              dict(purpose='SYNTHETIC_PRIVATE_CANARY')]:
                    mutations.append(response | {'result': response['result'] | {'pending': [pending | patch]}})
                mutations.append(response | {'result': response['result'] | {'pending': [pending] * 9}})
                mutations.append(response | {'result': response['result'] | {'pending': [pending] * 2}})
            else:
                for patch in [dict(instance_id='f' * 32), dict(pending_count=9), dict(integration_verified=1),
                              dict(token_store_state='SYNTHETIC_PRIVATE_CANARY'),
                              dict(required_engines={'laya': True}), dict(map={'SYNTHETIC_PRIVATE_CANARY': 'value'})]:
                    mutations.append(response | {'result': response['result'] | patch})
            for mutation in mutations:
                with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$'):
                    contract.response(json.dumps(mutation), request)

    def test_invalid_wire_errors_do_not_echo_injected_values(self):
        import contextlib
        import io
        output = io.StringIO()
        provider = self.provider()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$') as caught:
                provider(json.dumps(dict(schema_version=1, id=ID, instance_id=INSTANCE, op='status',
                                         metadata='SYNTHETIC_PRIVATE_CANARY')))
        self.assertNotIn('SYNTHETIC_PRIVATE_CANARY', str(caught.exception) + output.getvalue())

    def test_consumer_rejects_replayed_lower_policy_snapshot(self):
        from control_dto import Consumer
        provider = self.provider(pending=False)
        client = Consumer(provider, INSTANCE)
        client.call('set_verbose', enabled=True, expected_policy_version=1)
        provider.policy_version = 1
        with self.assertRaisesRegex(ValueError, '^POLICY_INVALID$'):
            client.call('status')


if __name__ == '__main__':
    unittest.main()
