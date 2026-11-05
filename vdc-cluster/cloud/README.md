# cloud/: the cloud API of vdc-cluster, organised by NIST CCRA

A FastAPI service that turns the vdc-cluster lab into a small IaaS cloud with three kinds of users.
The URLs, roles and documentation sections follow the NIST Cloud Computing Reference Architecture
(SP 500-292), so `/docs` shows the architecture as well as the API.

| Prefix | CCRA actor | Role | Can do |
|---|---|---|---|
| `/tenant` | Cloud Consumer | `consumer` | its own tenant only: VMs, network, jobs, usage, members |
| `/provider` | Cloud Provider | `provider` | onboard tenants, quotas, flavors, images, nodes, every tenant's VMs |
| `/audit` | Cloud Auditor | `auditor` (and provider) | read only: audit log, reports, metering, the cluster's command journal |
| `/carrier` | Cloud Carrier | `provider`, `auditor` | underlay health |
| `/auth` | Security | everyone | log in, who am I |

It is part of the kit: `sudo ./cluster.sh cloud install && sudo ./cluster.sh cloud start` on the
main PC, after `./cluster.sh up`.

## Architecture

```
Swagger /docs, portals, scripts
        │  Authorization: Bearer <JWT>        role + tenant are inside the token
        ▼
app/main.py ── routers/  auth  tenant  provider  audit  carrier
        │      every route: role check (router) + ownership check (my_vm, my_job)
        ▼
app/services.py   quotas · address pool · audit log · jobs · merged VM view
        │                     │                            │
        ▼                     ▼                            ▼
SQLite (app/db.py)      Redis + RQ → worker          app/infra/
users, tenants↔VNI,     app/tasks.py:                  labhub.hub()     ssh hub hubctl ...
quotas, flavors, VMs'   create/delete VM,              labhub.hub_read  /var/lib/lab registries
owner+flavor, the job   add image, add spokes          cluster()        sudo cluster.sh ...
queue, audit, usage     (app/worker.py)
```

The cluster stays the source of truth for infrastructure: it picks addresses and nodes, builds
tunnels and runs VMs. The API keeps only what the cluster doesn't know: who owns what, what
they may use, what it costs, and what happened.

## The CCRA mapping

| CCRA component | Where |
|---|---|
| Consumer / Provider / Auditor / Carrier | the `/tenant`, `/provider`, `/audit`, `/carrier` routers and the three roles |
| Business support: customer management | `POST /provider/tenants` (onboarding), users |
| Contract management | quotas: `PUT /provider/tenants/{t}/quota`, checked on every `POST /tenant/vms` |
| Pricing and rating | flavors with a price per hour |
| Accounting and billing | `app/metering.py` samples running VMs → `/tenant/usage`, `/provider/usage` |
| Reporting and auditing | audit log of every change and every refusal; `/audit/reports/{t}` |
| Inventory | `/provider/inventory` (one document: nodes, tenants, VMs; the NetBox feed) |
| Provisioning: rapid provisioning | `POST /tenant/vms` → `202` + job; the hub picks address and node |
| Resource changing | delete VMs; spokes: `POST /provider/nodes` (rapid elasticity) |
| Monitoring and reporting | `/provider/nodes`, `/provider/health`, VM power state |
| Portability and interoperability | cloud images (`/provider/images`), the OpenAPI contract `/openapi.json` |
| Security | JWT login, scrypt password hashes, role per router, ownership per object, input patterns = the kit's own rules |
| Service orchestration: service layer | the `/tenant` resources |
| Resource abstraction and control | `hubctl`, `lab-agent`, libvirt, Geneve (reached through `app/infra`) |
| Physical resource layer | `/provider/hosts` (the lab PC's VMs) |

## Rules worth knowing

- **The tenant comes from the token.** No tenant route accepts a tenant name, so a consumer
  cannot even ask for another tenant.
- **Another tenant's objects are "not found".** `GET /tenant/vms/b1` as tenant-a answers `404`,
  not `403`, so a tenant can't find out which names exist elsewhere.
- **VM names are unique in the whole cluster.** The hub keys guests by name, so a name taken by
  another tenant gives `409`.
- **Quotas count VMs from the moment they are accepted.** Two quick requests can't both slip
  under the limit. A VM whose creation failed (`status: error`) no longer counts; delete it to
  remove the record.
- **Onboarding without `vni`/`subnet`** takes the lowest free VNI from 100–254 and the subnet
  `10.<vni>.20.0/24` (your pattern: VNI 199 → 10.199.20.0/24). Both are checked against the hub's
  tenants, each other, and the underlay 10.20.0.0/16.
- **Tenants and VMs made with `hubctl` directly are not in the API.** The API lists what it
  created itself. `GET /provider/unmanaged` shows what exists only on the hub, and
  `POST /provider/import` adopts it: tenants get the default quota and no users (add an admin
  with `POST /provider/users`), and VMs get the flavor of their size or `custom`, with `hubctl`
  as their owner.
- **Web consoles use the URL as the secret.** `POST .../console` returns
  `/console/<32 hex>/`; whoever opens it gets the console, without logging in to the API, so
  hand it only to the person who asked. It works for one browser session and expires after
  `WEBCONSOLE_MINUTES`. ttyd on the hub only accepts connections from the main PC, with a
  per-session password that only the API knows.
- **Requests through a Cloudflare tunnel only reach `/console/`.** The API recognises them by
  Cloudflare's headers (`Cf-Ray`, `Cf-Connecting-Ip`) and their trycloudflare.com host, and
  answers anything else with 404, so a public console doesn't publish `/docs` or the logins.
- **Tunnels count against `max_tunnels`** in the tenant's quota (default 2), including public
  consoles. They close on expiry, on `DELETE`, when the API stops, or when their `cloudflared`
  dies.
- **Onboarding takes `"internet": false`** for tenants that start without internet access.
- **Errors:** `409` = the cluster refused (the message is hubctl's own), `502` = the hub couldn't
  be reached.

## Endpoints

| | Section | |
|---|---|---|
| `POST /auth/token` | Security | log in: username + password → bearer token |
| `GET /auth/me` | Security | who am I |
| `GET /tenant/me` | Business support | my tenant: network, quota, current usage |
| `GET /tenant/flavors` | Business support | what I can order, and its price |
| `GET /tenant/usage` | Business support | my metered usage and cost |
| `GET /tenant/users` · `POST` | Business support | users of my tenant; add one (tenant admin) |
| `GET /tenant/images` | Portability | images I can start a VM from |
| `GET /tenant/network` | Provisioning | my subnet, gateway and address allocations |
| `GET /tenant/vms` · `POST` | Provisioning | my VMs (`?state=true` for power state); create one (`202`) |
| `GET /tenant/vms/{name}` · `DELETE` | Provisioning | one of my VMs; delete it (`202`) |
| `GET /tenant/jobs` · `/{id}` | Provisioning | my jobs |
| `GET /provider/tenants` · `POST` | Business support | all tenants; onboard one |
| `GET/PATCH/DELETE /provider/tenants/{name}` | Business support | one tenant; off-board (refused while it has VMs) |
| `PUT /provider/tenants/{name}/quota` | Business support | contract management |
| `GET /provider/users` · `POST` · `DELETE /{u}` | Business support | every account: providers, auditors, tenant users |
| `GET /provider/unmanaged` · `POST /provider/import` | Business support | what exists only on the hub (made with hubctl); adopt it |
| `GET /provider/flavors` · `PUT/DELETE /{name}` | Business support | flavors and prices |
| `GET /provider/usage` | Business support | accounting across tenants |
| `GET /provider/images` · `POST` · `DELETE /{name}` | Portability | image catalog; import a cloud image (`202`) |
| `GET /provider/nodes` · `POST` · `DELETE /{name}` | Monitoring / Provisioning | members; add spokes (`202`); evict |
| `GET /provider/hosts` · `/inventory` · `/health` | Monitoring & inventory | physical layer, inventory, health |
| `GET /provider/vms` · `DELETE /{name}` | Provisioning | VMs of all tenants with their node |
| `GET /provider/jobs` · `/{id}` | Provisioning | all jobs |
| `GET /tenant/access` | Access | my gateway, internet, jump host, an `ssh -J` command per guest, a `~/.ssh/config` |
| `GET /tenant/keys` · `POST` · `DELETE /{id}` | Access | my SSH keys: jump host login, put into new guests |
| `POST /tenant/vms/{name}/console` | Access | web console (ttyd) of my VM: open the returned URL; `{"public": true}` adds a `public_url` through a tunnel |
| `GET /tenant/consoles` · `DELETE /{id}` | Access | my open web consoles |
| `PATCH /provider/tenants/{name}` `{"internet": false}` | Business support | switch a tenant's internet off or on |
| `GET /provider/tenants/{name}/access` · `/keys` | Access | the same for any tenant |
| `POST /tenant/vms/{name}/tunnels` `{"protocol": "http", "port": 80}` | Access | publish a port of my VM on the internet (Cloudflare quick tunnel) |
| `GET /tenant/tunnels` · `DELETE /{id}` | Access | my open tunnels |
| `GET /provider/tunnels` · `DELETE /{id}` | Access | every open tunnel |
| `POST /provider/vms/{name}/console` | Access | web console of any VM |
| `GET /console/{id}/` | (browser) | the console page and its WebSocket, passed through to ttyd on the hub |
| `GET /audit/events` | Auditor | API audit log (filter by tenant, user, action, result, since) |
| `GET /audit/reports/{tenant}` | Auditor | contract, usage, VMs, activity, refusals, cost |
| `GET /audit/metering` | Auditor | usage of every tenant |
| `GET /audit/journal?node=&tool=&lines=` | Auditor | what the cluster really ran (the kit's command journal) |
| `GET /carrier/underlay` | Carrier | full-MTU path to the hub, node reachability |

## Running it (from the kit folder, main PC)

```sh
sudo ./cluster.sh cloud install      # Python environment, Redis, cloudflared, JWT secret, provider password
sudo ./cluster.sh cloud start        # api + worker + meter; prints http://<LABPC_IP>:36100/docs
sudo ./cluster.sh cloud status
sudo ./cluster.sh cloud admin        # the provider login (admin / generated password)
sudo ./cluster.sh cloud logs worker  # redis | api | worker | meter   (all in logs/cloud/)
sudo ./cluster.sh cloud enable       # optional: systemd units, survive reboots
sudo ./cluster.sh cloud stop
```

The API uses four processes:
- **redis:** a private Redis for the job queue, on a Unix socket in `state/cloud/` (no TCP port).
- **api:** uvicorn on `CLOUD_PORT`.
- **worker:** `python -m app.worker`, an RQ worker that runs the queued jobs one at a time.
  Each job's state (queued, started, finished, failed) is also kept in the API's database,
  so `GET .../jobs` works the same whichever queue is used. With `CLOUD_QUEUE=sqlite` the
  worker reads its jobs straight from the database and no Redis runs at all.
- **meter:** samples running VMs every `CLOUD_METER_MINUTES`, and closes expired tunnels and
  consoles.

Cloudflare tunnels are further processes, one `cloudflared` each, which the API starts and
stops itself.

They run as root, like `cluster.sh`, because they read `state/id_lab` and call `cluster.sh`.

Everything stays inside the kit folder. `install` creates a private Python environment in
`state/cloud/venv` (with the pip cache and bytecode next to it) and installs no system packages;
it only needs `python3`, version 3.10 or newer. For Redis it uses a `redis-server` program on
PATH if there is one, or builds one into `state/cloud/redis`; it downloads `cloudflared` into
`state/cloud/bin`. If that Python has no `venv` module, it
bootstraps pip inside the environment instead of asking for `python3-venv`. The only step
outside the folder is the optional `cloud enable`, which writes systemd unit files
(`cloud disable` removes them).

| Where | What |
|---|---|
| `cloud/` | the code (part of the kit; goes into git) |
| `state/cloud/` | `venv/`, `pip-cache/`, `pycache/`, `cloud.db` (SQLite), `redis.conf`, `redis.sock`, `redis.rdb`, `bin/cloudflared`, `tunnels/` (one log per tunnel), `jwt-secret`, `admin-password`, `cloud.env`, pid files (git-ignored) |
| `logs/cloud/` | `redis.log`, `api.log`, `worker.log`, `meter.log` |
| `cluster.conf` | `CLOUD_PORT`, `CLOUD_BIND`, `CLOUD_ADMIN_USER`, `CLOUD_VNI_MIN/MAX`, `CLOUD_SUBNET_TEMPLATE`, `CLOUD_METER_MINUTES` |

Back up `state/cloud/cloud.db` together with the rest of `state/`: it holds the users, tenants'
quotas, VM ownership, the audit log and the usage records.

## Logging in from /docs

Open `http://<LABPC_IP>:36100/docs` and click **Authorize**. Enter a username and password
and leave `client_id` and `client_secret` empty. After **Authorize** and **Close**, every call
on the page is sent with your token.

- As the provider, use `admin` and the password from `sudo ./cluster.sh cloud admin`.
- As a tenant, use the `admin_username` / `admin_password` given when the tenant was created,
  or an account the tenant admin added with `POST /tenant/users`. Click **Logout** first if
  you were logged in as someone else.
- As an auditor, use an account the provider created with `"role": "auditor"`.

A login lasts 8 hours (`VDC_TOKEN_MINUTES`). A `403` that names a role means you are logged in
as the wrong kind of user.

## A walk-through with curl

```sh
API=http://10.20.1.1:36100
tok() { curl -s -d "username=$1&password=$2" $API/auth/token | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'; }
P="Authorization: Bearer $(tok admin admin-pass-123)"

# provider onboards the two tenants of the slides
curl -s -H "$P" -X POST $API/provider/tenants -H 'Content-Type: application/json' \
  -d '{"name":"tenant-a","vni":199,"subnet":"10.199.20.0/24","admin_username":"alice","admin_password":"alice-pass-1"}'
curl -s -H "$P" -X POST $API/provider/tenants -H 'Content-Type: application/json' \
  -d '{"name":"tenant-b","vni":198,"subnet":"10.198.20.0/24","admin_username":"bob","admin_password":"bob-pass-12"}'

# alice works in her tenant
A="Authorization: Bearer $(tok alice alice-pass-1)"
curl -s -H "$A" $API/tenant/me
curl -s -H "$A" -X POST $API/tenant/vms -H 'Content-Type: application/json' -d '{"name":"a1","flavor":"small"}'
#   {"job":"vm-create-9c1e...","status_url":"/tenant/jobs/vm-create-9c1e..."}
curl -s -H "$A" $API/tenant/jobs/vm-create-9c1e...          # queued → started → finished
curl -s -H "$A" $API/tenant/vms/a1                          # status active, ip 10.199.20.10, state running

# the rules
B="Authorization: Bearer $(tok bob bob-pass-12)"
curl -s -o /dev/null -w '%{http_code}\n' -H "$B" $API/tenant/vms/a1        # 404: not bob's
curl -s -o /dev/null -w '%{http_code}\n' -H "$A" $API/provider/tenants     # 403: not a provider

# an auditor
curl -s -H "$P" -X POST $API/provider/users -H 'Content-Type: application/json' \
  -d '{"username":"tari","password":"tari-pass-1","role":"auditor"}'
T="Authorization: Bearer $(tok tari tari-pass-1)"
curl -s -H "$T" "$API/audit/events?result=denied"
curl -s -H "$T" "$API/audit/journal?node=hv101&tool=virsh&lines=20"
```

Or open `http://10.20.1.1:36100/docs`, press **Authorize**, log in as any user, and try each call.

## Tests

`tests/` runs the whole story against a stand-in hub (`tests/fakehub.py`), with its own
temporary database.
That story is onboarding, isolation, quotas, a failing job, metering and the auditor's view:

```sh
sudo ./cluster.sh cloud install --dev && sudo ./cluster.sh cloud test     # 43 passed
```

## What is not here yet (needs small changes to the kit)

| Feature | Kit change |
|---|---|
| VM start / stop / resize | `hubctl vm start|stop NAME` (virsh on the guest's node) |
| VM export / snapshot | qcow2 export on the node, served by the hub |
| Broker (several clusters) | a `cluster` field and one `labhub` target per cluster |
| Web portals | separate front ends on this API; `/docs` serves meanwhile |
| Floating IPs, security groups | the next step after the tenant router, jump host and tunnels |
