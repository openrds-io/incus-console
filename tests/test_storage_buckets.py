"""Object storage: pools, buckets and bucket keys."""

from .test_app import text


def test_pools_page(browser, sid):
    page = text(browser.get(f"/servers/{sid}/storage"))
    assert "zfs" in page and f"/servers/{sid}/storage/default/buckets" in page


def test_navigation_has_buckets(browser, sid):
    page = text(browser.get(f"/servers/{sid}/buckets"))
    assert f'href="/servers/{sid}/buckets" aria-current="page"' in page


def test_all_buckets_across_pools(browser, sid, fake):
    fake.add_pool("archive", "dir")
    fake.add_bucket("default", "media", config={"size": "10GiB"}, description="Photos")
    fake.add_bucket("archive", "backups")
    page = text(browser.get(f"/servers/{sid}/buckets"))
    assert "Showing 2 buckets in project default" in page
    assert f"/servers/{sid}/storage/archive/buckets/backups" in page
    assert f"/servers/{sid}/storage/default/buckets/media" in page
    assert "10GiB" in page and "Photos" in page
    assert "https://127.0.0.1:8555/media" in page
    assert "<th>Pool</th>" in page


def test_pool_buckets_page(browser, sid, fake):
    fake.add_pool("archive", "dir")
    fake.add_bucket("archive", "backups")
    page = text(browser.get(f"/servers/{sid}/storage/archive/buckets"))
    assert "backups" in page and "<th>Pool</th>" not in page
    assert f"/servers/{sid}/buckets/new?pool=archive" in page
    assert "No buckets in pool default" in text(
        browser.get(f"/servers/{sid}/storage/default/buckets")
    )


def test_empty_and_unsupported_pools(browser, sid, fake):
    assert "No buckets in project default" in text(browser.get(f"/servers/{sid}/buckets"))
    fake.add_pool("shared", "cephfs")
    fake.no_bucket_drivers.add("cephfs")
    page = text(browser.get(f"/servers/{sid}/buckets"))
    assert "shared: Storage pool does not support buckets" in page
    response = browser.get(f"/servers/{sid}/storage/nope/buckets")
    assert response.status_code == 200 and "Storage pool not found" in text(response)


def test_missing_s3_address_is_explained(browser, sid, fake, monkeypatch):
    original = fake._route

    def no_address(method, seg, query, body, peer):
        status, payload = original(method, seg, query, body, peer)
        if method == "GET" and seg == []:
            payload["metadata"]["config"] = {"core.storage_buckets_address": ""}
        return status, payload

    monkeypatch.setattr(fake, "_route", no_address)
    page = text(browser.get(f"/servers/{sid}/buckets"))
    assert "incus config set core.storage_buckets_address" in page


def test_create_bucket_shows_admin_key_once(browser, sid, fake):
    form = text(browser.get(f"/servers/{sid}/buckets/new?pool=default"))
    assert '<option value="default" selected>' in form
    response = browser.post(
        f"/servers/{sid}/buckets/new",
        {"pool": "default", "name": "media", "size": "10GiB", "description": "Photos"},
    )
    page = text(response)
    admin = fake.buckets["default"]["media"]["keys"]["admin"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "Bucket media created with an admin key" in page
    assert admin["access-key"] in page and admin["secret-key"] in page
    assert fake.buckets["default"]["media"]["bucket"]["config"] == {"size": "10GiB"}
    assert fake.buckets["default"]["media"]["bucket"]["description"] == "Photos"


def test_create_bucket_errors(browser, sid, fake):
    url = f"/servers/{sid}/buckets/new"
    bad = text(browser.post(url, {"pool": "default", "name": "Media"}))
    assert "lowercase letters" in bad and 'value="Media"' in bad
    assert "lowercase" in text(browser.post(url, {"pool": "default", "name": "a..b"}))
    assert "Choose a storage pool" in text(browser.post(url, {"pool": "", "name": "media"}))
    fake.add_bucket("default", "media")
    assert "already exists" in text(browser.post(url, {"pool": "default", "name": "media"}))
    assert fake.buckets["default"].keys() == {"media"}


def test_bucket_detail_and_edit(browser, sid, fake):
    fake.add_bucket("default", "media", config={"size": "10GiB"}, description="Photos")
    base = f"/servers/{sid}/storage/default/buckets/media"
    page = text(browser.get(base))
    assert "10GiB" in page and "Photos" in page and "Admin" in page
    form = text(browser.get(f"{base}/edit"))
    assert 'value="10GiB"' in form and "can't be changed" in form
    done = browser.post(
        f"{base}/edit", {"size": "", "description": "All media"}, follow_redirects=True
    )
    assert "Bucket media updated." in text(done)
    assert fake.puts[-1] == {"description": "All media", "config": {}}
    assert "No limit" in text(done)


def test_delete_bucket(browser, sid, fake):
    fake.add_bucket("default", "media")
    response = browser.post(
        f"/servers/{sid}/storage/default/buckets/media/delete", follow_redirects=True
    )
    assert "Bucket media deleted." in text(response)
    assert fake.buckets["default"] == {}
    assert response.request.path == f"/servers/{sid}/buckets"


def test_create_key_generated_and_custom(browser, sid, fake):
    fake.add_bucket("default", "media")
    url = f"/servers/{sid}/storage/default/buckets/media/keys"
    assert "Leave empty to generate" in text(browser.get(url))
    response = browser.post(url, {"name": "reader", "role": "read-only"})
    keys = fake.buckets["default"]["media"]["keys"]
    assert "Key reader created" in text(response)
    assert keys["reader"]["secret-key"] in text(response)
    assert response.headers["Cache-Control"] == "no-store"

    browser.post(
        url,
        {
            "name": "writer",
            "role": "admin",
            "description": "CI uploads",
            "access_key": "MYACCESS",
            "secret_key": "my-secret",
        },
    )
    assert keys["writer"]["access-key"] == "MYACCESS"
    assert keys["writer"]["secret-key"] == "my-secret"
    assert keys["writer"]["description"] == "CI uploads"


def test_create_key_errors(browser, sid, fake):
    fake.add_bucket("default", "media")
    url = f"/servers/{sid}/storage/default/buckets/media/keys"
    assert "Give the key a name" in text(browser.post(url, {"name": "", "role": "admin"}))
    assert "Unknown role" in text(browser.post(url, {"name": "x", "role": "root"}))
    assert "already exists" in text(browser.post(url, {"name": "admin", "role": "admin"}))


def test_edit_key_keeps_credentials_when_left_empty(browser, sid, fake):
    fake.add_bucket("default", "media")
    keys = fake.buckets["default"]["media"]["keys"]
    before = dict(keys["admin"])
    url = f"/servers/{sid}/storage/default/buckets/media/keys/admin/edit"
    form = text(browser.get(url))
    assert before["access-key"] in form  # as the placeholder
    assert before["secret-key"] not in form
    done = browser.post(
        url, {"role": "read-only", "description": "now read-only"}, follow_redirects=True
    )
    assert "Key admin updated." in text(done)
    assert keys["admin"]["access-key"] == before["access-key"]
    assert keys["admin"]["secret-key"] == before["secret-key"]
    assert keys["admin"]["role"] == "read-only"

    browser.post(url, {"role": "admin", "secret_key": "rotated"})
    assert keys["admin"]["secret-key"] == "rotated"
    assert keys["admin"]["access-key"] == before["access-key"]
    assert "Unknown role" in text(browser.post(url, {"role": "root"}))
    assert (
        browser.get(f"/servers/{sid}/storage/default/buckets/media/keys/ghost/edit").status_code
        == 404
    )


def test_delete_key(browser, sid, fake):
    fake.add_bucket("default", "media")
    base = f"/servers/{sid}/storage/default/buckets/media/keys"
    done = browser.post(f"{base}/admin/delete", follow_redirects=True)
    assert "Key admin deleted." in text(done)
    assert fake.buckets["default"]["media"]["keys"] == {}
    assert "No access keys" in text(done)
    missing = browser.post(f"{base}/ghost/delete", follow_redirects=True)
    assert "not found" in text(missing)


def test_bucket_text_is_escaped(browser, sid, fake):
    fake.add_bucket("default", "media", description="<script>alert(1)</script>")
    browser.post(
        f"/servers/{sid}/storage/default/buckets/media/keys",
        {"name": "k", "role": "admin", "description": "<b>bold</b>"},
    )
    for url in (
        f"/servers/{sid}/buckets",
        f"/servers/{sid}/storage/default/buckets/media",
    ):
        page = text(browser.get(url))
        assert "<script>alert(1)</script>" not in page and "<b>bold</b>" not in page
    assert "&lt;script&gt;" in text(browser.get(f"/servers/{sid}/buckets"))


def test_bucket_posts_need_csrf(app, sid, fake):
    fake.add_bucket("default", "media")
    client = app.test_client()
    assert client.post(f"/servers/{sid}/buckets/new", data={"pool": "default"}).status_code == 400
    url = f"/servers/{sid}/storage/default/buckets/media/delete"
    assert client.post(url).status_code == 400
    assert "media" in fake.buckets["default"]
