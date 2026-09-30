from ducktest5 import test


@test()
def rule_matched_body(ctx):
    duck = ctx.client("duck")
    duck.expects("SELECT '{TEMP_DIR}' LIKE '%/scratch_a/%'", [(True,)])
    duck.oks("COPY (SELECT 42 AS answer) TO '{TEMP_DIR}/t' (FORMAT csv)")
    duck.expects("SELECT answer FROM read_csv('{TEMP_DIR}/t')", [(42,)])
