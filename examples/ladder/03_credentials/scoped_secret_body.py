from typing import NamedTuple

from ducktest5 import test


class Secret(NamedTuple):
    """A row of `SELECT name, type, scope FROM duckdb_secrets()`."""

    name: str
    type: str
    scope: list


# The secrets DuckDB holds for each `auth` value: the key gives one, scoped to the fake bucket.
SECRETS_BY_AUTH = {
    "key": [Secret(name="ducktest_fake_s3", type="s3", scope=["s3://ladder-fake"])],
    "none": [],
}


@test()
def scoped_secret_body(ctx):
    ctx.client("duckdb").expects("SELECT name, type, scope FROM duckdb_secrets()", SECRETS_BY_AUTH[ctx.values.auth])
