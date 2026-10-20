"""ducktest: scan -> plan -> provision -> exec -> cleanup, with no pytest underneath.

A conf or a body imports what it needs from here, in one line. Nouns are classes with `Def` (a
declaration, not the running thing), each with its makers on the class; verbs are functions."""

from .conf import Project, Rule, Rules, default, match
from .declare import test
from .engines.duckdb import DuckEngineDef
from .engines.spark import SparkEngineDef
from .outcomes import Skip, skip
from .resources import (
    AzureCredentialDef,
    AzureStorageDef,
    EngineDef,
    LocalStorageDef,
    S3CredentialDef,
    S3StorageDef,
    ServiceDef,
    StorageDef,
    default_storage,
    from_env,
)
from .services.azure import AzureServiceDef, container
from .services.http import HttpServerDef
from .services.s3 import S3ServiceDef, bucket
from .sqltext import L

__all__ = [
    "AzureCredentialDef",
    "AzureServiceDef",
    "AzureStorageDef",
    "DuckEngineDef",
    "EngineDef",
    "HttpServerDef",
    "L",
    "LocalStorageDef",
    "Project",
    "Rule",
    "Rules",
    "S3CredentialDef",
    "S3ServiceDef",
    "S3StorageDef",
    "ServiceDef",
    "Skip",
    "SparkEngineDef",
    "StorageDef",
    "bucket",
    "container",
    "default",
    "default_storage",
    "from_env",
    "match",
    "skip",
    "test",
]
