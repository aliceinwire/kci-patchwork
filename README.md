# kci-patchwork

Test a complete Patchwork series in KernelCI directly from its URL. The command
resolves the server and series, downloads and validates the ordered diffs, and
prepares a run. With `--submit`, it also submits once, watches the results and
compares them with the original checkout.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
kci-patchwork --help
```

Requires Python 3.10+. `python -m kci_patchwork` runs the same CLI.

## Start from a URL

Choose an existing successful checkout and a supported job for your KernelCI
instance, then use them as `BASE_NODE` and `JOB`:

```bash
kci-patchwork run https://patchwork.kernel.org/series/1175663/ \
  --config ~/.config/kci-dev/kci-dev.toml \
  --instance staging \
  --checkout "$BASE_NODE" \
  --job "$JOB" \
  --out runs/series-1175663-url
```

This creates `manifest.json`, frozen patches and HTML/JSON reports, without
starting jobs. No curl download, local mbox or preprocessing script is needed.
The shorter `kci-patchwork URL ...` form is equivalent to `kci-patchwork run URL ...`.

**Add `--submit` to the same command to prepare, submit and watch in one step.**
The token comes from `KCI_PIPELINE_TOKEN` or the selected config profile. Use
`--interval 30 --timeout 1800` to set the polling budget. Completed runs include
automatic baseline comparison.

Omitting `--out` uses `runs/SERVER/series-ID`. Existing directories are refused
to avoid accidental repeat submissions. To inspect or continue an existing run:

```bash
kci-patchwork status --run runs/series-1175663-url
kci-patchwork watch --run runs/series-1175663-url --timeout 1800
```

You can inspect a URL without contacting KernelCI:

```bash
kci-patchwork series https://patchwork.kernel.org/series/1175663/
```

## Other Patchwork servers and URL formats

A full URL selects its own Patchwork server and installation path. Supported
links include:

| URL path | Selection |
| --- | --- |
| `/series/123/` or `/series/123/mbox/` | Complete series |
| `/patch/456/`, including `/raw/` and `/mbox/` | The patch's complete series |
| `/project/linux/patch/MESSAGE-ID/` | The patch's complete series |
| `/cover/789/` or `/project/linux/cover/MESSAGE-ID/` | The cover letter's series |
| `/project/linux/list/?series=123` | Series in the named project |
| `/api/1.2/{series,patches,covers}/ID/` | API resource's series |

Patch/cover Message-ID links also accept mbox suffixes. HTTP website links are
normalized to HTTPS for API reads. URLs identify the input; diffs are retrieved
from the API, including when the input is an mbox URL. The instance must expose
a compatible public Patchwork API. Project landing pages and bundles do not
identify one complete series. Ambiguous patch membership requires a series URL
or an explicit `?series=ID` in the patch URL.

For numeric IDs, change the server with `--patchwork-server`:

```bash
kci-patchwork series --series 526640 \
  --patchwork-server https://patchwork.ozlabs.org
```

These options also work with `run` and `prepare`. Use `--patchwork-api` for an
explicit API endpoint or version. Numeric IDs default to patchwork.kernel.org
and API v1.2. You can save defaults in the same TOML file used by `--config`:

```toml
default_instance = "staging"

[staging]
api = "https://staging.kernelci.org:9000"
pipeline = "https://staging.kernelci.org:9100"

[patchwork]
server = "https://patchwork.ozlabs.org"
# api = "https://patchwork.ozlabs.org/api/1.2"
```

Full URLs override Patchwork config defaults. Explicit Patchwork flags override
those defaults; when combined with a URL, they must identify the same server.
KernelCI endpoints can also be supplied with `--api-url` and `--pipeline-url`.

## Choose the checkout and jobs

```bash
kci-patchwork checkouts \
  --config ~/.config/kci-dev/kci-dev.toml --instance staging \
  --giturl https://github.com/kernelci/linux.git --branch staging-mainline
```

Select a revision to which the series applies. Repeat `--job` and `--platform`
for additional filters. Preparation validates the inputs but does not prove
patch applicability or test coverage. For series 1175663, a tinyconfig build
does not exercise the modified `lpfc` driver.

## Result interpretation

| Status | Exit | Meaning |
| --- | --- | --- |
| `PREPARED` | 0 | Inputs saved; no jobs submitted |
| `RUNNING` | 3 | Jobs remain active, including after a watch timeout |
| `NO_FAILURES_IN_OBSERVED_RESULTS` | 0 | Complete comparison, no observed failures |
| `NO_REGRESSIONS_IN_OBSERVED_RESULTS` | 0 | Complete comparison, all failures also failed in the baseline |
| `REGRESSIONS_DETECTED` | 1 | A matched baseline pass became a failure |
| `REVIEW_REQUIRED` | 1 | A failure has no baseline match or its baseline was skipped |
| `EVIDENCE_INCOMPLETE` | 2 | Missing, inconsistent, ambiguous or incomplete evidence |
| `PATCH_APPLICATION_FAILED` | 1 | Patch application failed |
| `SUBMISSION_UNKNOWN` | 2 | The saved attempt needs reconciliation; do not resubmit |

Reports retain raw outcomes and paired baseline/patched results. Comparison
covers observed job subtrees and configuration, not required coverage or patch
approval. Matching failures compares outcomes, not failure causes.

The existing `prepare --series`, `submit`, `status`, `watch` and `reconcile`
commands remain available. See the [detailed workflow](docs/workflow.md) for
submission recovery, collection limits, comparison rules and the Python API.
For an optional local mbox example, see [examples/README.md](examples/README.md).

## Tests

```bash
python -m pip install -e '.[dev]'
python -m pytest tests --import-mode=importlib -q
python -m black --check kci_patchwork tests
python -m isort --check-only kci_patchwork tests
```

Tests block external HTTP and mock submissions. The kci-dev dependency is pinned
in `pyproject.toml`. No command posts Patchwork checks, sends mail or approves a
series. Only `submit` and `run --submit` start KernelCI jobs.
