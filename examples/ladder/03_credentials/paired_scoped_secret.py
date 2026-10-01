from ducktest5 import test


@test()
def paired_scoped_secret(ctx):
    ctx.client("duckdb").expects(
        "SELECT name, type, scope FROM duckdb_secrets()", [("ducktest_fake_key", "s3", ["s3://ladder-fake"])]
    )
    ctx.run_paired()
