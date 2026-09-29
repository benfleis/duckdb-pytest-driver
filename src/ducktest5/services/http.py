"""An HTTP file server that lives in this process: a directory, served by a thread."""

from __future__ import annotations

import functools
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from ..resources import ON_FAILURE, Service, ServiceDef


@dataclass(eq=False)
class HttpServerDef(ServiceDef):
    """Serves a copy of the directory `source` (or an empty one) on a free port of `host`, under a
    directory of the system temp dir named for the run. It outputs `url`."""

    kind = "http"
    attributes = ("url",)
    ready_timeout_s = 10

    id: str
    source: str | None = None
    host: str = "127.0.0.1"
    address: str | None = None
    inputs: list = field(default_factory=list)
    retain: str = ON_FAILURE


def served_dir(run_token, service_id) -> str:
    return os.path.join(tempfile.gettempdir(), "ducktest-http", f"{run_token}__{service_id}")


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class HttpServer(Service):
    def __init__(self, definition, identity, run_token, inputs=None):
        super().__init__(definition, identity, run_token, inputs)
        self.server = None
        self.thread = None

    @property
    def handle(self):
        return served_dir(self.run_token, self.id)

    @property
    def address(self):
        return self.url

    @property
    def url(self):
        if self.existing:
            return self.definition.address
        return f"http://{self.definition.host}:{self.server.server_port}" if self.server else None

    def alloc(self):
        if self.run_token is None:
            raise ValueError(f"service {self.id}: its directory is named for the run, so it needs the run_token")
        if self.definition.source:
            shutil.copytree(self.definition.source, self.handle, dirs_exist_ok=True)
        else:
            os.makedirs(self.handle, exist_ok=True)

    def dealloc(self):
        shutil.rmtree(self.handle, ignore_errors=True)

    def start(self):
        if self.server is not None:
            return
        handler = functools.partial(_Quiet, directory=self.handle)
        self.server = ThreadingHTTPServer((self.definition.host, 0), handler)
        # stop() waits for one poll of serve_forever; its default is half a second.
        serve = functools.partial(self.server.serve_forever, poll_interval=0.05)
        self.thread = threading.Thread(target=serve, name=f"ducktest-{self.id}", daemon=True)
        self.thread.start()

    def stop(self):
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()
            self.thread.join()

    def read(self):
        if not self.existing and not os.path.isdir(self.handle):
            return None
        return {"url": self.url}

    def ready(self):
        if self.url is None:
            return False
        try:
            with urllib.request.urlopen(self.url + "/", timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def list(self, run_token):
        path = served_dir(run_token, self.id)
        return [path] if os.path.isdir(path) else []


HttpServerDef.instance = HttpServer
