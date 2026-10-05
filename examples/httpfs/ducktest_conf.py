import os

from ducktest5 import Variant, default, match
from ducktest5.services.http import HttpServerDef
from ducktest5.services.s3local import bucket, s3_local

# Files the run uploads to test-bucket, as httpfs CI does; see s3_seed/README.md.
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s3_seed")
PYHTTP_DIR = os.environ.get("PYTHON_HTTP_SERVER_DIR", "/tmp/python_test_server")

# The buckets of httpfs CI. The run makes them and removes them after; it uses the S3 server that
# answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else it starts one in docker.
S3 = s3_local(
    buckets=[
        bucket("test-bucket", seed=[(SEED, "")]),
        bucket("test-bucket-2"),
        bucket("test-bucket-public", public=True),
    ]
)
CREDENTIALS = [S3.credential("s3_keys", scopes=[])]
# CI starts `python3 -m http.server 8008` in PYHTTP_DIR; the run uses it and does not start it.
SERVICES = [HttpServerDef("pyhttp").as_existing(address="http://127.0.0.1:8008")]

# Two CI configs (test/configs/httpfs_*.json) as one axis: each test runs once per client.
AXES = {
    "client": [
        Variant("curl", settings={"httpfs_client_implementation": "curl"}),
        Variant("httplib", settings={"httpfs_client_implementation": "httplib"}),
    ]
}

# Known gap: process_env does not fill {s3.host_port} as values do, so this repeats the run's address.
S3_HOST_PORT = (
    f"{os.environ.get('DUCKTEST_SERVICE_HOST', '127.0.0.1')}:{os.environ.get('DUCKTEST_S3_LOCAL_PORT', '9000')}"
)

RULES = [
    # Path style: CI gives each bucket a host name in /etc/hosts; there is no such name here.
    default(requires=["httpfs"], settings={"s3_url_style": "path"}, axes=["client"]),
    # A file with `require-env S3_TEST_SERVER_AVAILABLE` gets the S3 keys and the names it requires.
    match(
        "**",
        requires_env="S3_TEST_SERVER_AVAILABLE",
        credential="s3_keys",
        values={"S3_TEST_SERVER_AVAILABLE": "1", "S3_ATTACH_DB": "s3://test-bucket/presigned/attach.db"},
        process_env={"DUCKDB_S3_ENDPOINT": S3_HOST_PORT, "DUCKDB_S3_USE_SSL": "false"},  # httpfs reads them
    ),
    # Both clients of a file write one file in PYHTTP_DIR, so they take turns.
    match(
        "**",
        requires_env="PYTHON_HTTP_SERVER_URL",
        service="pyhttp",
        mutex="pyhttp",
        values={"PYTHON_HTTP_SERVER_URL": "{pyhttp.url}", "PYTHON_HTTP_SERVER_DIR": PYHTTP_DIR},
    ),
]
