"""The public API of a conf: the makers on the classes, and the module-level verbs that call them."""

from ducktest.conf import Rule, default, match


# -----------------------------------------------------------------------------
# Rules: the long form on the class, the verbs as convenience
#


def test_default_is_rule_default():
    assert default(tags=["x"], values={"A": "1"}) == Rule.default(tags=["x"], values={"A": "1"})
    assert Rule.default().label == "default"


def test_match_is_rule_match():
    assert match("sql/*.test", requires_env="X", tags=["y"]) == Rule.match("sql/*.test", requires_env="X", tags=["y"])
    assert Rule.match("a.test").label == "match 'a.test'"
