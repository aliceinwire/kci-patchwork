"""URL selection and the combined workflow, with all external requests blocked."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from kci_patchwork.__main__ import main
from kci_patchwork.patchwork import Patchwork, pipeline_hash
from kci_patchwork.source import parse_source
from kci_patchwork.storage import WorkflowError, read_json
from kci_patchwork.workflow import load_run

SITE = "https://pw.example.test/install"
API = SITE + "/api/1.2"
KCI_API = "https://kernelci.example.test"
PIPELINE = "https://pipeline.example.test"
BASE_ID, ROOT_ID = "1" * 24, "2" * 24
BASE_TREE, TREE = "a" * 64, "b" * 64
DIFF = "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n"


class Response:
    def __init__(self, data, *, status=200, links=None):
        self.raw = json.dumps(data).encode()
        self.status_code = status
        self.links = links or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, **kwargs):
        yield self.raw


class URLSyntaxTests(unittest.TestCase):
    def test_web_api_export_and_filtered_project_urls(self):
        cases = (
            ("/series/42/", "series", 42, None, None),
            ("/series/42/mbox/", "series", 42, None, None),
            ("/patch/99", "patches", 99, None, None),
            ("/patch/99/raw/", "patches", 99, None, None),
            ("/patch/99/mbox/?series=42#comment-1", "patches", 99, None, None),
            ("/cover/98/mbox/", "covers", 98, None, None),
            ("/api/1.2/patches/99/", "patches", 99, None, None),
            ("/api/1.2/covers/98/", "covers", 98, None, None),
            ("/api/series/42/", "series", 42, None, None),
            ("/project/linux/list/?series=42", "series", 42, "linux", None),
            ("/project/7/?series=42", "series", 42, "7", None),
            (
                "/project/linux/patch/a+b%2Fc@example.test/",
                "patches",
                None,
                "linux",
                "a+b/c@example.test",
            ),
            (
                "/project/linux/patch/a@example.test/raw/",
                "patches",
                None,
                "linux",
                "a@example.test",
            ),
            (
                "/project/linux/cover/a/b@example.test/mbox/",
                "covers",
                None,
                "linux",
                "a/b@example.test",
            ),
        )
        for suffix, resource, ident, project, msgid in cases:
            with self.subTest(suffix=suffix):
                source = parse_source(SITE + suffix)
                self.assertEqual(source.api_url, API)
                self.assertEqual(
                    (source.resource, source.object_id, source.project, source.msgid),
                    (resource, ident, project, msgid),
                )
        self.assertEqual(
            parse_source(SITE + "/api/1.3/series/42").api_url, SITE + "/api/1.3"
        )
        self.assertEqual(
            parse_source("http://pw.example.test:80/install/series/42/").api_url, API
        )

    def test_numeric_id_server_and_api_overrides(self):
        self.assertEqual(
            parse_source(series_id=42).api_url, "https://patchwork.kernel.org/api/1.2"
        )
        self.assertEqual(parse_source("42", server_url=SITE + "/").api_url, API)
        self.assertEqual(
            parse_source(series_id=42, api_url=SITE + "/custom-api").api_url,
            SITE + "/custom-api",
        )
        self.assertEqual(
            parse_source(SITE + "/series/42", api_url=SITE + "/api/1.3").api_url,
            SITE + "/api/1.3",
        )

    def test_invalid_ambiguous_and_conflicting_urls_are_rejected(self):
        for value in (
            None,
            "0",
            "-1",
            "1000000000001",
            "not-a-url",
            SITE,
            SITE + "/project/linux/list/",
            SITE + "/bundle/user/name/",
            SITE + "/series/42/raw/",
            SITE + "/series/42/?series=43",
            SITE + "/project/linux/list/?series=42&series=43",
            SITE + "/project/linux/list/?series=",
            SITE + "/api/1.2/patches/",
            "https://user:secret@pw.example.test/series/42/",
            "https://pw.example.test/../series/42/",
            "https://pw.example.test/%2e%2e/series/42/",
            "https://pw.example.test/series/42/\n",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_source(value)
        for kwargs in (
            {"series_id": 42},
            {"server_url": "https://other.example.test"},
            {"api_url": "https://other.example.test/api/1.2"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(WorkflowError):
                parse_source(SITE + "/series/42", **kwargs)


class URLWorkflowTests(unittest.TestCase):
    def setUp(self):
        block = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected HTTP"),
        )
        block.start()
        self.addCleanup(block.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.run_dir = self.directory / "run"
        self.project = {"id": 7, "link_name": "linux"}
        self.series = {
            "id": 42,
            "name": "Two patches",
            "version": 1,
            "total": 2,
            "received_total": 2,
            "received_all": True,
            "project": self.project,
            "patches": [{"id": 100}, {"id": 99}],
            "cover_letter": {"id": 98},
        }
        self.items = {
            99
            + i: {
                "id": 99 + i,
                "name": f"[PATCH {i+1}/2] Change",
                "diff": DIFF,
                "series": [{"id": 42}],
                "project": self.project,
                "msgid": f"<patch-{i+1}@example.test>",
            }
            for i in range(2)
        }
        self.cover = {
            "id": 98,
            "series": [{"id": 42}],
            "project": self.project,
            "msgid": "<cover@example.test>",
        }
        self.session = Mock()
        self.session.get.side_effect = self.request
        self.base = {
            "id": BASE_ID,
            "treeid": BASE_TREE,
            "kind": "checkout",
            "name": "checkout",
            "parent": None,
            "state": "done",
            "result": "pass",
            "platform_filter": [],
            "artifacts": {"tarball": "https://files.example.test/base.tar.gz"},
            "data": {
                "kernel_revision": {
                    "url": "https://git.example.test/linux.git",
                    "branch": "main",
                    "commit": "c" * 40,
                }
            },
        }
        self.root = deepcopy(self.base) | {
            "id": ROOT_ID,
            "treeid": TREE,
            "name": "patchset",
            "parent": BASE_ID,
            "jobfilter": ["build"],
        }
        self.root["data"]["kernel_revision"]["patchset"] = pipeline_hash(
            [DIFF.encode(), DIFF.encode()]
        )
        self.baseline = {
            "id": "3" * 24,
            "treeid": BASE_TREE,
            "parent": BASE_ID,
            "name": "build",
            "kind": "kbuild",
            "path": ["checkout", "build"],
            "state": "done",
            "result": "pass",
            "data": {},
        }
        self.child = self.baseline | {
            "id": "4" * 24,
            "treeid": TREE,
            "parent": ROOT_ID,
            "path": ["checkout", "patchset", "build"],
        }
        self.client = Mock()
        self.client.get_node.side_effect = lambda ident, **kwargs: deepcopy(
            self.base if ident == BASE_ID else self.root
        )
        self.client.trigger_patchset.return_value = {"node": self.root}
        self.client.get_nodes.side_effect = lambda **opts: deepcopy(
            (
                [self.root, self.child]
                if opts["filters"] == [f"treeid={TREE}"]
                else [self.baseline]
            )[opts["offset"] : opts["offset"] + opts["limit"]]
        )

    def request(self, url, **opts):
        self.assertFalse(opts["allow_redirects"])
        self.assertEqual(opts["timeout"], (10, 30))
        self.assertEqual(opts["headers"], {"Accept": "application/json"})
        path = urlsplit(url).path.rstrip("/")
        if path.endswith("/series/42"):
            data = self.series
        elif path.endswith("/patches/99"):
            data = self.items[99]
        elif path.endswith("/patches/100"):
            data = self.items[100]
        elif path.endswith("/covers/98"):
            data = self.cover
        elif path.endswith("/patches"):
            self.assertEqual(
                opts["params"],
                {"project": "linux", "msgid": "patch-1@example.test", "per_page": 2},
            )
            data = [self.items[99]]
        elif path.endswith("/covers"):
            self.assertEqual(opts["params"]["msgid"], "cover@example.test")
            data = [self.cover]
        else:
            raise AssertionError(f"Unexpected URL {url}")
        return Response(deepcopy(data))

    def resolve(self, suffix):
        return Patchwork.from_source(SITE + suffix, session=self.session)

    def invoke(self, args, *, environment=None):
        out, err = StringIO(), StringIO()
        with (
            patch(
                "kci_patchwork.patchwork.requests.Session", return_value=self.session
            ),
            patch(
                "kci_patchwork.__main__.KernelCIClient", return_value=self.client
            ) as constructor,
            patch.dict(os.environ, environment or {}, clear=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(args)
        self.constructor = constructor
        return (
            code,
            json.loads(out.getvalue()) if out.getvalue() else None,
            err.getvalue(),
        )

    def arguments(self, *extra):
        return [
            "run",
            SITE + "/patch/99/",
            "--checkout",
            BASE_ID,
            "--job",
            "build",
            "--api-url",
            KCI_API,
            "--pipeline-url",
            PIPELINE,
            "--out",
            str(self.run_dir),
            *extra,
        ]

    def test_patch_and_cover_urls_select_the_complete_ordered_series(self):
        for suffix in (
            "/patch/99/mbox/",
            "/project/linux/patch/patch-1@example.test/",
            "/cover/98/",
            "/project/linux/cover/cover@example.test/mbox/",
            "/project/linux/list/?series=42",
        ):
            with self.subTest(suffix=suffix):
                source, ident = self.resolve(suffix)
                series, patches = source.fetch_series(ident)
                self.assertEqual(ident, 42)
                self.assertEqual([p["id"] for p in patches], [99, 100])
                self.assertEqual(series["api_url"], API)
                self.assertEqual(series["input_url"], SITE + suffix)

    def test_ambiguous_association_requires_an_explicit_matching_series(self):
        self.items[99]["series"].append({"id": 43})
        with self.assertRaisesRegex(WorkflowError, "multiple series"):
            self.resolve("/patch/99/")
        source, ident = self.resolve("/patch/99/?series=42")
        self.assertEqual(source.fetch_series(ident)[0]["id"], 42)
        with self.assertRaisesRegex(WorkflowError, "not associated"):
            self.resolve("/patch/99/?series=44")
        self.items[99]["series"] = []
        with self.assertRaisesRegex(WorkflowError, "not associated"):
            self.resolve("/patch/99/")

    def test_membership_and_project_are_rechecked_during_fetch(self):
        source, ident = self.resolve("/cover/98/")
        self.series["cover_letter"] = {"id": 97}
        with self.assertRaisesRegex(WorkflowError, "cover letter"):
            source.fetch_series(ident)
        source, ident = self.resolve("/patch/99/")
        self.series.update(total=1, received_total=1, patches=[{"id": 100}])
        with self.assertRaisesRegex(WorkflowError, "absent"):
            source.fetch_series(ident)
        source, ident = self.resolve("/project/other/list/?series=42")
        with self.assertRaisesRegex(WorkflowError, "different project"):
            source.fetch_series(ident)

    def test_message_id_lookup_rejects_duplicates_pagination_and_wrong_matches(self):
        wrong_project = self.items[99] | {"project": {"id": 8, "link_name": "other"}}
        wrong_msgid = self.items[99] | {"msgid": "<other@example.test>"}
        for rows, links in (
            ([], {}),
            ([self.items[99]] * 2, {}),
            ([self.items[99]], {"next": {"url": "https://other.test/"}}),
            ([wrong_project], {}),
            ([wrong_msgid], {}),
        ):
            self.session.get.side_effect = lambda *a, **k: Response(rows, links=links)
            with self.subTest(rows=rows, links=links), self.assertRaises(WorkflowError):
                self.resolve("/project/linux/patch/patch-1@example.test/")
            self.assertEqual(
                self.session.get.call_args.kwargs["allow_redirects"], False
            )

    def test_http_errors_and_html_do_not_fall_back_to_another_server(self):
        for status in (301, 401, 404, 500):
            self.session.get.reset_mock()
            self.session.get.side_effect = lambda *a, **k: Response({}, status=status)
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(WorkflowError, f"HTTP {status}"),
            ):
                self.resolve("/patch/99/")
            self.assertEqual(self.session.get.call_count, 1)
        response = Response({})
        response.raw = b"<html>Challenge</html>"
        self.session.get.side_effect = lambda *a, **k: response
        with self.assertRaisesRegex(WorkflowError, "invalid JSON"):
            self.resolve("/patch/99/")

    def test_series_command_accepts_url_and_legacy_id_without_kernelci(self):
        for args in (
            ["series", SITE + "/series/42/"],
            ["series", "--series", "42", "--patchwork-server", SITE],
        ):
            with self.subTest(args=args):
                code, output, _ = self.invoke(args)
                self.assertEqual(code, 0)
                self.assertEqual(output["series"]["api_url"], API)
                self.assertEqual([p["id"] for p in output["patches"]], [99, 100])
                self.constructor.assert_not_called()
                self.assertNotIn("diff", output["patches"][0])

    def test_config_defaults_url_precedence_and_cli_override(self):
        config = self.directory / "config.toml"
        config.write_text(
            '[patchwork]\nserver = "https://configured.test/pw"\napi = "https://configured.test/pw/api/1.3"\n'
        )
        cases = (
            (["--series", "42"], "https://configured.test/pw/api/1.3"),
            ([SITE + "/series/42/"], API),
            (["--series", "42", "--patchwork-server", SITE], API),
            (["--series", "42", "--patchwork-api", API], API),
        )
        for selection, expected in cases:
            with self.subTest(selection=selection):
                code, output, _ = self.invoke(
                    ["series", "--config", str(config), *selection]
                )
                self.assertEqual(code, 0)
                self.assertEqual(output["series"]["api_url"], expected)

    def test_url_shortcut_prepares_without_downloading_an_mbox_or_submitting(self):
        code, output, _ = self.invoke(self.arguments()[1:])
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "PREPARED")
        manifest, state, diffs = load_run(self.run_dir)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(manifest["series"]["input_url"], SITE + "/patch/99/")
        self.assertEqual(diffs, [DIFF, DIFF])
        self.assertFalse((self.run_dir / "series.mbox").exists())
        self.assertTrue((self.run_dir / "report.html").exists())
        self.client.trigger_patchset.assert_not_called()
        self.assertTrue(
            all(
                call.args[0].startswith(API + "/")
                for call in self.session.get.call_args_list
            )
        )

    def test_default_output_directory_is_scoped_by_server(self):
        previous = Path.cwd()
        try:
            os.chdir(self.directory)
            args = self.arguments()
            index = args.index("--out")
            del args[index : index + 2]
            code, output, _ = self.invoke(args)
            self.assertEqual(code, 0)
            self.assertTrue(
                Path("runs/pw.example.test/series-42/manifest.json").is_file()
            )
        finally:
            os.chdir(previous)

    def test_legacy_prepare_options_still_freeze_the_selected_server(self):
        args = self.arguments()
        args[:2] = ["prepare", "--series", "42", "--patchwork-server", SITE]
        code, output, _ = self.invoke(args)
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "PREPARED")
        self.assertEqual(load_run(self.run_dir)[0]["series"]["api_url"], API)
        self.client.trigger_patchset.assert_not_called()

    def test_run_submit_watches_compares_and_sends_every_patch_once(self):
        code, output, _ = self.invoke(
            self.arguments("--submit"), environment={"KCI_PIPELINE_TOKEN": "test-token"}
        )
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "NO_FAILURES_IN_OBSERVED_RESULTS")
        self.assertTrue(output["comparison"]["complete"])
        self.client.trigger_patchset.assert_called_once()
        self.assertEqual(
            self.client.trigger_patchset.call_args.kwargs["patches"], [DIFF, DIFF]
        )
        self.assertEqual(
            self.client.trigger_patchset.call_args.kwargs["pipeline_url"], PIPELINE
        )
        self.assertNotIn("test-token", (self.run_dir / "report.json").read_text())
        code, _, error = self.invoke(
            self.arguments("--submit"), environment={"KCI_PIPELINE_TOKEN": "test-token"}
        )
        self.assertEqual(code, 2)
        self.assertIn("already exists", error)
        self.client.trigger_patchset.assert_called_once()

    def test_submission_error_keeps_unknown_state_and_report(self):
        self.client.trigger_patchset.side_effect = TimeoutError("response lost")
        code, _, _ = self.invoke(
            self.arguments("--submit"), environment={"KCI_PIPELINE_TOKEN": "test-token"}
        )
        self.assertEqual(code, 2)
        self.assertEqual(load_run(self.run_dir)[1]["status"], "submission_unknown")
        self.assertEqual(
            read_json(self.run_dir / "report.json")["status"], "SUBMISSION_UNKNOWN"
        )
        self.client.trigger_patchset.assert_called_once()

    def test_interrupted_submission_preserves_attempt_and_refreshes_report(self):
        self.client.trigger_patchset.side_effect = KeyboardInterrupt
        code, _, _ = self.invoke(
            self.arguments("--submit"), environment={"KCI_PIPELINE_TOKEN": "test-token"}
        )
        self.assertEqual(code, 130)
        self.assertEqual(load_run(self.run_dir)[1]["status"], "submitting")
        self.assertEqual(
            read_json(self.run_dir / "report.json")["status"], "SUBMISSION_UNKNOWN"
        )
        self.client.trigger_patchset.assert_called_once()

    def test_missing_token_and_config_endpoint_mismatch_stop_before_preparation(self):
        code, _, error = self.invoke(self.arguments("--submit"))
        self.assertEqual(code, 2)
        self.assertIn("token", error)
        self.assertFalse(self.run_dir.exists())
        self.session.get.assert_not_called()
        config = self.directory / "config.toml"
        config.write_text(
            'default_instance = "staging"\n[staging]\napi = "https://different.test"\npipeline = "https://pipeline.example.test"\ntoken = "test-token"\n'
        )
        code, _, error = self.invoke(
            self.arguments("--submit", "--config", str(config))
        )
        self.assertEqual(code, 2)
        self.assertIn("endpoints differ", error)
        self.assertFalse(self.run_dir.exists())
        self.client.trigger_patchset.assert_not_called()

    def test_combined_run_timeout_keeps_submitted_run_for_later_monitoring(self):
        self.root["state"] = "available"
        with patch("kci_patchwork.__main__.time.monotonic", side_effect=[0, 2]):
            code, output, _ = self.invoke(
                self.arguments("--submit", "--timeout", "1"),
                environment={"KCI_PIPELINE_TOKEN": "test-token"},
            )
        self.assertEqual(code, 3)
        self.assertEqual(output["status"], "RUNNING")
        self.assertEqual(load_run(self.run_dir)[1]["status"], "submitted")
        self.assertTrue(read_json(self.run_dir / "report.json")["watch_timed_out"])


if __name__ == "__main__":
    unittest.main()
