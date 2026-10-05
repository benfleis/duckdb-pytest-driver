from ducktest5 import match
from ducktest5.resources import S3CredentialDef, StorageDef, from_env

# Nothing reads this bucket: the tests only look at the secret made for it.
STORAGES = [StorageDef("fake_s3", "s3://ladder-fake")]

CREDENTIALS = [
    S3CredentialDef(
        "fake_key",
        sources=[from_env({"LADDER_FAKE_KEY_ID": "key_id", "LADDER_FAKE_SECRET": "secret"})],
        scopes=["fake_s3"],
    )
]

# A test that binds the bucket holds its credential; no_secret.test binds nothing, so it holds none.
RULES = [match("*scoped_*", storage="fake_s3")]
