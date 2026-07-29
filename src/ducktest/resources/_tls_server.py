"""Stdlib TLS static-file server — the HTTPS origin the ``httpserver`` service spawns.

Needed because neither existing piece is an HTTPS *origin*: ``python -m http.server`` has no TLS flag, and
mitm is a *proxy*, not a server. So this tiny import-clean module (spawned via ``sys.executable`` — the
``mitm_addon.py`` precedent, addressed by path) wraps ``ThreadingHTTPServer`` in an ``SSLContext`` and
serves ``docroot`` over HTTPS on 127.0.0.1.

argv: ``<port> <docroot> <certfile> <keyfile>``. The leaf cert (+ key) is minted by the shared ``ca``
service (``ca.leaf_cert``), so duckdb trusts it through the one ``ca_cert_file``. Runs until killed — the
parent (``httpserver.py``) owns its lifetime via the pidfile.
"""

import ssl
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def main(argv):
    port, docroot, certfile, keyfile = int(argv[0]), argv[1], argv[2], argv[3]
    handler = partial(SimpleHTTPRequestHandler, directory=docroot)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
