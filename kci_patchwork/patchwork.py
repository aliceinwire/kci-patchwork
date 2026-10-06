"""Read a complete series and freeze its ordered, text-only diffs."""

import hashlib
import json
import re
from io import BytesIO

import requests

from .source import parse_source, positive_id
from .storage import WorkflowError, digest, endpoint

MAX_PATCHES = 32
MAX_PATCH_BYTES = 10 * 1024 * 1024
MAX_SERIES_BYTES = 32 * 1024 * 1024


def patch_position(patch, total):
    """REST series lists may follow mail dates; use declared sequence numbers."""
    numbers = re.findall(
        r"\[[^\]\r\n]*?(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)[^\]\r\n]*\]",
        patch.get("name") or "",
    )
    if len(numbers) > 1:
        raise WorkflowError("Patch has ambiguous sequence numbers")
    declared = patch.get("number")
    if numbers:
        position, expected_total = map(int, numbers[0])
        if expected_total != total or (declared is not None and declared != position):
            raise WorkflowError("Patch sequence disagrees with the series metadata")
        declared = position
    if declared is None and total == 1:
        declared = 1
    if type(declared) is not int or not 1 <= declared <= total:
        raise WorkflowError(
            "Cannot establish patch order; need sequence numbers for every patch"
        )
    return declared


def validate_diff(diff):
    if not isinstance(diff, str) or not diff:
        raise WorkflowError("Patchwork returned an empty or missing diff")
    raw = diff.encode("utf-8")
    if len(raw) > MAX_PATCH_BYTES:
        raise WorkflowError("A patch exceeds 10 MiB")
    if "\x00" in diff or re.search(r"^(GIT binary patch|Binary files )", diff, re.M):
        raise WorkflowError("Binary patches are not supported by this pipeline")
    if not re.search(r"^--- [^\n]+\n\+\+\+ ", diff, re.M):
        raise WorkflowError("A patch has no text unified diff headers")
    return raw


def pipeline_hash(patches):
    """Match pipeline 6ab90397's hash contract; full-byte SHA256 is kept too.

    The server hashes old/new modes and +/- lines per patch, then hashes the
    concatenated hex digests in series order. This is not a full-content hash.
    """
    result = hashlib.sha256()
    for raw in patches:
        lines = [
            line
            for line in BytesIO(raw).readlines()
            if line.startswith((b"old mode", b"new mode", b"-", b"+"))
        ]
        result.update(digest(b"\n".join(lines)).encode("ascii"))
    return result.hexdigest()


class Patchwork:
    def __init__(self, api_url="https://patchwork.kernel.org/api/1.2", session=None):
        self.api_url = endpoint(api_url)
        self.session = session or requests.Session()
        self.source = None
        self.selected_series = None
        self.member_id = None

    def _request(self, path, params=None):
        url = f"{self.api_url}/{path}"
        # Construct resource URLs ourselves rather than following links in data.
        with self.session.get(
            url,
            headers={"Accept": "application/json"},
            timeout=(10, 30),
            stream=True,
            allow_redirects=False,
            params=params,
        ) as response:
            if response.status_code != 200:
                raise WorkflowError(
                    f"Patchwork {path}: HTTP {response.status_code}; check the server/API URL"
                )
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_SERIES_BYTES:
                    raise WorkflowError("Patchwork JSON response exceeds 32 MiB")
                chunks.append(chunk)
            has_next = bool(response.links.get("next"))
        try:
            data = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError) as exc:
            raise WorkflowError("Patchwork returned invalid JSON") from exc
        return data, has_next

    def get(self, resource, object_id):
        if type(object_id) is not int or object_id < 1:
            raise WorkflowError("Patchwork IDs must be positive integers")
        data, _ = self._request(f"{resource}/{object_id}/")
        if not isinstance(data, dict) or data.get("id") != object_id:
            raise WorkflowError("Patchwork returned an unexpected object")
        return data

    def _check_project(self, item):
        expected = self.source.project if self.source else None
        if expected is None:
            return
        project = item.get("project") or {}
        if (
            str(project.get("id")) != expected
            and str(project.get("link_name", "")).casefold() != expected.casefold()
        ):
            raise WorkflowError("Patchwork returned a different project than the URL")

    def _check_object(self, item):
        self._check_project(item)
        if self.source.msgid and item.get("msgid") != f"<{self.source.msgid}>":
            raise WorkflowError(
                "Patchwork returned a different Message-ID than the URL"
            )

    @classmethod
    def from_source(
        cls, value=None, *, series_id=None, server_url=None, api_url=None, session=None
    ):
        """Return the selected server and the complete series identified by a URL."""
        source = parse_source(
            value, series_id=series_id, server_url=server_url, api_url=api_url
        )
        client = cls(source.api_url, session=session)
        client.source = source
        if source.resource == "series":
            client.selected_series = source.object_id
            return client, source.object_id
        ident = source.object_id
        if source.msgid:
            rows, has_next = client._request(
                f"{source.resource}/",
                {"project": source.project, "msgid": source.msgid, "per_page": 2},
            )
            if (
                has_next
                or not isinstance(rows, list)
                or len(rows) != 1
                or not isinstance(rows[0], dict)
            ):
                raise WorkflowError(
                    "URL must resolve to exactly one Patchwork patch or cover letter"
                )
            client._check_object(rows[0])
            ident = positive_id(rows[0].get("id"))
        item = client.get(source.resource, ident)
        client._check_object(item)
        associations = item.get("series")
        if (
            not isinstance(associations, list)
            or not associations
            or any(not isinstance(s, dict) for s in associations)
        ):
            raise WorkflowError(
                "This patch or cover letter is not associated with a series"
            )
        ids = {positive_id(s.get("id")) for s in associations}
        if source.series_hint is not None:
            if source.series_hint not in ids:
                raise WorkflowError(
                    "The URL's series is not associated with this patch or cover letter"
                )
            selected = source.series_hint
        elif len(ids) == 1:
            selected = ids.pop()
        else:
            raise WorkflowError(
                "This item belongs to multiple series; use a series URL or ?series=ID"
            )
        client.member_id = ident
        client.selected_series = selected
        return client, selected

    def _check_selection(self, series, patch_ids):
        if self.source is None:
            return
        if series.get("id") != self.selected_series:
            raise WorkflowError("The resolved series changed")
        self._check_project(series)
        if self.source.resource == "patches" and self.member_id not in patch_ids:
            raise WorkflowError("The selected patch is absent from the complete series")
        if (
            self.source.resource == "covers"
            and (series.get("cover_letter") or {}).get("id") != self.member_id
        ):
            raise WorkflowError(
                "The selected cover letter does not belong to this series"
            )

    @staticmethod
    def series_ids(series):
        total, patches = series.get("total"), series.get("patches")
        if (
            type(total) is not int
            or not 1 <= total <= MAX_PATCHES
            or series.get("received_all") is not True
            or series.get("received_total") != total
            or not isinstance(patches, list)
            or len(patches) != total
        ):
            raise WorkflowError("Need a complete series containing 1..32 patches")
        ids = [
            patch.get("id") if isinstance(patch, dict) else None for patch in patches
        ]
        if any(type(i) is not int or i < 1 for i in ids) or len(set(ids)) != total:
            raise WorkflowError("Series has invalid or duplicate patch IDs")
        return ids

    def fetch_series(self, series_id):
        series = self.get("series", series_id)
        ids = self.series_ids(series)
        self._check_selection(series, ids)
        patches, total_bytes = [], 0
        for patch_id in ids:
            patch = self.get("patches", patch_id)
            if (
                self.source
                and self.source.resource == "patches"
                and patch_id == self.member_id
            ):
                self._check_object(patch)
            if not any(
                s.get("id") == series_id
                for s in patch.get("series", [])
                if isinstance(s, dict)
            ):
                raise WorkflowError(
                    f"Patch {patch_id} is not associated with this series"
                )
            raw = validate_diff(patch.get("diff"))
            position = patch_position(patch, series["total"])
            total_bytes += len(raw)
            if total_bytes > MAX_SERIES_BYTES:
                raise WorkflowError(
                    "Combined series diffs exceed this application's 32 MiB limit"
                )
            patches.append(
                {
                    "position": position,
                    "id": patch_id,
                    "name": patch.get("name"),
                    "web_url": patch.get("web_url"),
                    "msgid": patch.get("msgid"),
                    "bytes": len(raw),
                    "sha256": digest(raw),
                    "diff": patch["diff"],
                }
            )
        if sorted(p["position"] for p in patches) != list(range(1, len(ids) + 1)):
            raise WorkflowError("Series has duplicate or missing sequence numbers")
        patches.sort(key=lambda patch: patch["position"])
        final = self.get("series", series_id)
        self._check_selection(final, self.series_ids(final))
        if self.series_ids(final) != ids or final.get("version") != series.get(
            "version"
        ):
            raise WorkflowError("Series changed while being fetched; prepare again")
        selected = {
            key: series.get(key)
            for key in ("id", "name", "version", "date", "web_url", "total")
        }
        selected["project"] = (series.get("project") or {}).get("link_name")
        selected["api_url"] = self.api_url
        selected["received_all"] = True
        if self.source and self.source.url:
            selected["input_url"] = self.source.url
        return selected, patches
