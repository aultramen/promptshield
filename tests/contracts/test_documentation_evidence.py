"""Check that the offline delivery's published evidence remains reproducible."""

import hashlib
import json
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = (
    "docs/prd/prd-promptshield-v1.md",
    "docs/fsd/fsd-promptshield-v1.md",
    ".scratch/promptshield-v1/PROGRESS.md",
    ".scratch/promptshield-v1/issues/02-native-subscription-slice.md",
)


class OfflineEvidenceTests(unittest.TestCase):
    def test_stored_report_pins_the_current_offline_sources(self):
        report = json.loads(
            (ROOT / ".scratch/prototypes/promptshield-ui-v1/evidence/scenario-report.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(report["environment"], "MOCK")
        self.assertEqual(report["failures"], [])
        self.assertEqual(set(report["ui_states"]),
                         {f"UI-STATE-{number:03}" for number in range(1, 13)})
        self.assertTrue(report["source_digests"], "Evidence needs pinned sources")
        for source, expected in report["source_digests"].items():
            with self.subTest(source=source):
                content = (ROOT / source).read_bytes()
                # The historical Windows report hashes raw bytes. Git checks out
                # text with LF; accept only that configured LF/CRLF conversion.
                canonical = content.replace(b"\r\n", b"\n")
                digests = {hashlib.sha256(value).hexdigest() for value in
                           (content, canonical, canonical.replace(b"\n", b"\r\n"))}
                self.assertIn(expected, digests)

    def test_delivery_documents_link_to_available_local_evidence(self):
        for document in DOCUMENTS:
            path = ROOT / document
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
                target = unquote(link.split("#", 1)[0].strip("<>"))
                if not target or urlsplit(target).scheme:
                    continue
                with self.subTest(document=document, target=target):
                    self.assertTrue((path.parent / target).exists(), "Local evidence target is missing")


if __name__ == "__main__":
    unittest.main()
