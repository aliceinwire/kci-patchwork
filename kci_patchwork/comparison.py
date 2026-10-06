"""Compare observed job subtrees with the frozen, unpatched checkout."""

import json
from collections import Counter, defaultdict

from .storage import WorkflowError
from .workflow import checked_id, checkout_snapshot

# Include stable configuration, not run-specific job IDs or timestamps.
CONTEXT_FIELDS = (
    "arch",
    "defconfig",
    "config_full",
    "compiler",
    "fragments",
    "platform",
    "device",
    "runtime",
    "kernel_type",
    "test_source",
    "test_revision",
)
CATEGORIES = (
    "regression",
    "existing_failure",
    "fixed",
    "unchanged_pass",
    "unchanged_skip",
    "new_failure",
    "new_result",
    "missing",
    "incomparable",
    "ambiguous",
    "incomplete",
)


def index_nodes(nodes, root, revision, patchset=None):
    """Reconstruct relative paths from parents, inheriting execution context."""
    by_id = {node["id"]: node for node in nodes}
    by_id[root["id"]] = root
    paths, active = {}, set()

    def visit(node_id):
        if node_id == root["id"]:
            return (), {}, []
        if node_id in paths:
            return paths[node_id]
        if node_id in active or node_id not in by_id:
            raise WorkflowError("Comparison has an incomplete or cyclic parent chain")
        active.add(node_id)
        node = by_id[node_id]
        key, inherited, names = visit(node.get("parent"))
        if not node.get("name") or not node.get("kind"):
            raise WorkflowError("Comparison node has no name or kind")
        data = node.get("data") or {}
        observed = data.get("kernel_revision") or {}
        if any(observed.get(k, v) != v for k, v in revision.items()) or (
            observed.get("patchset") and observed["patchset"] != patchset
        ):
            raise WorkflowError("Comparison descendant has a different kernel revision")
        context = inherited | {
            field: data[field]
            for field in CONTEXT_FIELDS
            if data.get(field) is not None
        }
        signature = json.dumps(
            [node["kind"], node["name"], context], sort_keys=True, separators=(",", ":")
        )
        paths[node_id] = (key + (signature,), context, names + [node["name"]])
        active.remove(node_id)
        return paths[node_id]

    grouped = defaultdict(list)
    for node in nodes:
        if node["id"] != root["id"]:
            key, _, _ = visit(node["id"])
            grouped[key].append(node)
    return grouped, paths


def read_baseline(client, manifest, names, comparison, *, page_size, max_nodes):
    """GET only. Bound the combined size of the selected baseline subtrees."""
    api = manifest["endpoints"]["api"]
    frozen = manifest["checkout"]
    root = client.get_node(frozen["id"], api_url=api)
    snapshot = checkout_snapshot(root, frozen["id"])
    if any(snapshot[k] != frozen[k] for k in ("treeid", "revision", "tarball")):
        raise WorkflowError("Baseline checkout differs from the frozen selection")
    comparison["baseline_root"] = root
    seen = set()
    for name in names:
        offset = 0
        while True:
            limit = min(page_size, max_nodes - len(seen))
            if limit <= 0:
                raise WorkflowError("Baseline node limit reached; increase --max-nodes")
            page = client.get_nodes(
                filters=[f"treeid={frozen['treeid']}", f"path={name}"],
                limit=limit,
                offset=offset,
                api_url=api,
            )
            if not isinstance(page, list) or len(page) > limit:
                raise WorkflowError("Unexpected baseline page or ignored pagination")
            for node in page:
                if (
                    not isinstance(node, dict)
                    or node.get("treeid") != frozen["treeid"]
                    or name not in (node.get("path") or [])
                ):
                    raise WorkflowError("Baseline query returned an out-of-scope node")
                node_id = checked_id(node.get("id"))
                if node_id in seen:
                    raise WorkflowError(
                        "Repeated baseline node; comparison is ambiguous"
                    )
                seen.add(node_id)
                comparison["nodes"].append(node)
            offset += len(page)
            if len(page) < limit:
                break
    comparison["listing_complete"] = True
    return root


def outcome(before, after):
    if any(n.get("state") != "done" for n in before + after):
        return "incomplete"
    if any(n.get("result") not in ("pass", "fail", "skip") for n in before + after):
        return "incomplete"
    if not before:
        return "new_failure" if after[0]["result"] == "fail" else "new_result"
    if not after:
        return "missing"
    old, new = before[0]["result"], after[0]["result"]
    return {
        ("pass", "fail"): "regression",
        ("fail", "fail"): "existing_failure",
        ("fail", "pass"): "fixed",
        ("pass", "pass"): "unchanged_pass",
        ("skip", "skip"): "unchanged_skip",
        ("skip", "fail"): "new_failure",
    }.get((old, new), "incomparable")


def compare(client, manifest, patched_root, patched_nodes, *, page_size, max_nodes):
    comparison = {
        "performed": True,
        "complete": False,
        "listing_complete": False,
        "scope": "matching_observed_job_subtrees",
        "baseline_checkout_id": manifest["checkout"]["id"],
        "baseline_treeid": manifest["checkout"]["treeid"],
        "counts": dict.fromkeys(CATEGORIES, 0),
        "results": [],
        "nodes": [],
        "errors": [],
    }
    try:
        direct = [n for n in patched_nodes if n.get("parent") == patched_root["id"]]
        names = sorted({n["name"] for n in direct})
        if not names:
            raise WorkflowError("No observed job subtrees to compare")
        comparison["job_paths"] = names
        root = read_baseline(
            client,
            manifest,
            names,
            comparison,
            page_size=page_size,
            max_nodes=max_nodes,
        )
        revision = manifest["checkout"]["revision"]
        before, base_paths = index_nodes(comparison["nodes"], root, revision)
        after, patched_paths = index_nodes(
            patched_nodes,
            patched_root,
            revision,
            manifest["expected_pipeline_patchset_hash"],
        )
        # Baseline can contain other platform/configuration variants of this job.
        # Only include the variants actually observed in the patched run.
        scopes = {key[:1] for key in after}
        before = {key: values for key, values in before.items() if key[:1] in scopes}
        comparison["selected_baseline_nodes"] = sum(map(len, before.values()))
        keys = before.keys() | after.keys()
        ambiguous = {
            key
            for key in keys
            if len(before.get(key, [])) > 1 or len(after.get(key, [])) > 1
        }
        for key in sorted(keys):
            old, new = before.get(key, []), after.get(key, [])
            category = (
                "ambiguous"
                if any(key[:i] in ambiguous for i in range(1, len(key) + 1))
                else outcome(old, new)
            )
            node = (new or old)[0]
            paths = patched_paths if new else base_paths
            _, context, path = paths[node["id"]]
            comparison["results"].append(
                {
                    "name": node["name"],
                    "kind": node["kind"],
                    "path": path,
                    "context": context,
                    "classification": category,
                    "baseline": [
                        {k: n.get(k) for k in ("id", "state", "result")} for n in old
                    ],
                    "patched": [
                        {k: n.get(k) for k in ("id", "state", "result")} for n in new
                    ],
                }
            )
        comparison["counts"].update(
            Counter(row["classification"] for row in comparison["results"])
        )
        if root.get("state") != "done":
            comparison["errors"].append("Baseline checkout is not terminal yet")
        comparison["complete"] = not comparison["errors"] and not any(
            comparison["counts"][key]
            for key in (
                "new_failure",
                "new_result",
                "missing",
                "incomparable",
                "ambiguous",
                "incomplete",
            )
        )
    except Exception as exc:
        comparison["errors"].append(
            f"Baseline comparison failed: {type(exc).__name__}: {exc}"
        )
    return comparison
