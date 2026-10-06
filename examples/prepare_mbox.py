#!/usr/bin/env python3
"""Verify a Patchwork series mbox, optionally prepare a KernelCI run (GET only)."""

import argparse
import json
import mailbox
import re
import sys
import tempfile
from contextlib import closing, redirect_stdout
from pathlib import Path

import requests
from kcidev import KernelCIClient

from kci_patchwork.patchwork import MAX_SERIES_BYTES, Patchwork, validate_diff
from kci_patchwork.report import write_report
from kci_patchwork.results import collect
from kci_patchwork.storage import WorkflowError, digest
from kci_patchwork.workflow import prepare


def mbox_patches(raw, patches):
    """Keep API ordering, but obtain the exact verified diff bytes from the mbox."""
    expected = {patch["id"]: patch for patch in patches}
    found = {}
    # Parse a snapshot so inspection, preparation and the saved mbox agree.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "series.mbox"
        path.write_bytes(raw)
        with closing(mailbox.mbox(path, create=False)) as messages:
            if len(messages) != len(patches):
                raise WorkflowError("Mbox must contain exactly the complete series")
            for message in messages:
                ids = message.get_all("X-Patchwork-Id", [])
                if len(ids) != 1 or not re.fullmatch(r"[0-9]+", ids[0].strip()):
                    raise WorkflowError("Expected a Patchwork-exported mbox")
                patch_id = int(ids[0])
                if patch_id not in expected or patch_id in found:
                    raise WorkflowError("Mbox contains an unrelated or duplicate patch")
                patch = expected[patch_id]
                msgids = message.get_all("Message-ID", [])
                if len(msgids) != 1 or msgids[0].strip() != patch["msgid"]:
                    raise WorkflowError("Mbox Message-ID differs from Patchwork")
                if message.is_multipart() or message.get_content_type() != "text/plain":
                    raise WorkflowError("Use Patchwork's plain-text series mbox export")
                body = message.get_payload(decode=True).decode(
                    message.get_content_charset() or "utf-8"
                )
                start = re.search(r"^diff --git |^--- [^\n]+\n\+\+\+ ", body, re.M)
                if start is None:
                    raise WorkflowError("Mbox message has no unified diff")
                candidate = body[start.start() :]
                diff = candidate[: len(patch["diff"])]
                # Patchwork adds a blank separator after the exported diff.
                # Do not use patch-id or strip whitespace inside a hunk.
                if diff != patch["diff"] or candidate[len(diff) :].strip("\n"):
                    raise WorkflowError(
                        f"Mbox diff for patch {patch_id} differs from API"
                    )
                frozen = validate_diff(diff)
                found[patch_id] = patch | {
                    "diff": diff,
                    "bytes": len(frozen),
                    "sha256": digest(frozen),
                }
    return [found[patch["id"]] for patch in patches]


class MboxPatchwork(Patchwork):
    """Example adapter for prepare()'s existing patchwork argument."""

    def __init__(self, path, api_url="https://patchwork.kernel.org/api/1.2"):
        super().__init__(api_url)
        with Path(path).open("rb") as stream:
            self.raw = stream.read(MAX_SERIES_BYTES + 1)
        if not self.raw or len(self.raw) > MAX_SERIES_BYTES:
            raise WorkflowError("Mbox must be nonempty and no larger than 32 MiB")

    def fetch_series(self, series_id):
        series, patches = super().fetch_series(series_id)
        patches = mbox_patches(self.raw, patches)
        series["input_format"] = "patchwork-mbox"
        series["mbox_sha256"] = digest(self.raw)
        return series, patches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", type=int, required=True)
    parser.add_argument("--mbox", type=Path, required=True)
    parser.add_argument(
        "--patchwork-api", default="https://patchwork.kernel.org/api/1.2"
    )
    parser.add_argument(
        "--checkout", help="Optional existing KernelCI checkout node ID"
    )
    parser.add_argument("--api-url")
    parser.add_argument("--pipeline-url")
    parser.add_argument("--job", action="append")
    parser.add_argument("--platform", action="append")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    preparation = (args.api_url, args.pipeline_url, args.job, args.out)
    if args.checkout:
        if not all(preparation):
            parser.error(
                "--checkout also needs --api-url, --pipeline-url, --job and --out"
            )
    elif any(preparation) or args.platform:
        parser.error("Preparation options require --checkout")
    try:
        source = MboxPatchwork(args.mbox, args.patchwork_api)
        if not args.checkout:
            series, patches = source.fetch_series(args.series)
            result = {
                "status": "MBOX_VERIFIED",
                "series": series,
                "patches": [
                    {k: v for k, v in p.items() if k != "diff"} for p in patches
                ],
            }
        else:
            with redirect_stdout(sys.stderr):
                client = KernelCIClient()
                prepare(
                    client,
                    series_id=args.series,
                    checkout_id=args.checkout,
                    job_filter=args.job,
                    platform_filter=args.platform,
                    api_url=args.api_url,
                    pipeline_url=args.pipeline_url,
                    patchwork=source,
                    out=args.out,
                )
                (args.out / "series.mbox").write_bytes(source.raw)
                report = collect(client, args.out)
                write_report(args.out, report)
            result = {
                "status": report["status"],
                "report_html": str((args.out / "report.html").resolve()),
                "report_json": str((args.out / "report.json").resolve()),
            }
        print(json.dumps(result, indent=2))
        return 0
    except (
        WorkflowError,
        OSError,
        ValueError,
        LookupError,
        requests.RequestException,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
