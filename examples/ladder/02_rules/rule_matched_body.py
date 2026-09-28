from ducktest5 import test


@test()
def rule_matched_body(ctx):
    assert "/scratch_a/" in ctx.location("answer.csv")
