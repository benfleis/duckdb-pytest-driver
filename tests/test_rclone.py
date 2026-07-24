"""Offline self-tests for the rclone runner (P3): Remote serialization + access-mode prefixes +
conf dump + azurite's remote. The verbs (subprocess rclone) need a live endpoint — verified on
Ben's box against a booted Azurite, not here.
"""

import os

import pytest

from ducktest.tools.rclone import Remote, _effective_config, _inline_safe, object_prefix, write_conf
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
    # blob_endpoint (account-suffixed), not the bare endpoint -- rclone's azureblob backend addresses
    # containers as <endpoint>/<container> when `account` is also given; a bare host:port endpoint
    # gives every request a 400 from azurite (found live, a real reverse-port-mapped instance).
    r = rclone_remote(
        {
            "endpoint": "http://host.docker.internal:10000",
            "blob_endpoint": "http://host.docker.internal:10000/devstoreaccount1",
            "account": "devstoreaccount1",
            "key": "K",
        }
    )
    assert r.params["type"] == "azureblob"
    assert r.params["account"] == "devstoreaccount1"
    assert r.params["endpoint"] == "http://host.docker.internal:10000/devstoreaccount1"
    assert "use_emulator" not in r.params  # moved => explicit account/key (config-file, not inline)


def test_inline_safe_true_for_emulator_defaults():
    assert _inline_safe(Remote("azurite", {"type": "azureblob", "use_emulator": True}))


def test_inline_safe_false_for_a_real_key_or_url():
    # a base64 key (/, =) and a URL endpoint (:, /) both break inline `:type,k=v:path` parsing --
    # found live: a moved azurite instance's mkdir/sync silently failed with "no Host in request URL".
    assert not _inline_safe(Remote("azurite", {"type": "azureblob", "key": "Eby8v/dM0=="}))
    assert not _inline_safe(Remote("azurite", {"type": "azureblob", "endpoint": "http://host:10000"}))


def test_effective_config_stays_inline_when_safe():
    r = Remote("azurite", {"type": "azureblob", "use_emulator": True})
    with _effective_config(r, config=None) as cfg:
        assert cfg is None


def test_effective_config_honors_explicit_config_even_if_safe():
    r = Remote("azurite", {"type": "azureblob", "use_emulator": True})
    with _effective_config(r, config="/tmp/explicit.conf") as cfg:
        assert cfg == "/tmp/explicit.conf"


def test_effective_config_auto_writes_and_cleans_up_temp_conf_when_unsafe():
    r = Remote("azurite", {"type": "azureblob", "account": "a", "key": "K/ey=="})
    captured = {}
    with _effective_config(r, config=None) as cfg:
        assert cfg is not None
        assert os.path.isfile(cfg)
        captured["path"] = cfg
        assert "[azurite]" in open(cfg).read()
    assert not os.path.exists(captured["path"])  # cleaned up on exit
