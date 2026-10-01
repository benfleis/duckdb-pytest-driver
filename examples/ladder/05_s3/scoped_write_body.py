from ducktest5 import test


@test()
def scoped_write_body(ctx):
    duck = ctx.client("duck")
    keys = ctx.credential("keys")
    duck.expects("SELECT name, type, scope FROM duckdb_secrets()", [(keys.secret_name, keys.type, keys.scope)])
    duck.oks("COPY (SELECT 42 AS answer) TO '{TEMP_DIR}/answer.csv'")
    duck.expects("SELECT answer FROM read_csv('{TEMP_DIR}/answer.csv')", [(42,)])
