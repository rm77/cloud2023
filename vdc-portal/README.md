# vdc-portal: a web console for vdc-cluster

A web console in the style of a public cloud's, for the cloud API of vdc-cluster (the kit's
`cloud/` folder). Tenants create and run VMs and follow their bill; the provider onboards
tenants, sets quotas and prices and watches the nodes; the auditor reads the audit log and the
metering.

It is separate from the kit and changes nothing in it: it uses the API's endpoints as they are.

| File | What it is |
|---|---|
| `index.html` | the console: one page, HTML and JavaScript, no build step, nothing loaded from the internet |
| `portal.py` | serves the page and passes its API calls on to the cloud API (Python 3.8+, standard library only) |
| `test_portal.py` | tests for `portal.py` against a stand-in API: `python3 test_portal.py` |
| `vdc-passwd.sh` | sets the password of any API account, the provider's own included (main PC, root) |
| `vdc_meter.py` | meters additional billing metrics through the API, at your own prices |
| `metrics.json` | the metrics `vdc_meter.py` measures and their prices |
| `examples/accounts.py` | a custom metric from the API: user accounts per tenant |
| `examples/traffic.sh` | a custom metric from the cluster's command line: network traffic per VM |
| `test_meter.py` | tests for `vdc_meter.py` and the console's `/addon/` calls: `python3 test_meter.py` |

## Running it

The cloud API must be running (`sudo ./cluster.sh cloud start` in the kit). Then, on any machine
that reaches the API, for example the main PC:

```sh
python3 portal.py --api http://10.20.1.1:36100
# vdc-portal on http://0.0.0.0:8080/  ->  API http://10.20.1.1:36100
```

Open `http://<that machine>:8080/` and log in with an API account: the provider (`admin` and the
password from `sudo ./cluster.sh cloud admin`), a tenant user, or an auditor.

| Option | Default | |
|---|---|---|
| `--api` | `$VDC_API`, else `http://127.0.0.1:36100` | the cloud API, as this machine reaches it |
| `--public-api` | `$VDC_API_PUBLIC`, else `--api` | the cloud API as browsers reach it |
| `--bind` | `0.0.0.0` | address to listen on |
| `--port` | `8080` | port to listen on |
| `--meter-config` | `$VDC_METER_CONFIG` | `metrics.json` of `vdc_meter.py`: show its charges in the console |
| `--addon NAME=URL` | `$VDC_PORTAL_ADDONS` (comma-separated) | an add-on service, e.g. `containers=http://127.0.0.1:8091` (vdc-containers) or `k8s=http://127.0.0.1:8092` (vdc-k8s): `/addon/NAME/...` is passed on to it; repeat for more |
| `--quiet` | | no request log |

Why a small server and not just the file: the API sends no CORS headers, so a browser lets a page
call it only from the API's own origin. `portal.py` gives the page and the API one origin by
passing `/auth/`, `/tenant/`, `/provider/`, `/audit/` and `/carrier/` on to the API, headers and
answers unchanged (`502` when the API doesn't answer).

Two things are not passed through and open on the API's own address instead: web consoles (ttyd
needs a WebSocket) and `/docs`. The page learns that address from `/config.json`. When the portal
runs on the main PC with `--api http://127.0.0.1:36100`, set `--public-api` to the address
students use, e.g. `--public-api http://<lab-pc>:36100`, so their consoles open.

To keep it running, start it from a systemd unit, `tmux`, or `nohup python3 portal.py ... &`.

## Pages

| Role | Pages |
|---|---|
| consumer | dashboard; VM instances (create with a cost estimate and the equivalent REST request; start, stop, reboot, delete, web console, tunnels); images; VPC network; connect (ssh commands, `~/.ssh/config`); SSH keys; web consoles; jobs; billing; members |
| provider | dashboard (health, capacity, revenue); tenants (onboard, quota, internet, members, keys, off-board); accounts; import from hub; every tenant's VMs; images (import); machine types and prices; nodes (add spokes, evict); physical hosts; underlay; tunnels; jobs; billing; audit |
| auditor | dashboard; audit log (filters, CSV); tenant reports; command journal; underlay; metering (the billing pages, read only) |

Passwords: the API keeps only a hash, so nobody can look a password up. The provider resets one
under Accounts (or a tenant's Members) with the key icon: the console deletes the account and
creates it again with the same name, role, tenant and admin flag and the new password, and shows
the password once. The tenant's VMs and keys are not touched. A login the user already has keeps
working until it expires (8 hours).

Long operations (create, delete, power, image import, adding spokes) answer `202`. The console
follows each job under the bell icon and reloads the page when the job ends.

## Changing a password from the shell: vdc-passwd.sh

The console's reset works for every account but the provider's own, because the API does not let
an account delete itself. `vdc-passwd.sh` works for all of them, admin included. Run it on the
main PC, as root. `-k` names the kit folder; without it the script uses `$VDC_KIT`, else the
folder you are in:

```sh
sudo ./vdc-passwd.sh -k <kit> -l               # the accounts: user, role, tenant, admin
sudo ./vdc-passwd.sh -k <kit> alice            # asks for the new password twice
sudo ./vdc-passwd.sh -k <kit> -r tari          # a random password, printed once
sudo ./vdc-passwd.sh -k <kit> admin            # the provider's own account
```

It writes a new hash, in the API's own format, into `state/cloud/cloud.db`, so the account keeps
its role, tenant, admin flag and creation date, and it adds a `user.password` event to the audit
log. For `admin` it also updates `state/cloud/admin-password`, so `sudo ./cluster.sh cloud admin`
shows the new password. The API can keep running. It needs only `python3`, which the kit's cloud
API needs anyway; nothing in the kit is changed.

A login the user already has stays valid until it expires (8 hours).

## Billing

- **Overview:** cost this month so far, a forecast for the month (so far, plus the VMs running
  now at their price for the hours left), last month, cost per day over 30 days, cost per VM (or
  tenant) and per machine type. A monthly budget can be set as a reminder; it is kept in the
  browser and stops nothing (quotas do that).
- **Reports:** a period (this month, last month, 7, 30 or 90 days, or from–to), grouped by VM,
  machine type or tenant, with a CSV export. Periods over 45 days are charted per week.
- **Invoices:** one per tenant and calendar month (UTC); print it (or save as PDF) or export CSV.
  The current month shows as open.
- **Pricing:** the machine types and their price per hour.

The API's usage report covers "since a date until now". The console gets a closed period, such
as last month, as *usage since its start minus usage since its end*, line by line, and the cost
per day the same way, one report per day boundary. The result is the same as summing the
metering samples of that period.

Prices are plain numbers in the API. The currency symbol is a console setting (account menu →
Console settings), kept in the browser. The login token stays in the browser tab, so closing the
tab logs out.

## Additional metrics: vdc_meter.py

The kit charges VM running hours at the machine type's price. `vdc_meter.py` adds charges for
other things, defined in `metrics.json`, each with its own price. It gets its data from the cloud
API and keeps its own database (`state/meter.db`), so the kit is not changed. The console shows
the charges in the billing pages, on invoices and in the price lists, next to the VM hours.

The kit's own reports (`/tenant/usage`, `/provider/usage`, `/audit/metering`) still show VM hours
only; the additional metrics are in the console and in `vdc_meter.py --report`.

### Running it

The meter logs in as a provider account, because only the provider sees every VM through the
API. Create one for it in the console (Accounts → Create account, role provider), then:

```sh
export VDC_METER_USER=meter VDC_METER_PASSWORD='<its password>'
python3 vdc_meter.py --check          # one round, nothing saved: what each metric would charge
python3 vdc_meter.py                  # every interval_minutes, until stopped
python3 portal.py --api http://<lab-pc>:36100 --meter-config metrics.json
```

`--once` takes one round and exits, for cron or a systemd timer. `--password-file FILE` reads the
password from a file instead of the environment. `--report --since 2026-11-01 --tenant tenant-a`
prints the totals as JSON.

Like the kit's meter, it counts what it sees on each round. While the meter is not running,
nothing is metered.

### metrics.json

```json
{
  "api": "http://127.0.0.1:36100",
  "interval_minutes": 5,
  "db": "state/meter.db",
  "metrics": [
    {"name": "disk", "label": "Disk, allocated", "source": "vm", "value": "disk_gb",
     "when": "always", "kind": "gauge", "per": "month", "unit": "GB-month", "price": 0.10}
  ]
}
```

| Field | Values | Meaning |
|---|---|---|
| `name` | letters, digits, `-`, `_` | the metric's id, unique |
| `label`, `unit`, `description` | text | what the console and invoices show |
| `source` | `vm`, `tunnels`, `tenant`, `command` | where the value comes from (below) |
| `value` | see below | which number of the source |
| `when` | `running`, `stopped`, `always` | for `vm` and `tenant`: which VMs count (default `running`) |
| `kind` | `gauge`, `amount` | `gauge`: a level held over time (value × time); `amount`: used since the last round |
| `per` | `hour`, `month` | for a gauge: the price is per unit-hour or per unit-month (730 h) |
| `price` | number | per unit (per `unit`) |
| `enabled` | `true`, `false` | off without deleting it |

| `source` | `value` | gives |
|---|---|---|
| `vm` | `count`, `vcpus`, `mem_gb`, `disk_gb` | one value per VM (from `/provider/vms` and the machine type) |
| `tunnels` | | the open tunnels of each VM (`/provider/tunnels`); `"protocol": "http"` counts one protocol only |
| `tenant` | `count`, `internet`, `vms`, `vcpus`, `mem_gb`, `disk_gb` | one value per tenant (`/provider/tenants`), e.g. 1 while internet access is on |
| `command` | | whatever a command prints (below) |

Examples of what this covers: disk while a VM exists (stopped included), a price per vCPU-hour
or per GB of RAM, public tunnels, internet access, a flat fee per tenant or per VM.

### Your own metric: source command

A `command` metric runs a command on every round and reads one line per value:
`TENANT VM VALUE`, with `-` as VM for a value of the whole tenant. The command gets the meter's
login in `VDC_API` and `VDC_TOKEN`, so it can ask the API for anything the provider sees.
`examples/accounts.py` counts each tenant's user accounts (`/provider/users`) and is in
`metrics.json` as the metric `accounts` (switched off; set `"enabled": true`).

A command can also take its numbers from the cluster's command line, outside the API. With
`"kind": "amount"`, VALUE is what was used since the last round (GB, say), charged at `price`
per unit. `examples/traffic.sh` does this for network traffic, as the metric `traffic` in
`metrics.json` (switched off):

- it asks the hub `hubctl vms` (which guest is on which node, for which tenant), then each node
  `virsh domstats --interface` (the guests' byte counters, through `hubctl exec NODE`);
- it reaches the hub with SSH and the kit's own key (`state/id_lab`, `HUB_IP` from
  `cluster.conf`), so the meter has to run as root on the main PC;
- it keeps the counters of the last round in `state/traffic.last` and prints the difference:
  the first round only records them, a counter that went down (the guest restarted) counts from
  zero, and a node that does not answer is charged once it answers again.

To use it, set the kit folder in its command and switch it on:
`"command": "sh examples/traffic.sh /home/<you>/vdc-cluster", "enabled": true`. The same way
works for any other number a node can give: `virsh domstats --block` for the disk space a guest
really uses, `--vcpu` for CPU time.

A command that fails is logged and that round skips it; the other metrics are still metered.

Prices: each sample keeps the price it was charged at, so a new price in `metrics.json` applies
from the meter's next start, and past charges stay as they were.
