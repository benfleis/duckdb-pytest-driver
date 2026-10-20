from ducktest import test


@test(storage="scratch_b", temp="scratch_b")
def own_inputs(ctx):
    assert "/scratch_b/" in ctx.location("answer.csv")
