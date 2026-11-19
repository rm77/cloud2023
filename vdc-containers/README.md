# vdc-containers: a container service for vdc-cluster

Tenants run Docker containers and Compose stacks with one API call, without setting up a VM
first. The add-on puts them on **container hosts**: ordinary VMs of the tenant, named
`ctr-<tenant>-1`, `-2`, ..., which it creates through the cloud API with the tenant's own login.
The tenant's quota, ownership and VM-hour billing apply to them as to any VM, and they show up in
the tenant's VM list. The add-on sets each host up over SSH (Docker, Docker Compose and a small
helper, `vdc-host`) and runs every container action there.

It is separate from the kit and changes nothing in it.

| File | What it is |
|---|---|
| `vdc_containers.py` | the service (Python 3.8+, standard library only) |
| `host/vdc-host` | the helper it installs on each host; every action on a host goes through it |
| `host/host-setup.sh` | makes a VM a container host: Docker, Docker Compose, vdc-host (Alpine or Ubuntu/Debian) |
| `image/build.sh`, `image/docker-host.pkr.hcl` | optional: bake a host image with all of that already in it |
| `examples/containers.py` | a custom metric for `vdc_meter.py`: vCPU, memory or count of running containers |
| `test_containers.py` | tests against a stand-in API and stand-in hosts: `python3 test_containers.py` |

## Running it

On the main PC (it needs the routes to the tenant subnets, which the main PC has), with the cloud
API running, as any user with an `ssh` client:

```sh
python3 vdc_containers.py --api http://127.0.0.1:36100
# vdc-containers on http://0.0.0.0:8091/addon/containers  ->  API http://127.0.0.1:36100; hosts: medium alpine, ...
```

Everything it keeps is in `state/` next to it: the database (`containers.db`), its own SSH key
(`id_containers`) and the hosts' SSH host keys. `--check` tells whether the API answers and the
key exists.

| Option | Default | |
|---|---|---|
| `--api` | `$VDC_API`, else `http://127.0.0.1:36100` | the cloud API |
| `--bind`, `--port` | `0.0.0.0`, `8091` | where it listens |
| `--host-image` | `alpine` | image of a new host: an Alpine or Ubuntu cloud-init image of the kit (`hubctl images`) |
| `--host-flavor` | `medium` | machine type of a new host (its memory is what containers can share) |
| `--max-hosts` | `2` | container hosts per tenant |
| `--reserve-mb` | `256` | memory of each host kept for the host itself |
| `--host-ports` | `20000-20999` | ports on a host for the containers' published ports |
| `--state` | `./state` | database, SSH key, known hosts |

The same settings can come from the environment: `VDC_CONTAINERS_PORT`, `_IMAGE`, `_FLAVOR`,
`_MAX_HOSTS`, `_RESERVE_MB`, `_HOST_PORTS`, `_STATE`. To keep it running, start it from a systemd
unit, `tmux`, or `nohup ... &`.

With the web console, start `portal.py` with `--addon containers=http://127.0.0.1:8091`; it then
passes `/addon/containers/...` on to this service, so the console's origin reaches it too.

## Using it

Every call carries the caller's cloud API token, exactly as for the API itself
(`Authorization: Bearer <token>` from `POST /auth/token`). A tenant user sees and changes only
their own tenant's containers; the provider sees all of them and can delete or stop any; an
auditor can only read.

```sh
TOKEN=$(curl -s -d 'username=alice&password=...' http://<lab-pc>:36100/auth/token | jq -r .access_token)
A=http://<lab-pc>:8091/addon/containers

curl -s $A -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"name": "web", "image": "nginx:alpine", "ports": [80], "expose": "ports"}'
# {"job": "container-create-3f2a...", "status_url": "/addon/containers/jobs/container-create-3f2a..."}
curl -s $A/jobs/container-create-3f2a... -H "Authorization: Bearer $TOKEN"     # state and log
curl -s $A/web -H "Authorization: Bearer $TOKEN"                                 # ports[0].connect: <lab-pc>:47000
```

| Call | |
|---|---|
| `POST /addon/containers` | run a container (`202` + job) |
| `GET /addon/containers` · `/{name}` | my containers; `?state=true` asks the hosts for the live state |
| `DELETE /addon/containers/{name}` | remove it, with its ports, address and floating IP (`202` + job) |
| `POST /addon/containers/{name}/start` · `/stop` · `/restart` | power |
| `GET /addon/containers/{name}/logs?tail=100` | its output |
| `POST /addon/containers/{name}/exec` `{"command": ["ls", "/"]}` | run a command in it (30 s at most) |
| `POST /addon/containers/stacks` · `GET` · `GET/DELETE /stacks/{name}` · `/stacks/{name}/logs` | Compose stacks |
| `GET /addon/containers/hosts` · `DELETE /hosts/{name}` | the container hosts; delete an empty one |
| `GET /addon/containers/jobs` · `/jobs/{id}` | jobs, each with a log of what was run where |
| `GET /addon/containers/config` | the settings above, for a front end |

Provider and auditor calls take `?tenant=` to narrow the lists.

### A container

```json
{
  "name": "web",
  "image": "nginx:alpine",
  "command": ["nginx", "-g", "daemon off;"],
  "env": {"GREETING": "hello"},
  "cpus": 0.5,
  "mem_mb": 256,
  "ports": [80, {"port": 5353, "protocol": "udp"}],
  "expose": "ports"
}
```

Only `name` and `image` are needed. `cpus` (default 0.5) and `mem_mb` (default 256) become
Docker's `--cpus` and `--memory`; the memory also decides where the container goes. Containers
restart by themselves after a crash or a host reboot (`--restart unless-stopped`) unless they
were stopped on purpose.

`expose` says who reaches the ports:

| `expose` | Reachable at | Uses |
|---|---|---|
| `internal` (default) | `<host address>:<host port>` from the tenant's own subnet | nothing |
| `ports` | `<lab-pc>:<campus port>` from campus, and as above | one tenant port of the kit per port (`max_ports`) |
| `floating` | its own floating IP, on its own port numbers, from campus | one extra address and one floating IP (`max_ips`, `max_floating_ips`) |

With `internal` and `ports`, each container port is published on a free host port from
20000-20999 (`host_port` in the answer); with `ports`, the answer also has the campus port and a
`connect` line. With `floating`, the container gets an address of its own in the tenant subnet
(`address`), the host takes it as a second address, and the floating IP is attached to it: the
container is reached on `<floating IP>:80`, like a machine of its own.

### Where containers go

A container goes to the tenant's fullest host that still has `mem_mb` free (the host's memory
minus `--reserve-mb`, minus what the other containers and stacks reserve). If none has room and
the tenant has fewer than `--max-hosts` hosts, the add-on creates a new one: it adds its SSH key
to the tenant (`POST /tenant/keys`, shown as `vdc-containers` in the tenant's keys), creates the
VM (`POST /tenant/vms`), waits for SSH, and runs `host-setup.sh`. The first container of a tenant
therefore takes a few minutes; the job log shows each step. Hosts stay when their containers are
gone, so the next container starts at once; delete an empty host with
`DELETE /addon/containers/hosts/{name}`.

A host is a normal VM of the tenant, so the tenant's own SSH keys work on it too, and
`docker ps` there shows the containers (labelled `vdc.managed=1`). What separates tenants is the
VM, not Docker: each tenant's containers run only on its own hosts.

### Compose stacks

```json
{
  "name": "shop",
  "compose": "services:\n  web:\n    image: nginx\n    ports: ['8081:80']\n  db:\n    image: postgres:16\n    environment: {POSTGRES_PASSWORD: example}\n",
  "mem_mb": 768,
  "ports": [8081],
  "expose": "ports"
}
```

The add-on doesn't read the compose file: `mem_mb` is what to keep free for the whole stack on
its host, and `ports` lists the host ports the file publishes, so they can be given campus ports
(`expose: "ports"`) and kept apart from other stacks on the same host. Keep them outside
20000-20999. `GET /addon/containers/stacks/shop?state=true` lists the stack's containers.

## The host image

Without anything else, a new host installs Docker from Alpine's (or Ubuntu's) package
repositories on its first start, so the tenant needs internet then, and pulls container images
from Docker Hub when they are first run. For a lab without internet, or just to start hosts
faster, bake an image with Docker and some container images already in it:

```sh
image/build.sh /path/to/alpine-cloudinit.qcow2 nginx:alpine alpine:3.20 postgres:16
sudo ./cluster.sh image add docker-alpine /path/to/vdc-containers/state/image/docker-alpine.qcow2 --user alpine   # in the kit
python3 vdc_containers.py --host-image docker-alpine
```

`build.sh` needs `packer`, `qemu-system-x86_64` and `xorriso`; Packer keeps its plugins and cache
in `state/packer`. Start from the same Alpine cloud image you gave the kit's `image add`.

## Billing

The hosts are VMs, so the kit already bills their running hours at their machine type's price.
To bill the containers themselves, add a metric to `vdc_meter.py` (vdc-portal) that runs
`examples/containers.py`:

```json
{"name": "container-vcpu", "label": "Container vCPU", "source": "command",
 "command": "VDC_CONTAINERS=http://127.0.0.1:8091 python3 /path/to/vdc-containers/examples/containers.py cpus",
 "kind": "gauge", "per": "hour", "unit": "vCPU-hour", "price": 0.01}
```

`cpus` meters each running container's vCPUs, `mem_gb` its memory, `count` 1 per container.

## What a host runs

Every action ends as one `vdc-host` call on a host, as root (`doas` or `sudo`), and the job log
shows it, for example:

```
ctr-tenant-a-1$ doas -n /usr/local/bin/vdc-host run --name web --label vdc.managed=1 --label vdc.tenant=tenant-a
                --restart unless-stopped --cpus 0.5 --memory 256m -p 20000:80/tcp nginx:alpine
```

`vdc-host` itself is a short shell script (`host/vdc-host`): `docker run`, `docker logs`,
`docker compose up`, and `ip addr` for the extra addresses, which it keeps in `/etc/vdc/addrs`
and puts back at boot.

## Troubleshooting

| Problem | What to check |
|---|---|
| a job fails at "waiting for SSH" | the host VM booted? (`hubctl vm console NAME` in the kit); the add-on runs on the main PC (it needs the route to the tenant subnet) |
| `can't become root without a password` | the host image's default user has no password-less `doas`/`sudo`; use another image, or allow it in the image |
| `host-setup: installing docker` fails | the tenant's internet is off, or the package mirror can't be reached: switch it on for the first start, or bake an image |
| `pull access denied` / `manifest unknown` | the image name or tag is wrong, or the host can't reach the registry (internet off: bake the image with it) |
| `no container host has N MB free, and the tenant has the most hosts allowed` | delete containers, raise `--max-hosts`, or use a bigger `--host-flavor` |
| `quota exceeded` from the cloud API | the tenant's VM, port, address or floating IP quota: the provider raises it (`PUT /provider/tenants/{name}/quota`) |
| a container is `error` | its job's log (`GET /addon/containers/jobs/{id}`); delete it and create it again |
| `?state=true` shows `unknown` | the host doesn't answer SSH: is the VM running? |
