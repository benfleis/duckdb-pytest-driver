from ducktest5 import test


@test()
def scoped_write_body(ctx):
    duck = ctx.client("duck")
    assert ctx.credential("keys").secret_name is None  # a connection string is a setting, not a secret
    duck.expects(
        "SELECT current_setting('azure_storage_connection_string') <> '', count(*) FROM duckdb_secrets()",
        [(True, 0)],
    )
    duck.oks("COPY (SELECT 42 AS answer) TO '{TEMP_DIR}/answer.csv'")
    duck.expects("SELECT answer FROM read_csv('{TEMP_DIR}/answer.csv')", [(42,)])
