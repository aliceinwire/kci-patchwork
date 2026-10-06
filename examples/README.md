# Test a Patchwork URL

The installed CLI accepts the website URL directly. No local mbox is needed:

```bash
kci-patchwork series https://patchwork.kernel.org/series/1175663/

kci-patchwork run https://patchwork.kernel.org/series/1175663/ \
  --config ~/.config/kci-dev/kci-dev.toml --instance staging \
  --checkout "$BASE_NODE" --job "$JOB" \
  --out runs/series-1175663-url
```

Set `BASE_NODE` and `JOB` to a suitable checkout and job in your instance.
The second command prepares the run. Add `--submit` to submit once, monitor
results and compare against the original checkout in the same command.
Both commands select the Patchwork server from the URL; patch and cover-letter
links resolve to their complete series too. See the [main README](../README.md)
for server configuration and supported URL formats.

## Optional: verify a downloaded mbox

Run these commands from the repository root after `python -m pip install -e .`.
The example uses [series 1175663](https://patchwork.kernel.org/series/1175663/):
**[PATCH v4 1/1] scsi: lpfc: defer SCSI rport node put until devloss callback**, by
Dai Ngo, posted on 29 September 2026. Its patch ID is **14853710**.

### Download and verify

Download the **series mbox** offered by the Patchwork website:

```bash
mkdir -p runs/mbox-input
curl --fail --show-error --max-time 60 \
  'https://patchwork.kernel.org/series/1175663/mbox/' \
  --output runs/mbox-input/series-1175663.mbox

python examples/prepare_mbox.py \
  --series 1175663 \
  --mbox runs/mbox-input/series-1175663.mbox
```

Expected: `"status": "MBOX_VERIFIED"`, one patch with ID `14853710`, and its
SHA256. This downloads metadata/diffs through the Patchwork REST API and checks
the mbox's patch IDs, Message-IDs and exact decoded diff bytes against them.
It does not contact KernelCI. HTTP errors or an HTML challenge page are not a
valid mbox; a mismatch stops the example with exit code 2.

For another series, change the ID in both the download URL and `--series`.
Use the complete Patchwork-generated plain-text series export, without a cover
letter or replies. This example caps the whole mbox at 32 MiB. It restores the
API-validated patch order even if messages in the downloaded file are reordered.

### Prepare a KernelCI run

List successful checkouts on staging:

```bash
python -m kci_patchwork checkouts \
  --api-url https://staging.kernelci.org:9000 \
  --giturl https://github.com/kernelci/linux.git \
  --branch staging-mainline \
  --limit 20
```

Choose a checkout with a revision to which the patch applies. A new checkout
may already contain the change, and an older one may lack prerequisites. The
example does not prove applicability or select a base automatically.

Set the checkout ID and a job filter supported by that staging instance:

```bash
BASE_NODE='REPLACE_WITH_CHECKOUT_NODE_ID'
JOB='REPLACE_WITH_SUPPORTED_JOB_FILTER'

python examples/prepare_mbox.py \
  --series 1175663 \
  --mbox runs/mbox-input/series-1175663.mbox \
  --checkout "$BASE_NODE" \
  --api-url https://staging.kernelci.org:9000 \
  --pipeline-url https://staging.kernelci.org:9100 \
  --job "$JOB" \
  --out runs/series-1175663
```

For compile coverage of this SCSI change, select a configuration enabling the
`lpfc` driver (`CONFIG_SCSI_LPFC`); a tinyconfig build does not exercise this code.
The reported use-after-free needs a suitable runtime reproducer and hardware
to assess, beyond simply compiling the patch.

Expected: `"status": "PREPARED"`. Open `runs/series-1175663/report.html`.
The run contains the original `series.mbox`, extracted `patches/*.patch`, their
checksums and the source mbox SHA256 in `manifest.json`. Use a new `--out` path
for another attempt. Patch bytes come from the downloaded mbox through the
existing Python `prepare(..., patchwork=...)` extension point. The installed CLI
accepts a Patchwork URL or series ID directly, without a local mbox.

All commands above make remote GET requests only. No token is needed. Neither
`MBOX_VERIFIED` nor `PREPARED` means the patch applies, builds or passes tests.
To explicitly start jobs later, follow the repository's
[submission instructions](../docs/workflow.md#optional-submission-and-monitoring).

## Compare an already submitted run

After the run completes, `status` and `watch` automatically compare its results
with the original checkout recorded during preparation:

```bash
python -m kci_patchwork status --run runs/series-1175663
```

This also refreshes runs created before baseline comparison was added. Keep the
existing run directory; preparation and submission do not need to be repeated.
Open `runs/series-1175663/report.html` for paired baseline and patched results.

Failures that also failed in a matching baseline job are `existing_failure`.
When the comparison is complete and these are the only failures, the status is
`NO_REGRESSIONS_IN_OBSERVED_RESULTS` with exit code 0. The raw failure count is
preserved. A matched pass-to-fail change produces `REGRESSIONS_DETECTED` with
exit code 1. Missing, ambiguous or unfinished baseline evidence stays visible
and cannot produce a successful comparison. See
[result interpretation](../README.md#result-interpretation) for all outcomes.
