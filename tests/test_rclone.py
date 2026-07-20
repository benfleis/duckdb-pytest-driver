"""Offline self-tests for the rclone runner (P3): Remote serialization + access-mode prefixes +
conf dump + azurite's remote. The verbs (subprocess rclone) need a live endpoint — verified on
Ben's box against a booted Azurite, not here.
"""

import pytest

from ducktest.tools.rclone import Remote, object_prefix, write_conf
from ducktest.resources.azurite import AZURITE_RCLONE, rclone_remote


def test_inline_form_is_file_free_and_bool_lowercased():
    r = Remote("azurite", {"type": "azureblob", "use_emulator": True})
    assert r.inline("cont/pfx") == ":azureblob,use_emulator=true:cont/pfx"
    assert r.inline() == ":azureblob,use_emulator=true:"


def test_inline_requires_type():
    with pytest.raises(ValueError, match="must include a 'type'"):
        Remote("x", {"use_emulator": True}).inline()


def test_stanza_form():
    r = Remote("azurite", {"type": "azureblob", "use_emulator": True})
    assert r.stanza() == "[azurite]\ntype = azureblob\nuse_emulator = true\n"


def test_target_picks_inline_vs_named_by_config():
    r = Remote("azurite", {"type": "azureblob", "use_emulator": True})
    assert r.target("c/p") == ":azureblob,use_emulator=true:c/p"  # no config -> inline
    assert r.target("c/p", config="/tmp/x.conf") == "azurite:c/p"  # config -> named remote


def test_write_conf(tmp_path):
    a = Remote("azurite", {"type": "azureblob", "use_emulator": True})
    b = Remote("s3", {"type": "s3", "provider": "Minio"})
    path = write_conf([a, b], str(tmp_path / "rclone.conf"))
    text = open(path).read()
    assert "[azurite]" in text and "[s3]" in text and "use_emulator = true" in text


def test_object_prefix_access_modes():
    assert object_prefix("cont", "ds", access="ro") == "cont/ds"  # shared, seed-once
    assert object_prefix("cont", "ds", access="rw", token="tok123") == "cont/tok123/ds"  # per-test


def test_object_prefix_rw_needs_token():
    with pytest.raises(ValueError, match="rw access requires a token"):
        object_prefix("cont", "ds", access="rw")


def test_object_prefix_bad_access():
    with pytest.raises(ValueError, match="must be 'ro' or 'rw'"):
        object_prefix("cont", "ds", access="wat")


def test_azurite_remote_default_is_emulator():
    assert rclone_remote() is AZURITE_RCLONE
    assert rclone_remote({"endpoint": "http://127.0.0.1:10000"}) is AZURITE_RCLONE


def test_azurite_remote_moved_instance_uses_account_key():
    r = rclone_remote({"endpoint": "http://host.docker.internal:10000", "account": "devstoreaccount1", "key": "K"})
    assert r.params["type"] == "azureblob"
    assert r.params["account"] == "devstoreaccount1" and r.params["endpoint"].startswith("http://host")
    assert "use_emulator" not in r.params  # moved => explicit account/key (config-file, not inline)
