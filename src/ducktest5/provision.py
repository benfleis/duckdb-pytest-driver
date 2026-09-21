"""How a run gets each service a selected test needs. The mode is the run's choice, per service:

    managed    start it, use it, stop it                 the default when the service has `start`
    existing   find it running, never stop it            `--existing-service ID[=URL|={json}]`, or
                                                         DUCKTEST_EXISTING_SERVICE_<ID>; and always
                                                         when the service has no `start`
    keep       find it, else start it; never stop it     `--keep-services`

In every mode the instance must answer `alive`, then `setup` runs (idempotent), and what the tests
wrote is cleaned by the run that wrote it. Only `managed` ends with `stop`. `keep` prints the
argument that lets the next run find the instance.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass

from .binary import UsageError
from .host import wait_until

MANAGED, EXISTING, KEPT = "managed", "existing", "kept"
MASK = "***"


def _key(service_id):
    return service_id.replace("-", "_").lower()


def existing_overrides(service_id, cli_entries):
    """The overrides for an existing instance of `service_id`, or None when none was named.
    Entry grammar: `ID` (the service's defaults), `ID=URL` ({"endpoint": URL}), `ID={json}`.
    The command line wins over DUCKTEST_EXISTING_SERVICE_<ID>."""
    for entry in cli_entries or ():
        name, _, value = entry.partition("=")
        if _key(name) == _key(service_id):
            return _parse(value)
    value = os.environ.get(f"DUCKTEST_EXISTING_SERVICE_{_key(service_id).upper()}")
    return None if value is None else _parse(value)


def _parse(value):
    value = value.strip()
    if not value:
        return {}
    if value.startswith("{"):
        overrides = json.loads(value)
        if masked := sorted(k for k, v in overrides.items() if v == MASK):
            raise UsageError(
                f"an existing service with {masked} = {MASK!r}: put the value in, or set it in the environment"
            )
        return overrides
    return {"endpoint": value}


@dataclass
class Provisioned:
    service: object
    block: dict
    mode: str
    started: bool = False  # this process started the instance; one it found is not ours to stop

    def redacted(self):
        return {k: (MASK if k in self.service.secrets else v) for k, v in self.block.items()}


def provision(service, *, existing=None, keep=False, ready_timeout_s=60):
    """Returns a Provisioned, or raises with the reason the service is not there."""
    started = False
    if existing is not None or service.start is None:
        block = service.block(**(existing or {}))
        if not service.alive(block):
            where = block.get("endpoint", "its default address")
            raise RuntimeError(f"existing service {service.id!r} does not answer at {where}")
        mode = EXISTING
    else:
        block = service.block()
        if keep and service.alive(block):
            mode = KEPT
        elif service.alive(block):
            # `start` begins by removing a leftover container of its name. An instance that answers
            # is not a leftover: it may be one that another run, or the user, keeps.
            raise RuntimeError(
                f"service {service.id!r} already answers at {block.get('endpoint')}. Use it with "
                f"--existing-service {service.id}, or keep it with --keep-services, or stop it first."
            )
        else:
            service.start(block)
            started = True
            mode = KEPT if keep else MANAGED
            try:
                wait_until(
                    lambda: service.alive(block),
                    ready_timeout_s,
                    lambda: f"service {service.id!r} did not come up at {block.get('endpoint')} in {ready_timeout_s}s",
                )
            except Exception:
                if service.stop:
                    service.stop(block)  # do not leak a half-booted instance
                raise
    if service.setup:
        try:
            service.setup(block)
        except BaseException:
            if mode == MANAGED and service.stop:
                service.stop(block)  # else the next run is refused: "already answers"
            raise
    return Provisioned(service, block, mode, started)


def release(provisioned):
    """Stop what this run started, unless it was asked to keep it."""
    if provisioned.mode == MANAGED and provisioned.service.stop:
        provisioned.service.stop(provisioned.block)


@contextlib.contextmanager
def _no_ducktest_environment():
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("DUCKTEST_")}
    try:
        yield
    finally:
        os.environ.update(saved)


def attach_overrides(provisioned):
    """What finds this instance again: the endpoint, and every other field that is not what the
    service gives with no DUCKTEST_* variable set, so that a shell without those variables finds the
    same instance, bucket and all. A secret is masked: it is never written down."""
    service, block = provisioned.service, provisioned.block
    with _no_ducktest_environment():
        defaults = service.block()
        overrides = {
            k: v
            for k, v in block.items()
            # a field the service works out from others is not an override: it does not come back
            if k == "endpoint" or (v != defaults.get(k) and service.block(**{k: v}).get(k) == v)
        }
    return {k: (MASK if k in service.secrets else v) for k, v in overrides.items()}


def attach_argument(provisioned):
    """The `--existing-service` argument for a kept instance, as a line to print."""
    overrides = attach_overrides(provisioned)
    masked = sorted(k for k, v in overrides.items() if v == MASK)
    note = f"  ({MASK}: a secret, not printed; put the value in, or set it in the environment)" if masked else ""
    return f"--existing-service '{provisioned.service.id}={json.dumps(overrides)}'{note}"
