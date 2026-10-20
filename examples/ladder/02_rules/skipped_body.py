import shutil

from ducktest import test


@test()
def skipped_body(ctx):
    # A condition only the body can check; an input that does not come up is BLOCKED instead.
    if shutil.which("ducktest-ladder-tool") is None:
        ctx.skip("ducktest-ladder-tool is not installed")
    ctx.client("duck").expects("SELECT 1", [(1,)])
