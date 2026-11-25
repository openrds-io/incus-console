"""Operations handed off to Operations keep their outcome after Incus forgets them."""

import time

from app import tracker
from app.db import connect

from .test_app import text


def wait_for(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def rows(app):
    with connect(app.config["DATA_DIR"]) as db:
        return [dict(r) for r in db.execute("SELECT * FROM tracked_operations ORDER BY id")]


def slow_create(browser, sid, fake, name):
    fake.pending_ops = True
    form = {"name": name, "source": "remote", "alias": "rockylinux/10"}
    response = browser.post(f"/servers/{sid}/create-instance", form, follow_redirects=True)
    assert "You will see the outcome here" in text(response)
    return [op for op in fake.operations.values() if op["status"] == "Running"][-1]["id"]


def test_success_is_reported_once(app, browser, sid, fake):
    op_id = slow_create(browser, sid, fake, "r10")
    assert rows(app)[0]["status"] == "Running"
    fake.finish(op_id)
    fake.forget(op_id)  # Incus drops it soon after; the console already knows
    assert wait_for(lambda: rows(app)[0]["status"] == "Success")

    page = text(browser.get(f"/servers/{sid}/instances"))
    assert "Container r10 created." in page
    assert "Container r10 created." not in text(browser.get(f"/servers/{sid}/instances"))
    ops = text(browser.get(f"/servers/{sid}/operations"))
    assert "Started from this console" in ops and "Container r10 created." in ops


def test_failure_is_reported(app, browser, sid, fake):
    op_id = slow_create(browser, sid, fake, "r11")
    fake.finish(op_id, err="Failed getting image: no space left on device")
    assert wait_for(lambda: rows(app)[0]["status"] == "Failure")
    page = text(browser.get(f"/servers/{sid}/operations"))
    assert "Did not finish: Container r11 created (Failed getting image" in page
    assert "no space left on device" in page


def test_forgotten_operation_is_unknown(app, browser, sid, fake):
    op_id = slow_create(browser, sid, fake, "r12")
    fake.forget(op_id)
    assert wait_for(lambda: rows(app)[0]["status"] == "Unknown")
    assert "no longer knows" in rows(app)[0]["error"]


def test_watching_resumes_after_restart(app, browser, sid, fake):
    op_id = slow_create(browser, sid, fake, "r13")
    row_id = rows(app)[0]["id"]
    # Pretend this process never started a watcher for it (a restarted console).
    key = (app.config["DATA_DIR"], row_id)
    assert wait_for(lambda: key in tracker._watching)
    tracker._watching.discard(key)
    browser.get(f"/servers/{sid}/operations")
    assert key in tracker._watching
    fake.finish(op_id)
    assert wait_for(lambda: rows(app)[0]["status"] == "Success")
