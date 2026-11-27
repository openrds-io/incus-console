"""Two-step sign-in: codes, the QR picture, setting up, signing in, and getting back in."""

import re
import time

import pytest

from app import qr, totp
from app.db import get_db

from .conftest import PASSWORD, Browser, login, user_id

# RFC 6238's test secret ("12345678901234567890") and some of its SHA-1 answers.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_codes_match_the_rfc():
    answers = ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"))
    for moment, code in (*answers, (20000000000, "353130")):
        assert totp.code_at(RFC_SECRET, moment // 30) == code


def test_a_code_is_good_for_its_moment_and_once():
    step = 1234567890 // 30
    assert totp.matching_step(RFC_SECRET, "005924", now=1234567890) == step
    assert totp.matching_step(RFC_SECRET, "005 924", now=1234567890 + 30) == step
    assert totp.matching_step(RFC_SECRET, "005924", now=1234567890 + 90) is None
    assert totp.matching_step(RFC_SECRET, "005924", after=step, now=1234567890) is None
    assert totp.matching_step(RFC_SECRET, "5924", now=1234567890) is None
    assert totp.matching_step(RFC_SECRET, "abcdef", now=1234567890) is None


def test_qr_code_shape():
    modules = qr.matrix("otpauth://totp/Incus%20Console:root?secret=" + RFC_SECRET)
    size = len(modules)
    assert size in range(21, 58, 4) and all(len(row) == size for row in modules)
    finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"]
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        rows = ["".join("1" if m else "0" for m in row[left : left + 7]) for row in modules[top:]]
        assert rows[:7] == finder
    assert 'viewBox="0 0' in qr.svg("hello")
    with pytest.raises(ValueError, match="too long"):
        qr.matrix("x" * 500)


def test_qr_code_reads_back():
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    for length in (20, 60, 100, 150, 213):  # versions 2 to 10
        text = (
            "otpauth://totp/Incus%20Console:someone?secret=" + RFC_SECRET + "&pad=" + "z" * 200
        )[:length]
        light = [[0 if module else 255 for module in row] for row in qr.matrix(text)]
        image = cv2.copyMakeBorder(
            numpy.array(light, dtype=numpy.uint8), 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255
        )
        image = cv2.resize(image, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
        assert cv2.QRCodeDetector().detectAndDecode(image)[0] == text


def current_code(app, username, shift=0):
    with app.app_context():
        row = get_db().execute("SELECT totp_secret FROM users WHERE username = ?", (username,))
        return totp.code_at(row.fetchone()[0], int(time.time() // 30) + shift)


def set_up(app, browser):
    """Turn two-step sign-in on through the pages; returns the recovery codes shown."""
    page = browser.get("/account/two-factor").get_data(as_text=True)
    assert "<svg" in page
    secret = re.search(r'class="totp-key">([A-Z2-7 ]+)<', page).group(1).replace(" ", "")
    code = totp.code_at(secret, int(time.time() // 30))
    done = browser.post(
        "/account/two-factor", {"action": "enable", "code": code, "current": PASSWORD}
    )
    assert done.status_code == 200
    codes = re.findall(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", done.get_data(as_text=True))
    assert len(codes) == totp.RECOVERY_CODES
    return codes


def second_step(app, username):
    browser = Browser(app)
    response = browser.post("/login", {"username": username, "password": PASSWORD})
    assert response.headers["Location"].endswith("/login/code")
    return browser


def test_setting_up_needs_the_password_and_a_right_code(server_app):
    vic = login(server_app, "vic")
    page = vic.get("/account/two-factor").get_data(as_text=True)
    secret = re.search(r'class="totp-key">([A-Z2-7 ]+)<', page).group(1).replace(" ", "")
    code = totp.code_at(secret, int(time.time() // 30))
    vic.post("/account/two-factor", {"action": "enable", "code": code, "current": "not it"})
    vic.post("/account/two-factor", {"action": "enable", "code": "000000", "current": PASSWORD})
    assert "Off." in vic.get("/account").get_data(as_text=True)
    # The same key is still offered, so the app that scanned it is not left behind.
    assert secret[:4] in vic.get("/account/two-factor").get_data(as_text=True)
    set_up(server_app, vic)
    assert "On:" in vic.get("/account").get_data(as_text=True)


def test_sign_in_asks_for_the_code(server_app):
    other = login(server_app, "vic")
    set_up(server_app, login(server_app, "vic"))
    # Turning it on ended the session opened with the password alone.
    assert other.get("/account").status_code == 302

    browser = second_step(server_app, "vic")
    assert browser.get("/account").status_code == 302  # not signed in yet
    assert "Enter your code" in browser.get("/login/code").get_data(as_text=True)
    wrong = browser.post("/login/code", {"code": "000000"})
    assert "not right" in wrong.get_data(as_text=True)
    # The digits that confirmed the set-up are still on their screen: they work, once.
    code = current_code(server_app, "vic")
    assert browser.post("/login/code", {"code": code}).status_code == 302
    assert browser.get("/account").status_code == 200
    again = second_step(server_app, "vic")
    assert "not right" in again.post("/login/code", {"code": code}).get_data(as_text=True)


def test_the_second_step_alone_opens_nothing(server_app):
    set_up(server_app, login(server_app, "vic"))
    stranger = Browser(server_app)
    assert stranger.get("/login/code").headers["Location"].endswith("/login")
    late = second_step(server_app, "vic")
    with late.client.session_transaction() as session:
        session["pending"] = {**session["pending"], "at": int(time.time()) - 3600}
    assert (
        late.post("/login/code", {"code": current_code(server_app, "vic", 1)})
        .headers["Location"]
        .endswith("/login")
    )
    assert late.get("/account").status_code == 302


def test_recovery_codes_work_once_and_can_be_renewed(server_app):
    vic = login(server_app, "vic")
    codes = set_up(server_app, vic)
    browser = second_step(server_app, "vic")
    assert browser.post("/login/code", {"code": codes[0].upper()}).status_code == 302
    assert "9 left" in browser.get("/account").get_data(as_text=True)
    again = second_step(server_app, "vic")
    assert "not right" in again.post("/login/code", {"code": codes[0]}).get_data(as_text=True)

    fresh = browser.post("/account/two-factor", {"action": "codes", "current": PASSWORD})
    new = re.findall(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", fresh.get_data(as_text=True))
    assert len(new) == totp.RECOVERY_CODES and not set(new) & set(codes)
    assert "not right" in again.post("/login/code", {"code": codes[1]}).get_data(as_text=True)
    assert again.post("/login/code", {"code": new[0]}).status_code == 302
    with server_app.app_context():
        stored = get_db().execute("SELECT code_hash FROM recovery_codes").fetchall()
        assert all(len(row[0]) == 64 for row in stored)


def test_wrong_codes_lock_the_username(server_app):
    set_up(server_app, login(server_app, "vic"))
    time.sleep(2.1)  # failures in the second after a sign-in are not counted
    browser = second_step(server_app, "vic")
    for _ in range(server_app.config["LOGIN_MAX_FAILURES"]):
        browser.post("/login/code", {"code": "000000"})
    refused = browser.post("/login/code", {"code": current_code(server_app, "vic", 1)})
    assert refused.headers["Location"].endswith("/login")
    assert browser.get("/account").status_code == 302


def test_turning_it_off(server_app, admin):
    vic = login(server_app, "vic")
    set_up(server_app, vic)
    vic.post("/account/two-factor", {"action": "disable", "current": "not it"})
    assert "On:" in vic.get("/account").get_data(as_text=True)
    vic.post("/account/two-factor", {"action": "disable", "current": PASSWORD})
    assert "Off." in vic.get("/account").get_data(as_text=True)
    login(server_app, "vic")  # the password alone again

    set_up(server_app, vic)
    uid = user_id(server_app, "vic")
    assert "Two-step sign-in is on" in admin.get(f"/admin/users/{uid}").get_data(as_text=True)
    assert vic.post(f"/admin/users/{uid}/two-factor-off").status_code == 403
    admin.post(f"/admin/users/{uid}/two-factor-off")
    assert vic.get("/account").status_code == 302  # signed out everywhere
    login(server_app, "vic")
    with server_app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM recovery_codes").fetchone()[0] == 0


def test_command_line_way_back_in(server_app):
    set_up(server_app, login(server_app, "root"))
    runner = server_app.test_cli_runner()
    assert "off for root" in runner.invoke(args=["two-factor-off", "root"]).output
    assert runner.invoke(args=["two-factor-off", "nobody"]).exit_code != 0
    login(server_app, "root")


def test_a_console_can_require_it(server_app):
    server_app.config["REQUIRE_TWO_FACTOR"] = True
    vic = login(server_app, "vic")
    assert vic.get("/account").headers["Location"].endswith("/account/two-factor")
    assert vic.get("/").headers["Location"].endswith("/account/two-factor")
    set_up(server_app, vic)
    assert vic.get("/account").status_code == 200


def test_audit_shows_the_second_step(server_app, admin):
    codes = set_up(server_app, login(server_app, "vic"))
    browser = second_step(server_app, "vic")
    browser.post("/login/code", {"code": "000000"})
    browser.post("/login/code", {"code": codes[3]})
    log = admin.get("/admin/audit?user=vic").get_data(as_text=True)
    assert "Wrong code" in log and "With a recovery code" in log
    assert codes[3] not in log and "two factor: enable" in log


def test_older_databases_gain_the_columns(tmp_path):
    import sqlite3

    from app import create_app

    data = tmp_path / "data"
    data.mkdir()
    old = sqlite3.connect(data / "incus-console.db")
    old.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
        "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
        "disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT, "
        "session_epoch INTEGER NOT NULL DEFAULT 0, failures_reset_at TEXT)"
    )
    old.commit()
    old.close()
    app = create_app({"TESTING": True, "SERVER_MODE": True, "DATA_DIR": str(data)})
    with app.app_context():
        columns = {row["name"] for row in get_db().execute("PRAGMA table_info(users)")}
    assert {"totp_secret", "totp_step"} <= columns


def test_command_line_unlock(server_app):
    time.sleep(1.1)
    browser = Browser(server_app)
    for _ in range(server_app.config["LOGIN_MAX_FAILURES"]):
        browser.post("/login", {"username": "root", "password": "not it"})
    assert browser.post("/login", {"username": "root", "password": PASSWORD}).status_code == 200
    runner = server_app.test_cli_runner()
    assert "can sign in again" in runner.invoke(args=["unlock", "root"]).output
    assert runner.invoke(args=["unlock", "nobody"]).exit_code != 0
    time.sleep(2.1)  # failures up to a second after the unlock are forgotten with it
    login(server_app, "root")


def test_failures_are_counted_per_address(server_app):
    server_app.config["LOGIN_MAX_FAILURES_PER_ADDRESS"] = 6
    browser = Browser(server_app)
    # Different names each time, so no single username is locked.
    for number in range(6):
        browser.post("/login", {"username": f"guess{number}", "password": "not it"})
    refused = browser.post("/login", {"username": "root", "password": PASSWORD})
    assert refused.status_code == 200 and "network address" in refused.get_data(as_text=True)
    elsewhere = Browser(server_app)
    token = elsewhere.token()
    fine = elsewhere.client.post(
        "/login",
        data={"csrf_token": token, "username": "root", "password": PASSWORD},
        environ_base={"REMOTE_ADDR": "203.0.113.9"},
    )
    assert fine.status_code == 302
    with server_app.app_context():
        seen = get_db().execute("SELECT DISTINCT address FROM audit ORDER BY address").fetchall()
        assert [row[0] for row in seen] == ["127.0.0.1", "203.0.113.9"]
    server_app.config["LOGIN_MAX_FAILURES_PER_ADDRESS"] = 0
    assert browser.post("/login", {"username": "root", "password": PASSWORD}).status_code == 302


def test_older_audit_logs_gain_the_address(tmp_path):
    import sqlite3

    from app import create_app
    from app.db import SCHEMA, path_for

    data = tmp_path / "data"
    data.mkdir()
    old = sqlite3.connect(path_for(str(data)))
    before = re.sub(
        r",\n    -- Where the request came from.*?address TEXT\n", "\n", SCHEMA, flags=re.S
    )
    assert "Where the request came from" not in before
    old.executescript(before)
    old.close()
    app = create_app({"TESTING": True, "SERVER_MODE": True, "DATA_DIR": str(data)})
    with app.app_context():
        assert "address" in {row["name"] for row in get_db().execute("PRAGMA table_info(audit)")}
