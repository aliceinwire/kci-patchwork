"""Offline tests of the mbox example; all real HTTP requests are blocked."""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from email.message import EmailMessage
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from examples.prepare_mbox import MboxPatchwork, main, mbox_patches
from kci_patchwork.patchwork import Patchwork
from kci_patchwork.storage import WorkflowError, digest
from kci_patchwork.workflow import load_run

DIFF = "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n"


def patch_entry(number):
    return {
        "position": number,
        "id": 100 + number,
        "msgid": f"<patch-{number}@example.test>",
        "name": f"[PATCH {number}/2] Example {number}",
        "web_url": f"https://patchwork.example.test/patch/{100 + number}/",
        "diff": DIFF.replace("new", f"new-{number}"),
    }


def message(item, cte="7bit"):
    mail = EmailMessage()
    mail["X-Patchwork-Id"] = str(item["id"])
    mail["Message-ID"] = item["msgid"]
    mail["Subject"] = item["name"]
    mail.set_content("Example commit message.\n\n" + item["diff"], cte=cte)
    return b"From patchwork Mon Oct  5 00:00:00 2026\n" + mail.as_bytes() + b"\n"


class MboxExampleTests(unittest.TestCase):
    def setUp(self):
        blocker = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected HTTP request"),
        )
        blocker.start()
        self.addCleanup(blocker.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "series.mbox"
        self.patches = [patch_entry(1), patch_entry(2)]
        self.raw = b"".join(message(item) for item in self.patches)
        self.path.write_bytes(self.raw)

    def test_reordered_mime_encoded_messages_keep_series_order_and_bytes(self):
        raw = message(self.patches[1], "base64") + message(
            self.patches[0], "quoted-printable"
        )
        parsed = mbox_patches(raw, self.patches)
        self.assertEqual([p["id"] for p in parsed], [101, 102])
        self.assertEqual([p["diff"] for p in parsed], [p["diff"] for p in self.patches])
        self.assertEqual(parsed[0]["sha256"], digest(self.patches[0]["diff"].encode()))

    def test_reject_missing_duplicate_unrelated_and_html_inputs(self):
        bad_inputs = [
            message(self.patches[0]),
            message(self.patches[0]) * 2,
            self.raw.replace(b"X-Patchwork-Id: 102", b"X-Patchwork-Id: 999"),
            b"<html>Please enable JavaScript</html>",
        ]
        for raw in bad_inputs:
            with self.subTest(raw=raw[:60]), self.assertRaises(WorkflowError):
                mbox_patches(raw, self.patches)

    def test_reject_wrong_identity_or_changed_hunk_context(self):
        for raw in (
            self.raw.replace(b"patch-1@", b"other-1@"),
            self.raw.replace(b"@@ -1 +1 @@", b"@@ -2 +2 @@"),
            self.raw.replace(b"+new-1", b"+changed"),
            self.raw + message(self.patches[0]),
        ):
            with self.subTest(raw=raw[:60]), self.assertRaises(WorkflowError):
                mbox_patches(raw, self.patches)

    def test_snapshot_is_used_even_if_original_file_changes(self):
        source = MboxPatchwork(self.path)
        self.path.write_bytes(b"changed after reading")
        with patch.object(
            Patchwork, "fetch_series", return_value=({"id": 42}, self.patches)
        ):
            series, patches = source.fetch_series(42)
        self.assertEqual(source.raw, self.raw)
        self.assertEqual(series["mbox_sha256"], digest(self.raw))
        self.assertEqual(patches[0]["diff"], self.patches[0]["diff"])

    def test_verify_without_contacting_kernelci(self):
        output = StringIO()
        with (
            patch.object(
                Patchwork, "fetch_series", return_value=({"id": 42}, self.patches)
            ),
            patch("examples.prepare_mbox.KernelCIClient") as client,
            redirect_stdout(output),
        ):
            result = main(["--series", "42", "--mbox", str(self.path)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "MBOX_VERIFIED")
        client.assert_not_called()

    def test_preparation_freezes_mbox_and_reports_prepared_without_submission(self):
        run = self.directory / "run"
        client = Mock()
        client.get_node.return_value = {
            "id": "1" * 24,
            "treeid": "a" * 64,
            "kind": "checkout",
            "name": "checkout",
            "state": "done",
            "result": "pass",
            "artifacts": {"tarball": "https://example.test/source.tar.gz"},
            "data": {
                "kernel_revision": {
                    "url": "https://example.test/linux.git",
                    "branch": "master",
                    "commit": "b" * 40,
                }
            },
        }
        arguments = [
            "--series",
            "42",
            "--mbox",
            str(self.path),
            "--checkout",
            "1" * 24,
            "--api-url",
            "https://api.example.test",
            "--pipeline-url",
            "https://pipeline.example.test",
            "--job",
            "example-build",
            "--out",
            str(run),
        ]
        with (
            patch.object(
                Patchwork,
                "fetch_series",
                return_value=(
                    {"id": 42, "name": "Example series", "version": 1},
                    deepcopy(self.patches),
                ),
            ),
            patch("examples.prepare_mbox.KernelCIClient", return_value=client),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(arguments), 0)
        manifest, state, patches = load_run(run)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(patches, [p["diff"] for p in self.patches])
        self.assertEqual((run / "series.mbox").read_bytes(), self.raw)
        self.assertEqual(manifest["series"]["mbox_sha256"], digest(self.raw))
        self.assertEqual(
            json.loads((run / "report.json").read_text())["status"], "PREPARED"
        )
        self.assertTrue((run / "report.html").is_file())
        self.assertEqual([call[0] for call in client.method_calls], ["get_node"])

    def test_invalid_mbox_returns_error_without_creating_run(self):
        self.path.write_bytes(b"<html>not an mbox</html>")
        with (
            patch.object(
                Patchwork, "fetch_series", return_value=({"id": 42}, self.patches)
            ),
            redirect_stderr(StringIO()),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["--series", "42", "--mbox", str(self.path)]), 2)


if __name__ == "__main__":
    unittest.main()
