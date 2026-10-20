from ducktest import Project, match
from ducktest.resources import S3CredentialDef, StorageDef, from_env

# Nothing reads this bucket: the tests only look at the secret made for it.
FAKE_S3 = StorageDef("fake_s3", "s3://ladder-fake")
FAKE_KEY = S3CredentialDef(
    "fake_key",
    sources=[from_env({"LADDER_FAKE_KEY_ID": "key_id", "LADDER_FAKE_SECRET": "secret"})],
    scopes=[FAKE_S3],
)

project = Project(
    storages=[FAKE_S3],
    credentials=[FAKE_KEY],
    # A test that binds the bucket holds its credential; no_secret.test binds nothing, so it holds none.
    rules=[match("*scoped_*", storage=FAKE_S3)],
)
