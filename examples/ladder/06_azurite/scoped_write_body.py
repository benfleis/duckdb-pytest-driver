from ducktest import test


@test()
def scoped_write_body(ctx):
    duck = ctx.client("duck")
    assert ctx.credential("local_keys").secret_name == "ducktest_local_keys"
    duck.expects(
        "SELECT type, provider, current_setting('azure_storage_connection_string') = '' FROM duckdb_secrets()",
        [("azure", "config", True)],
    )
    duck.oks("COPY (SELECT 42 AS answer) TO '{TEMP_DIR}/answer.csv'")
    duck.expects("SELECT answer FROM read_csv('{TEMP_DIR}/answer.csv')", [(42,)])
