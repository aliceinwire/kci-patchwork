"""A portable, escaped HTML report with the JSON evidence beside it."""

from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .storage import atomic_write, write_json


def text(value):
    return escape(str(value if value is not None else ""))


def link(url, label):
    try:
        parsed = urlsplit(url or "")
        valid = (
            parsed.scheme in ("https", "http")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    if not valid:
        return text(label)
    return f'<a href="{text(url)}" rel="noreferrer">{text(label)}</a>'


def comparison_html(report):
    comparison = report.get("comparison", {})
    if not comparison.get("performed"):
        return (
            "<section><h2>Baseline comparison</h2><p>"
            + text(comparison.get("reason", "Not collected yet"))
            + "</p></section>"
        )
    api = report["selection"]["endpoints"]["api"]

    def observations(nodes):
        return (
            "<br>".join(
                link(api + "/viewer?" + urlencode({"node_id": node["id"]}), node["id"])
                + f"<small>{text(node.get('state'))} / {text(node.get('result'))}</small>"
                for node in nodes
            )
            or "No matching result"
        )

    changed = [
        row
        for row in comparison["results"]
        if row["classification"] not in ("unchanged_pass", "unchanged_skip")
    ]
    rows = (
        "".join(
            f"<tr><td>{text(' / '.join(row['path']))}</td>"
            f"<td>{text(row['classification'].replace('_', ' '))}</td>"
            f"<td>{observations(row['baseline'])}</td>"
            f"<td>{observations(row['patched'])}</td></tr>"
            for row in changed[:300]
        )
        or '<tr><td colspan="4">No changed or failing results in the comparison.</td></tr>'
    )
    counts = " · ".join(
        f"{key.replace('_', ' ')}: {value}"
        for key, value in comparison["counts"].items()
        if value
    )
    errors = "".join(f"<li>{text(error)}</li>" for error in comparison["errors"])
    return f"""<section><h2>Baseline comparison</h2>
<p>Original checkout: <code>{text(comparison['baseline_checkout_id'])}</code>.<br>
Baseline treeid: <code>{text(comparison['baseline_treeid'])}</code>.<br>
Comparison complete: {text(comparison['complete'])}.</p>
<p>{text(counts or 'No comparable results collected')}</p><ul>{errors}</ul>
<p>Matches use job ancestry and execution configuration within the frozen checkout.
Only variants present in the patched run are selected. Missing, new or ambiguous
results remain visible. A pass-to-fail transition is evidence to investigate,
not proof that the patch caused it.</p>
<table><thead><tr><th>Relative path</th><th>Classification</th><th>Baseline</th><th>Patched</th></tr></thead>
<tbody>{rows}</tbody></table>
<p>Showing {min(300, len(changed))} of {len(changed)} changed or failing comparisons.
Unchanged results, matching configuration and baseline evidence are in <a href="report.json">report.json</a>.</p></section>"""


def render(report):
    manifest, state = report["selection"], report["submission"]
    series, base = manifest["series"], manifest["checkout"]
    status = report["status"]
    color = (
        "#a23c36"
        if status
        in ("REVIEW_REQUIRED", "PATCH_APPLICATION_FAILED", "REGRESSIONS_DETECTED")
        else "#365b9a"
    )
    if status in ("EVIDENCE_INCOMPLETE", "SUBMISSION_UNKNOWN"):
        color = "#97651d"
    patches = "".join(
        f"<tr><td>{p['position']}</td><td>{link(p.get('web_url'), p['name'])}</td>"
        f"<td>{p['id']}</td><td><code>{text(p['sha256'])}</code></td></tr>"
        for p in manifest["patches"]
    )
    nodes = report.get("nodes", [])
    shown = nodes[:300]
    rows = []
    for node in shown:
        data = node.get("data") or {}
        url = (
            manifest["endpoints"]["api"]
            + "/viewer?"
            + urlencode({"node_id": node["id"]})
        )
        artifacts = node.get("artifacts") or {}
        links = " · ".join(
            link(value, key)
            for key, value in list(artifacts.items())[:8]
            if isinstance(value, str)
        )
        rows.append(
            f"<tr><td>{link(url, node.get('name'))}<small>{text(node['id'])}</small></td>"
            f"<td>{text(node.get('kind'))}</td><td>{text(data.get('platform'))}</td>"
            f"<td>{text(node.get('state'))}</td><td>{text(node.get('result') or 'unset')}</td>"
            f"<td>{links}</td></tr>"
        )
    errors = "".join(f"<li>{text(error)}</li>" for error in report["errors"])
    counts = report.get("counts", {})
    result_counts = " · ".join(
        f"{key}: {value}" for key, value in counts.get("results", {}).items()
    )
    demo = (
        '<p class="banner">SIMULATED EXAMPLE: these test results are fixtures, not a live submission.</p>'
        if report.get("example") == "simulated"
        else ""
    )
    body_rows = (
        "".join(rows)
        or '<tr><td colspan="6">No submitted-run observations collected.</td></tr>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>Patchwork series {series['id']} · KernelCI</title>
<style>
body{{margin:0;background:#f3f5f8;color:#172b43;font:16px/1.55 system-ui,sans-serif}}
main{{max-width:1200px;margin:40px auto;padding:0 24px}}h1{{font-size:30px;line-height:1.2;margin:10px 0}}
h2{{font-size:20px;margin-top:0}}a{{color:#245995}}small{{display:block;color:#64748b;font-size:12px}}
.eyebrow{{font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#52667e}}
.badge{{display:inline-block;background:{color};color:white;padding:7px 12px;border-radius:6px;margin:16px 0}}
section{{background:white;padding:24px;border:1px solid #dce2eb;border-radius:10px;margin:20px 0;overflow:auto}}
dl{{display:grid;grid-template-columns:140px 1fr;gap:8px 18px}}dt{{color:#5e6b7c}}dd{{margin:0;overflow-wrap:anywhere}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #e5eaf1}}
th{{color:#61738a;font-size:12px;text-transform:uppercase}}code{{font-size:12px;overflow-wrap:anywhere}}
.banner{{background:#fff0cd;padding:16px;border-radius:6px}}footer{{color:#61738a;font-size:13px;padding:14px 0}}
@media(max-width:650px){{main{{margin:20px auto;padding:0 12px}}section{{padding:16px}}dl{{grid-template-columns:1fr;gap:2px}}dd{{margin-bottom:10px}}}}
</style></head><body><main>
<div class="eyebrow">KernelCI / Patchwork application</div>
<h1>{text(series['name'])}</h1>
<div>{link(series.get('web_url'), 'Series ' + str(series['id']))} · version {text(series['version'])} · {text(series.get('project'))}</div>
<div class="badge">{text(status.replace('_', ' '))}</div>{demo}
<section><h2>Selected run</h2><dl>
<dt>Base checkout</dt><dd><code>{text(base['id'])}</code></dd>
<dt>Base revision</dt><dd>{text(base['revision']['branch'])} · <code>{text(base['revision']['commit'])}</code></dd>
<dt>Git repository</dt><dd>{link(base['revision']['url'], base['revision']['url'])}</dd>
<dt>Jobs</dt><dd>{text(', '.join(manifest['job_filter']))}</dd>
<dt>Platforms</dt><dd>{text(', '.join(manifest['platform_filter']) or 'No platform restriction')}</dd>
<dt>Patched treeid</dt><dd><code>{text(state.get('treeid') or 'Not submitted')}</code></dd>
<dt>Patchset node</dt><dd><code>{text(state.get('patchset_node_id') or 'Not submitted')}</code></dd>
<dt>Submission</dt><dd>{text(state['status'])}</dd>
</dl><p>Preparation does not test whether the patches apply to the base. The pipeline reports patch application separately.</p></section>
{comparison_html(report)}
<section><h2>Observed results</h2><p>{text(counts.get('total', 0))} nodes · {text(result_counts or 'No results yet')}</p>
<p>Listing complete: {text(report['listing_complete'])}. Observed run terminal: {text(report['terminal'])}.</p>
<ul>{errors}</ul><table><thead><tr><th>Node</th><th>Kind</th><th>Platform</th><th>State</th><th>Result</th><th>Artifacts</th></tr></thead>
<tbody>{body_rows}</tbody></table><p>Showing {len(shown)} of {len(nodes)} nodes. All collected nodes are in <a href="report.json">report.json</a>.</p>
<p>Results belong to the submitted treeid. Required coverage is not assessed, and this report does not approve the series.</p></section>
<section><h2>Frozen patches</h2><table><thead><tr><th>Order</th><th>Patch</th><th>ID</th><th>Content SHA256</th></tr></thead><tbody>{patches}</tbody></table></section>
<footer>Generated {text(report['generated_at'])} · kci-patchwork 0.1.0 · kci-dev Python library API</footer>
</main></body></html>"""


def write_report(directory, report):
    directory = Path(directory)
    write_json(directory / "report.json", report)
    atomic_write(directory / "report.html", render(report))
