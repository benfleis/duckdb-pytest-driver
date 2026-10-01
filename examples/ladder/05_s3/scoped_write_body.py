from ducktest5 import test


@test()
def scoped_write_body(ctx):
    duck = ctx.client("duck")
    duck.expects("SELECT name, type, scope FROM duckdb_secrets()", [("ducktest_keys", "s3", ["s3://ducktest/lake"])])
    duck.oks("COPY (SELECT 42 AS answer) TO '{TEMP_DIR}/answer.csv'")
    duck.expects("SELECT answer FROM read_csv('{TEMP_DIR}/answer.csv')", [(42,)])
