"""CLI orchestration. KernelCI operations go through KernelCIClient directly."""

import argparse
import json
import os
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from kcidev import KernelCIClient

from .patchwork import Patchwork
from .report import write_report
from .results import collect
from .storage import WorkflowError, endpoint, locked
from .workflow import load_run, prepare, reconcile, submit


def bounded(low, high):
    def convert(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"Must be {low}..{high}")
        return number

    return convert


def parser():
    root = argparse.ArgumentParser(
        description="Test a Patchwork URL using the kci-dev Python API",
        epilog="Shortcut: kci-patchwork URL [run options]. Add --submit to start jobs and wait for results.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    remote = argparse.ArgumentParser(add_help=False)
    remote.add_argument("--config", type=Path, help="Explicit kci-dev TOML file")
    remote.add_argument("--instance", help="Profile in --config")
    selection = argparse.ArgumentParser(add_help=False)
    selection.add_argument(
        "source", nargs="?", help="Patchwork URL or numeric series ID"
    )
    selection.add_argument(
        "--series", type=bounded(1, 10**12), help="Legacy numeric series ID"
    )
    selection.add_argument(
        "--patchwork-server", help="Patchwork website base URL for numeric IDs"
    )
    selection.add_argument("--patchwork-api", help="Explicit Patchwork API endpoint")
    commands.add_parser(
        "series",
        parents=[remote, selection],
        help="Resolve a URL/ID and inspect the complete ordered series",
    )
    checkouts = commands.add_parser(
        "checkouts",
        parents=[remote],
        help="List successful base checkouts (oldest first)",
    )
    checkouts.add_argument("--api-url")
    checkouts.add_argument(
        "--since",
        default=(datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat(),
    )
    checkouts.add_argument("--giturl")
    checkouts.add_argument("--branch")
    checkouts.add_argument("--limit", type=bounded(1, 100), default=20)
    for command in ("prepare", "run"):
        build = commands.add_parser(
            command,
            parents=[remote, selection],
            help=(
                "Resolve and prepare a URL; --submit also submits and watches"
                if command == "run"
                else "Freeze inputs and create a read-only preview"
            ),
        )
        build.add_argument("--api-url")
        build.add_argument("--pipeline-url")
        build.add_argument(
            "--checkout", required=True, help="Existing successful checkout node ID"
        )
        build.add_argument(
            "--job",
            action="append",
            required=True,
            help="Explicit job filter; repeat for more",
        )
        build.add_argument(
            "--platform",
            action="append",
            help="Platform filter; repeat for more (default: inherit checkout)",
        )
        build.add_argument(
            "--out",
            type=Path,
            required=command == "prepare",
            help="New run directory (run default: runs/SERVER/series-ID)",
        )
        if command == "run":
            build.add_argument(
                "--submit",
                action="store_true",
                help="Start KernelCI jobs once, then watch and compare results",
            )
            build.add_argument("--token-env", default="KCI_PIPELINE_TOKEN")
            monitoring_options(build, watch=True)
    send = commands.add_parser(
        "submit",
        parents=[remote],
        help="Submit the frozen patches once; starts KernelCI jobs",
    )
    send.add_argument("--run", type=Path, required=True)
    send.add_argument(
        "--token-env",
        default="KCI_PIPELINE_TOKEN",
        help="Environment variable containing pipeline token",
    )
    attach = commands.add_parser(
        "reconcile", help="Attach an operator-identified node after a lost response"
    )
    attach.add_argument("--run", type=Path, required=True)
    attach.add_argument("--node-id", required=True)
    for command in ("status", "watch"):
        monitor = commands.add_parser(
            command, help="Collect the submitted tree and refresh HTML/JSON reports"
        )
        monitor.add_argument("--run", type=Path, required=True)
        monitoring_options(monitor, watch=command == "watch")
    return root


def monitoring_options(command, *, watch):
    command.add_argument("--max-nodes", type=bounded(1, 100000), default=5000)
    command.add_argument("--page-size", type=bounded(1, 1000), default=200)
    if watch:
        command.add_argument("--interval", type=bounded(5, 60), default=30)
        command.add_argument(
            "--timeout",
            type=bounded(1, 86400),
            default=900,
            help="Polling budget in seconds, checked between HTTP snapshots",
        )


def configuration(args):
    cfg = {}
    if args.config:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with args.config.expanduser().open("rb") as stream:
            cfg = tomllib.load(stream)
    return cfg


def profile(args, cfg=None):
    cfg = configuration(args) if cfg is None else cfg
    selected = args.instance or cfg.get("default_instance")
    if selected is not None and not isinstance(cfg.get(selected), dict):
        raise WorkflowError(f"Instance {selected!r} is absent from --config")
    return cfg.get(selected, {})


def patchwork_source(args, cfg):
    defaults = cfg.get("patchwork", {})
    if not isinstance(defaults, dict):
        raise WorkflowError("[patchwork] must be a TOML table")
    # A full input URL selects its own server, even when a default is configured.
    if args.source and "://" in args.source:
        defaults = {}
    return Patchwork.from_source(
        args.source,
        series_id=args.series,
        server_url=args.patchwork_server
        or (defaults.get("server") if not args.patchwork_api else None),
        api_url=args.patchwork_api
        or (defaults.get("api") if not args.patchwork_server else None),
    )


def submission_token(args, selected, endpoints):
    for key in ("api", "pipeline"):
        if selected.get(key) and endpoint(selected[key]) != endpoints[key]:
            raise WorkflowError("Token config endpoints differ from the prepared run")
    token = os.environ.get(args.token_env) or selected.get("token")
    if not isinstance(token, str) or not token.strip():
        raise WorkflowError("Set KCI_PIPELINE_TOKEN or configure an instance token")
    return token


def emit(result):
    print(json.dumps(result, indent=2, ensure_ascii=False))


def report_summary(report, directory):
    comparison = report.get("comparison", {})
    return {
        key: report.get(key)
        for key in ("status", "exit_code", "terminal", "counts", "errors")
    } | {
        "comparison": {
            key: comparison[key]
            for key in (
                "performed",
                "complete",
                "baseline_checkout_id",
                "baseline_treeid",
                "counts",
                "errors",
                "reason",
            )
            if key in comparison
        },
        "report_html": str(directory.resolve() / "report.html"),
        "report_json": str(directory.resolve() / "report.json"),
    }


def monitor_run(client, args, directory, *, watch):
    deadline = time.monotonic() + getattr(args, "timeout", 0)
    while True:
        with redirect_stdout(sys.stderr), locked(directory):
            report = collect(
                client, directory, max_nodes=args.max_nodes, page_size=args.page_size
            )
            write_report(directory, report)
        if (
            not watch
            or report["terminal"]
            or report["status"] in ("PREPARED", "SUBMISSION_UNKNOWN")
            or time.monotonic() >= deadline
        ):
            if watch and not report["terminal"] and report["status"] == "RUNNING":
                report["watch_timed_out"] = True
                with locked(directory):
                    write_report(directory, report)
            emit(report_summary(report, directory))
            return report["exit_code"]
        print(
            f"{report['status']}: {report.get('counts', {}).get('total', 0)} nodes",
            file=sys.stderr,
        )
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0].startswith(("https://", "http://")):
        arguments.insert(0, "run")
    args = parser().parse_args(arguments)
    try:
        if args.command == "series":
            with redirect_stdout(sys.stderr):
                source, series_id = patchwork_source(args, configuration(args))
                series, patches = source.fetch_series(series_id)
            emit(
                {
                    "series": series,
                    "patches": [
                        {k: v for k, v in p.items() if k != "diff"} for p in patches
                    ],
                }
            )
            return 0
        client = KernelCIClient()
        if args.command in ("checkouts", "prepare", "run"):
            cfg = configuration(args)
            selected = profile(args, cfg)
            api = endpoint(args.api_url or selected.get("api"))
            if args.command == "checkouts":
                datetime.fromisoformat(args.since)  # Reject malformed time windows.
                filters = [
                    "name=checkout",
                    "kind=checkout",
                    "state=done",
                    "result=pass",
                    f"created__gt={args.since}",
                ]
                if args.giturl:
                    filters.append(f"data.kernel_revision.url={args.giturl}")
                if args.branch:
                    filters.append(f"data.kernel_revision.branch={args.branch}")
                with redirect_stdout(sys.stderr):
                    nodes = client.get_nodes(
                        filters=filters, limit=args.limit, api_url=api
                    )
                if not isinstance(nodes, list):
                    raise WorkflowError("Unexpected checkout response")
                emit(
                    [
                        {
                            key: node.get(key)
                            for key in ("id", "created", "state", "result", "treeid")
                        }
                        | {"revision": (node.get("data") or {}).get("kernel_revision")}
                        for node in nodes
                    ]
                )
                return 0
            pipeline = endpoint(args.pipeline_url or selected.get("pipeline"))
            start = args.command == "run" and args.submit
            token = (
                submission_token(args, selected, {"api": api, "pipeline": pipeline})
                if start
                else None
            )
            if args.out and args.out.exists():
                raise WorkflowError(
                    "Output directory already exists; use status or watch for that run"
                )
            with redirect_stdout(sys.stderr):
                source, series_id = patchwork_source(args, cfg)
                directory = (
                    args.out
                    or Path("runs")
                    / urlsplit(source.api_url).netloc
                    / f"series-{series_id}"
                )
                prepare(
                    client,
                    series_id=series_id,
                    checkout_id=args.checkout,
                    job_filter=args.job,
                    platform_filter=args.platform,
                    api_url=api,
                    pipeline_url=pipeline,
                    out=directory,
                    patchwork=source,
                )
                report = collect(client, directory)
                write_report(directory, report)
                if start:
                    try:
                        submit(client, directory, token=token)
                    except (Exception, KeyboardInterrupt):
                        write_report(directory, collect(client, directory))
                        raise
            if start:
                return monitor_run(client, args, directory, watch=True)
            emit(report_summary(report, directory))
            return report["exit_code"]
        if args.command == "submit":
            selected = profile(args)
            manifest, _, _ = load_run(args.run)
            token = submission_token(args, selected, manifest["endpoints"])
            with redirect_stdout(sys.stderr):
                result = submit(client, args.run, token=token)
            emit(result)
            return 0
        if args.command == "reconcile":
            with redirect_stdout(sys.stderr):
                result = reconcile(client, args.run, node_id=args.node_id)
            emit(result)
            return 0
        return monitor_run(client, args, args.run, watch=args.command == "watch")
    except KeyboardInterrupt:
        print(
            "Interrupted. The run record is retained; use status before any further action.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
