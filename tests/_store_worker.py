"""Out-of-process worker for test_store.py's cross-process check (run as a script,
not collected). Connects to a store via env, reads the eager credential, and races
to provision the shared service; writes what it saw to <results_dir>/<idx>.json.
"""

import json
import os
import sys
import time

from ducktest import store as S


def main():
    results_dir, idx = sys.argv[1], sys.argv[2]
    address, authkey = S.from_env()
    st = S.connect(address, authkey).store()

    creds = S.copy(st, "creds", missing="creds not pre-filled")  # eager, cross-process

    ran = []

    def factory():
        ran.append(True)
        time.sleep(0.3)  # widen the window so all workers race the lock
        return {"name": "svc", "pid": os.getpid()}

    svc = S.copy_or_provision(st, "svc", factory)

    with open(os.path.join(results_dir, idx + ".json"), "w") as f:
        json.dump({"pid": os.getpid(), "ran_factory": bool(ran), "creds": creds, "svc": svc}, f)


if __name__ == "__main__":
    main()
