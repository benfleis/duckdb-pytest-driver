import os

from ducktest import Project, default, match
from ducktest.services.http import HttpServerDef
from ducktest.services.s3 import bucket, S3ServiceDef

# Files the run uploads to test-bucket, as httpfs CI does; see s3_seed/README.md.
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s3_seed")

# The buckets of httpfs CI. The run makes them and removes them after; it uses the S3 server that
# answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else it starts one in docker.
S3 = S3ServiceDef.rustfs(
    buckets=[
        bucket("test-bucket", seed=[(SEED, "")]),
        bucket("test-bucket-2"),
        bucket("test-bucket-public", public=True),
    ]
)
# httpfs builds no aws extension, so its secret names the keys instead of the chain reading them.
S3_KEYS = S3.credential("s3_keys", scopes=[], provider="config")
# A small HTTP server the run starts in its own process: it serves a directory tests may write to.
PYHTTP = HttpServerDef("pyhttp")
# Known gap: process_env does not fill {s3.host_port} as values do, so this repeats the run's address.
S3_HOST_PORT = (
    f"{os.environ.get('DUCKTEST_SERVICE_HOST', '127.0.0.1')}:{os.environ.get('DUCKTEST_S3_LOCAL_PORT', '9000')}"
)

project = Project(
    credentials=[S3_KEYS],
    services=[PYHTTP],
    # Two CI configs (test/configs/httpfs_*.json) as one parameter: each test runs once per client.
    matrix={
        "httpfs_client": {
            "curl": {"settings": {"httpfs_client_implementation": "curl"}},
            "httplib": {"settings": {"httpfs_client_implementation": "httplib"}},
        }
    },
    rules=[
        # Every file loads httpfs and runs once per client. S3 URLs put the bucket in the path,
        # http://172.18.0.1:9999/test-bucket/key, not in the host, http://test-bucket.172.18.0.1:9999/key:
        # CI makes such host names resolve in /etc/hosts; the run reaches its server by address.
        default(requires=["httpfs"], settings={"s3_url_style": "path"}, matrix=["httpfs_client"]),
        # A file with `require-env S3_TEST_SERVER_AVAILABLE` gets the S3 keys and the names it requires.
        match(
            "**",
            requires_env="S3_TEST_SERVER_AVAILABLE",
            credential=S3_KEYS,
            values={"S3_TEST_SERVER_AVAILABLE": "1", "S3_ATTACH_DB": "s3://test-bucket/presigned/attach.db"},
            process_env={"DUCKDB_S3_ENDPOINT": S3_HOST_PORT, "DUCKDB_S3_USE_SSL": "false"},  # httpfs reads them
        ),
        # Both clients of a file write one file in the served directory, so they take turns.
        match(
            "**",
            requires_env="PYTHON_HTTP_SERVER_URL",
            service=PYHTTP,
            mutex="pyhttp",
            values={"PYTHON_HTTP_SERVER_URL": "{pyhttp.url}", "PYTHON_HTTP_SERVER_DIR": "{pyhttp.dir}"},
        ),
    ],
)
