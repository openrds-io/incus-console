"""Scheduled snapshots for instances, profiles and volumes."""

import pytest

from app.incus import IncusError
from app.views.config_forms import check_expiry, check_schedule

from .test_app import text


@pytest.mark.parametrize(
    "value",
    ["@daily", "@hourly, @startup", "0 3 * * *", "*/15 * * * mon-fri", "0 3 * * *, @startup"],
)
def test_good_schedules(value):
    check_schedule(value)


@pytest.mark.parametrize("value", ["daily", "0 3 * *", "@sometimes", "0 3 * * * ;rm"])
def test_bad_schedules(value):
    with pytest.raises(IncusError, match="not a schedule"):
        check_schedule(value)


def test_expiry():
    check_expiry("7d")
    check_expiry("1w 3d")
    with pytest.raises(IncusError):
        check_expiry("7 days")


def test_instance_schedule(browser, sid, fake):
    fake.add_instance("web-1")
    detail = f"/servers/{sid}/instances/web-1"
    assert "No scheduled snapshots." in text(browser.get(detail))
    settings = f"{detail}/settings"
    page = text(browser.get(settings))
    assert "Scheduled snapshots</h3>" in page and 'placeholder="Keep until deleted"' in page

    bad = {"cfg:snapshots.schedule": "daily", "cfg:limits.memory": "2GiB"}
    response = text(browser.post(settings, bad))
    assert "not a schedule" in response
    assert 'value="daily"' in response and 'value="2GiB"' in response  # kept for fixing
    assert "snapshots.schedule" not in fake.instances["web-1"]["config"]

    good = {
        "cfg:snapshots.schedule": "@daily",
        "cfg:snapshots.expiry": "2w",
        "cfg:snapshots.schedule.stopped": "true",
    }
    browser.post(settings, good)
    config = fake.instances["web-1"]["config"]
    assert config["snapshots.schedule"] == "@daily"
    assert config["snapshots.expiry"] == "2w"
    assert config["snapshots.schedule.stopped"] == "true"


def test_instance_schedule_from_profile_is_shown(browser, sid, fake):
    fake.add_instance("web-1")
    fake.instances["web-1"]["expanded_config"]["snapshots.schedule"] = "0 3 * * *"
    page = text(browser.get(f"/servers/{sid}/instances/web-1"))
    assert "Taken on schedule <code>0 3 * * *</code>" in page


def test_profile_schedule(browser, sid, fake):
    url = f"/servers/{sid}/profiles/big/settings"
    browser.post(url, {"cfg:limits.cpu": "4", "cfg:snapshots.schedule": "@weekly"})
    assert fake.profiles["big"]["config"]["snapshots.schedule"] == "@weekly"
    assert "not a duration" in text(browser.post(url, {"cfg:snapshots.expiry": "forever"}))


def test_volume_schedule(browser, sid, fake):
    fake.add_volume("default", "data", config={"size": "5GiB"})
    edit = f"/servers/{sid}/storage/default/volumes/data/edit"
    assert "not a schedule" in text(browser.post(edit, {"size": "5GiB", "schedule": "nightly"}))
    browser.post(edit, {"size": "5GiB", "schedule": "@daily", "expiry": "7d"})
    assert fake.volumes["default"]["data"]["config"] == {
        "size": "5GiB",
        "snapshots.schedule": "@daily",
        "snapshots.expiry": "7d",
    }
    detail = text(browser.get(f"/servers/{sid}/storage/default/volumes/data"))
    assert "Taken on schedule <code>@daily</code>, kept for 7d." in detail
    browser.post(edit, {"size": "5GiB"})
    assert fake.volumes["default"]["data"]["config"] == {"size": "5GiB"}
