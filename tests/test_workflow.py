"""Offline workflow tests. No real POST or other HTTP request can escape."""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from kcidev import KernelCIClient

from kci_patchwork.__main__ import main
from kci_patchwork.patchwork import Patchwork, patch_position, pipeline_hash
from kci_patchwork.report import render, write_report
from kci_patchwork.results import collect
from kci_patchwork.storage import WorkflowError, locked, now, read_json, write_json
from kci_patchwork.workflow import load_run, prepare, reconcile, submit

BASE_ID, ROOT_ID, CHILD_ID = "1" * 24, "2" * 24, "3" * 24
BASE_TREE, TREE = "a" * 64, "b" * 64
API, PIPELINE = "https://maestro.example.test", "https://pipeline.example.test"
DIFF = "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n"


def base_node():
    return {
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
                "branch": "master",
                "commit": "c" * 40,
            }
        },
    }


def patchset_node(state="running", result=None):
    node = base_node()
    node.update(
        id=ROOT_ID,
        treeid=TREE,
        name="patchset",
        parent=BASE_ID,
        state=state,
        result=result,
        jobfilter=["kbuild-gcc-12-x86"],
        created=now(),
    )
    if result == "pass":
        node["data"]["kernel_revision"]["patchset"] = pipeline_hash([DIFF.encode()])
    return node


def child_node(state="done", result="pass"):
    return {
        "id": CHILD_ID,
        "treeid": TREE,
        "parent": ROOT_ID,
        "name": "kbuild-gcc-12-x86",
        "kind": "kbuild",
        "state": state,
        "result": result,
        "data": {"platform": "x86"},
        "artifacts": {"log": "https://files.example.test/log.txt"},
    }


def baseline_child():
    return child_node() | {
        "id": "4" * 24,
        "treeid": BASE_TREE,
        "parent": BASE_ID,
        "path": ["checkout", "kbuild-gcc-12-x86"],
    }


def patchwork_fixture():
    pw = Patchwork()
    series = {
        "id": 42,
        "name": "Example text change",
        "version": 2,
        "total": 1,
        "received_total": 1,
        "received_all": True,
        "date": "2026-09-29T00:00:00",
        "project": {"link_name": "example"},
        "patches": [{"id": 99}],
        "web_url": "https://patchwork.example.test/series/42/",
    }
    item = {
        "id": 99,
        "name": "[v2] Example text change",
        "diff": DIFF,
        "series": [{"id": 42}],
        "web_url": "https://patchwork.example.test/patch/99/",
    }
    pw.get = Mock(
        side_effect=lambda resource, ident: deepcopy(
            series if resource == "series" else item
        )
    )
    return pw, series, item


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.block_http = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected HTTP request"),
        )
        self.block_http.start()
        self.addCleanup(self.block_http.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "run"
        self.client = Mock()
        self.client.get_node.return_value = base_node()
        self.client.trigger_patchset.return_value = {
            "message": "OK",
            "node": patchset_node(),
        }
        self.pw, self.series, self.item = patchwork_fixture()

    def prepared(self, **overrides):
        kwargs = dict(
            series_id=42,
            checkout_id=BASE_ID,
            job_filter=["kbuild-gcc-12-x86"],
            api_url=API,
            pipeline_url=PIPELINE,
            out=self.directory,
            patchwork=self.pw,
        )
        kwargs.update(overrides)
        return prepare(self.client, **kwargs)

    def submitted(self):
        self.prepared()
        submit(self.client, self.directory, token="test-token")

    def observed(self, root=None, children=None, **kwargs):
        root = root or patchset_node("done", "pass")
        children = [child_node()] if children is None else children
        self.client.get_node.side_effect = lambda node_id, **opts: deepcopy(
            base_node() if node_id == BASE_ID else root
        )
        nodes = [root] + children
        self.client.get_nodes.side_effect = lambda **opts: deepcopy(
            (nodes if opts["filters"] == [f"treeid={TREE}"] else [baseline_child()])[
                opts["offset"] : opts["offset"] + opts["limit"]
            ]
        )
        return collect(self.client, self.directory, **kwargs)

    def test_preparation_is_read_only_and_freezes_exact_bytes(self):
        manifest = self.prepared()
        _, state, patches = load_run(self.directory)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(patches, [DIFF])
        self.assertEqual(manifest["checkout"]["revision"]["commit"], "c" * 40)
        self.client.trigger_patchset.assert_not_called()
        self.assertEqual(collect(self.client, self.directory)["status"], "PREPARED")

    def test_series_order_is_preserved_even_when_ids_descend(self):
        self.series.update(total=2, received_total=2, patches=[{"id": 100}, {"id": 99}])
        self.pw.get.side_effect = lambda resource, ident: deepcopy(
            self.series
            if resource == "series"
            else self.item | {"id": ident, "name": f"[v2,{101-ident}/2] Change"}
        )
        manifest = self.prepared()
        self.assertEqual([p["id"] for p in manifest["patches"]], [100, 99])

    def test_patch_numbers_override_response_order(self):
        self.series.update(total=2, received_total=2, patches=[{"id": 99}, {"id": 100}])
        self.pw.get.side_effect = lambda resource, ident: deepcopy(
            self.series
            if resource == "series"
            else self.item | {"id": ident, "name": f"[PATCH v2 0{101-ident}/02] Change"}
        )
        manifest = self.prepared()
        self.assertEqual([p["id"] for p in manifest["patches"]], [100, 99])

    def test_ambiguous_patch_order_is_rejected(self):
        for name in (
            "unnumbered",
            "[0/2] cover",
            "[1/3] wrong total",
            "[1/2][2/2] ambiguous",
        ):
            with self.subTest(name=name), self.assertRaises(WorkflowError):
                patch_position({"name": name}, 2)
        self.series.update(total=2, received_total=2, patches=[{"id": 99}, {"id": 100}])
        self.pw.get.side_effect = lambda resource, ident: deepcopy(
            self.series
            if resource == "series"
            else self.item | {"id": ident, "name": "[1/2] duplicate"}
        )
        with self.assertRaisesRegex(WorkflowError, "sequence"):
            self.prepared()

    def test_incomplete_duplicate_and_foreign_patches_are_rejected(self):
        for changes in (
            {"received_all": False},
            {"received_total": 0},
            {"total": 2, "received_total": 2, "patches": [{"id": 99}, {"id": 99}]},
        ):
            with self.subTest(changes=changes):
                _, series, _ = patchwork_fixture()
                self.series.clear()
                self.series.update(series | changes)
                with self.assertRaises(WorkflowError):
                    self.prepared()
        _, series, _ = patchwork_fixture()
        self.series.clear()
        self.series.update(series)
        self.item["series"] = [{"id": 123}]
        with self.assertRaises(WorkflowError):
            self.prepared()

    def test_series_change_during_fetch_is_rejected(self):
        self.pw.get.side_effect = [self.series, self.item, self.series | {"version": 3}]
        with self.assertRaisesRegex(WorkflowError, "changed"):
            self.prepared()

    def test_binary_and_empty_diffs_are_rejected(self):
        for diff in ("", DIFF + "GIT binary patch\n", DIFF + "\x00"):
            self.item["diff"] = diff
            with self.subTest(diff=diff), self.assertRaises(WorkflowError):
                self.prepared()

    def test_jobs_and_successful_checkout_are_required(self):
        with self.assertRaises(WorkflowError):
            self.prepared(job_filter=[])
        self.client.get_node.return_value["state"] = "running"
        with self.assertRaises(WorkflowError):
            self.prepared()
        self.client.trigger_patchset.assert_not_called()

    def test_real_library_payload_and_durable_attempt_before_post(self):
        self.prepared()
        response = Mock(status_code=200)
        response.json.return_value = {"message": "OK", "node": patchset_node()}
        get_response = Mock()
        get_response.json.return_value = base_node()

        def post(url, **kwargs):
            self.assertEqual(
                read_json(self.directory / "state.json")["status"], "submitting"
            )
            self.assertEqual(url, PIPELINE + "/api/patchset")
            self.assertEqual(kwargs["headers"]["Authorization"], "private-test-token")
            self.assertEqual(
                json.loads(kwargs["data"]),
                {
                    "nodeid": BASE_ID,
                    "patch": [DIFF],
                    "jobfilter": ["kbuild-gcc-12-x86"],
                },
            )
            return response

        with (
            patch(
                "kcidev.libs.maestro_common.kcidev_session.get",
                return_value=get_response,
            ),
            patch(
                "kcidev.libs.maestro_common.kcidev_session.post", side_effect=post
            ) as mocked_post,
            redirect_stdout(StringIO()),
        ):
            result = submit(
                KernelCIClient(), self.directory, token="private-test-token"
            )
        self.assertEqual(mocked_post.call_count, 1)
        self.assertEqual(result["treeid"], TREE)
        self.assertNotIn(
            "private-test-token", (self.directory / "state.json").read_text()
        )

    def test_repeated_submit_is_blocked(self):
        self.submitted()
        with self.assertRaises(WorkflowError):
            submit(self.client, self.directory, token="test")
        self.assertEqual(self.client.trigger_patchset.call_count, 1)

    def test_changed_inherited_platforms_are_rejected_before_post(self):
        self.prepared()
        self.client.get_node.return_value["platform_filter"] = ["unexpected-board"]
        with self.assertRaises(WorkflowError):
            submit(self.client, self.directory, token="test")
        self.client.trigger_patchset.assert_not_called()

    def test_explicit_platform_is_forwarded(self):
        self.prepared(platform_filter=["qemu-x86_64"])
        self.client.trigger_patchset.return_value["node"]["platform_filter"] = [
            "qemu-x86_64"
        ]
        submit(self.client, self.directory, token="test")
        self.assertEqual(
            self.client.trigger_patchset.call_args.kwargs["platform_filter"],
            ["qemu-x86_64"],
        )

    def test_uncertain_submit_never_retries(self):
        self.prepared()
        self.client.trigger_patchset.side_effect = TimeoutError("lost response")
        for _ in range(2):
            with self.assertRaises(WorkflowError):
                submit(self.client, self.directory, token="test")
        self.assertEqual(self.client.trigger_patchset.call_count, 1)
        self.assertEqual(
            collect(self.client, self.directory)["status"], "SUBMISSION_UNKNOWN"
        )

    def test_interrupt_keeps_durable_submitting_state(self):
        self.prepared()
        self.client.trigger_patchset.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            submit(self.client, self.directory, token="test")
        self.assertEqual(
            read_json(self.directory / "state.json")["status"], "submitting"
        )
        with self.assertRaises(WorkflowError):
            submit(self.client, self.directory, token="test")

    def test_invalid_response_cannot_become_a_submitted_run(self):
        self.prepared()
        self.client.trigger_patchset.return_value["node"]["parent"] = "f" * 24
        with self.assertRaises(WorkflowError):
            submit(self.client, self.directory, token="test")
        self.assertEqual(
            read_json(self.directory / "state.json")["status"], "submission_unknown"
        )

    def test_changed_patch_and_manifest_are_rejected_before_post(self):
        manifest = self.prepared()
        path = self.directory / manifest["patches"][0]["file"]
        path.write_text(DIFF + "extra\n")
        with self.assertRaises(WorkflowError):
            submit(self.client, self.directory, token="test")
        path.write_text(DIFF)
        manifest["job_filter"] = ["unexpected-job"]
        write_json(self.directory / "manifest.json", manifest)
        with self.assertRaises(WorkflowError):
            submit(self.client, self.directory, token="test")
        self.client.trigger_patchset.assert_not_called()

    def test_concurrent_run_lock_blocks_submission(self):
        self.prepared()
        with locked(self.directory):
            with self.assertRaises(WorkflowError):
                submit(self.client, self.directory, token="test")
        self.client.trigger_patchset.assert_not_called()

    def test_pagination_exact_tree_and_observed_pass(self):
        self.submitted()
        report = self.observed(page_size=1)
        self.assertEqual(report["status"], "NO_FAILURES_IN_OBSERVED_RESULTS")
        self.assertTrue(report["terminal"])
        self.assertFalse(report["approved"])
        self.assertEqual(report["required_coverage"], "not_assessed")
        self.assertEqual(self.client.get_nodes.call_count, 5)
        for call in self.client.get_nodes.call_args_list:
            self.assertIn(
                call.kwargs["filters"],
                (
                    [f"treeid={TREE}"],
                    [f"treeid={BASE_TREE}", "path=kbuild-gcc-12-x86"],
                ),
            )
            self.assertEqual(call.kwargs["api_url"], API)
        self.client.compare_results.assert_not_called()

    def test_available_root_does_not_imply_finished_pipeline(self):
        self.submitted()
        report = self.observed(root=patchset_node("available", "pass"))
        self.assertEqual(report["status"], "RUNNING")
        self.assertFalse(report["terminal"])

    def test_patch_application_and_test_failures_are_distinct(self):
        self.submitted()
        report = self.observed(root=patchset_node("done", "fail"), children=[])
        self.assertEqual(report["status"], "PATCH_APPLICATION_FAILED")
        report = self.observed(children=[child_node(result="fail")])
        self.assertEqual(report["status"], "REGRESSIONS_DETECTED")

    def test_empty_results_no_children_incomplete_and_skipped_are_not_passes(self):
        self.submitted()
        for children in (
            [],
            [child_node(result="incomplete")],
            [child_node(result="skip")],
        ):
            with self.subTest(children=children):
                self.assertEqual(
                    self.observed(children=children)["status"], "EVIDENCE_INCOMPLETE"
                )
        self.client.get_nodes.side_effect = None
        self.client.get_nodes.return_value = []
        self.assertEqual(
            collect(self.client, self.directory)["status"], "EVIDENCE_INCOMPLETE"
        )

    def test_partial_pages_preserve_evidence_and_never_pass(self):
        self.submitted()
        root = patchset_node("done", "pass")
        self.client.get_node.return_value = root
        self.client.get_nodes.side_effect = [
            [root],
            TimeoutError("temporary read failure"),
        ]
        report = collect(self.client, self.directory, page_size=1)
        self.assertEqual(report["status"], "EVIDENCE_INCOMPLETE")
        self.assertEqual(len(report["nodes"]), 1)
        self.assertFalse(report["listing_complete"])

    def test_node_limit_wrong_tree_duplicate_and_orphans_are_incomplete(self):
        self.submitted()
        self.assertEqual(self.observed(max_nodes=1)["status"], "EVIDENCE_INCOMPLETE")
        for child in (
            child_node() | {"treeid": BASE_TREE},
            child_node() | {"parent": "f" * 24},
            patchset_node("done", "pass"),
        ):
            with self.subTest(child=child):
                self.assertEqual(
                    self.observed(children=[child])["status"], "EVIDENCE_INCOMPLETE"
                )

    def test_missing_or_wrong_patch_hash_is_incomplete(self):
        self.submitted()
        for value in (None, "f" * 64):
            root = patchset_node("done", "pass")
            root["data"]["kernel_revision"]["patchset"] = value
            self.assertEqual(self.observed(root=root)["status"], "EVIDENCE_INCOMPLETE")

    def test_operator_reconciliation_requires_matching_hash(self):
        self.prepared()
        self.client.trigger_patchset.side_effect = TimeoutError
        with self.assertRaises(WorkflowError):
            submit(self.client, self.directory, token="test")
        self.client.get_node.return_value = patchset_node()
        with self.assertRaises(WorkflowError):
            reconcile(self.client, self.directory, node_id=ROOT_ID)
        self.client.get_node.return_value = patchset_node("done", "pass")
        self.assertEqual(
            reconcile(self.client, self.directory, node_id=ROOT_ID)["treeid"], TREE
        )
        self.assertEqual(self.client.trigger_patchset.call_count, 1)

    def test_html_escapes_remote_data_and_rejects_script_links(self):
        self.submitted()
        child = child_node()
        child["name"] = '<script>alert("bad")</script>'
        child["artifacts"] = {"log": "javascript:alert(1)"}
        report = self.observed(children=[child])
        html = render(report)
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn("&lt;script&gt;", html)
        write_report(self.directory, report)
        self.assertEqual(
            read_json(self.directory / "report.json")["status"], report["status"]
        )

    def test_cli_json_is_clean_and_status_never_submits(self):
        self.submitted()
        self.observed()
        get_nodes = self.client.get_nodes.side_effect

        def noisy(**kwargs):
            print("Library diagnostic")
            return get_nodes(**kwargs)

        self.client.get_nodes.side_effect = noisy
        out, err = StringIO(), StringIO()
        with (
            patch("kci_patchwork.__main__.KernelCIClient", return_value=self.client),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(["status", "--run", str(self.directory)])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out.getvalue())["status"], "NO_FAILURES_IN_OBSERVED_RESULTS"
        )
        self.assertIn("Library diagnostic", err.getvalue())
        self.assertEqual(self.client.trigger_patchset.call_count, 1)


if __name__ == "__main__":
    unittest.main()
