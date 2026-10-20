"""The Blob service calls of a local Azure server: the SharedKey signer against signatures the Azure SDK
for Python (azure-storage-blob 12.31.0) gives, and the calls against a small HTTP server on the
loopback that records what it was asked."""

import datetime
import http.server
import threading

import pytest

from ducktest.services.azblob import BlobApi, BlobApiError, sign, string_to_sign
from ducktest.services.azlocal import AZURITE_ACCOUNT, AZURITE_KEY

NOW = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
ENDPOINT = "http://127.0.0.1:10000/devstoreaccount1"


@pytest.mark.parametrize(
    "method, url, headers, signature",
    [
        (
            "PUT",
            f"{ENDPOINT}/data?restype=container",
            {"Content-Length": "0"},
            "c7pDe+rcO51VGHwf+CBHkRoAd51ExuVqmK1IVw4dDs4=",
        ),
        (
            "PUT",
            f"{ENDPOINT}/open?restype=container&comp=acl",
            {"Content-Length": "0", "x-ms-blob-public-access": "container"},
            "LBDGbDdE0nj70r+iFGM3SeK+yHhhCG3vF6p4K5f9GiE=",
        ),
        (
            "GET",
            f"{ENDPOINT}/data?restype=container&comp=list&marker=a%2Bb",
            {},
            "qx4vRGGNBYbDkE3iCY6zq6lz4M3yWRPYXfK/eB0us6c=",
        ),
    ],
)
def test_the_signature_is_the_one_the_azure_sdk_gives(method, url, headers, signature):
    signed = sign(method, url, headers, account=AZURITE_ACCOUNT, key=AZURITE_KEY, now=NOW)
    assert signed["x-ms-date"] == "Thu, 01 Oct 2026 12:00:00 GMT" and signed["x-ms-version"] == "2021-08-06"
    assert signed["Authorization"] == f"SharedKey devstoreaccount1:{signature}"


def test_the_account_comes_twice_in_an_emulators_resource_and_query_names_are_sorted_and_decoded():
    headers = {"x-ms-version": "2021-08-06", "x-ms-date": "D", "Content-Length": "0"}
    text = string_to_sign(
        "GET", f"{ENDPOINT}/data?restype=container&comp=list&marker=a%2Bb", headers, "devstoreaccount1"
    )
    assert text.split("\n")[-6:] == [
        "x-ms-date:D",
        "x-ms-version:2021-08-06",
        "/devstoreaccount1/devstoreaccount1/data",
        "comp:list",
        "marker:a+b",
        "restype:container",
    ]
    assert text.split("\n")[:4] == ["GET", "", "", ""], "a Content-Length of 0 is signed as empty"


ERROR = "<?xml version='1.0'?><Error><Code>{code}</Code><Message>{message}\nRequestId:1</Message></Error>"


class FakeServer(http.server.BaseHTTPRequestHandler):
    """Answers from `routes`: (method, path with query) -> (status, headers, body); records each request."""

    routes, seen = {}, []

    def _answer(self):
        FakeServer.seen.append((self.command, self.path, self.headers))
        status, headers, out = FakeServer.routes.get((self.command, self.path), (200, {}, b""))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
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
    yield BlobApi(f"http://127.0.0.1:{httpd.server_port}/acct", "acct", lambda: AZURITE_KEY), FakeServer
    httpd.shutdown()


def test_each_call_is_signed_and_has_no_body(server):
    api, fake = server
    api.create_container("data")
    ((method, path, headers),) = fake.seen
    assert (method, path) == ("PUT", "/acct/data?restype=container")
    assert headers["Authorization"].startswith("SharedKey acct:") and headers["x-ms-version"] == "2021-08-06"
    assert headers["Content-Length"] == "0" and "Content-Type" not in headers, "SharedKey would sign a Content-Type"


def test_a_container_is_there_when_its_properties_say_so(server):
    api, fake = server
    fake.routes[("HEAD", "/acct/gone?restype=container")] = (404, {"x-ms-error-code": "ContainerNotFound"}, b"")
    assert api.container_exists("data") and not api.container_exists("gone")


def test_making_a_container_that_is_there_is_fine_and_another_error_is_not(server):
    api, fake = server
    fake.routes[("PUT", "/acct/data?restype=container")] = (
        409,
        {},
        ERROR.format(code="ContainerAlreadyExists", message="").encode(),
    )
    api.create_container("data")
    denied = ERROR.format(code="AuthorizationFailure", message="no")
    fake.routes[("PUT", "/acct/data?restype=container")] = (403, {}, denied.encode())
    with pytest.raises(BlobApiError, match="AuthorizationFailure: no$"):
        api.create_container("data")


def test_removing_a_container_that_is_not_there_is_fine(server):
    api, fake = server
    fake.routes[("DELETE", "/acct/gone?restype=container")] = (
        404,
        {},
        ERROR.format(code="ContainerNotFound", message="").encode(),
    )
    api.delete_container("gone")
    assert [(m, p) for m, p, _ in fake.seen] == [("DELETE", "/acct/gone?restype=container")]


def test_a_public_container_says_its_level_in_its_properties(server):
    api, fake = server
    api.set_public_access("open", "container")
    (method, path, headers) = fake.seen[-1]
    assert (method, path, headers["x-ms-blob-public-access"]) == (
        "PUT",
        "/acct/open?restype=container&comp=acl",
        "container",
    )
    fake.routes[("HEAD", "/acct/open?restype=container")] = (200, {"x-ms-blob-public-access": "container"}, b"")
    assert api.public_access("open") == "container" and api.public_access("shut") is None
    with pytest.raises(ValueError, match="public access 'all'"):
        api.set_public_access("open", "all")
