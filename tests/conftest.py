import pytest

from cuc.target_app import create_app
from cuc.target_app import data as target_data


@pytest.fixture()
def app():
    target_data.reset()
    app = create_app({"TESTING": True, "SLOW_SECONDS": 0.05, "SECRET_KEY": "test"})
    yield app
    target_data.reset()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def teller(client):
    r = client.post("/login", data={"uid": "teller1", "pwd": "Passw0rd!"})
    assert r.status_code == 302
    return client


import threading  # noqa: E402

import pytest as _pytest  # noqa: E402


@_pytest.fixture(scope="session")
def live_server():
    """Target app served on a random port in a daemon thread for browser-level tests."""
    from werkzeug.serving import make_server

    target_data.reset()
    app = create_app({"SLOW_SECONDS": 0.3, "SECRET_KEY": "test"})
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@_pytest.fixture(scope="module")
def surface():
    from cuc.surface.playwright_surface import PlaywrightSurface

    s = PlaywrightSurface(headless=True)
    yield s
    s.close()


def login_via_surface(surface, base_url):
    from cuc.schema import ActionType
    from fixtures import loc_anchor, loc_role, text_present

    surface.act(ActionType.NAVIGATE, None, base_url + "/", 10_000)
    surface.act(ActionType.TYPE, surface.resolve([loc_anchor("textbox", "User ID")], 5000), "teller1", 5000)
    surface.act(ActionType.TYPE, surface.resolve([loc_anchor("textbox", "Password")], 5000), "Passw0rd!", 5000)
    surface.act(ActionType.CLICK, surface.resolve([loc_role("button", "Sign On")], 5000), None, 5000)
    assert surface.wait_for(text_present("Member Search"), 5000)
