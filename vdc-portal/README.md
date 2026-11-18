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

Long operations (create, delete, power, image import, adding spokes) answer `202`. The console
follows each job under the bell icon and reloads the page when the job ends.

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
