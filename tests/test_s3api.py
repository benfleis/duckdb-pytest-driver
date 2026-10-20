"""The S3 API calls of a local S3 server: the signer against AWS's own test vector, and the calls
against a small HTTP server on the loopback that records what it was asked."""

import datetime
import http.server
import json
import threading

import pytest

from ducktest.services.s3api import EMPTY_SHA256, S3Api, S3ApiError, sign

LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <IsTruncated>{truncated}</IsTruncated>{token}
  {keys}
</ListBucketResult>"""

ERROR = "<Error><Code>{code}</Code><Message>{message}</Message></Error>"


def test_the_signature_is_the_one_aws_gives_for_its_get_vanilla_case():
    headers = sign(
        "GET",
        "https://example.amazonaws.com/",
        {},
        EMPTY_SHA256,
        key_id="AKIDEXAMPLE",
        secret="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="service",
        now=datetime.datetime(2015, 8, 30, 12, 36, 0, tzinfo=datetime.timezone.utc),
    )
    assert headers["x-amz-date"] == "20150830T123600Z"
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


class FakeServer(http.server.BaseHTTPRequestHandler):
    """Answers from `routes`: (method, path with query) -> (status, body); records each request."""

    routes, seen = {}, []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeServer.seen.append((self.command, self.path, self.headers, body))  # headers: any case
        status, out = FakeServer.routes.get((self.command, self.path), (200, b""))
        self.send_response(status)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(out)

    do_GET = do_PUT = do_HEAD = do_DELETE = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    FakeServer.routes, FakeServer.seen = {}, []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield S3Api(f"http://127.0.0.1:{httpd.server_port}", "us-east-1", lambda: ("key-id", "secret")), FakeServer
    httpd.shutdown()


def test_each_call_is_signed_and_names_its_payload(server):
    api, fake = server
    api.create_bucket("data")
    ((method, path, headers, body),) = fake.seen
    assert (method, path, body) == ("PUT", "/data", b"")
    assert headers["x-amz-content-sha256"] == EMPTY_SHA256 and headers["Content-Length"] == "0"
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=key-id/")
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date," in headers["Authorization"]


def test_a_bucket_is_there_when_head_says_so(server):
    api, fake = server
    fake.routes[("HEAD", "/gone")] = (404, b"")
    assert api.bucket_exists("data") and not api.bucket_exists("gone")


@pytest.mark.parametrize("code", ["BucketAlreadyOwnedByYou", "BucketAlreadyExists"])
def test_making_a_bucket_that_is_there_is_fine_and_another_error_is_not(server, code):
    api, fake = server
    fake.routes[("PUT", "/data")] = (409, ERROR.format(code=code, message="").encode())
    api.create_bucket("data")
    fake.routes[("PUT", "/data")] = (403, ERROR.format(code="AccessDenied", message="no").encode())
    with pytest.raises(S3ApiError, match="AccessDenied: no"):
        api.create_bucket("data")


def test_force_removes_every_object_page_by_page_then_the_bucket(server):
    api, fake = server
    first = LISTING.format(
        truncated="true",
        token="<NextContinuationToken>t+2</NextContinuationToken>",
        keys="<Contents><Key>a b</Key></Contents>",
    )
    second = LISTING.format(truncated="false", token="", keys="<Contents><Key>dir/c</Key></Contents>")
    fake.routes[("GET", "/data?list-type=2")] = (200, first.encode())
    fake.routes[("GET", "/data?list-type=2&continuation-token=t%2B2")] = (200, second.encode())
    api.delete_bucket("data", force=True)
    deletes = [path for method, path, _, _ in fake.seen if method == "DELETE"]
    assert deletes == ["/data/a%20b", "/data/dir/c", "/data"]


def test_removing_a_bucket_that_is_not_there_is_fine(server):
    api, fake = server
    fake.routes[("DELETE", "/gone")] = (404, ERROR.format(code="NoSuchBucket", message="").encode())
    api.delete_bucket("gone")


def test_a_public_bucket_gets_a_read_policy_and_is_public_when_it_lists_without_keys(server):
    api, fake = server
    api.make_public("open")
    (method, path, headers, body) = fake.seen[-1]
    assert (method, path) == ("PUT", "/open?policy=")
    (statement,) = json.loads(body)["Statement"]
    assert statement["Principal"] == {"AWS": ["*"]} and statement["Action"] == ["s3:GetObject", "s3:ListBucket"]
    assert statement["Resource"] == ["arn:aws:s3:::open", "arn:aws:s3:::open/*"]
    assert api.is_public("open") and "Authorization" not in fake.seen[-1][2]
    fake.routes[("GET", "/shut?list-type=2&max-keys=1")] = (403, b"")
    assert not api.is_public("shut")
