"""In-process simulation only. Owner checks here are NOT production authentication."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / '.scratch/prototypes/promptshield-ui-v1'))
from control_dto import Contract, MAX_VERSION

INSTANCE = '2' * 32
PENDING = '3' * 32
REQUEST = '4' * 32


class MockControl:
    contract_version = '1.0.0'

    def __init__(self, *, owner=True, locked=False, clock=time.monotonic,
                 action_kind='REQUEST_REVIEW', state='STREAMING', pending=True,
                 mode='detect-first', engines_ready=True, forced_error=None):
        self.contract = Contract()
        self.clock = clock
        self.owner = owner
        self.locked = locked
        self.lock = threading.Lock()
        self.policy_version = 1
        self.mode = mode
        self.verbose = False
        self.state = state
        self.active_mode = 'always-mask' if state in ('SENT', 'STREAMING') else None
        self.engines_ready = engines_ready
        if forced_error is not None:
            self.contract.value(forced_error, 'error_code')
        self.forced_error = forced_error
        self.store = 'active' if self.active_mode else 'empty'
        self.stopped = False
        self.pending = {}
        self.deadlines = {}
        self.expired = set()
        self.dispositions = {}
        self.cancelled = {}
        if not pending and state in self.contract.definition['enums']['cancel_state']:
            self.cancelled[REQUEST] = state
        if pending:
            self.pending[PENDING] = dict(pending_id=PENDING, revision=1, action_kind=action_kind,
                                        request_id=REQUEST, mode=mode, policy_version=1,
                                        time_remaining=120, categories={'SECRETS': 1, 'PII': 2},
                                        permitted_decisions=self.contract.definition['decisions'][action_kind],
                                        purpose='CONNECTIVITY_CHECK')
            self.deadlines[PENDING] = self.clock() + 120

    def error(self, request, code):
        return dict(schema_version=1, id=request.id, ok=False,
                    error=dict(code=code, retryable=code in ('STALE', 'NOT_READY')))

    def status(self):
        return dict(kind='STATUS', instance_id=INSTANCE, state=self.state,
                    effective_mode=self.mode, policy_version=self.policy_version,
                    active_request_mode=self.active_mode, next_mode=self.mode,
                    required_engines={name: self.engines_ready for name in self.contract.definition['enums']['engine']},
                    pending_count=len(self.pending), coverage_profile_id='5' * 32,
                    integration_verified=False, token_store_state=self.store)

    def expire(self):
        for key in list(self.pending):
            remaining = self.deadlines[key] - self.clock()
            if remaining <= 0:
                self.expired.add(key)
                del self.pending[key]
                del self.deadlines[key]
                if not self.active_mode:
                    self.state, self.store = 'CANCELLED', 'revoked'
            else:
                self.pending[key]['time_remaining'] = min(120, int(remaining))

    def dispatch(self, request):
        if not self.owner or request.instance_id != INSTANCE:
            return self.error(request, 'FORBIDDEN')
        if self.forced_error and request.op in ('status', 'list_pending'):
            return self.error(request, self.forced_error)
        self.expire()
        op = request.op
        if self.stopped and op not in ('status', 'stop', 'cancel_request'):
            return self.error(request, 'CANCELLED')
        if op == 'status':
            result = self.status()
        elif op == 'list_pending':
            result = dict(kind='PENDING_LIST', pending=deepcopy(list(self.pending.values())))
        elif op in ('set_mode', 'set_verbose'):
            if self.locked:
                return self.error(request, 'FORBIDDEN')
            if request.expected_policy_version != self.policy_version:
                return self.error(request, 'STALE')
            if self.policy_version == MAX_VERSION or any(p['revision'] == MAX_VERSION for p in self.pending.values()):
                return self.error(request, 'POLICY_INVALID')
            self.policy_version += 1
            if op == 'set_mode':
                self.mode = request.mode
            else:
                self.verbose = request.enabled
            for pending in self.pending.values():
                pending.update(revision=pending['revision'] + 1, policy_version=self.policy_version, mode=self.mode)
            result = dict(kind=self.contract.definition['operations'][op]['result'], policy_version=self.policy_version)
        elif op == 'decide':
            if request.pending_id in self.expired:
                return self.error(request, 'EXPIRED')
            pending = self.pending.get(request.pending_id)
            if not pending or request.pending_revision != pending['revision']:
                return self.error(request, 'STALE')
            if request.decision not in pending['permitted_decisions']:
                return self.error(request, 'POLICY_INVALID')
            disposition = self.contract.definition['terminal_dispositions'][request.decision]
            self.dispositions[request.pending_id] = disposition
            del self.pending[request.pending_id]
            del self.deadlines[request.pending_id]
            if not self.active_mode:
                self.state = 'PREPARED' if request.decision == 'mask' else 'CANCELLED'
                if request.decision != 'mask':
                    self.cancelled[pending['request_id']] = 'CANCELLED'
                    self.store = 'revoked'
            # Terminal metadata only: no tool, network, secret or old-call execution path.
            result = dict(kind='DECISION_RECEIPT', state=disposition)
        elif op == 'cancel_request':
            if request.request_id not in (REQUEST, '6' * 32):
                return self.error(request, 'STALE')
            terminal = self.cancelled.setdefault(request.request_id, 'CANCELLED')
            for key in list(self.pending):
                if self.pending[key]['request_id'] == request.request_id:
                    del self.pending[key]
                    del self.deadlines[key]
            if not self.stopped and (request.request_id == '6' * 32 or not self.active_mode):
                self.state, self.active_mode, self.store = terminal, None, 'revoked'
            result = dict(kind='CANCEL_RECEIPT', state=terminal)
        elif op == 'stop':
            self.pending.clear()
            self.deadlines.clear()
            self.stopped = True
            self.state, self.active_mode, self.store = 'STOPPED', None, 'revoked'
            result = dict(kind='STOP_RECEIPT', state='STOPPED')
        else:
            return self.error(request, 'NOT_READY')
        return dict(schema_version=1, id=request.id, ok=True, result=result)

    def __call__(self, wire):
        request = self.contract.request(wire)
        with self.lock:
            response = self.dispatch(request)
        encoded = json.dumps(response, allow_nan=False)
        self.contract.response(encoded, request)
        return encoded
