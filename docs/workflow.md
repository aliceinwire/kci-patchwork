# Detailed workflow and result contracts

For the consolidated URL command, start with the [main README](../README.md).

A standalone application that freezes one complete Patchwork series, prepares it for
an existing KernelCI checkout, and reports the resulting patchset tree. All
KernelCI operations use the public `KernelCIClient` Python API directly.
Patchwork metadata and diffs are read through its REST API.

`prepare`, `series`, `checkouts`, `status`, `watch`, and `reconcile` make only
remote GET requests. The `submit` command and `run --submit` start jobs. No command posts
Patchwork checks, sends mail, changes GitHub, retries a job, or approves a series.

## Install and run the offline tests first

Requires Python 3.10+ and a local filesystem supporting `fcntl.flock` for run
locking. From the [kci-patchwork repository](https://github.com/aliceinwire/kci-patchwork)
root after applying the patch:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
kci-patchwork --help
```

The tests block real HTTP requests. Submission tests use mocks, including a
contract test through the actual `KernelCIClient.trigger_patchset()` method.
`pyproject.toml` pins the kci-dev library revision against which this application
was developed. `python -m pip install -r requirements.txt` is an equivalent
editable installation. Both `kci-patchwork` and `python -m kci_patchwork` invoke
the same application.

For the optional development checks:

```bash
python -m pip install -e '.[dev]'
python -m pytest tests --import-mode=importlib -q
python -m black --check kci_patchwork tests
python -m isort --check-only kci_patchwork tests
```

## Read-only preparation

For a real Patchwork website mbox download and a runnable Python example, see
[Test a real Patchwork mbox](../examples/README.md).

Inspect a series. Replace the ID with the series you want to test:

```bash
python -m kci_patchwork series --series 1175663
```

List recent successful checkouts, oldest first within the time window:

```bash
python -m kci_patchwork checkouts \
  --api-url https://api.kernelci.org \
  --since 2026-09-28 \
  --branch master \
  --limit 20
```

Choose a checkout whose repository and revision are appropriate for the series.
Preparation verifies the checkout metadata and source-tarball reference; it
does not download that tarball or prove that the series applies to it.

Use an explicit kci-dev TOML config, for example:

```toml
default_instance = "staging"

[staging]
api = "https://staging.kernelci.org:9000/"
pipeline = "https://staging.kernelci.org:9100/"
```

The API and pipeline endpoints must refer to the same KernelCI instance. Their
association cannot be established automatically. Endpoints are frozen in the
manifest. A token is unnecessary for preparation.

```bash
SERIES_ID='REPLACE_WITH_SERIES_ID'
BASE_NODE='REPLACE_WITH_CHECKOUT_NODE_ID'

python -m kci_patchwork prepare \
  --config /path/to/kci-dev.toml \
  --instance staging \
  --series "$SERIES_ID" \
  --checkout "$BASE_NODE" \
  --job kbuild-gcc-12-x86 \
  --out runs/series-review
```

The job name above is an example from the KernelCI patchset documentation. Choose
filters supported by your instance. Repeat `--job` and, optionally, `--platform`
to select more. Omitting platforms inherits the checkout's platform filter;
the manifest records the effective selection. You can supply `--api-url` and
`--pipeline-url` instead of a config.

Preparation creates a new directory containing:

| File | Purpose |
| --- | --- |
| `manifest.json` | Series/version, checkout, endpoints, filters, patch order, checksums |
| `patches/*.patch` | Exact UTF-8 diffs to send inline |
| `state.json` | Durable submission state and, after submission, returned IDs |
| `report.html` | Portable preview, initially labelled PREPARED |
| `report.json` | Structured report and collected evidence |

Open `report.html` and review `manifest.json`. Preparation never submits jobs.
An existing output directory is refused. Changed patch bytes or a changed
manifest are rejected when loading the run.

## Optional submission and monitoring

This section documents the application's explicit write operation. It is not
part of installation, preparation, or testing the example.

Provide a pipeline token with patchset permission in `KCI_PIPELINE_TOKEN` using
your normal credential setup. Do not put it in a run directory. Alternatively,
`submit --config ... --instance ...` can read the selected instance's `token`;
its configured endpoints must match the prepared manifest.

```bash
# This command starts KernelCI jobs when explicitly invoked:
python -m kci_patchwork submit --run runs/series-review

# These commands only read results and update local reports:
python -m kci_patchwork status --run runs/series-review
python -m kci_patchwork watch --run runs/series-review --interval 30 --timeout 900
```

`watch` polls within a configurable budget, checked between snapshots. A
snapshot uses bounded HTTP requests and can extend beyond that budget. Pressing
Ctrl-C preserves the run. Run `status` or `watch` again to resume observation.
Reports are refreshed by `status` and `watch`, not automatically by `submit`.
Once the patched tree has complete terminal results, both commands automatically
compare it with the original checkout saved in `manifest.json`. Existing run
directories work without migration: update the application and run `status`
again. Comparison makes GET requests only and does not submit a baseline run.

The local attempt record is written and fsynced before calling
`trigger_patchset()`. A repeated `submit` for that directory is refused, even
after a timeout, process interruption, malformed response, or application error.
The pipeline API has no idempotency key. This protection is local to the run
directory; copying it, deleting its state, or preparing the same series again
can create a separate submission.

If submission is uncertain, do not rerun it or reset the state. Have an operator
identify the actual submitted patchset node and inspect its patch artifacts.
After patch application produces a hash, an explicit reconciliation is possible:

```bash
python -m kci_patchwork reconcile \
  --run runs/series-review \
  --node-id REPLACE_WITH_PATCHSET_NODE_ID
```

Reconciliation checks the parent checkout, revision, job/platform filters,
creation time, and pipeline patchset hash. It only changes the local run record.
The pipeline hash excludes some patch content, including context lines, so it is
not proof of identical patch bytes. The operator must identify the correct
submission. Failed application without a hash cannot be reconciled automatically.
A server clock behind the submitting computer can also prevent reconciliation.

## Result interpretation

Patchsets keep the original commit hash and add `kernel_revision.patchset`.
Consequently, this application never calls commit-based `compare_results()` for
the patched run. It records the returned patchset node and `treeid`, then uses
`get_node()` and paginated `get_nodes(filters=["treeid=..."])` to collect only
that tree. It checks patchset identity, hash, and descendant parent chains.

The baseline is the exact unpatched checkout frozen during preparation. Its
identity, revision and source tarball are checked again. Baseline reads combine
its `treeid` with `path=<observed job name>` filters, so unrelated jobs in a large
checkout do not exhaust the collection budget. `--max-nodes` applies separately
to the patched tree and to the combined baseline query results.

Results are matched by their parent ancestry relative to each root, node kind,
name and inherited execution configuration. Configuration includes architecture,
compiler, defconfig/config fragments, platform, device, runtime and test revision
where supplied. Only baseline job variants observed in the patched run are
selected. Duplicate matches, changed configuration, missing results, unfinished
baseline jobs and read errors cannot produce a successful comparison.

`report.json` retains the raw patched results and adds baseline nodes, matching
configuration, paired node IDs, per-result classifications and comparison errors.
`report.html` shows the comparison counts and links to both nodes for failures,
fixes and unmatched results. The CLI JSON includes a compact comparison summary.

| Status | Exit code | Meaning |
| --- | --- | --- |
| `PREPARED` | 0 | Local inputs are ready; nothing submitted |
| `RUNNING` | 3 | Observed nodes are still active, or the watch budget expired |
| `SUBMISSION_UNKNOWN` | 2 | A recorded attempt needs operator reconciliation |
| `EVIDENCE_INCOMPLETE` | 2 | Missing, truncated, inconsistent, skipped-only, ambiguous or incomplete evidence, including baseline comparison |
| `PATCH_APPLICATION_FAILED` | 1 | The patchset checkout finished with failure |
| `REGRESSIONS_DETECTED` | 1 | At least one matched baseline pass became a failure |
| `REVIEW_REQUIRED` | 1 | A patched failure has no matching baseline result or the baseline skipped it |
| `NO_REGRESSIONS_IN_OBSERVED_RESULTS` | 0 | Comparison is complete and all observed failures also failed in the baseline |
| `NO_FAILURES_IN_OBSERVED_RESULTS` | 0 | Comparison is complete and the observed terminal tree has no failures |

Exit 0 from `prepare` or `submit` describes that operation, not passing tests.
An available patchset checkout is not a completed test run. An empty result
list, a root without descendants, read errors, or a page limit never produces
a passing result. Increase `--max-nodes` if a report hits its collection limit.
Known regressions take precedence over incomplete comparisons; otherwise an
unmatched failure requires review and incomplete evidence returns exit 2.
Check `comparison.complete`, its counts and errors alongside the status.
The `terminal` flag describes the patched run. If the baseline is still active,
collect again with `status` once it finishes.

A matched fail-to-fail result is an `existing_failure`; fail-to-pass is `fixed`.
These labels compare result values, not failure signatures or logs. A
pass-to-fail transition is evidence to investigate, not proof of patch causality.
Unmatched passing results, missing patched results and transitions between a
skip and a pass remain incomplete. Matching skips are recorded separately.

Required coverage is always `not_assessed` and `approved` is always false. Job
filters may select several jobs and their dependencies; this example cannot
prove that every intended job/platform combination was scheduled. A successful
comparison only covers the observed job subtrees, not every baseline job.
Artifact links are included; this version does not download logs.

## Calling the application from Python

```python
from kcidev import KernelCIClient
from kci_patchwork.patchwork import Patchwork
from kci_patchwork.workflow import prepare

client = KernelCIClient()
manifest = prepare(
    client,
    series_id=series_id,
    checkout_id=checkout_node_id,
    job_filter=["kbuild-gcc-12-x86"],
    api_url="https://staging.kernelci.org:9000",
    pipeline_url="https://staging.kernelci.org:9100",
    patchwork=Patchwork("https://patchwork.kernel.org/api/1.2"),
    out="runs/python-example",
)
```

`prepare()` is read-only remotely. `submit(client, directory, token=...)` is the
separate write operation; `collect(client, directory)` and `write_report(...)`
handle monitoring and report generation. The CLI keeps stdout valid JSON and
sends the library's diagnostic output to stderr.

## Contracts and limits

- kci-dev API revision: `ba6b7134f1296702182b6559b5a2320f3d6b40fa`.
- Pipeline behavior inspected at `6ab90397d53b8572f56fbba2d3b725dbc5c1311c`.
  A deployed pipeline with a different hash contract will produce an incomplete
  report until the adapter is updated.
- Complete series only: 1 to 32 unique patches, at most 10 MiB per diff and
  32 MiB combined in this application. Binary diffs are rejected. Detailed patch
  path validation and applicability remain the pipeline's responsibility.
- Patchwork series lists can follow mail dates. For multi-patch series, the
  application uses each patch's sequence number or numbered subject (`[1/3]`,
  `[PATCH v2 1/3]`, etc.), and requires exactly positions 1 through the total.
  Missing or ambiguous order is rejected instead of guessing from IDs or dates.
- Public HTTPS Patchwork API v1.2 is the default. Requests are bounded and do not
  follow redirects. Private Patchwork authentication is outside this example.
- Tokens are passed to the library in memory and excluded from local records.
  Patch contents and result metadata are saved locally. Keep run directories on
  a local filesystem supporting `flock`; network filesystem lock behavior varies.

References:

- [kci-dev Python API](https://github.com/kernelci/kci-dev/blob/ba6b7134f1296702182b6559b5a2320f3d6b40fa/kcidev/api.py)
- [KernelCI patchset documentation](https://docs.kernelci.org/components/kci-dev/patchset/)
- [Pipeline patchset endpoint](https://github.com/kernelci/kernelci-pipeline/blob/6ab90397d53b8572f56fbba2d3b725dbc5c1311c/src/lava_callback.py)
- [Pipeline patch application and hashing](https://github.com/kernelci/kernelci-pipeline/blob/6ab90397d53b8572f56fbba2d3b725dbc5c1311c/src/patchset.py)
- [Patchwork REST API v1.2](https://patchwork.readthedocs.io/en/latest/api/rest/schemas/v1.2/)
