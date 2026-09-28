from ducktest5 import test


@test(bind={"storage": "scratch_b"})
def own_inputs(ctx):
    assert "/scratch_b/" in ctx.location("answer.csv")
