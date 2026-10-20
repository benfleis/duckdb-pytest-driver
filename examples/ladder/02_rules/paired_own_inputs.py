from ducktest import test


@test(storage="scratch_b", temp="scratch_b")
def paired_own_inputs(ctx):
    ctx.run_paired()
