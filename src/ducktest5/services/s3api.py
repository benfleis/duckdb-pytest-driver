"""The S3 API calls a local S3 server's buckets need -- create, head, delete (also of a bucket that is
not empty), put a policy -- as signed HTTP requests, with the standard library only.

Path-style addressing (`<endpoint>/<bucket>`), AWS Signature Version 4 over the whole payload."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _quote(text, safe="-_.~"):
    return urllib.parse.quote(text, safe=safe)


def sign(method, url, headers, payload_hash, *, key_id, secret, region, service="s3", now=None):
    """`headers` with `x-amz-date` and `Authorization` added: AWS Signature Version 4. Every header in
    `headers`, and `host`, is signed; `payload_hash` is the hex sha256 of the body."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    parts = urllib.parse.urlsplit(url)
    signed = {k.lower(): str(v).strip() for k, v in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = stamp
    query = sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    canonical_query = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in query)
    names = sorted(signed)
    canonical = "\n".join(
        [
            method,
            parts.path or "/",  # sent encoded once, as S3 signs it
            canonical_query,
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = f"AWS4{secret}".encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in headers.items()}
    out["x-amz-date"] = stamp
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return out


class S3ApiError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(f"{code or status}: {message}" if message else f"{code or status}")
        self.status, self.code = status, code


def public_read_policy(bucket: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject", "s3:ListBucket"],
                    "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                }
            ],
        }
    )


class S3Api:
    """One S3 server, reached at `endpoint` with `keys()` -> (key id, secret)."""

    def __init__(self, endpoint: str, region: str, keys: Callable[[], tuple[str, str]], timeout: float = 30):
        self.endpoint, self.region, self.keys, self.timeout = endpoint.rstrip("/"), region, keys, timeout

    def request(self, method, path, query="", body=b"", signed=True):
        """(status, body) of one request; an error status raises S3ApiError."""
        url = f"{self.endpoint}{path}" + (f"?{query}" if query else "")
        headers = {"x-amz-content-sha256": hashlib.sha256(body).hexdigest()}
        if signed:
            key_id, secret = self.keys()
            headers = sign(method, url, headers, headers["x-amz-content-sha256"], key_id=key_id, secret=secret,
                           region=self.region)  # fmt: skip
        data = body if method in ("PUT", "POST") else None  # a PUT with no body still says Content-Length: 0
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            raise S3ApiError(exc.code, *_error(exc.read() if method != "HEAD" else b"")) from None

    @staticmethod
    def _bucket(bucket):
        return "/" + _quote(bucket)

    def bucket_exists(self, bucket) -> bool:
        try:
            self.request("HEAD", self._bucket(bucket))
            return True
        except S3ApiError as exc:
            if exc.status == 404:
                return False
            raise

    def create_bucket(self, bucket) -> None:
        """Make `bucket`; one this account has already is not an error."""
        try:
            self.request("PUT", self._bucket(bucket))
        except S3ApiError as exc:
            # A local server has one account: a bucket that is there is this one's.
            if exc.code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise

    def keys_in(self, bucket):
        """Every object key in `bucket`."""
        token = None
        while True:
            query = "list-type=2" + (f"&continuation-token={_quote(token)}" if token else "")
            _, body = self.request("GET", self._bucket(bucket), query)
            root = ET.fromstring(body)
            yield from (e.text for e in root.iter(f"{_NS}Key"))
            token = root.findtext(f"{_NS}NextContinuationToken")
            if root.findtext(f"{_NS}IsTruncated") != "true" or not token:
                return

    def delete_bucket(self, bucket, force=False) -> None:
        """Remove `bucket`; `force` removes what is in it first. One that is not there is not an error."""
        try:
            if force:
                for key in list(self.keys_in(bucket)):
                    self.request("DELETE", f"{self._bucket(bucket)}/{_quote(key, safe='/-_.~')}")
            self.request("DELETE", self._bucket(bucket))
        except S3ApiError as exc:
            if exc.code != "NoSuchBucket" and exc.status != 404:
                raise

    def make_public(self, bucket) -> None:
        """Anyone may list `bucket` and read its objects, without keys."""
        self.request("PUT", self._bucket(bucket), "policy=", public_read_policy(bucket).encode())

    def is_public(self, bucket) -> bool:
        """Whether the server lists `bucket` for a request with no keys."""
        try:
            self.request("GET", self._bucket(bucket), "list-type=2&max-keys=1", signed=False)
            return True
        except S3ApiError:
            return False


def _error(body):
    """(code, message) of an S3 error document; empty where there is none."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", body.decode(errors="replace")[:200]
    return root.findtext("Code") or "", root.findtext("Message") or ""
