import os

from ducktest import Project, default
from ducktest.resources import StorageDef
from ducktest.services.s3local import s3_local

LOCAL = StorageDef("local", os.path.join(os.path.dirname(os.path.abspath(__file__)), "local"))
# The run uses a server that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else starts one in docker.
S3 = s3_local()
LAKE = S3.storage("lake", "lake")

project = Project(
    storages=[LOCAL, LAKE],
    credentials=[S3.credential("keys", scopes=[LAKE])],
    # Each cell binds the storage and writes under it; only the s3 cell needs httpfs.
    matrix={
        "storage": {
            "local": {"storage": LOCAL, "temp": LOCAL},
            "s3": {"storage": LAKE, "temp": LAKE, "requires": ["httpfs"]},
        }
    },
    rules=[default(matrix=["storage"])],
)
