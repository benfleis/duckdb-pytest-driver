"""How a run gets each service its selected tests need. The mode is the run's choice, per service:

    managed    create it, use it, end it                 the default
    existing   adopt the one running at an address;      `--existing-service ID=URL`, or
               never start it, never end it              DUCKTEST_EXISTING_SERVICE_<ID>=URL
    kept       create it, retain what it holds, and      `--keep-services`
               leave it running; the run prints the
               `--existing-service` that adopts it

Every mode makes the service in the run's `Made`, so it is made once, whichever phase makes it,
and every consumer gets that one instance.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re

from .binary import UsageError
from .resources import ALWAYS, ServiceNotUp
from .resources.made import EXISTING, KEPT, MANAGED  # noqa: F401

_log = logging.getLogger(__name__)

MASK = "***"

# A name that says the value is a credential: masked wherever it is written down.
SECRET_NAME = re.compile(r"SECRET|TOKEN|PASSW|CREDENTIAL|_KEY(_ID)?$", re.IGNORECASE)


def _key(service_id):
    return service_id.replace("-", "_").lower()


def existing_address(service_id, cli_entries):
    """The address of a running instance of `service_id` the run was told to adopt, or None.
    `--existing-service ID=URL` wins over DUCKTEST_EXISTING_SERVICE_<ID>."""
    for entry in cli_entries or ():
        name, _, value = entry.partition("=")
        if _key(name) == _key(service_id):
            return _address(value, f"--existing-service {entry}")
    name = f"DUCKTEST_EXISTING_SERVICE_{_key(service_id).upper()}"
    value = os.environ.get(name)
    return None if value is None else _address(value, name)


def _address(value, where):
    value = value.strip()
    if not value:
        raise UsageError(f"{where}: name the address of the running instance, as ID=URL")
    return value


def bring_up(made, definitions, *, existing_services=(), keep=False, separate=False, on=None):
    """Make each service of `definitions` in `made`, in order: adopted where the run names a
    running instance or the service was declared `as_existing`, else created. `on(id, **fields)` is told what happened to each one. Returns
    {service id: why it is not up}.

    `separate` is the `provision` subcommand, which is not the process that runs the tests: it
    refuses a service that cannot outlive it."""
    errors = {}
    for d in definitions:
        address = existing_address(d.id, existing_services) or d.address
        if separate and address is None and not d.detached:
            raise UsageError(
                f"service {d.id!r} lives in the process that starts it, so a separate `provision` would stop "
                f"it again at once. Run `ducktest5 exec --run-dir DIR --provision`, which provisions and runs in "
                f"one process, or name a running instance with --existing-service {d.id}=URL."
            )
        if address is not None:
            _make(made, d.as_existing(address), EXISTING, errors, on)
        elif keep:
            _make(made, dataclasses.replace(d, retain=ALWAYS), KEPT, errors, on)
        else:
            _make(made, d, MANAGED, errors, on)
    return errors


def adopt(made, definitions, recorded, *, existing_services=(), on=None):
    """For an exec that is not the process that provisioned: each service is adopted at the address
    the command line or `03` (`recorded`, its `services`) gives. One that lives in this process
    cannot have been made anywhere else, so it is made here. Returns {service id: why it is not up}."""
    errors = {}
    for d in definitions:
        address = existing_address(d.id, existing_services) or d.address or (recorded.get(d.id) or {}).get("address")
        if address is not None:
            _make(made, d.as_existing(address), EXISTING, errors, on)
        elif not d.detached:
            _make(made, d, MANAGED, errors, on)
        else:
            errors[d.id] = (
                f"no instance of {d.id!r} was provisioned for this run. Run "
                f"`ducktest5 provision --run-dir DIR` first, or `exec --provision` to provision here."
            )
            if on:
                on(d.id, state="failed", error=errors[d.id])
    return errors


def _make(made, definition, mode, errors, on):
    if on:
        on(definition.id, state="starting")
    try:
        service = made.create(definition, mode)
    except ServiceNotUp as exc:
        errors[definition.id] = str(exc)
        if on:
            on(definition.id, state="failed", error=errors[definition.id])
        _log.info("service %s: failed to start: %s", definition.id, errors[definition.id])
        return
    if on:
        on(definition.id, state="up", mode=mode, address=service.address)
    _log.info("service %s (%s): up at %s", definition.id, mode, service.address)


def record(service, mode, *, owned):
    """What `03` says about one service: what it is, how it was reached, where it answers, its
    attributes with every secret masked, and `owned`: whether cleanup is the one to end it."""
    attributes = {
        name: MASK if SECRET_NAME.search(name) else value for name, value in service.attribute_values().items()
    }
    attributes.update({name: MASK for name in service.secrets})
    return {
        "kind": service.definition.kind,
        "identity": service.identity,
        "mode": mode,
        "detached": bool(service.definition.detached),
        "owned": owned,
        "address": service.address,
        "attributes": attributes,
    }


def attach_argument(service):
    """The `--existing-service` argument that adopts a kept instance in the next run."""
    return f"--existing-service {service.id}={service.address}"
