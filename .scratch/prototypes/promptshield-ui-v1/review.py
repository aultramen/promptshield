"""Throwaway Bahasa Indonesia keyboard reviewer; fixed local DTOs, MOCK only."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import platform
import textwrap

from control_dto import Consumer, Contract, ROOT
sys.path.insert(0, str(ROOT / 'tests/contracts'))
from mock_control import MockControl, INSTANCE

CATALOG = ROOT / 'tests/fixtures/synthetic/contracts-v1.json'
MODE_LABELS = {
    'detect-first': 'Detect First: tahan finding hingga pilihan sah.',
    'detect-only': 'Detect Only: konten asli dapat dikirim, tanpa masking.',
    'always-mask': 'Always Masking: direkomendasikan untuk proteksi.',
    'off': 'Protection: OFF. Konten asli dapat dikirim tanpa pemeriksaan.'
}
DECISION_LABELS = {
    'mask': 'Mask: lanjutkan dengan masking (simulasi)',
    'edit': 'Edit: batalkan request lama, ubah input lalu ajukan request baru',
    'cancel': 'Cancel: batalkan tindakan',
    'manual': 'Manual: akhiri call lama; lakukan pekerjaan secara manual',
    'variable': 'Variable: akhiri call lama; siapkan value lokal dan task baru'
}
ERROR_LABELS = {
    'STALE': 'Pilihan lama ditolak. Muat ulang status dan tinjau revisi baru.',
    'EXPIRED': 'Waktu review habis. Buat request baru; tidak ada persetujuan otomatis.',
    'FORBIDDEN': 'Akses ditolak. Pilih sesi yang Anda miliki atau hubungi pengelola policy.',
    'POLICY_INVALID': 'Pilihan tidak valid. Policy yang sah tetap dipertahankan.',
    'CANCELLED': 'Sesi berhenti. Mulai sesi baru setelah readiness diperiksa.',
    'NOT_READY': 'Belum siap. Periksa konfigurasi dan required engines lokal.',
    'AUTH_REQUIRED': 'Login melalui native Codex lalu mulai sesi baru.',
    'AUTH_FAILED': 'Autentikasi native gagal. Periksa login di native Codex lalu coba sesi baru.',
    'QUOTA_RESTRICTED': 'Quota subscription dibatasi. Periksa account native; tidak ada fallback otomatis.',
    'MODEL_UNAVAILABLE': 'Model lokal belum tersedia. Periksa provisioning model yang qualified.',
    'DETECTOR_TIMEOUT': 'Detektor melewati deadline. Protected send diblokir; periksa engine lokal.',
    'DETECTOR_FAILED': 'Detektor gagal. Protected send diblokir; periksa engine lokal.',
    'INPUT_LIMIT': 'Input melewati batas. Kurangi input dan ajukan request baru.',
    'RESPONSE_LIMIT': 'Response melewati batas dan belum lengkap. Ajukan task baru setelah review.',
    'TIMEOUT': 'Deadline habis. Status belum sukses; tinjau status sebelum request baru.',
    'UNSUPPORTED_CONTENT': 'Jenis konten belum didukung. Gunakan input teks yang qualified.',
    'MAP_LIMIT': 'Map mencapai batas lokal. Protected send diblokir; mulai scope baru.',
    'MAP_EXPIRED': 'Map berakhir. Tidak ada pemulihan value lama; mulai request baru.',
    'TOOL_HELD': 'Tool ditahan. Pilih manual, variable, atau cancel; call lama tidak dilanjutkan.',
    'TOOL_GATE_LIMIT': 'Tool gate melewati batas. Call tetap ditahan; batalkan dan buat task baru.',
    'STREAM_INVALID': 'Stream tidak valid dan response belum lengkap. Periksa status lalu buat task baru.',
    'UPSTREAM_UNAVAILABLE': 'Upstream tidak tersedia. Tidak ada direct-provider fallback; coba task baru nanti.',
    'INTERNAL_ERROR': 'Operasi lokal gagal. Hentikan sesi dan periksa readiness sebelum mulai lagi.'
}


def catalog():
    result = json.loads(CATALOG.read_text(encoding='utf-8'))
    if result['contract_version'] != Contract().version:
        raise ValueError('POLICY_INVALID')
    return result


class Reviewer:
    def __init__(self, client, *, width=80, output=sys.stdout):
        self.client, self.width, self.output = client, width, output
        self.selected = None
        self.status = None
        self.verbose = False

    def setup(self, read=input):
        self.say('PromptShield | SIMULASI OFFLINE | MOCK | pilih mode awal')
        self.say('Belum ada pilihan. Tidak ada request yang dikirim. Cancel: q atau Ctrl+C.')
        modes = self.client.contract.definition['enums']['mode']
        for index, mode in enumerate(modes, 1):
            self.say(f'{index}. {mode}: ' + MODE_LABELS[mode])
        while True:
            self.say('Pilih nomor lalu Enter; kosong tidak memilih default. q Cancel setup.')
            try:
                key = read().strip()
            except (EOFError, KeyboardInterrupt):
                self.say('Setup dibatalkan; tidak ada mode yang disimpan.')
                return False
            if key == 'q':
                self.say('Setup dibatalkan; tidak ada mode yang disimpan.')
                return False
            if key not in ('1', '2', '3', '4'):
                self.say('Pilihan tidak valid. Pilih mode secara eksplisit atau Cancel.')
                continue
            self.say(MODE_LABELS[modes[int(key) - 1]])
            response = self.client.call('set_mode', mode=modes[int(key) - 1], expected_policy_version=1)
            return self.result(response)

    def say(self, line):
        self.output.write(textwrap.fill(line, width=self.width, break_long_words=True,
                                        break_on_hyphens=False) + '\n')

    def result(self, response):
        if not response.ok:
            self.say(response.payload.code + ': ' + ERROR_LABELS.get(response.payload.code,
                     'Operasi gagal. Periksa readiness lokal; ulangi sebagai request baru.'))
            return False
        if response.payload.kind.endswith('_RECEIPT'):
            if hasattr(response.payload, 'state'):
                self.say('Hasil tindakan: ' + response.payload.state)
                if response.payload.state == 'MANUAL_HANDOFF':
                    self.say('Call lama berakhir. Kerjakan langkah manual di luar reviewer.')
                elif response.payload.state == 'VARIABLE_GUIDANCE':
                    self.say('Call lama berakhir. Siapkan variable lokal melalui izin native dan kirim task baru.')
                elif response.payload.state == 'EDIT':
                    self.say('Request lama berakhir. Ubah input di native CLI lalu ajukan request baru.')
            else:
                self.say('Policy version: ' + str(response.payload.policy_version))
        return True

    def refresh(self):
        response = self.client.call('status')
        if not self.result(response):
            return False
        self.status = response.payload
        pending = self.client.call('list_pending')
        if not self.result(pending):
            return False
        self.selected = pending.payload.pending[0] if pending.payload.pending else None
        return True

    def interrupt(self):
        if self.selected:
            self.result(self.client.call('cancel_request', request_id=self.selected.request_id))
        self.say('Reviewer dibatalkan. Tidak ada call lama yang dilanjutkan.')

    def screen(self, label):
        self.say('PromptShield | SIMULASI OFFLINE | MOCK | integrasi belum diverifikasi')
        self.say(label)
        if self.status:
            status = self.status
            self.say('Sesi lokal: ' + status.instance_id)
            self.say('Status: ' + status.state + ' | mode: ' + status.effective_mode)
            self.say(MODE_LABELS[status.effective_mode])
            self.say('Mode request aktif: ' + str(status.active_request_mode) + ' | mode berikutnya: ' + status.next_mode)
            self.say('Map: ' + status.token_store_state + ' | policy version: ' + str(status.policy_version))
            self.say('Required engines: ' + ', '.join(name + (' siap' if ready else ' belum siap')
                                                     for name, ready in status.required_engines.items()))
            if not all(status.required_engines.values()):
                self.say('NOT_READY: protected send diblokir. Periksa engine dan konfigurasi lokal.')
        if self.selected:
            item = self.selected
            self.say('Fokus: tindakan tertunda ' + item.action_kind + ' | revisi: ' + str(item.revision))
            self.say('Kategori/jumlah: ' + ', '.join(name + ': ' + str(count) for name, count in item.categories.items()))
            self.say('Tujuan: ' + item.purpose + ' | sisa waktu: ' + str(item.time_remaining) + ' detik')
            for index, decision in enumerate(item.permitted_decisions, 1):
                self.say(f'{index}. ' + DECISION_LABELS[decision])
        else:
            self.say('EMPTY: tidak ada tindakan tertunda. Pilih sesi atau mulai request baru.')
        self.say('m Mode | v Verbose metadata | r Refresh | c Cancel request aktif | s Stop | q Keluar')
        self.say('Ketik pilihan lalu Enter. Enter kosong tidak menyetujui tindakan. Ctrl+C membatalkan pilihan aktif.')

    def run(self, label, read=input, initial_setup=False):
        if initial_setup and not self.setup(read):
            return
        self.refresh()
        while True:
            self.screen(label)
            try:
                key = read().strip()
            except KeyboardInterrupt:
                self.interrupt()
                return
            except EOFError:
                self.say('Reviewer terputus. Tindakan tetap pending sampai deadline; tidak otomatis disetujui.')
                return
            if key == 'q':
                self.say('Reviewer ditutup. Tindakan pending mengikuti deadline.')
                return
            if key == 'm' and self.status:
                modes = self.client.contract.definition['enums']['mode']
                for i, mode in enumerate(modes, 1):
                    self.say(f'{i}. {mode}: ' + MODE_LABELS[mode])
                self.say('Pilih mode lalu Enter; kosong membatalkan perubahan.')
                try:
                    mode_key = read().strip()
                except KeyboardInterrupt:
                    self.interrupt()
                    return
                except EOFError:
                    self.say('Perubahan mode dibatalkan. Policy tetap.')
                    self.say('Reviewer terputus. Tindakan tetap pending sampai deadline.')
                    return
                if mode_key in ('1', '2', '3', '4'):
                    self.result(self.client.call('set_mode', mode=modes[int(mode_key) - 1],
                                                 expected_policy_version=self.status.policy_version))
                    # Retain the selected revision until r, so stale approval is visibly rejected.
                    response = self.client.call('status')
                    if self.result(response):
                        self.status = response.payload
                else:
                    self.say('Pilihan mode tidak valid. Policy tetap; tidak ada perubahan.')
            elif key == 'r':
                self.refresh()
            elif key == 'v' and self.status:
                if self.result(self.client.call('set_verbose', enabled=not self.verbose,
                                               expected_policy_version=self.status.policy_version)):
                    self.verbose = not self.verbose
                    self.say('Verbose metadata: ' + ('aktif' if self.verbose else 'mati'))
                self.refresh()
            elif key == 's':
                self.result(self.client.call('stop'))
                self.refresh()
                return
            elif key == 'c' and self.status:
                request_id = '6' * 32 if self.status.active_request_mode else (self.selected.request_id if self.selected else None)
                if request_id:
                    self.result(self.client.call('cancel_request', request_id=request_id))
                    self.refresh()
            elif self.selected and key in ('1', '2', '3'):
                item = self.selected
                response = self.client.call('decide', pending_id=item.pending_id,
                                             pending_revision=item.revision,
                                             decision=item.permitted_decisions[int(key) - 1])
                self.result(response)
                self.refresh()
            else:
                self.say('Pilihan tidak valid. Fokus dan tindakan tertunda tetap; pilih nomor atau Cancel.')


def scenario_suite(widths, evidence_dir=None):
    fixtures = catalog()
    failures, checks, transcripts = [], 0, {}
    for scenario in fixtures['scenarios']:
        for width in widths:
            output = io.StringIO()
            provider = MockControl(clock=lambda: 0.0, **scenario['options'])
            if scenario['name'] == 'expired':
                provider.clock = lambda: 121.0
            client = Consumer(provider, INSTANCE)
            for exchange in scenario['exchanges']:
                actual = json.loads(provider(json.dumps(exchange['request'])))
                if actual != exchange['response']:
                    failures.append(scenario['name'] + ': golden DTO mismatch')
                request = client.contract.request(json.dumps(exchange['request']))
                client.contract.response(json.dumps(actual), request)
            keys = iter(scenario['keys'])
            def read():
                try:
                    key = next(keys)
                except StopIteration:
                    raise EOFError from None
                output.write('> ' + key + '\n')
                if key == 'CTRL_C':
                    raise KeyboardInterrupt
                if key == 'DISCONNECT':
                    raise EOFError
                return key
            Reviewer(client, width=width, output=output).run(scenario['label'], read,
                                                             initial_setup=scenario['name'] == 'setup')
            transcript = output.getvalue()
            checks += 1
            if any(len(line) > width for line in transcript.splitlines()):
                failures.append(scenario['name'] + ': overflow')
            if 'Cancel' not in transcript or 'MOCK' not in transcript or '\x1b' in transcript:
                failures.append(scenario['name'] + ': essential plain status missing')
            expected = {'pending': 'Hasil tindakan: MASK', 'held': 'VARIABLE_GUIDANCE',
                        'stale': 'STALE:', 'forbidden': 'FORBIDDEN:', 'cleanup': 'STOPPED',
                        'keyboard': 'Hasil tindakan: CANCEL', 'unsafe': 'Protection: OFF'}
            if scenario['name'] in expected and expected[scenario['name']] not in transcript:
                failures.append(scenario['name'] + ': expected outcome missing')
            transcripts[f"{scenario['name']}-{width}.txt"] = transcript
    report = dict(environment='MOCK', contract_version=fixtures['contract_version'],
                  fixture_revision=fixtures['revision'], checks=checks, failures=failures,
                  widths=widths, ui_states=sorted({s for f in fixtures['scenarios'] for s in f['states']}),
                  verified_at=datetime.now(timezone.utc).isoformat(), python=sys.version,
                  platform=platform.platform(), reviewer='Codex agent (sequential local review)',
                  command='rtk python .scratch/prototypes/promptshield-ui-v1/review.py --scenario-suite --widths '
                          + ','.join(str(width) for width in widths),
                  configuration='stdlib, synthetic in-process mock, no production transport')
    if evidence_dir:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for name, transcript in transcripts.items():
            (evidence_dir / name).write_text(transcript, encoding='utf-8')
        report['source_digests'] = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in [Path(__file__), Path(__file__).with_name('control_dto.py'),
                                               ROOT / 'tests/contracts/mock_control.py', CATALOG,
                                               ROOT / 'tests/contracts/test_local_contract.py',
                                               ROOT / 'tests/contracts/build_fixtures.py',
                                               ROOT / 'contracts/promptshield/local-v1.json']}
        (evidence_dir / 'scenario-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description='PromptShield offline UI simulation; no native Codex or inference.')
    parser.add_argument('--scenario-suite', action='store_true')
    parser.add_argument('--widths', default='80,120')
    parser.add_argument('--scenario', default='pending', choices=[s['name'] for s in catalog()['scenarios']])
    parser.add_argument('--width', type=int, default=80)
    parser.add_argument('--plain', action='store_true', help='All output is plain, including TTY output.')
    parser.add_argument('--evidence-dir', type=Path)
    args = parser.parse_args()
    try:
        widths = [int(width) for width in args.widths.split(',')]
        if not widths or any(width < 20 or width > 240 for width in widths) or not 20 <= args.width <= 240:
            raise ValueError
    except ValueError:
        parser.error('Lebar terminal harus 20..240.')
    if args.scenario_suite:
        report = scenario_suite(widths, args.evidence_dir)
        print(json.dumps(report, indent=2))
        return 1 if report['failures'] else 0
    scenario = next(s for s in catalog()['scenarios'] if s['name'] == args.scenario)
    provider = MockControl(clock=lambda: 0.0, **scenario['options'])
    # Interactive deadlines use real monotonic time, unlike deterministic fixture replay.
    import time
    provider.clock = time.monotonic
    provider.deadlines = {key: time.monotonic() + 120 for key in provider.pending}
    Reviewer(Consumer(provider, INSTANCE), width=args.width).run(scenario['label'],
                                                               initial_setup=args.scenario == 'setup')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
