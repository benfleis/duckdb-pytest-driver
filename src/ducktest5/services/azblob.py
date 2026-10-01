"""The Blob service calls a local Azure storage server's containers need -- create, read the
properties of, delete, open to anonymous reads -- as requests signed with the account key
(SharedKey), with the standard library only.

The endpoint names the account in its path (`http://host:10000/devstoreaccount1`), as Azurite serves
it; a container is `<endpoint>/<container>`."""

from __future__ import annotations

import base64
import datetime
import email.utils
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

API_VERSION = "2021-08-06"
PUBLIC_LEVELS = ("blob", "container")  # anonymous read of blobs; and listing them too

_STANDARD = (
    "content-encoding",
    "content-language",
    "content-length",
    "content-md5",
    "content-type",
    "date",
    "if-modified-since",
    "if-match",
    "if-none-match",
    "if-unmodified-since",
    "range",
)


def string_to_sign(method, url, headers, account):
    """What SharedKey signs (Blob service, version 2015-02-21 and later). The account comes first in
    the resource and again in an emulator's path: `/devstoreaccount1/devstoreaccount1/data`."""
    parts = urllib.parse.urlsplit(url)
    given = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    if given.get("content-length") == "0":
        del given["content-length"]
    standard = [given.get(name, "") for name in _STANDARD]
    ms = "".join(f"{name}:{given[name]}\n" for name in sorted(given) if name.startswith("x-ms-"))
    query = {}
    for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        query.setdefault(name.lower(), []).append(value)
    resource = f"/{account}{parts.path or '/'}" + "".join(
        f"\n{name}:{','.join(sorted(values))}" for name, values in sorted(query.items())
    )
    return "\n".join([method, *standard]) + "\n" + ms + resource


def sign(method, url, headers, *, account, key, now=None):
    """`headers` with `x-ms-date`, `x-ms-version` (where not given) and `Authorization` added."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = {"x-ms-date": email.utils.format_datetime(now, usegmt=True), "x-ms-version": API_VERSION, **headers}
    digest = hmac.new(base64.b64decode(key), string_to_sign(method, url, out, account).encode(), hashlib.sha256)
    out["Authorization"] = f"SharedKey {account}:{base64.b64encode(digest.digest()).decode()}"
    return out


class BlobApiError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(f"{code or status}: {message}" if message else f"{code or status}")
        self.status, self.code = status, code


class BlobApi:
    """One account of a Blob service, reached at `endpoint` with `key()`, the account key (base64)."""

    def __init__(self, endpoint: str, account: str, key: Callable[[], str], timeout: float = 30):
        self.endpoint, self.account, self.key, self.timeout = endpoint.rstrip("/"), account, key, timeout

    def request(self, method, container, query, headers=None):
        """(status, headers) of one request to `container`; an error status raises BlobApiError."""
        url = f"{self.endpoint}/{urllib.parse.quote(container, safe='')}?{query}"
        headers = sign(method, url, dict(headers or {}), account=self.account, key=self.key())
        # No body: http.client still says Content-Length: 0 for a PUT, and urllib adds no Content-Type,
        # which SharedKey would sign.
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
                return resp.status, resp.headers
        except urllib.error.HTTPError as exc:
            body = exc.read() if method != "HEAD" else b""
            code, message = _error(body)
            raise BlobApiError(exc.code, exc.headers.get("x-ms-error-code") or code, message) from None

    def properties(self, container):
        """The container's headers, or None when it is not there."""
        try:
            return self.request("HEAD", container, "restype=container")[1]
        except BlobApiError as exc:
            if exc.status == 404:
                return None
            raise

    def container_exists(self, container) -> bool:
        return self.properties(container) is not None

    def public_access(self, container) -> str | None:
        """'blob', 'container', or None: anonymous requests may not read it."""
        found = self.properties(container)
        return (found.get("x-ms-blob-public-access") or None) if found is not None else None

    def create_container(self, container) -> None:
        """Make `container`; one that is there already is not an error."""
        try:
            self.request("PUT", container, "restype=container")
        except BlobApiError as exc:
            if exc.code != "ContainerAlreadyExists":
                raise

    def delete_container(self, container) -> None:
        """Remove `container` with what is in it; one that is not there is not an error."""
        try:
            self.request("DELETE", container, "restype=container")
        except BlobApiError as exc:
            if exc.code != "ContainerNotFound" and exc.status != 404:
                raise

    def set_public_access(self, container, level) -> None:
        """Open `container` to anonymous reads: `level` is one of PUBLIC_LEVELS."""
        if level not in PUBLIC_LEVELS:
            raise ValueError(f"public access {level!r}; it is one of {PUBLIC_LEVELS}")
        self.request("PUT", container, "restype=container&comp=acl", {"x-ms-blob-public-access": level})


def _error(body):
    """(code, message) of a Blob service error document; empty where there is none."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", body.decode(errors="replace")[:200]
    return root.findtext("Code") or "", (root.findtext("Message") or "").split("\n")[0]
