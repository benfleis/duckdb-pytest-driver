#!/usr/bin/env python3
"""A stand-in for `lldb --`, for ducktest5's self-tests: it writes how it was started to the file
$FAKE_DEBUGGER_RECORD and ends with $FAKE_DEBUGGER_EXIT (0 by default)."""

import json
import os
import sys

with open(os.environ["FAKE_DEBUGGER_RECORD"], "w") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}, f)
sys.exit(int(os.environ.get("FAKE_DEBUGGER_EXIT", "0")))
