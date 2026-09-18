import json

import pytest

from cuc.policy import Redactor, SecretRef, SecretStore
from cuc.observability import RunLog


def test_patterns():
    r = Redactor()
    assert r.text("ssn 123-45-6789 ok") == "ssn [REDACTED:ssn] ok"
    assert r.text("acct 100230001") == "acct [REDACTED:account]"
    assert r.text("card 4111 1111 1111 1111") == "card [REDACTED:card]"
    assert r.text("Authorization: Bearer abc.def-ghi") == "[REDACTED:kv_secret]" and r.text("got Bearer abc.def-ghi") == "got [REDACTED:bearer]"
    assert r.text("password=hunter2 next") == "[REDACTED:kv_secret] next"
    assert r.text("member 10023 balance $1,250.75") == "member 10023 balance $1,250.75"  # short ids and money survive
    assert r.text("typed {{secret:TARGET_APP_USER}} then secret:abc") == "typed {{secret:TARGET_APP_USER}} then [REDACTED:kv_secret]"


def test_registered_secrets_and_sensitive_keys():
    r = Redactor(sensitive_keys={"password", "uid"})
    r.register_secret("Passw0rd!")
    r.register_secret("teller1")
    out = r({"uid": "teller1", "note": "typed Passw0rd! into the form as teller1", "nested": {"password": "x", "ok": ["teller1"]}})
    assert out == {"uid": "[REDACTED]", "note": "typed [REDACTED:secret] into the form as [REDACTED:secret]",
                   "nested": {"password": "[REDACTED]", "ok": ["[REDACTED:secret]"]}}


def test_secret_store_resolves_by_reference_and_registers():
    r = Redactor()
    store = SecretStore(r, env={"APP_PW": "s3cret-value"})
    s = store.resolve(SecretRef("APP_PW"))
    assert s.reveal() == "s3cret-value" and repr(s) == "Secret(****)" and "s3cret" not in str(s)
    assert r.text("the value s3cret-value leaked") == "the value [REDACTED:secret] leaked"
    with pytest.raises(KeyError):
        store.resolve(SecretRef("MISSING"))
    assert SecretRef.parse("secret:APP_PW") == SecretRef("APP_PW") and SecretRef.parse("plain") is None


def test_runlog_redacts_at_the_boundary(tmp_path):
    r = Redactor(sensitive_keys={"password"})
    r.register_secret("Passw0rd!")
    log = RunLog(tmp_path, "run-1", r)
    log.event("acted", value="Passw0rd!", password="whatever", text="ssn 123-45-6789")
    log.write_json("result.json", {"outputs": {"note": "Passw0rd!"}})
    log.close()
    raw = (tmp_path / "events.jsonl").read_text() + (tmp_path / "result.json").read_text()
    assert "Passw0rd!" not in raw and "123-45-6789" not in raw and "whatever" not in raw
    rec = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
    assert rec["event"] == "acted" and rec["seq"] == 1 and rec["value"] == "[REDACTED:secret]"
