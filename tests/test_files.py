"""The file browser."""

import io

import pytest

from app.views.files import clean

from .test_app import text


@pytest.mark.parametrize(
    ("given", "expected"),
    [(None, "/"), ("", "/"), ("etc", "/etc"), ("//etc/../root/", "/root"), ("/../../x", "/x")],
)
def test_clean(given, expected):
    assert clean(given) == expected


@pytest.fixture
def web(fake):
    fake.add_instance("web-1")
    return "web-1"


def test_listing(browser, sid, fake, web):
    page = text(browser.get(f"/servers/{sid}/instances/web-1/files?path=/"))
    assert page.index(">bin") > page.index(">usr/<")  # folders first; the link is not a folder
    assert ">etc/<" in page and "(link)" in page
    etc = text(browser.get(f"/servers/{sid}/instances/web-1/files?path=/etc"))
    assert ">hostname<" in etc and "<code>0644</code>" in etc and "0:0" in etc
    assert "Up one folder" in etc
    assert f'href="/servers/{sid}/instances/web-1/files?path=/"' in etc  # breadcrumb back


def test_missing_folder(browser, sid, web):
    response = browser.get(f"/servers/{sid}/instances/web-1/files?path=/nope/deeper")
    assert response.headers["Location"].endswith("files?path=/nope")


def test_file_path_redirects_to_view(browser, sid, web):
    response = browser.get(f"/servers/{sid}/instances/web-1/files?path=/etc/motd")
    assert "/files/view?path=/etc/motd" in response.headers["Location"]


def test_view_and_edit(browser, sid, fake, web):
    url = f"/servers/{sid}/instances/web-1/files/view?path=/etc/motd"
    page = text(browser.get(url))
    assert "Welcome &lt;b&gt;here&lt;/b&gt;" in page  # escaped
    fake.filesystem("web-1")["/etc/motd"].update(mode=0o600, uid=1000, gid=1000)
    edit = f"/servers/{sid}/instances/web-1/files/edit?path=/etc/motd"
    done = browser.post(edit, {"content": "line one\r\nline two\r\n"}, follow_redirects=True)
    assert "Saved /etc/motd." in text(done)
    saved = fake.filesystem("web-1")["/etc/motd"]
    assert saved["content"] == b"line one\nline two\n"
    assert (saved["mode"], saved["uid"], saved["gid"]) == (0o600, 1000, 1000)  # kept


def test_binary_and_big_files_are_not_shown(browser, sid, fake, web):
    url = f"/servers/{sid}/instances/web-1/files/view?path="
    assert "This file is not text." in text(browser.get(url + "/usr/bin/true"))
    fake.filesystem("web-1")["/root/big.log"] = {
        "type": "file",
        "content": b"x" * (512 * 1024 + 1),
        "mode": 0o644,
        "uid": 0,
        "gid": 0,
    }
    assert "larger than 512 KiB" in text(browser.get(url + "/root/big.log"))


def test_download(browser, sid, web):
    response = browser.get(f"/servers/{sid}/instances/web-1/files/download?path=/usr/bin/true")
    assert response.data == b"\x7fELF\x00\x00binary"
    assert response.mimetype == "application/octet-stream"
    assert response.headers["Content-Disposition"].startswith('attachment; filename="true"')
    folder = browser.get(f"/servers/{sid}/instances/web-1/files/download?path=/etc")
    assert folder.status_code == 302


def test_upload(browser, sid, fake, web):
    url = f"/servers/{sid}/instances/web-1/files/upload?path=/root"
    data = {
        "files": [
            (io.BytesIO(b"hello"), "a.txt"),
            (io.BytesIO(b"\x00\x01"), "b.bin"),
        ]
    }
    done = browser.post(url, data, content_type="multipart/form-data", follow_redirects=True)
    assert "Uploaded a.txt, b.bin." in text(done)
    fs = fake.filesystem("web-1")
    assert fs["/root/a.txt"]["content"] == b"hello"
    assert fs["/root/b.bin"]["content"] == b"\x00\x01"
    assert fs["/root/a.txt"]["mode"] == 0o644
    none = browser.post(url, {}, content_type="multipart/form-data", follow_redirects=True)
    assert "Choose one or more files" in text(none)
    bad = {"files": [(io.BytesIO(b"x"), "..")]}
    refused = browser.post(url, bad, content_type="multipart/form-data", follow_redirects=True)
    assert "cannot be empty" in text(refused)


def test_upload_over_the_limit_is_refused_cleanly(server_app, setup, admin, fake):
    """An upload over MAX_CONTENT_LENGTH gets a 413, not a server error from the
    audit log (server mode) trying to read the refused form afterwards."""
    server_app.config["MAX_CONTENT_LENGTH"] = 1000
    response = admin.post(
        f"/servers/{setup}/instances/web-1/files/upload?path=/root",
        {"files": [(io.BytesIO(b"x" * 5000), "big.bin")]},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert "/root/big.bin" not in fake.filesystem("web-1")


def test_upload_needs_csrf(app, sid, web):
    response = app.test_client().post(
        f"/servers/{sid}/instances/web-1/files/upload?path=/root",
        data={"files": [(io.BytesIO(b"x"), "x")]},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_mkdir_and_delete(browser, sid, fake, web):
    base = f"/servers/{sid}/instances/web-1/files"
    done = browser.post(f"{base}/mkdir?path=/root", {"folder": "data"}, follow_redirects=True)
    assert "Created folder data." in text(done)
    assert fake.filesystem("web-1")["/root/data"]["type"] == "directory"
    assert "contain a slash" in text(
        browser.post(f"{base}/mkdir?path=/root", {"folder": "a/b"}, follow_redirects=True)
    )
    browser.post(f"{base}/mkdir?path=/root/data", {"folder": "inner"})
    not_empty = browser.post(f"{base}/delete?path=/root/data", follow_redirects=True)
    assert "not empty" in text(not_empty)
    browser.post(f"{base}/delete?path=/root/data/inner")
    gone = browser.post(f"{base}/delete?path=/root/data", follow_redirects=True)
    assert "Deleted data." in text(gone)
    assert "/root/data" not in fake.filesystem("web-1")
    root = browser.post(f"{base}/delete?path=/", follow_redirects=True)
    assert "root folder cannot be deleted" in text(root)
