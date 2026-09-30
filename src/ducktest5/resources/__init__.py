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
    Attribute,
    Resource,
    ResourceDef,
)
from .contract import check_contract  # noqa: F401
from .credential import CredentialDef, from_env, pick_credentials  # noqa: F401
from .service import Service, ServiceDef, ServiceNotUp  # noqa: F401
from .engine import BootedEngineDef, Engine, EngineDef  # noqa: F401
from .storage import DefaultStorage, StorageDef, local_path, scheme_of  # noqa: F401
