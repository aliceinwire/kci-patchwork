"""Prepare, submit once per run directory, and reconcile a patchset run."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .patchwork import Patchwork, pipeline_hash, validate_diff
from .storage import (
    WorkflowError,
    digest,
    endpoint,
    fingerprint,
    locked,
    now,
    read_json,
    write_json,
)


def checked_id(value, length=24):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise WorkflowError(f"Expected a {length}-character hexadecimal ID")
    return value


def filters(values, required=False):
    if (
        not isinstance(values, list)
        or (required and not values)
        or any(
            not isinstance(v, str) or not v.strip() or v != v.strip() for v in values
        )
        or len(set(values)) != len(values)
    ):
        raise WorkflowError(
            "Filters must be distinct, nonempty strings; select at least one job"
        )
    return values


def revision(node):
    value = (node.get("data") or {}).get("kernel_revision")
    if not isinstance(value, dict):
        raise WorkflowError("Node has no kernel revision")
    checked_id(value.get("commit"), 40)
    if not value.get("url") or not value.get("branch"):
        raise WorkflowError("Node has no Git URL or branch")
    return {key: value.get(key) for key in ("url", "branch", "commit")}


def checkout_snapshot(node, expected_id):
    if (
        not isinstance(node, dict)
        or node.get("id") != expected_id
        or node.get("kind") != "checkout"
        or node.get("name") != "checkout"
    ):
        raise WorkflowError(
            "Select an original checkout node, not a build, test or patchset"
        )
    if (
        node.get("state") not in ("available", "closing", "done")
        or node.get("result") != "pass"
    ):
        raise WorkflowError(
            "The checkout must have completed successfully and be available"
        )
    if not (node.get("artifacts") or {}).get("tarball"):
        raise WorkflowError("Checkout has no source tarball")
    if (node.get("data") or {}).get("kernel_revision", {}).get("patchset"):
        raise WorkflowError("The base checkout already contains a patchset")
    return {
        "id": checked_id(expected_id),
        "treeid": checked_id(node.get("treeid"), 64),
        "revision": revision(node),
        "state": node["state"],
        "result": node["result"],
        "tarball": node["artifacts"]["tarball"],
    }


def prepare(
    client,
    *,
    series_id,
    checkout_id,
    job_filter,
    api_url,
    pipeline_url,
    out,
    platform_filter=None,
    patchwork=None,
):
    """Read-only remotely. Save a new, immutable selection and exact patch bytes."""
    out = Path(out)
    if out.exists():
        raise WorkflowError(
            "Output directory already exists; choose a new run directory"
        )
    job_filter = filters(job_filter, required=True)
    api_url, pipeline_url = endpoint(api_url), endpoint(pipeline_url)
    checked_id(checkout_id)
    base_node = client.get_node(checkout_id, api_url=api_url)
    base = checkout_snapshot(base_node, checkout_id)
    platforms = filters(
        platform_filter
        if platform_filter is not None
        else (base_node.get("platform_filter") or [])
    )
    series, fetched = (patchwork or Patchwork()).fetch_series(series_id)
    manifest = {
        "schema_version": 1,
        "created_at": now(),
        "series": series,
        "checkout": base,
        "endpoints": {"api": api_url, "pipeline": pipeline_url},
        "job_filter": job_filter,
        "platform_filter": platforms,
        "platform_selection": (
            "explicit" if platform_filter is not None else "inherited"
        ),
        "patches": [],
        "compatibility_with_base": "not_tested",
    }
    raw_patches = [p["diff"].encode("utf-8") for p in fetched]
    manifest["expected_pipeline_patchset_hash"] = pipeline_hash(raw_patches)
    out.mkdir(parents=True, mode=0o700)
    (out / "patches").mkdir()
    for patch, raw in zip(fetched, raw_patches):
        entry = {key: value for key, value in patch.items() if key != "diff"}
        entry["file"] = f"patches/{entry['position']:04d}-{entry['id']}.patch"
        path = out / entry["file"]
        with path.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        manifest["patches"].append(entry)
    write_json(out / "manifest.json", manifest)
    state = {
        "schema_version": 1,
        "manifest_sha256": fingerprint(manifest),
        "status": "prepared",
        "updated_at": now(),
    }
    write_json(out / "state.json", state)
    return manifest


def load_run(directory):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    state = read_json(directory / "state.json")
    if (
        manifest.get("schema_version") != 1
        or state.get("schema_version") != 1
        or fingerprint(manifest) != state.get("manifest_sha256")
    ):
        raise WorkflowError(
            "Run schema is unsupported or the saved manifest has changed"
        )
    for value in manifest["endpoints"].values():
        endpoint(value)
    filters(manifest["job_filter"], required=True)
    filters(manifest["platform_filter"])
    checked_id(manifest["checkout"]["id"])
    patches = []
    entries = manifest["patches"]
    if not 1 <= len(entries) <= 32:
        raise WorkflowError("Saved patch count is invalid")
    for position, patch in enumerate(entries, 1):
        expected = f"patches/{position:04d}-{int(patch['id'])}.patch"
        if patch["file"] != expected or patch["position"] != position:
            raise WorkflowError("Saved patch order or filename has changed")
        path = directory / expected
        if path.stat().st_size > 10 * 1024 * 1024:
            raise WorkflowError("Saved patch exceeds 10 MiB")
        raw = path.read_bytes()
        if digest(raw) != patch["sha256"] or len(raw) != patch["bytes"]:
            raise WorkflowError(
                f"Saved patch {position} has changed; prepare a new run"
            )
        diff = raw.decode("utf-8")
        validate_diff(diff)
        patches.append(diff)
    if (
        pipeline_hash([p.encode("utf-8") for p in patches])
        != manifest["expected_pipeline_patchset_hash"]
    ):
        raise WorkflowError("Saved pipeline patchset hash is inconsistent")
    return manifest, state, patches


def check_patchset(node, manifest, *, node_id=None, treeid=None, require_hash=False):
    if (
        not isinstance(node, dict)
        or node.get("name") != "patchset"
        or node.get("kind") != "checkout"
    ):
        raise WorkflowError("Response is not a patchset checkout node")
    checked_id(node.get("id"))
    checked_id(node.get("treeid"), 64)
    if node_id is not None and node["id"] != node_id:
        raise WorkflowError("Patchset node ID does not match the recorded submission")
    if treeid is not None and node["treeid"] != treeid:
        raise WorkflowError("Patchset treeid does not match the recorded submission")
    if node["treeid"] == manifest["checkout"]["treeid"]:
        raise WorkflowError("Patchset unexpectedly uses the original checkout treeid")
    if (
        node.get("parent") != manifest["checkout"]["id"]
        or revision(node) != manifest["checkout"]["revision"]
    ):
        raise WorkflowError("Patchset is based on a different checkout or revision")
    if (
        node.get("jobfilter") != manifest["job_filter"]
        or (node.get("platform_filter") or []) != manifest["platform_filter"]
    ):
        raise WorkflowError(
            "Patchset job/platform filters differ from the prepared selection"
        )
    observed = node["data"]["kernel_revision"].get("patchset")
    if observed and observed != manifest["expected_pipeline_patchset_hash"]:
        raise WorkflowError("Pipeline patchset hash differs from the frozen series")
    if require_hash and not observed:
        raise WorkflowError(
            "Patchset content cannot be verified until its hash is available"
        )


def submit(client, directory, *, token):
    """One attempt per directory. Unknown outcomes require operator reconciliation."""
    if not isinstance(token, str) or not token.strip():
        raise WorkflowError("Set KCI_PIPELINE_TOKEN or configure an instance token")
    with locked(directory):
        manifest, state, patches = load_run(directory)
        if state["status"] != "prepared":
            raise WorkflowError(
                f"Submission refused: run is {state['status']}. Use status or reconcile"
            )
        base_node = client.get_node(
            manifest["checkout"]["id"], api_url=manifest["endpoints"]["api"]
        )
        base = checkout_snapshot(base_node, manifest["checkout"]["id"])
        if any(
            base[key] != manifest["checkout"][key]
            for key in ("treeid", "revision", "tarball")
        ):
            raise WorkflowError("Base checkout changed after preparation")
        if not manifest["platform_filter"] and base_node.get("platform_filter"):
            raise WorkflowError(
                "Base checkout platform filters changed after preparation"
            )
        state.update(status="submitting", attempted_at=now(), updated_at=now())
        write_json(Path(directory) / "state.json", state)  # Durable before the POST.
        try:
            response = client.trigger_patchset(
                nodeid=base["id"],
                patches=patches,
                job_filter=manifest["job_filter"],
                platform_filter=manifest["platform_filter"] or None,
                pipeline_url=manifest["endpoints"]["pipeline"],
                token=token,
            )
            node = response.get("node") if isinstance(response, dict) else None
            check_patchset(node, manifest)
            state.update(
                status="submitted",
                patchset_node_id=node["id"],
                treeid=node["treeid"],
                submitted_at=now(),
                updated_at=now(),
            )
            write_json(Path(directory) / "state.json", state)
        except Exception as exc:
            state.update(
                status="submission_unknown",
                error_type=type(exc).__name__,
                updated_at=now(),
            )
            write_json(Path(directory) / "state.json", state)
            raise WorkflowError(
                "Submission outcome is unknown. Do not resubmit. "
                "Inspect the pipeline and use reconcile with its patchset node ID"
            ) from exc
        return state


def reconcile(client, directory, *, node_id):
    """Attach an operator-identified node after a lost submission response."""
    checked_id(node_id)
    with locked(directory):
        manifest, state, _ = load_run(directory)
        if state["status"] not in ("submitting", "submission_unknown"):
            raise WorkflowError(
                "Reconcile is only for an interrupted or uncertain submission"
            )
        node = client.get_node(node_id, api_url=manifest["endpoints"]["api"])
        check_patchset(node, manifest, node_id=node_id, require_hash=True)

        def utc(value):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (
                result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
            )

        if utc(node.get("created", "")) < utc(state["attempted_at"]):
            raise WorkflowError("Patchset predates the submission attempt")
        state.update(
            status="submitted",
            patchset_node_id=node["id"],
            treeid=node["treeid"],
            reconciliation="operator_selected_node_with_matching_pipeline_hash",
            reconciled_at=now(),
            updated_at=now(),
        )
        write_json(Path(directory) / "state.json", state)
        return state
