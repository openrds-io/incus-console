"""A file browser for an instance: list folders, view and edit text files,
download, upload, make folders, delete."""

from __future__ import annotations

import posixpath
from urllib.parse import quote

from flask import Response, flash, g, redirect, render_template, request, url_for

from ..incus import IncusError
from .scoped import bp

MAX_LISTED = 1000  # entries looked up per folder
EDITABLE_BYTES = 512 * 1024  # text files up to this size can be viewed and edited
CHUNK = 64 * 1024


def clean(value: str | None) -> str:
    """An absolute, normalised path inside the instance."""
    normal = posixpath.normpath("/" + (value or "/").lstrip("/"))
    return "/" + normal.lstrip("/")


def _check_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise IncusError(400, "A name cannot be empty, . or .., or contain a slash.")
    return name


def _crumbs(name: str, file_path: str) -> list:
    items = [
        ("Instances", url_for("srv.instances")),
        (name, url_for("srv.instance", name=name)),
        ("Files", url_for("srv.files", name=name, path="/")),
    ]
    parts = [p for p in file_path.split("/") if p]
    for index, part in enumerate(parts):
        link = url_for("srv.files", name=name, path="/" + "/".join(parts[: index + 1]))
        items.append((part, None if index == len(parts) - 1 else link))
    if not parts:
        items[-1] = ("Files", None)
    return items


def _back_to(name: str, folder: str):
    return redirect(url_for("srv.files", name=name, path=folder))


@bp.get("/instances/<name>/files")
def files(name):
    folder = clean(request.args.get("path"))
    try:
        info = g.incus.file_info(name, folder)
    except IncusError as error:
        if error.status != 404:
            raise
        flash(error.message, "error")
        if folder == "/":
            return redirect(url_for("srv.instance", name=name))
        return _back_to(name, posixpath.dirname(folder))
    if info["type"] != "directory":
        return redirect(url_for("srv.file_view", name=name, path=folder))
    names = g.incus.list_directory(name, folder)
    entries = []
    for entry in names[:MAX_LISTED]:
        full = posixpath.join(folder, entry)
        try:
            details = g.incus.file_info(name, full)
        except IncusError as error:
            if error.status == 0:
                raise
            details = {"type": "unknown"}
        entries.append({"name": entry, "path": full, **details})
    entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
    return render_template(
        "files/list.html",
        name=name,
        folder=folder,
        parent=posixpath.dirname(folder) if folder != "/" else None,
        entries=entries,
        more=max(0, len(names) - MAX_LISTED),
        crumbs=_crumbs(name, folder),
        instance=g.incus.instance(name),
    )


def _is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@bp.get("/instances/<name>/files/view")
def file_view(name):
    file_path = clean(request.args.get("path"))
    info, response, conn = g.incus.open_file(name, file_path)
    try:
        if info["type"] == "directory":
            return redirect(url_for("srv.files", name=name, path=file_path))
        data = response.read(EDITABLE_BYTES + 1)
    finally:
        conn.close()
    editable = len(data) <= EDITABLE_BYTES and _is_text(data)
    return render_template(
        "files/view.html",
        name=name,
        file_path=file_path,
        folder=posixpath.dirname(file_path),
        info=info,
        text=data.decode("utf-8") if editable else None,
        too_big=len(data) > EDITABLE_BYTES,
        crumbs=_crumbs(name, file_path),
    )


@bp.post("/instances/<name>/files/edit")
def file_edit(name):
    file_path = clean(request.args.get("path"))
    info = g.incus.file_info(name, file_path)
    text = request.form.get("content", "")
    # Browsers send CRLF line ends from a textarea; files on Linux use LF.
    data = text.replace("\r\n", "\n").encode()
    try:
        g.incus.write_file(
            name, file_path, data, len(data), mode=info["mode"], uid=info["uid"], gid=info["gid"]
        )
    except IncusError as error:
        flash(error.message, "error")
    else:
        flash(f"Saved {file_path}.", "success")
    return redirect(url_for("srv.file_view", name=name, path=file_path))


def _disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@bp.get("/instances/<name>/files/download")
def file_download(name):
    file_path = clean(request.args.get("path"))
    info, response, conn = g.incus.open_file(name, file_path)
    if info["type"] == "directory":
        conn.close()
        flash("Folders cannot be downloaded; download the files in it.", "error")
        return redirect(url_for("srv.files", name=name, path=file_path))

    def stream():
        try:
            while chunk := response.read(CHUNK):
                yield chunk
        finally:
            conn.close()

    headers = {
        "Content-Disposition": _disposition(posixpath.basename(file_path) or "file"),
        "Cache-Control": "no-store",
    }
    if info["size"] is not None:
        headers["Content-Length"] = str(info["size"])
    return Response(stream(), mimetype="application/octet-stream", headers=headers)


@bp.post("/instances/<name>/files/upload")
def file_upload(name):
    folder = clean(request.args.get("path"))
    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        flash("Choose one or more files to upload.", "error")
        return _back_to(name, folder)
    done = []
    for upload in uploads:
        try:
            filename = _check_name(posixpath.basename(upload.filename.replace("\\", "/")))
            stream = upload.stream
            stream.seek(0, 2)
            length = stream.tell()
            stream.seek(0)
            g.incus.write_file(
                name, posixpath.join(folder, filename), stream, length, mode=0o644, uid=0, gid=0
            )
        except IncusError as error:
            flash(f"{upload.filename}: {error.message}", "error")
        else:
            done.append(filename)
    if done:
        flash(f"Uploaded {', '.join(done)}.", "success")
    return _back_to(name, folder)


@bp.post("/instances/<name>/files/mkdir")
def file_mkdir(name):
    folder = clean(request.args.get("path"))
    try:
        new = _check_name(request.form.get("folder", ""))
        g.incus.write_file(
            name, posixpath.join(folder, new), b"", 0, mode=0o755, uid=0, gid=0, kind="directory"
        )
    except IncusError as error:
        flash(error.message, "error")
    else:
        flash(f"Created folder {new}.", "success")
    return _back_to(name, folder)


@bp.post("/instances/<name>/files/delete")
def file_delete(name):
    file_path = clean(request.args.get("path"))
    folder = posixpath.dirname(file_path)
    if file_path == "/":
        flash("The root folder cannot be deleted.", "error")
    else:
        try:
            g.incus.delete_file(name, file_path)
        except IncusError as error:
            flash(error.message, "error")
        else:
            flash(f"Deleted {posixpath.basename(file_path)}.", "success")
    return _back_to(name, folder)
