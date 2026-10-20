import os

from ducktest import LocalStorageDef, Project, S3ServiceDef, default

LOCAL_DIR = LocalStorageDef("local_dir", os.path.join(os.path.dirname(os.path.abspath(__file__)), "local"))
# The run uses a server that answers at DUCKTEST_SERVICE_HOST:DUCKTEST_S3_LOCAL_PORT, else starts one in docker.
LOCAL_S3 = S3ServiceDef.rustfs("local_s3")
LOCAL_LAKE = LOCAL_S3.storage("local_lake", path="lake")
LOCAL_KEYS = LOCAL_S3.credential("local_keys", scopes=[LOCAL_LAKE])

project = Project(
    storages=[LOCAL_DIR, LOCAL_LAKE],
    credentials=[LOCAL_KEYS],
    # Each cell binds the storage and writes under it; only the s3 cell needs httpfs.
    matrix={
        "storage": {
            "local": {"storage": LOCAL_DIR, "temp": LOCAL_DIR},
            "s3": {"storage": LOCAL_LAKE, "temp": LOCAL_LAKE, "requires": ["httpfs"]},
        }
    },
    rules=[default(matrix=["storage"])],
)
