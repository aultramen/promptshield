"""Throwaway fixed CONTRACT-001 consumer. Standard library; no runtime transport."""
from dataclasses import make_dataclass
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Literal, Mapping

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = ROOT / 'contracts/promptshield/local-v1.json'
MAX_VERSION = 2**53 - 1


def invalid():
    raise ValueError('POLICY_INVALID') from None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid()
        result[key] = value
    return result


def decode(data, max_bytes=65536):
    try:
        if not isinstance(data, (str, bytes)):
            invalid()
        raw = data.encode('utf-8') if isinstance(data, str) else data
        if len(raw) > max_bytes:
            invalid()
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object,
                          parse_constant=lambda _: invalid())
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        invalid()


class Contract:
    """Only the fixed DTO primitives in local-v1; no external schema dialect."""
    def __init__(self):
        self.definition = decode(CONTRACT_PATH.read_bytes())
        self.version = self.definition['version']
        self.types = {}
        for name, fields in self.definition['objects'].items():
            self.types[name] = make_dataclass(name, [(k, self.annotation(v))
                                                    for k, v in fields.items()], frozen=True)
        for op, spec in self.definition['operations'].items():
            fields = self.definition['request_common'] | spec['fields']
            self.types[op] = make_dataclass('Request_' + op,
                                           [(k, self.annotation(v)) for k, v in fields.items()],
                                           frozen=True)
        self.response_type = make_dataclass('Response', [('schema_version', int), ('id', str),
                                                        ('ok', bool), ('payload', object)], frozen=True)

    def annotation(self, spec):
        if isinstance(spec, str):
            if spec in ('schema_version', 'version'):
                return int
            if spec == 'bool':
                return bool
            if spec in self.definition['enums']:
                return Literal[tuple(self.definition['enums'][spec])]
            return str if spec in ('id', 'operation') else spec
        if 'int' in spec:
            return int
        if 'const' in spec:
            return Literal[spec['const']]
        if 'nullable' in spec:
            return self.annotation(spec['nullable']) | type(None)
        if 'array' in spec:
            return tuple[self.annotation(spec['array']), ...]
        return Mapping[self.annotation(spec['map']), self.annotation(spec['value'])]

    def value(self, value, spec):
        if isinstance(spec, str):
            if spec in self.definition['objects']:
                return self.object(value, spec)
            if spec == 'id':
                valid = type(value) is str and re.fullmatch('[0-9a-f]{32}', value)
            elif spec == 'bool':
                valid = type(value) is bool
            elif spec == 'version':
                valid = type(value) is int and 1 <= value <= MAX_VERSION
            elif spec == 'schema_version':
                valid = type(value) is int and value == 1
            else:
                allowed = self.definition['operations'] if spec == 'operation' else self.definition['enums'][spec]
                valid = type(value) is str and value in allowed
            if not valid:
                invalid()
            return value
        if 'const' in spec:
            if type(value) is not type(spec['const']) or value != spec['const']:
                invalid()
            return value
        if 'int' in spec:
            if type(value) is not int or not spec['int'][0] <= value <= spec['int'][1]:
                invalid()
            return value
        if 'nullable' in spec:
            return None if value is None else self.value(value, spec['nullable'])
        if 'array' in spec:
            if type(value) is not list or len(value) > spec['max']:
                invalid()
            return tuple(self.value(item, spec['array']) for item in value)
        if 'map' in spec:
            allowed = set(self.definition['enums'][spec['map']])
            if type(value) is not dict or not set(value) <= allowed:
                invalid()
            if spec['complete'] and set(value) != allowed:
                invalid()
            return MappingProxyType({key: self.value(item, spec['value']) for key, item in value.items()})
        invalid()

    def fields(self, value, fields):
        if type(value) is not dict or set(value) != set(fields):
            invalid()
        return {key: self.value(value[key], spec) for key, spec in fields.items()}

    def object(self, value, name):
        result = self.types[name](**self.fields(value, self.definition['objects'][name]))
        if name == 'PendingSummary' and list(result.permitted_decisions) != self.definition['decisions'][result.action_kind]:
            invalid()
        if name == 'PendingList':
            ids = [item.pending_id for item in result.pending]
            if len(ids) != len(set(ids)):
                invalid()
        return result

    def request(self, data):
        value = decode(data, self.definition['max_bytes'])
        if type(value) is not dict:
            invalid()
        op = self.value(value.get('op'), 'operation')
        fields = self.definition['request_common'] | self.definition['operations'][op]['fields']
        return self.types[op](**self.fields(value, fields))

    def response(self, data, request):
        value = decode(data, self.definition['max_bytes'])
        if type(value) is not dict or type(value.get('ok')) is not bool:
            invalid()
        payload_key = 'result' if value['ok'] else 'error'
        if set(value) != {'schema_version', 'id', 'ok', payload_key}:
            invalid()
        self.value(value['schema_version'], 'schema_version')
        if self.value(value['id'], 'id') != request.id:
            invalid()
        result_name = self.definition['operations'][request.op]['result']
        payload = self.object(value[payload_key], self.definition['results'][result_name] if value['ok'] else 'SafeError')
        if value['ok'] and result_name == 'STATUS' and payload.instance_id != request.instance_id:
            invalid()
        if value['ok'] and result_name in ('MODE_RECEIPT', 'VERBOSE_RECEIPT'):
            if payload.policy_version != request.expected_policy_version + 1:
                invalid()
        if value['ok'] and result_name == 'DECISION_RECEIPT':
            if payload.state != self.definition['terminal_dispositions'][request.decision]:
                invalid()
        return self.response_type(1, request.id, value['ok'], payload)


class Consumer:
    def __init__(self, transport, instance_id):
        self.contract = Contract()
        self.transport = transport
        self.instance_id = instance_id
        self.sequence = 0
        self.policy_version = 0

    def call(self, op, **fields):
        self.sequence += 1
        value = dict(schema_version=1, id=f'{self.sequence:032x}', instance_id=self.instance_id, op=op) | fields
        wire = json.dumps(value)
        request = self.contract.request(wire)
        response = self.contract.response(self.transport(wire), request)
        if response.ok and hasattr(response.payload, 'policy_version'):
            if response.payload.policy_version < self.policy_version:
                invalid()
            self.policy_version = response.payload.policy_version
        return response
