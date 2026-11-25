"""Rename, copy, logs, settings and devices."""

from .test_app import text


def test_rename(browser, sid, fake):
    fake.add_instance("web-1", status="Stopped")
    url = f"/servers/{sid}/instances/web-1/rename"
    assert 'value="web-1"' in text(browser.get(url))
    assert "letters, digits" in text(browser.post(url, {"new_name": "9x"})).lower()
    response = browser.post(url, {"new_name": "web-2"}, follow_redirects=True)
    assert "web-1 renamed to web-2." in text(response)
    assert "web-2" in fake.instances and "web-1" not in fake.instances


def test_rename_running_is_refused(browser, sid, fake):
    fake.add_instance("web-1")
    url = f"/servers/{sid}/instances/web-1/rename"
    assert "Stop web-1 first" in text(browser.get(url))
    assert "not allowed" in text(browser.post(url, {"new_name": "web-2"}))
    assert "web-1" in fake.instances


def test_copy(browser, sid, fake):
    fake.add_instance("web-1")
    fake.instances["web-1"]["snapshots"] = [{"name": "s1"}]
    url = f"/servers/{sid}/instances/web-1/copy"
    assert 'value="web-1-copy"' in text(browser.get(url))
    response = browser.post(url, {"new_name": "web-2", "snapshots": "1"}, follow_redirects=True)
    assert "web-1 copied to web-2." in text(response)
    assert fake.instances["web-2"]["status"] == "Stopped"
    assert fake.instances["web-2"]["snapshots"] == [{"name": "s1"}]
    browser.post(url, {"new_name": "web-3", "start": "1"})
    assert fake.instances["web-3"]["snapshots"] == []
    assert fake.instances["web-3"]["status"] == "Running"


def test_logs(browser, sid, fake):
    fake.add_instance("web-1")
    base = f"/servers/{sid}/instances/web-1/logs"
    page = text(browser.get(base))
    assert "Debian GNU/Linux 13 web-1 console" in page
    assert "&lt;b&gt;login:&lt;/b&gt;" in page  # log text is escaped
    assert "lxc.conf" in page and "lxc.log" in page
    assert "INFO start" in text(browser.get(f"{base}?file=lxc.log"))
    assert "Debian GNU/Linux" in text(browser.get(f"{base}?file=../../etc/passwd"))


def test_console_log_problem_is_shown(browser, sid, fake):
    fake.add_instance("idle", status="Stopped")
    response = browser.get(f"/servers/{sid}/instances/idle/logs")
    assert response.status_code == 200
    assert "not running" in text(response)


def test_settings(browser, sid, fake):
    fake.add_instance("web-1")
    fake.instances["web-1"]["config"]["limits.processes"] = "500"
    url = f"/servers/{sid}/instances/web-1/settings"
    page = text(browser.get(url))
    assert 'placeholder="From profile: 2"' in page  # limits.cpu from expanded_config
    assert 'value="500"' in page
    form = {
        "description": "web tier",
        "cfg:limits.cpu": "4",
        "cfg:limits.memory": "2GiB",
        "cfg:limits.processes": "",
        "cfg:security.nesting": "true",
        "cfg:security.privileged": "bogus",
    }
    assert "web-1 updated." in text(browser.post(url, form, follow_redirects=True))
    sent = fake.puts[-1]
    assert sent["description"] == "web tier"
    assert sent["config"]["limits.cpu"] == "4"
    assert sent["config"]["limits.memory"] == "2GiB"
    assert sent["config"]["security.nesting"] == "true"
    assert "limits.processes" not in sent["config"]  # emptied: back to the profile's value
    assert "security.privileged" not in sent["config"]
    assert sent["config"]["volatile.eth0.hwaddr"]  # other keys are kept


def test_add_and_remove_device(browser, sid, fake):
    fake.add_instance("web-1")
    url = f"/servers/{sid}/instances/web-1/devices/new"
    assert "listen=tcp:0.0.0.0:8080" in text(browser.get(url))
    bad = browser.post(url, {"device": "web", "type": "proxy", "settings": "listen"})
    assert "Line 1" in text(bad)
    form = {
        "device": "web",
        "type": "proxy",
        "settings": "# forward port 80\nlisten=tcp:0.0.0.0:8080\nconnect = tcp:127.0.0.1:80\n",
    }
    assert "Device web added." in text(browser.post(url, form, follow_redirects=True))
    assert fake.instances["web-1"]["devices"]["web"] == {
        "type": "proxy",
        "listen": "tcp:0.0.0.0:8080",
        "connect": "tcp:127.0.0.1:80",
    }
    again = browser.post(url, form)
    assert "already has a device named web" in text(again)

    detail = text(browser.get(f"/servers/{sid}/instances/web-1"))
    assert "This instance" in detail and "Profile" in detail
    gone = browser.post(f"/servers/{sid}/instances/web-1/devices/web/delete", follow_redirects=True)
    assert "Device web removed." in text(gone)
    assert "web" not in fake.instances["web-1"]["devices"]
    profile = browser.post(
        f"/servers/{sid}/instances/web-1/devices/root/delete", follow_redirects=True
    )
    assert "comes from a profile" in text(profile)
