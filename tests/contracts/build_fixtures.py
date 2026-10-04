"""Rebuild deterministic MOCK golden exchanges. Never calls a provider or tool."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '.scratch/prototypes/promptshield-ui-v1'))
from control_dto import Contract
from mock_control import MockControl, INSTANCE


def build():
    specs = [
        ('setup', 'FIXTURE-001', [1, 4, 5, 9], 'Pilih mode awal; Always Masking direkomendasikan.',
         dict(state='NOT_READY', pending=False, engines_ready=False), ['', '9', '3', 'q']),
        ('empty', 'FIXTURE-002', [2, 6], 'Tidak ada tindakan tertunda. Pilih sesi lokal yang tepat.',
         dict(state='EMPTY', pending=False), ['q']),
        ('pending', 'FIXTURE-003', [7, 10], 'Tinjau kategori dan jumlah sebelum memilih Mask, Edit, atau Cancel.',
         dict(state='WAITING_USER'), ['', '9', '1', 'q']),
        ('success', 'FIXTURE-004', [3, 8], 'Simulasi selesai. Submitted, streaming, dan completed adalah status berbeda.',
         dict(state='COMPLETED', pending=False), ['q']),
        ('held', 'FIXTURE-005', [5, 10], 'Tool ditahan. Manual dan variable mengakhiri call lama; mulai task baru.',
         dict(state='TOOL_HELD', action_kind='TOOL_HELD'), ['2', 'q']),
        ('stale', 'FIXTURE-006', [6, 7, 11], 'Perubahan mode membuat pilihan lama kedaluwarsa; stream aktif memakai mode awal.',
         dict(state='STREAMING'), ['m', '4', '1', 'q']),
        ('error', 'FIXTURE-007', [1, 4, 5, 8, 9], 'Engine wajib belum siap. Periksa instalasi lokal sebelum protected send.',
         dict(state='NOT_READY', engines_ready=False, pending=False), ['q']),
        ('verbose', 'FIXTURE-008', [3, 4, 6], 'Verbose hanya menambah metadata kategori dan jumlah.',
         dict(state='READY', pending=False), ['v', 'q']),
        ('cleanup', 'FIXTURE-009', [5, 7, 8, 10], 'Cancel dan stop mengakhiri proses lokal; login dan file tidak dihapus.',
         dict(state='STREAMING'), ['c', 's']),
        ('offline', 'FIXTURE-010', [1, 3, 5, 9], 'Offline self-test menguji mock lokal. Hasilnya bukan verifikasi integrasi.',
         dict(state='NOT_READY', engines_ready=False, pending=False), ['q']),
        ('keyboard', 'FIXTURE-011', [12], 'Kategori infrastruktur dan identitas sintetis dengan label panjang tetap terbaca melalui keyboard.',
         dict(state='WAITING_USER'), ['x', '3', 'q']),
        ('forbidden', 'FIXTURE-002', [6], 'Akses sesi ditolak. Gunakan reviewer milik owner yang benar.',
         dict(owner=False), ['q']),
        ('degraded', 'FIXTURE-007', [8], 'Detect Only degraded: konten asli dapat dikirim. Perbaiki engine lokal.',
         dict(state='DEGRADED', mode='detect-only', engines_ready=False, pending=False), ['q']),
        ('unsafe', 'FIXTURE-006', [11], 'Protection: OFF. Request baru tidak diperiksa atau dimasking.',
         dict(state='READY', mode='off', pending=False), ['q']),
        ('expired', 'FIXTURE-009', [7, 10], 'Deadline habis. Pilihan lama ditolak; buat request baru.',
         dict(state='WAITING_USER'), ['1', 'q']),
        ('manual', 'FIXTURE-005', [3, 10], 'Manual handoff mengakhiri call lama; bukan tool success.',
         dict(state='TOOL_HELD', action_kind='TOOL_HELD'), ['1', 'q']),
        ('edit', 'FIXTURE-003', [3, 10], 'Edit mengakhiri request lama; input baru ditinjau kembali.',
         dict(state='WAITING_USER'), ['2', 'q']),
        ('ctrl-c', 'FIXTURE-011', [10, 12], 'Ctrl+C membatalkan pilihan aktif dan menutup reviewer.',
         dict(state='WAITING_USER'), ['CTRL_C']),
        ('disconnect', 'FIXTURE-009', [7, 10], 'Reviewer terputus tidak menyetujui tindakan pending.',
         dict(state='WAITING_USER'), ['DISCONNECT']),
        ('auth', 'FIXTURE-007', [5], 'Simulasi login native diperlukan.',
         dict(pending=False, forced_error='AUTH_REQUIRED'), ['q']),
        ('upstream', 'FIXTURE-007', [5, 9], 'Simulasi upstream tidak tersedia.',
         dict(pending=False, forced_error='UPSTREAM_UNAVAILABLE'), ['q']),
        ('tool-limit', 'FIXTURE-005', [5], 'Simulasi batas tool gate; tidak ada executable output.',
         dict(pending=False, forced_error='TOOL_GATE_LIMIT'), ['q']),
        ('stream-invalid', 'FIXTURE-007', [5, 8], 'Simulasi stream belum lengkap.',
         dict(pending=False, forced_error='STREAM_INVALID'), ['q']),
    ]
    scenarios = []
    for name, fixture, states, label, options, keys in specs:
        provider = MockControl(clock=lambda: 0.0, **options)
        if name == 'expired':
            provider.clock = lambda: 121.0
        exchanges = []
        for number, op in enumerate(('status', 'list_pending'), 1):
            request = dict(schema_version=1, id=f'{number:032x}', instance_id=INSTANCE, op=op)
            exchanges.append(dict(request=request, response=json.loads(provider(json.dumps(request)))))
        scenarios.append(dict(name=name, fixture_id=fixture,
                              states=[f'UI-STATE-{state:03d}' for state in states],
                              label=label, options=options, keys=keys, exchanges=exchanges))
    base = dict(schema_version=1, id='1' * 32, instance_id=INSTANCE, op='status')
    wire = json.dumps(base)
    abuses = [
        ('unknown_value_field', json.dumps(base | {'value': 'SYNTHETIC_PRIVATE_CANARY'})),
        ('duplicate_key', wire.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')),
        ('nonfinite', wire.replace('"schema_version": 1', '"schema_version": NaN')),
        ('bool_not_integer', wire.replace('"schema_version": 1', '"schema_version": true')),
        ('invalid_identifier', wire.replace(INSTANCE, 'A' * 32)),
        ('missing_field', json.dumps({key: value for key, value in base.items() if key != 'id'})),
        ('unknown_operation', json.dumps(base | {'op': 'submit_inference'})),
    ]
    return dict(revision='1.0.0', contract_version=Contract().version,
                environment='MOCK', authority='FSD-PROMPTSHIELD-V1#TEST-001', scenarios=scenarios,
                abuse_requests=[dict(name=name, wire=raw, error='POLICY_INVALID') for name, raw in abuses])


if __name__ == '__main__':
    target = ROOT / 'tests/fixtures/synthetic/contracts-v1.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('Wrote deterministic MOCK catalog: ' + target.relative_to(ROOT).as_posix())
