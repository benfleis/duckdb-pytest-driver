"""Resources: what a test needs and the run provides, each as a `FooDef` and the `Foo` made from it."""

from .base import (  # noqa: F401
    ALWAYS,
    EXCLUSIVE,
    NEVER,
    ON_FAILURE,
    RO,
    RW,
    SHARED_RO,
    SHARED_RW,
    Output,
    Resource,
    ResourceDef,
)
from .contract import check_contract  # noqa: F401
from .credential import CredentialDef, env, pick_credentials  # noqa: F401
from .storage import StorageDef, scheme_of  # noqa: F401
