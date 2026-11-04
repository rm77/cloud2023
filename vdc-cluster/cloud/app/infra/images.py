# images.py - tenant base images (catalog on the hub)
from .labhub import expose, hub, hub_read, check


@expose("images.list")
def list(**kw):
    return [dict(name=i["name"], type=i["type"], mem_mb=int(i["mem"] or 0), disk=i["disk"],
                 user=i["user"], sha256=i["sha256"], size_mb=int(i["size_mb"] or 0))
            for i in hub_read("images")]


@expose("images.add", async_only=True)
def add(name, url, type="cloud-init", mem=None, disk=None, user=None, **kw):
    """POST /controllersvc/images.add/async
       {"name":"ubuntu","url":"https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "type":"cloud-init","mem":1024,"disk":"10G","user":"ubuntu"}
       the hub downloads the image, so this can take minutes"""
    args = ["image", "add", check("name", name), check("url", url), "--type", check("type", type)]
    if mem:
        args += ["--mem", check("num", mem, "mem")]
    if disk:
        args += ["--disk", check("size", disk, "disk")]
    if user:
        args += ["--user", check("login", user, "user")]
    hub(*args, timeout=3600)
    return [i for i in list() if i["name"] == name][0]


@expose("images.delete")
def delete(name, **kw):
    check("name", name)
    hub("image", "del", name)
    return dict(deleted=name)
