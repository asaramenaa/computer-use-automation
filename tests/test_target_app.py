import time


def test_frameset_and_nav(client):
    body = client.get("/").data.decode()
    assert "<frameset" in body and 'name="main"' in body
    assert "Member Search" in client.get("/nav").data.decode()


def test_login_bad_then_good(client):
    r = client.post("/login", data={"uid": "teller1", "pwd": "wrong"})
    assert r.status_code == 200 and "SEC-401" in r.data.decode()
    r = client.post("/login", data={"uid": "teller1", "pwd": "Passw0rd!"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/members/search")


def test_unauthenticated_is_session_expired_page(client):
    r = client.get("/members/search")
    assert r.status_code == 200 and "SEC-440" in r.data.decode()


def test_search_found_redirects_and_detail_shows_balance(teller):
    r = teller.post("/members/search", data={"member_number": "10023"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/members/10023")
    body = teller.get("/members/10023").data.decode()
    assert "Testerson, Avery" in body and "$1,250.75" in body and "***-**-4821" in body


def test_search_not_found_is_business_outcome_200(teller):
    r = teller.post("/members/search", data={"member_number": "99999"})
    assert r.status_code == 200 and "No member found matching member number 99999" in r.data.decode()


def test_subaccount_validation_then_success(teller):
    r = teller.post("/members/10023/subaccount", data={"product": "SAV", "nickname": "", "deposit": "abc"})
    body = r.data.decode()
    assert "Nickname is required" in body and "non-negative amount" in body
    r = teller.post("/members/10023/subaccount", data={"product": "MMK", "nickname": "Vacation", "deposit": "25.00"})
    assert r.status_code == 302
    body = teller.get(r.headers["Location"]).data.decode()
    assert "Sub-account S-03 opened for member 10023" in body and "CNF-" in body
    assert "Money Market (Vacation)" in teller.get("/members/10023").data.decode()


def test_auditor_role_denied_on_subaccount(client):
    client.post("/login", data={"uid": "auditor1", "pwd": "Passw0rd!"})
    assert "SEC-403" in client.get("/members/10023/subaccount").data.decode()
    assert "Member Profile" in client.get("/members/10023").data.decode()


def test_query_fault_server_error(teller):
    r = teller.get("/members/10023?fault=server_error")
    assert r.status_code == 500 and "HTTP 500" in r.data.decode()


def test_query_fault_not_found_and_validation(teller):
    r = teller.post("/members/search?fault=not_found", data={"member_number": "10023"})
    assert "No member found" in r.data.decode()
    r = teller.post("/members/10023/subaccount?fault=validation", data={"product": "SAV", "nickname": "x", "deposit": "1"})
    assert "VAL-118" in r.data.decode()


def test_armed_fault_consumed_once(teller):
    r = teller.get("/admin/fault/session_timeout?route=/members&count=1")
    assert r.get_json()["armed"][0]["remaining"] == 1
    assert "SEC-440" in teller.get("/members/search").data.decode()  # fault fired, session cleared
    assert teller.get("/admin/faults").get_json()["armed"] == []
    assert "SEC-440" in teller.get("/members/search").data.decode()  # still logged out: real consequence
    teller.post("/login", data={"uid": "teller1", "pwd": "Passw0rd!"})
    assert "Member Search" in teller.get("/members/search").data.decode()


def test_armed_fault_route_prefix_and_exempt_paths(teller):
    teller.get("/admin/fault/server_error?route=/members/10023")
    assert teller.get("/members/search").status_code == 200  # prefix does not match
    assert teller.get("/nav").status_code == 200              # exempt
    assert teller.get("/members/10023").status_code == 500
    assert teller.get("/members/10023").status_code == 200    # consumed


def test_slow_fault_delays(teller):
    t = time.perf_counter()
    assert teller.get("/members/search?fault=slow").status_code == 200
    assert time.perf_counter() - t >= 0.05


def test_interstitial_and_confirm_dialog_render(teller):
    assert "SYSTEM NOTICE" in teller.get("/members/search?fault=interstitial").data.decode()
    assert "window.confirm" in teller.get("/members/search?fault=confirm_dialog").data.decode()


def test_permission_denied_fault_and_unknown_kind(teller):
    assert "SEC-403" in teller.get("/members/10023?fault=permission_denied").data.decode()
    assert teller.get("/admin/fault/bogus").status_code == 400
    assert teller.post("/admin/reset").get_json() == {"reset": True}
