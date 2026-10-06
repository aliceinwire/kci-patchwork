"""Resolve Patchwork URL syntax without fetching website HTML or following links."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .storage import WorkflowError, endpoint

DEFAULT_SERVER = "https://patchwork.kernel.org"


def positive_id(value):
    if not re.fullmatch(r"[0-9]+", str(value)) or not 1 <= int(value) <= 10**12:
        raise WorkflowError("Patchwork IDs must be positive integers up to 10^12")
    return int(value)


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or 443


def checked_url(value, *, website=False):
    if (
        not isinstance(value, str)
        or len(value) > 8192
        or any(ord(c) < 32 for c in value)
    ):
        raise WorkflowError("Invalid Patchwork URL")
    parsed = urlsplit(value)
    if website and parsed.scheme == "http":
        # Patchwork deployments can emit HTTP website links even when HTTPS
        # is available. Keep API reads on HTTPS, including these pasted links.
        parsed = parsed._replace(
            scheme="https", netloc=parsed.netloc.removesuffix(":80")
        )
    endpoint(urlunsplit(parsed._replace(query="", fragment="")))
    origin(urlunsplit(parsed))  # Validate the port as well as the HTTPS endpoint.
    if "\\" in value or parsed.hostname in (".", ".."):
        raise WorkflowError("Invalid Patchwork URL")
    return parsed


@dataclass(frozen=True)
class Source:
    api_url: str
    resource: str
    object_id: int | None = None
    project: str | None = None
    msgid: str | None = None
    series_hint: int | None = None
    url: str | None = None


def parse_source(value=None, *, series_id=None, server_url=None, api_url=None):
    """Select a server from a URL, or use explicit defaults for a numeric ID."""
    if value is not None and series_id is not None:
        raise WorkflowError("Supply a Patchwork URL/ID or --series, not both")
    if value is None:
        value = series_id
    if value is None:
        raise WorkflowError("Supply a Patchwork URL or --series ID")
    if any(ord(c) < 32 for c in str(value)):
        raise WorkflowError("Invalid Patchwork URL or ID")
    value = str(value).strip()
    if server_url:
        server_url = endpoint(urlunsplit(checked_url(server_url, website=True)))
    if api_url:
        checked_url(api_url)
        api_url = endpoint(api_url)
    if value.isascii() and value.isdecimal():
        if server_url and api_url and origin(server_url) != origin(api_url):
            raise WorkflowError("Patchwork server and API must use the same origin")
        return Source(
            api_url or (server_url or DEFAULT_SERVER) + "/api/1.2",
            "series",
            positive_id(value),
        )

    parsed = checked_url(value, website=True)
    query = parse_qs(parsed.query, keep_blank_values=True)
    hint = None
    if "series" in query:
        if len(query["series"]) != 1:
            raise WorkflowError("URL must select exactly one series")
        hint = positive_id(query["series"][0])
    patterns = (
        r"(?P<prefix>.*?)/api(?:/(?P<version>1\.[0-9]+))?/(?P<kind>series|patches|covers)/(?P<id>[0-9]+)/?",
        r"(?P<prefix>.*?)/project/(?P<project>[^/]+)/(?P<kind>patch|cover)/(?P<msgid>.+?)(?:/(?P<export>mbox|raw))?/?",
        r"(?P<prefix>.*?)/(?P<kind>series|patch|cover)/(?P<id>[0-9]+)(?:/(?P<export>mbox|raw))?/?",
        r"(?P<prefix>.*?)/project/(?P<project>[^/]+)(?:/list)?/?",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, parsed.path)
        if match:
            break
    else:
        raise WorkflowError(
            "Use a series, patch or cover-letter URL, or a project list with ?series=ID"
        )
    parts = match.groupdict()
    kind = parts.get("kind", "series")
    resource = {"patch": "patches", "cover": "covers"}.get(kind, kind)
    if parts.get("export") == "raw" and resource != "patches":
        raise WorkflowError("Only patch URLs have raw exports")
    ident = positive_id(parts["id"]) if parts.get("id") else None
    if not parts.get("kind"):
        if hint is None:
            raise WorkflowError(
                "A project list needs ?series=ID; choose one complete series"
            )
        ident = hint
    if resource == "series" and hint is not None and hint != ident:
        raise WorkflowError("Conflicting series IDs in the URL")
    prefix = parts["prefix"]
    if any(unquote(p) in (".", "..") or "/" in unquote(p) for p in prefix.split("/")):
        raise WorkflowError("Invalid Patchwork installation path")
    site = urlunsplit((parsed.scheme, parsed.netloc, prefix, "", "")).rstrip("/")
    if server_url and (
        origin(server_url) != origin(site)
        or urlsplit(server_url).path.rstrip("/") != prefix
    ):
        raise WorkflowError("--patchwork-server conflicts with the input URL")
    if api_url and origin(api_url) != origin(site):
        raise WorkflowError("--patchwork-api must use the input URL's origin")
    project = unquote(parts["project"]) if parts.get("project") else None
    msgid = unquote(parts["msgid"]) if parts.get("msgid") else None
    if project and (project in (".", "..") or "/" in project):
        raise WorkflowError("Invalid Patchwork project")
    if msgid and (
        any(ord(c) < 32 for c in msgid) or msgid.startswith("<") or msgid.endswith(">")
    ):
        raise WorkflowError("Use the Message-ID as it appears in the Patchwork URL")
    return Source(
        api_url or site + "/api/" + (parts.get("version") or "1.2"),
        resource,
        ident,
        project,
        msgid,
        hint,
        value,
    )
