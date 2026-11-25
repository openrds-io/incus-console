"""Groups: bundles of grants given to many users at once."""

import re

from app.db import get_db

from .conftest import login, user_id
from .test_app import text
from .test_user_certs import own_certs, remote  # noqa: F401  (remote is a fixture)


def make_group(admin, name="ops"):
    response = admin.post("/admin/groups/new", {"name": name, "description": "Operations"})
    assert response.status_code == 302, text(response)
    return int(re.search(r"/admin/groups/(\d+)", response.headers["Location"]).group(1))


def join(admin, app, gid, username):
    return admin.post(
        f"/admin/groups/{gid}/members",
        {"user_id": user_id(app, username)},
        follow_redirects=True,
    )


def give(admin, gid, server_id, project, role="operator"):
    return admin.post(
        f"/admin/groups/{gid}/grants",
        {"server_id": server_id, "project": project, "role": role},
        follow_redirects=True,
    )


def drop_own_grants(app, username):
    with app.app_context():
        get_db().execute("DELETE FROM grants WHERE user_id = ?", (user_id(app, username),))
        get_db().commit()


def test_group_grant_gives_access(server_app, setup, admin, fake):
    drop_own_grants(server_app, "vic")
    vic = login(server_app, "vic")
    assert "Lab" not in text(vic.get("/servers"))
    gid = make_group(admin)
    assert "now Operator in project default" in text(give(admin, gid, setup, "default"))
    assert "vic added to ops" in text(join(admin, server_app, gid, "vic"))
    assert "Lab" in text(vic.get("/servers"))
    vic.post(f"/servers/{setup}/instances/web-1/action", {"action": "stop", "back": "detail"})
    assert fake.instances["web-1"]["status"] == "Stopped"


def test_strongest_role_wins_and_leaving_takes_it_away(server_app, setup, admin, fake):
    gid = make_group(admin)  # vic is a Viewer of default on their own
    give(admin, gid, setup, "default", "operator")
    join(admin, server_app, gid, "vic")
    vic = login(server_app, "vic")
    base = f"/servers/{setup}"
    assert ">Stop<" in text(vic.get(f"{base}/instances/web-1"))
    uid = user_id(server_app, "vic")
    admin.post(f"/admin/groups/{gid}/members/{uid}/delete")
    assert vic.post(f"{base}/instances/web-1/action", {"action": "stop"}).status_code == 403
    assert fake.instances["web-1"]["status"] == "Running"
    assert "web-1" in text(vic.get(f"{base}/instances"))  # their own Viewer grant stays


def test_all_projects_through_a_group(server_app, setup, admin):
    gid = make_group(admin)
    give(admin, gid, setup, "", "viewer")
    join(admin, server_app, gid, "vic")
    vic = login(server_app, "vic")
    assert ">team-a<" in text(vic.get(f"/servers/{setup}/projects"))


def test_deleting_a_group_keeps_members_own_access(server_app, setup, admin):
    gid = make_group(admin)
    give(admin, gid, setup, "team-a", "viewer")
    join(admin, server_app, gid, "vic")
    vic = login(server_app, "vic")
    assert ">team-a<" in text(vic.get(f"/servers/{setup}/projects"))
    assert "keep their own access" in text(
        admin.post(f"/admin/groups/{gid}/delete", follow_redirects=True)
    )
    page = text(vic.get(f"/servers/{setup}/projects"))
    assert ">team-a<" not in page and ">default<" in page


def test_group_grants_follow_renamed_and_deleted_projects(server_app, setup, admin):
    gid = make_group(admin)
    give(admin, gid, setup, "team-a", "viewer")
    admin.post(f"/servers/{setup}/projects/team-a/rename", {"new_name": "team-z"})
    assert "team-z" in text(admin.get(f"/admin/groups/{gid}"))
    admin.post(f"/servers/{setup}/projects/team-z/delete")
    assert "No access yet" in text(admin.get(f"/admin/groups/{gid}"))


def test_group_admin_pages(server_app, setup, admin):
    gid = make_group(admin, "Ops Team")
    assert "Ops Team" in text(admin.get("/admin/groups"))
    assert "already exists" in text(
        admin.post("/admin/groups/new", {"name": "ops team"}, follow_redirects=True)
    )
    assert "starting with a letter" in text(
        admin.post("/admin/groups/new", {"name": "-bad"}, follow_redirects=True)
    )
    admin.post(f"/admin/groups/{gid}/settings", {"name": "Ops", "description": "Night shift"})
    assert "Night shift" in text(admin.get(f"/admin/groups/{gid}"))
    give(admin, gid, setup, "team-a", "project_admin")
    # Joining from the user's page, and what the user page and list show.
    uid = user_id(server_app, "otto")
    page = text(admin.post(f"/admin/users/{uid}/groups", {"group_id": gid}, follow_redirects=True))
    assert "otto added to Ops" in page and "Through groups" in page and "team-a" in page
    assert "1 grant, 1 group" in text(admin.get("/admin/users"))
    # The account page says where each role comes from.
    account = text(login(server_app, "otto").get("/account"))
    assert "You directly" in account and "Ops" in account
    # Removing from the user's page goes back there.
    response = admin.post(f"/admin/groups/{gid}/members/{uid}/delete", {"back_user": uid})
    assert response.headers["Location"].endswith(f"/admin/users/{uid}")
    assert "Not in any group" in text(admin.get(f"/admin/users/{uid}"))


def test_groups_are_for_admins_only(server_app, setup):
    vic = login(server_app, "vic")
    assert vic.get("/admin/groups").status_code == 403
    assert vic.post("/admin/groups/new", {"name": "mine"}).status_code == 403


def test_group_changes_update_restricted_certificates(remote, fake):  # noqa: F811
    admin = login(remote, "root")
    gid = make_group(admin)
    give(admin, gid, 1, "team-a")
    assert own_certs(fake) == []  # no members yet
    join(admin, remote, gid, "vic")
    (cert,) = own_certs(fake)
    assert cert["name"] == "incus-console/vic" and cert["projects"] == ["team-a"]
    give(admin, gid, 1, "default", "viewer")
    assert own_certs(fake)[0]["projects"] == ["default", "team-a"]
    vic = login(remote, "vic")
    assert "web-1" in text(vic.get("/servers/1/instances?project=default"))
    with remote.app_context():
        grant_id = (
            get_db().execute("SELECT id FROM group_grants WHERE project = 'default'").fetchone()[0]
        )
    admin.post(f"/admin/groups/{gid}/grants/{grant_id}/delete")
    assert own_certs(fake)[0]["projects"] == ["team-a"]
    uid = user_id(remote, "vic")
    admin.post(f"/admin/groups/{gid}/members/{uid}/delete")
    assert own_certs(fake) == []
    join(admin, remote, gid, "vic")
    assert len(own_certs(fake)) == 1
    admin.post(f"/admin/groups/{gid}/delete")
    assert own_certs(fake) == []
