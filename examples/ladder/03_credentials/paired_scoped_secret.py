from ducktest5 import test

SECRETS = {"key": [("ducktest_fake_s3", "s3", ["s3://ladder-fake"])], "none": []}


@test()
def paired_scoped_secret(ctx):
    ctx.client("duckdb").expects("SELECT name, type, scope FROM duckdb_secrets()", SECRETS[ctx.env.auth])
    ctx.run_test()
