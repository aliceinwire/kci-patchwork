"""Collect exact-tree observations without using commit-keyed dashboard results."""

from collections import Counter

from .comparison import compare
from .storage import WorkflowError, now
from .workflow import check_patchset, checked_id, load_run

EXIT_CODES = {
    "PREPARED": 0,
    "RUNNING": 3,
    "SUBMISSION_UNKNOWN": 2,
    "EVIDENCE_INCOMPLETE": 2,
    "PATCH_APPLICATION_FAILED": 1,
    "REVIEW_REQUIRED": 1,
    "NO_FAILURES_IN_OBSERVED_RESULTS": 0,
    "NO_REGRESSIONS_IN_OBSERVED_RESULTS": 0,
    "REGRESSIONS_DETECTED": 1,
}


def base_report(manifest, state):
    return {
        "schema_version": 1,
        "generated_at": now(),
        "selection": manifest,
        "submission": state,
        "nodes": [],
        "errors": [],
        "counts": {},
        "listing_complete": False,
        "terminal": False,
        "approved": False,
        "required_coverage": "not_assessed",
        "comparison": {
            "performed": False,
            "complete": False,
            "reason": "Comparison starts after the patched tree has complete terminal results.",
        },
    }


def classify(report, root):
    nodes, errors = report["nodes"], report["errors"]
    children = [n for n in nodes if n["id"] != root.get("id")]
    report["counts"] = {
        "total": len(nodes),
        "descendants": len(children),
        "states": dict(Counter(n.get("state", "unknown") for n in nodes)),
        "results": dict(Counter(n.get("result") or "unset" for n in nodes)),
    }
    report["patch_application"] = {
        "state": root.get("state"),
        "result": root.get("result"),
        "pipeline_patchset_hash": (root.get("data") or {})
        .get("kernel_revision", {})
        .get("patchset"),
    }
    ids = {n["id"]: n for n in nodes}
    for child in children:
        cursor, visited = child, set()
        while cursor["id"] != root.get("id"):
            if cursor["id"] in visited or cursor.get("parent") not in ids:
                errors.append(
                    f"Node {child['id']} has an incomplete or cyclic parent chain"
                )
                break
            visited.add(cursor["id"])
            cursor = ids[cursor["parent"]]
    report["terminal"] = (
        root.get("state") == "done"
        and bool(nodes)
        and all(n.get("state") == "done" for n in nodes)
        and report["listing_complete"]
        and not errors
    )
    failures = [n["id"] for n in nodes if n.get("result") == "fail"]
    report["failing_node_ids"] = failures
    if errors or not report["listing_complete"]:
        status = "EVIDENCE_INCOMPLETE"
    elif root.get("state") == "done" and root.get("result") == "fail":
        status = "PATCH_APPLICATION_FAILED"
    elif any(
        n.get("state") not in ("running", "available", "closing", "done") for n in nodes
    ):
        errors.append("One or more nodes have an unknown state")
        status = "EVIDENCE_INCOMPLETE"
    elif not report["terminal"]:
        status = "RUNNING"
    elif (
        root.get("result") != "pass"
        or not children
        or any(n.get("result") not in ("pass", "fail", "skip") for n in children)
        or not any(n.get("result") in ("pass", "fail") for n in children)
    ):
        errors.append("No complete set of observed build/test outcomes is available")
        status = "EVIDENCE_INCOMPLETE"
    elif failures:
        status = "REVIEW_REQUIRED"
    else:
        status = "NO_FAILURES_IN_OBSERVED_RESULTS"
    report["status"], report["exit_code"] = status, EXIT_CODES[status]
    return report


def collect(client, directory, *, page_size=200, max_nodes=5000):
    if not 1 <= page_size <= 1000 or not 1 <= max_nodes <= 100000:
        raise WorkflowError("Invalid pagination bounds")
    manifest, state, _ = load_run(directory)
    report = base_report(manifest, state)
    if state["status"] != "submitted":
        status = "PREPARED" if state["status"] == "prepared" else "SUBMISSION_UNKNOWN"
        report.update(status=status, exit_code=EXIT_CODES[status])
        return report
    api = manifest["endpoints"]["api"]
    node_id, treeid = checked_id(state["patchset_node_id"]), checked_id(
        state["treeid"], 64
    )
    try:
        root = client.get_node(node_id, api_url=api)
        check_patchset(root, manifest, node_id=node_id, treeid=treeid)
    except Exception as exc:
        report["errors"].append(
            f"Patchset identity/read failed: {type(exc).__name__}: {exc}"
        )
        report.update(status="EVIDENCE_INCOMPLETE", exit_code=2)
        return report
    seen, offset = set(), 0
    while offset < max_nodes:
        limit = min(page_size, max_nodes - offset)
        try:
            page = client.get_nodes(
                limit=limit, offset=offset, filters=[f"treeid={treeid}"], api_url=api
            )
            if not isinstance(page, list) or len(page) > limit:
                raise WorkflowError("Unexpected nodes response or ignored pagination")
            for node in page:
                if not isinstance(node, dict) or node.get("treeid") != treeid:
                    raise WorkflowError(
                        "Server returned a node outside the submitted treeid"
                    )
                current_id = checked_id(node.get("id"))
                if current_id in seen:
                    raise WorkflowError(
                        "Repeated node across result pages; retry the snapshot"
                    )
                seen.add(current_id)
                report["nodes"].append(node)
            offset += len(page)
            if len(page) < limit:
                report["listing_complete"] = True
                break
        except Exception as exc:
            report["errors"].append(f"Node listing failed: {type(exc).__name__}: {exc}")
            break
    if offset >= max_nodes and not report["listing_complete"]:
        report["errors"].append(
            "Node limit reached; increase --max-nodes and collect again"
        )
    if node_id not in seen:
        report["errors"].append(
            "The submitted patchset node is absent from its tree listing"
        )
    else:
        # Use the root as observed in the listing, not the earlier preflight read.
        root = next(n for n in report["nodes"] if n["id"] == node_id)
        try:
            check_patchset(
                root,
                manifest,
                node_id=node_id,
                treeid=treeid,
                require_hash=root.get("result") == "pass",
            )
        except Exception as exc:
            report["errors"].append(f"Patchset verification failed: {exc}")
    classify(report, root)
    if report["status"] in ("REVIEW_REQUIRED", "NO_FAILURES_IN_OBSERVED_RESULTS"):
        comparison = compare(
            client,
            manifest,
            root,
            report["nodes"],
            page_size=page_size,
            max_nodes=max_nodes,
        )
        report["comparison"] = comparison
        counts = comparison["counts"]
        if counts["regression"]:
            status = "REGRESSIONS_DETECTED"
        elif counts["new_failure"]:
            status = "REVIEW_REQUIRED"
        elif not comparison["complete"]:
            status = "EVIDENCE_INCOMPLETE"
        elif counts["existing_failure"]:
            status = "NO_REGRESSIONS_IN_OBSERVED_RESULTS"
        else:
            status = "NO_FAILURES_IN_OBSERVED_RESULTS"
        report["status"], report["exit_code"] = status, EXIT_CODES[status]
    return report
