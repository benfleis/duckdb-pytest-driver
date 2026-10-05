from ducktest5 import test

# A parameter of this test's own: each value is a cell, and its fields are template values.
CASES = {
    "small": {"a": 2, "b": 3, "product": 6},
    "large": {"a": 1000, "b": 1000, "product": 1000000},
}


@test(matrix=[{"case": CASES}])
def matrix_body(ctx):
    duck = ctx.client("duck")
    duck.expects("SELECT {a} * {b}", [ctx.params.case["product"]])
    # `{storage}` is the name of this cell's value of the project's parameter: `a` or `b`.
    duck.expects("SELECT '{TEMP_DIR}' LIKE '%/scratch_{storage}/%'", [(True,)])
