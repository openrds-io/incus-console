"""Projects: features, limits, restrictions, switching, rename, delete."""

from .test_app import text


def test_list(browser, sid, fake):
    fake.add_instance("web-1")
    fake.projects["lab"] = {
        "name": "lab",
        "description": "Lab <x>",
        "config": {"features.images": "false"},
    }
    page = text(browser.get(f"/servers/{sid}/projects"))
    assert page.index(">default<") < page.index(">lab<")
    assert "(current)" in page and "Lab &lt;x&gt;" in page
    assert "Shared" in page  # lab uses the default project's images
    assert page.count("Switch to") == 1


def test_create_defaults(browser, sid, fake):
    url = f"/servers/{sid}/projects/new"
    form = text(browser.get(url))
    # Features start as Incus's defaults: own images, profiles, volumes, buckets.
    assert 'name="cfg:features.images" value="true" checked' in form
    assert 'name="cfg:features.networks" value="true">' in form
    assert "Default (block)" in form  # nesting once restricted
    assert "bad name" not in fake.projects
    assert "Use letters" in text(browser.post(url, {"name": "bad name"}))
    posted = {
        "name": "team-a",
        "description": "Team A",
        "cfg:features.images": "true",
        "cfg:features.profiles": "true",
        "cfg:limits.instances": "10",
        "cfg:limits.memory": "32GiB",
        "cfg:restricted": "true",
        "cfg:restricted.containers.nesting": "allow",
        "cfg:restricted.devices.disk": "nonsense",
    }
    done = browser.post(url, posted, follow_redirects=True)
    assert "Project team-a created." in text(done)
    config = fake.projects["team-a"]["config"]
    assert config["features.images"] == "true"
    assert config["features.storage.volumes"] == "false"  # unchecked is written as false
    assert config["limits.instances"] == "10" and config["limits.memory"] == "32GiB"
    assert config["restricted"] == "true"
    assert config["restricted.containers.nesting"] == "allow"
    assert "restricted.devices.disk" not in config  # not one of the choices


def test_detail(browser, sid, fake):
    fake.add_instance("web-1")
    fake.projects["default"]["config"] = {"limits.instances": "5"}
    page = text(browser.get(f"/servers/{sid}/projects/default"))
    assert "1 instances" in page and "2 profiles" in page
    assert "<td>Instances</td>" in page and "<td>5</td>" in page
    assert "2.0 GiB" in page  # memory used
    assert "Not restricted" in page
    assert ">Rename<" not in page  # the default project stays


def test_edit_and_yaml(browser, sid, fake):
    fake.projects["lab"] = {"name": "lab", "description": "", "config": {"limits.cpu": "8"}}
    url = f"/servers/{sid}/projects/lab/edit"
    page = text(browser.get(url))
    assert 'value="8"' in page
    browser.post(
        url,
        {
            "description": "Lab",
            "cfg:features.images": "true",
            "cfg:restricted": "true",
            "cfg:restricted.snapshots": "block",
        },
    )
    config = fake.projects["lab"]["config"]
    assert "limits.cpu" not in config
    assert config["restricted.snapshots"] == "block"
    detail = text(browser.get(f"/servers/{sid}/projects/lab"))
    assert "<th>Snapshots</th><td>block</td>" in detail
    yaml_url = f"/servers/{sid}/projects/lab/yaml"
    assert "incus project edit lab" in text(browser.get(yaml_url))
    browser.post(yaml_url, {"yaml": "description: From YAML\nconfig: {}\n"})
    assert fake.projects["lab"]["description"] == "From YAML"


def test_features_of_a_project_in_use(browser, sid, fake):
    fake.add_instance("web-1")
    url = f"/servers/{sid}/projects/default/edit"
    refused = browser.post(url, {"cfg:features.images": "true"}, follow_redirects=True)
    assert "only be changed on empty projects" in text(refused)


def test_switch(browser, sid, fake):
    fake.projects["lab"] = {"name": "lab", "description": "", "config": {}}
    done = browser.post(f"/servers/{sid}/projects/lab/use", follow_redirects=True)
    assert "Now working in project lab." in text(done)
    assert '<option value="lab" selected>' in text(done)


def test_rename_and_delete(browser, sid, fake):
    fake.projects["lab"] = {"name": "lab", "description": "", "config": {}}
    browser.post(f"/servers/{sid}/projects/lab/use")
    renamed = browser.post(f"/servers/{sid}/projects/lab/rename", {"new_name": "lab2"})
    assert renamed.headers["Location"].endswith("/projects/lab2")
    page = text(browser.get(f"/servers/{sid}/projects"))
    assert "lab2</a> <span" in page  # still the current project, under its new name
    fake.project_used_by["lab2"] = ["/1.0/instances/x?project=lab2"]
    busy = browser.post(f"/servers/{sid}/projects/lab2/delete", follow_redirects=True)
    assert "Only empty projects" in text(busy)
    fake.project_used_by["lab2"] = []
    gone = browser.post(f"/servers/{sid}/projects/lab2/delete", follow_redirects=True)
    assert "Project lab2 deleted." in text(gone)
    assert '<option value="default" selected>' in text(gone)


def test_default_is_protected(browser, sid):
    rename = browser.get(f"/servers/{sid}/projects/default/rename", follow_redirects=True)
    assert "cannot be renamed" in text(rename)
    delete = browser.post(f"/servers/{sid}/projects/default/delete", follow_redirects=True)
    assert "cannot be deleted" in text(delete)
