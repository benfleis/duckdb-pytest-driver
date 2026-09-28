from ducktest5 import Variant, default
from ducktest5.resources import CredentialDef, StorageDef, from_env

# Nothing reads this bucket: the tests only look at the secret made for it.
STORAGES = [StorageDef("fake_s3", "s3://ladder-fake")]

CREDENTIALS = [
    CredentialDef(
        "fake_key",
        sources=[from_env({"LADDER_FAKE_KEY_ID": "AWS_ACCESS_KEY_ID", "LADDER_FAKE_SECRET": "AWS_SECRET_ACCESS_KEY"})],
        scopes=["fake_s3"],
    )
]

AXES = {
    "auth": [
        Variant("key", inputs=["fake_key"], values={"scoped_rows": 1}),
        # Its scope matches, so the credential is hidden by unsetting the names it delivers.
        Variant("none", env={"AWS_ACCESS_KEY_ID": None, "AWS_SECRET_ACCESS_KEY": None}, values={"scoped_rows": 0}),
    ]
}

RULES = [default(bind={"storage": "fake_s3"}, variants=["auth"])]
