import dataclasses
import io
import os.path
import tempfile
from typing import Optional

import aiohttp
import pytest  # type: ignore
import yarl
from xprocess import ProcessStarter  # type: ignore


@pytest.fixture
async def tus_server(aiohttp_server):
    """Return a fake tus server that can consume a single file."""

    state = {
        # Number of times the respective handlers will fail.
        "retries_create": 0,
        "retries_head": 0,
        "retries_upload": 0,
        # URLs to create and upload. (Filled out later.)
        "create_endpoint": None,
        "upload_endpoint": None,
        # The uploaded data will be accumulated here.
        "data": None,
        # Metadata included in the creation will be placed here.
        "metadata": None,
        # HTTP headers of the creation.
        "headers": None,
    }

    async def handler_create(request):
        state["retries_create"] -= 1
        if state["retries_create"] > 0:
            raise aiohttp.web.HTTPInternalServerError()

        if "Upload-Metadata" in request.headers:
            state["metadata"] = request.headers["Upload-Metadata"]

        state["headers"] = request.headers

        # "Create" the upload.
        state["data"] = bytearray()

        headers = {"Location": str(state["upload_endpoint"])}
        raise aiohttp.web.HTTPCreated(headers=headers)

    async def handler_head(request):
        state["retries_head"] -= 1
        if state["retries_head"] > 0:
            raise aiohttp.web.HTTPInternalServerError()

        if state["data"] is None:
            raise aiohttp.web.HTTPNotFound()

        headers = {"Upload-Offset": str(len(state["data"]))}
        if state["metadata"] is not None:
            headers["Upload-Metadata"] = state["metadata"]
        raise aiohttp.web.HTTPOk(headers=headers)

    async def handler_upload(request):
        body = await request.read()

        state["retries_upload"] -= 1
        if state["retries_upload"] > 0:
            # Pretend we did only receive half of the data before an error happend.
            state["data"].extend(body[: len(body) // 2])
            raise aiohttp.web.HTTPInternalServerError()

        state["data"].extend(body)
        raise aiohttp.web.HTTPNoContent()

    upload_name = "1234abcdefgh"

    app = aiohttp.web.Application()
    app.router.add_route("POST", "/files", handler_create)
    app.router.add_route("HEAD", "/files/" + upload_name, handler_head)
    app.router.add_route("PATCH", "/files/" + upload_name, handler_upload)

    state["server"] = await aiohttp_server(app)
    state["create_endpoint"] = state["server"].make_url("/files")
    state["upload_endpoint"] = state["create_endpoint"] / upload_name

    return state


@pytest.fixture
def memory_file():
    """Dummy data to use during tests."""
    return io.BytesIO(b"\x00\x01\x02\x03")


@dataclasses.dataclass
class TusServer:

    # The URL where the server is listening on.
    url: yarl.URL

    # The path of the certificate file of the server, if TLS is used.
    certificate: Optional[str] = None


@pytest.fixture(scope="module")
def tusd(pytestconfig, xprocess):
    """Start the tusd (tus.io reference implementation) and yield the upload URL.

    Assumes that the tusd executable is located in the pytest rootdir.
    """

    host = "0.0.0.0"
    port = "8080"
    basepath = "/files/"

    executable = pytestconfig.rootdir.join("/tusd")

    class Starter(ProcessStarter):
        pattern = "You can now upload files to:"
        args = [executable, "-host", host, "-port", port, "-base-path", basepath]

    server_name = "tusd-server"

    xprocess.ensure(server_name, Starter)
    server = TusServer(yarl.URL(f"http://{host}:{port}{basepath}"))
    yield server
    xprocess.getinfo(server_name).terminate()


# Template for the nginx configuration file. Stripped-down from
# https://github.com/tus/tusd/blob/master/examples/nginx.conf
_nginx_conf = """
    pid nginx.pid;
    daemon off;
    error_log /dev/null;

    events {{
    }}

    http {{
        client_body_temp_path ./client_body;
        proxy_temp_path ./proxy;
        fastcgi_temp_path ./fastcgi;
        uwsgi_temp_path ./uwsgi;
        scgi_temp_path ./scgi;

        access_log off;

        server {{
            listen {port} ssl;
            server_name _;
            ssl_certificate {crt};
            ssl_certificate_key {key};

            location / {{
                # Forward incoming requests to local tusd instance
                proxy_pass {tusd_url};

                # Disable request and response buffering
                proxy_request_buffering  off;
                proxy_buffering off;
                proxy_http_version 1.1;

                # Add X-Forwarded-* headers
                proxy_set_header X-Forwarded-Host $hostname;
                proxy_set_header X-Forwarded-Proto $scheme;

                proxy_set_header Upgrade $http_upgrade;
                proxy_set_header Connection "upgrade";
                client_max_body_size 0;
            }}
        }}
    }}
"""


@pytest.fixture(scope="module")
def nginx_proxy(xprocess, tusd):
    """Start an nginx proxy in front of tusd that does TLS termination."""

    test_dir = os.path.dirname(os.path.abspath(__file__))
    fmt = {
        "crt": os.path.join(test_dir, "selfsigned.crt"),
        "key": os.path.join(test_dir, "nginx.key"),
        "port": 8443,
        "tusd_url": str(tusd.url),
    }
    conf = _nginx_conf.format(**fmt)

    with tempfile.TemporaryDirectory() as d:
        conf_file = os.path.join(d, "nginx.conf")
        with open(conf_file, "w") as f:
            f.write(conf)

        class Starter(ProcessStarter):
            pattern = "could not open error log file"
            args = ["nginx", "-p", d, "-c", "nginx.conf"]

        server_name = "nginx-server"

        xprocess.ensure(server_name, Starter)
        server = TusServer(yarl.URL(f"https://localhost:{fmt['port']}"))
        server.certificate = fmt["crt"]
        yield server
        xprocess.getinfo(server_name).terminate()
