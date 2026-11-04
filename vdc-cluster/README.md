# vdc-cluster

A small virtual data centre you can run on one lab PC, or spread over several. It has one hub
VM and any number of spoke VMs (hypervisors). Tenants get isolated networks that span all
spokes, and VMs on those networks.

To add a hypervisor you clone the spoke image and boot it. It finds the hub and joins by
itself, and nothing has to be edited by hand.

```
 PC 1 (main)                                         PC 2 (optional)
 br-under 10.20.1.1 ── [uplink NIC] ── switch ── [uplink NIC] ── br-under 10.20.2.1
   ├─ hub    10.20.0.2    DNS, NAT to the internet,         ├─ hv201 10.20.2.101
   │                      join API :8080, hubctl            └─ hv202 10.20.2.102
   ├─ hv101  10.20.1.101  spoke: libvirt + lab-agent
   └─ hv102  10.20.1.102
```

Everything is driven by two tools. `cluster.sh` runs on the lab PC and manages the VMs and the
underlay network. `hubctl` runs on the hub and manages tenants, addresses and guest VMs. On top
of both there is an optional REST API with logins for providers, tenants and auditors (see
[The cloud API](#the-cloud-api)).


## What you need

- Linux with `qemu-system-x86_64`, `qemu-img`, `packer`, `socat`, `iproute2` and `tcpdump`
- nested KVM: `cat /sys/module/kvm_intel/parameters/nested` should print `Y` (on AMD, the
  `kvm_amd` module)
- about 2 GB of RAM per VM, more if tenants run Ubuntu guests
- root: every `cluster.sh` command runs with `sudo`

The lab uses 10.20.0.0/16 for its own network. Run `ip route` first: if 10.20.x is already used
where you are, pick another /16 in `cluster.conf` (`UNDERLAY_NET`, `HUB_IP`, `LABPC_IP`).


## First run

```bash
sudo ./cluster.sh init            # SSH key and join token in state/, network bridge br-under
sudo ./cluster.sh build           # builds images/hub.qcow2 and images/spoke.qcow2 (about 10 minutes)
sudo ./cluster.sh up              # starts the hub and waits until it answers
sudo ./cluster.sh spoke add 3     # hv101, hv102, hv103: clone, boot, join
sudo ./cluster.sh list
```

`list` shows the VMs on this PC and, below them, the hub's view of the cluster. Every spoke
should be `Ready`:

```
NAME     IP             MAC               STATUS    GUESTS RAM FREE/TOTAL
hub      10.20.0.2      52:54:00:20:00:02 Ready     0      1465/1977 MB
hv101    10.20.1.101    52:54:00:21:01:01 Ready     0      1480/1977 MB
```

While the spokes boot, `spoke add` shows each one's progress (booting, address, join, tunnels,
Ready). It usually takes 10 to 20 seconds per spoke. Ctrl-C only stops the display, the VMs keep
going.

Logins: the hub and spokes accept `root` / `vdclab` on the console (`sudo ./cluster.sh console
hv101`, leave with Ctrl-]). `sudo ./cluster.sh ssh hv101` logs in with the lab key.

Keep `state/` safe. It holds the SSH key the images were built with. If you lose it, `init`
makes a new key and you have to rebuild the images.


## Tenants and VMs

A tenant is a network: a VNI (the tunnel ID) and a subnet. The hub gives every guest an address
from its tenant's subnet, so you never type one.

```bash
sudo ./cluster.sh hubctl tenant add tenant-a 199 10.199.20.0/24
sudo ./cluster.sh hubctl tenant add tenant-b 198 10.198.20.0/24

sudo ./cluster.sh hubctl vm add tenant-a a1       # CirrOS, gets 10.199.20.10
sudo ./cluster.sh hubctl vm add tenant-a a2       # 10.199.20.11, probably on another spoke
sudo ./cluster.sh hubctl vm add tenant-b b1       # 10.198.20.10
sudo ./cluster.sh hubctl vms                      # where each guest runs and its state
sudo ./cluster.sh hubctl vm console a1            # log in as cirros / vdclab, then ping 10.199.20.11
```

a1 can reach a2 even when they run on different spokes. Neither can reach b1: the two tenants
have separate bridges with no router between them, even on the same spoke.

Addresses start at .10 of the subnet (.1 to .9 are kept free). Use `--ip` to pick one yourself,
and `hubctl ips tenant-a` to see what is taken and what comes next. The hub places each guest on
the Ready spoke with the most free RAM, unless you name a spoke: `hubctl vm add tenant-a a3 hv102`.

To remove things:

```bash
sudo ./cluster.sh hubctl vm del a2                # its address becomes free again
sudo ./cluster.sh hubctl tenant del tenant-b      # only once it has no guests
```

### Other guest images

CirrOS is built in. Any cloud image with cloud-init works too:

```bash
sudo ./cluster.sh image add ubuntu ~/Downloads/noble-server-cloudimg-amd64.img
sudo ./cluster.sh image add debian https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2 --user debian
sudo ./cluster.sh images
sudo ./cluster.sh hubctl vm add tenant-a web1 --image ubuntu
```

A file is uploaded from this PC, and a URL is downloaded by the hub. The guest's hostname,
password and address are handed over on a small cloud-init CD (NoCloud). For an image without
cloud-init use `--type none`: the address is still reserved, but you set it inside the guest.

The login is the image's default user (`ubuntu`, `debian`, ...) with the password `vdclab`
(`GUEST_PASSWORD` in `cluster.conf`). `hubctl vm info web1` shows it.

An Ubuntu guest needs 1 GB of RAM, and a spoke has 2 GB by default with 512 MB kept for itself.
If `vm add` says no node has enough RAM free, add spokes or raise `SPOKE_MEM` in `cluster.conf`.

### Growing and shrinking

```bash
sudo ./cluster.sh spoke add 2         # hv104, hv105; existing tenants reach them at once
sudo ./cluster.sh stop hv105          # power off (start brings it back and waits for the join)
sudo ./cluster.sh rm hv105            # remove it from the hub and delete its disk
```

`rm` refuses while the spoke still has guests. Delete them first (`hubctl vms` shows where each one runs).


## The cloud API

The API lets students manage their own tenant through a web page or `curl`, without root on
the lab PC. It follows the NIST Cloud Computing Reference Architecture (CCRA). Each kind of
user (the CCRA calls them actors) has its own part of the API:

| Path | Who | Can do |
|---|---|---|
| `/provider` | lab staff (role `provider`) | create tenants, set quotas, flavors and images, add spokes, see everything |
| `/tenant` | a student group (role `consumer`) | its own VMs, network, jobs and usage, and its own members |
| `/audit` | TA or auditor (role `auditor`) | read only: who did what, usage, and the commands the cluster ran |
| `/carrier` | provider, auditor | health of the underlay network |

Under the hood every request becomes a `hubctl` or `cluster.sh` call, so the hub still picks
addresses and spokes. The API adds the logins, quotas, prices, an audit log and usage metering.

### Starting it

On the main PC, after `cluster.sh up`:

```bash
sudo ./cluster.sh cloud install       # Python environment, Redis, secrets (once)
sudo ./cluster.sh cloud start         # prints the address, e.g. http://10.20.1.1:36100/docs
sudo ./cluster.sh cloud admin         # the provider login: admin and a generated password
```

`cloud status` shows whether it runs, `cloud logs worker` follows a log, `cloud stop` stops it,
and `cloud enable` installs systemd services so it comes back after a reboot. After a reboot,
start the hub again with `cluster.sh up`.

Open the address in a browser. The page (Swagger UI) lists every call, grouped by CCRA function,
and lets you try each one.

### Logging in as the provider and creating a tenant

1. Click **Authorize** at the top right.
2. Enter the username `admin` and the password from `cloud admin`. Leave `client_id` and
   `client_secret` empty.
3. Click **Authorize**, then **Close**.
4. Open `POST /provider/tenants`, click **Try it out**, and send:

```json
{
  "name": "tenant-a",
  "vni": 199,
  "subnet": "10.199.20.0/24",
  "admin_username": "alice",
  "admin_password": "alice-pass-1"
}
```

This creates the tenant network on the hub and an account for its admin, alice. If you leave
out `vni` and `subnet`, the API takes the next free VNI from 100 upward and the subnet
`10.<vni>.20.0/24`.

### Logging in as a tenant

1. Click **Authorize**, then **Logout** to drop the provider login.
2. Click **Authorize** again and enter `alice` / `alice-pass-1` (`client_id` and
   `client_secret` stay empty).
3. Try `GET /tenant/me`. It shows tenant-a's network, quota and usage.
4. Create a VM with `POST /tenant/vms` and `{"name": "a1", "flavor": "small"}`. The answer
   contains a job; `GET /tenant/jobs/{id}` shows when it is finished, and `GET /tenant/vms/a1`
   shows the address the hub gave it.

The tenant is part of the login, so tenant calls never ask which tenant you mean. A tenant
can't reach anything else: `/provider/...` answers 403, and another tenant's VM answers 404.

Alice is the tenant admin. She can add her group members with `POST /tenant/users`, and they
log in the same way. The provider can also create tenant users with `POST /provider/users`
(`"role": "consumer", "tenant": "tenant-a"`), and auditors with `"role": "auditor"`.

A login lasts 8 hours. When calls start answering 401, log in again. To show the provider,
tenant and auditor views side by side in class, use a private browser window for each.

### The same from the command line

The login is a plain form post, not JSON:

```bash
API=http://10.20.1.1:36100
TOKEN=$(curl -s -d 'username=alice&password=alice-pass-1' $API/auth/token |
        python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -s -H "Authorization: Bearer $TOKEN" $API/tenant/me
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -X POST $API/tenant/vms -d '{"name":"a1","flavor":"small"}'
```

### When a call fails

| Answer | Meaning |
|---|---|
| 401 wrong username or password | no such account, or the wrong password |
| 401 invalid or expired token | log in again |
| 403 this endpoint is for the consumer role | you are logged in as the provider: Logout and log in as the tenant |
| 403 quota exceeded | the tenant reached its limit; the provider can raise it (`PUT /provider/tenants/{name}/quota`) |
| 404 no VM ... in tenant ... | the VM doesn't exist in your tenant |
| 409 cluster: ... | the hub refused, and the message says why (name taken, subnet full, ...) |
| 502 cluster: hub not reachable | the hub VM is down: `sudo ./cluster.sh list` |
| 503 job queue | Redis is down: `sudo ./cluster.sh cloud status` |

The full list of calls, the rules behind them and how they map to the CCRA are in
`cloud/README.md`. The API's settings are the `CLOUD_*` lines in `cluster.conf`.


## More than one PC

All PCs share one underlay network. Each PC bridges a spare NIC, or a VLAN on one, into its
`br-under`, and those NICs go to the same switch. The hub runs on the main PC; every PC can run
spokes.

Use a separate switch or VLAN for this, never the normal campus LAN: the underlay carries lab
traffic and the join token unencrypted. The uplink NIC must not have an IP address, and the
switch must pass 1600-byte frames.

On the main PC:

```bash
sudo ./cluster.sh host init 1 enp2s0      # this PC is host 1, its uplink NIC is enp2s0
sudo ./cluster.sh init && sudo ./cluster.sh build && sudo ./cluster.sh up
sudo ./cluster.sh host export             # vdc-host-bundle.tar for the other PCs
```

The bundle contains the join token and the lab's SSH key, so copy it only to your own PCs.
On each other PC:

```bash
mkdir vdc-cluster && tar xf vdc-host-bundle.tar -C vdc-cluster && cd vdc-cluster
sudo ./cluster.sh host init 2 enp2s0      # host 2 (3, 4, ... for the next PCs)
sudo ./cluster.sh net-up
sudo ./cluster.sh mtu-check               # do 1600-byte frames reach the hub?
sudo ./cluster.sh spoke add 4             # hv201 .. hv204, joined to the hub on PC 1
```

If `mtu-check` fails and you can't change the switch, set `UNDERLAY_MTU=1500` and
`TENANT_MTU=1450` in `cluster.conf` on every PC and rebuild the images.

Tenants and guests are cluster-wide: `hubctl vm add tenant-a a7` may place a7 on a spoke in
the next room.


## Addresses

Nothing on the underlay uses DHCP. Every address follows from the machine's name:

| Machine | Address |
|---|---|
| hub | 10.20.0.2 |
| lab PC with host ID *n* | 10.20.*n*.1 (the main PC is 10.20.1.1) |
| spoke hv*n**NN* | 10.20.*n*.(100 + *NN*): hv101 is 10.20.1.101, hv203 is 10.20.2.103 |
| bare-metal spoke | 10.20.0.3 to 10.20.0.99, set with `IP=` in its `/etc/lab/join.conf` |

The spokes use the hub as gateway (NAT to the internet) and as DNS server (`hv101.lab`). If you
really want DHCP, set `IP_MODE=dhcp` in `cluster.conf` and rebuild the images. The hub then
hands out 10.20.0.100 to 10.20.0.250.


## Logs

Everything ends up in `logs/` on the PC where you ran the command:

| File | What |
|---|---|
| `logs/cluster.log` | every `cluster.sh` run with its output |
| `logs/vm/<name>.console.log` | each VM's serial console, all boots |
| `logs/build/` | the image builds (Packer output, installer console) |
| `logs/commands/<tool>.log` | every command `cluster.sh` ran on this PC (qemu, ip, ...) |
| `logs/cloud/` | the cloud API (api, worker, meter, redis) |
| `logs/nodes/<name>/` | logs copied from inside the VMs by `logs collect` |

```bash
sudo ./cluster.sh logs                    # list everything
sudo ./cluster.sh logs tail hv101         # follow a VM's console as it boots
sudo ./cluster.sh logs collect            # copy /var/log/lab from the hub and all spokes
```

### The command journal

Every tool the lab uses to build things (`ip`, `virsh`, `qemu-img`, `mkfs`, `apk`, ...) is
logged with the exact command line, who ran it, why, and the exit code:

```
2026-10-28 08:29:55 hv101 [lab-agent] $ ip link add gnv199 type geneve id 199 remote 10.20.0.2  # tunnel to the hub for tenant VNI 199
2026-10-28 08:29:55 hv101 [lab-agent] = rc=0 0.01s ip
```

There is one file per tool plus `all.log` in order. You find it on the lab PC in
`logs/commands/`, in each VM in `/var/log/lab/commands/`, and for the image build in
`logs/build/<role>-commands/`. Passwords and the join token are masked. Commands that only read
state are left out, so the journal shows only changes. Set `TRACE=all` in `cluster.conf` to
include them, or `TRACE=off` to turn the journal off (rebuild the images after changing this).

```bash
sudo ./cluster.sh logs collect            # get the VMs' journals first
sudo ./cluster.sh logs commands           # how many calls per tool and machine
sudo ./cluster.sh logs commands qemu      # every QEMU start, in time order
```


## How a spoke joins

1. At boot, `lab-net` gives the spoke its address. `cluster.sh` passes the address to QEMU,
   and from there it reaches the spoke.
2. `lab-agent` sends the join token, its MAC and its SSH host key to the hub (`POST /cgi-bin/join`
   on port 8080).
3. The hub checks the token and registers the spoke. It builds a tunnel to it for every tenant,
   and answers with the spoke's name and a secret of its own.
4. From then on the agent asks the hub every 10 seconds which tenants exist (using its secret,
   not the token). It creates the tenant bridges, tunnels and libvirt networks itself.

A spoke that misses three polls shows as NotReady. If the hub forgets a spoke, the spoke joins
again under its old name. If the hub is down, spokes keep their tunnels, and guests on the same
spoke can still talk to each other.


## Settings

All settings are in `cluster.conf`, and are copied into the images when you build them.
Rebuild after changing anything except the `CLOUD_*` lines.

| Setting | Default | |
|---|---|---|
| `ROOT_PASSWORD` | `vdclab` | console login of hub and spokes |
| `GUEST_PASSWORD` | `vdclab` | login of tenant guests |
| `HUB_MEM`, `SPOKE_MEM` | 2048 | MB of RAM per VM |
| `DISK_SIZE` | 20G | disk of hub and spokes (sparse) |
| `UNDERLAY_NET`, `HUB_IP`, `LABPC_IP` | 10.20.0.0/16, 10.20.0.2, 10.20.1.1 | the lab's own network |
| `UNDERLAY_MTU` | 1600 | must pass the switch in multi-PC setups |
| `HUB_SCHEDULABLE` | yes | the hub also runs guests |
| `GUEST_HOST_RESERVE` | 512 | MB of each node's RAM never given to guests |
| `TRACE` | changes | command journal: `changes`, `all` or `off` |
| `CLOUD_PORT` | 36100 | the cloud API |

Settings that differ per PC (host ID, uplink NIC) are in `host.conf`, written by `host init`.


## Command reference

`cluster.sh` (on a lab PC, with sudo):

| Command | |
|---|---|
| `init` | SSH key and join token; resets `br-under` and the VMs' network ports |
| `build [hub\|spoke]` | build the images |
| `up` | start the hub |
| `spoke add [N]` | add N spokes |
| `list` | VMs on this PC and the cluster's members |
| `start\|stop NAME\|all`, `rm NAME` | power a VM on or off, remove it |
| `console NAME`, `ssh NAME [CMD]` | serial console (Ctrl-] leaves), SSH |
| `hubctl ARGS` | run hubctl on the hub |
| `image add NAME FILE\|URL`, `images` | guest images |
| `capture NAME [FILTER]` | tcpdump on a VM's network port |
| `logs`, `logs tail NAME`, `logs collect`, `logs commands [TOOL]` | logs and the command journal |
| `host init ID [NIC]`, `host show`, `host export` | multi-PC setup |
| `net-up`, `net-down`, `mtu-check` | the underlay network |
| `cloud install\|start\|stop\|status\|logs\|admin\|enable\|disable\|test` | the cloud API |

`hubctl` (through `cluster.sh hubctl`, or on the hub itself):

| Command | |
|---|---|
| `nodes`, `node del NAME` | members and their state; remove one |
| `tenant add NAME VNI [SUBNET]`, `tenant del NAME`, `tenants` | tenant networks |
| `ips [TENANT]` | address allocations and the next free one |
| `vm add TENANT NAME [NODE] [--image I] [--ip A] [--mem MB] [--cpus N] [--disk SIZE]` | create a guest |
| `vm info NAME`, `vm console NAME`, `vm del NAME`, `vms` | guests |
| `image add NAME URL`, `image del NAME`, `images` | the image catalog |
| `token` | the join token |
| `exec NODE CMD` | run a command on any node |

On a spoke, `lab-agent status` shows its view of the cluster.


## Exercises

**E1: Watch a spoke join.** Run `sudo ./cluster.sh capture hub 'tcp port 8080'` in one
terminal and `sudo ./cluster.sh spoke add` in another. Match the packets to the progress lines.
What is the only secret in the join request, and what replaces it afterwards?

**E2: The scheduler.** Create six guests without naming a spoke and predict where each one
lands. Then stop a spoke. How long until `hubctl nodes` shows it NotReady, and where does the
next guest go?

**E3: Scale out under traffic.** Keep a ping running between two guests on different spokes,
and add two spokes. Does the ping notice? Compare `hubctl tenants` before and after.

**E4: The hub forgets.** On the hub, empty the member list: `cp /var/lib/lab/nodes /root/; :
> /var/lib/lab/nodes; hubctl apply`. Watch `hubctl nodes` for 20 seconds, then read the
agents' `agent.log`. Who rebuilt the list, and why did every spoke keep its name?

**E5: The hub goes down.** Stop the hub while guests ping each other on the same spoke and
across spokes. Which traffic survives? Reboot a spoke while the hub is down: which progress
steps appear, and does it still have its tunnels?

**E6: Join security.** Make a spoke join with a wrong token and find the refusal on its console,
in its `agent.log` and in the hub's `hub.log`. Then, from a joined spoke, call `/cgi-bin/state`
pretending to be another spoke. What stops you? What could an attacker on the underlay still do?

**E7: Two rooms (multi-PC).** Add spokes from a second PC. Capture on its uplink NIC: what
crosses the switch when a guest on hv201 pings one on hv101? Then set `UNDERLAY_MTU` to 1500 on
one side only and explain what `mtu-check` and `ping -s 1472` between guests show.

**E8: Read the build.** Using only `logs/build/spoke-commands/all.log` and `files.log`, write
down how an empty disk becomes a spoke image. Explain every option of one spoke's QEMU command
line from `logs/commands/qemu.log`.

**E9: One image, two guests.** Add an Ubuntu image and create two guests from it. Find the
download, the two disk overlays, the two cloud-init seeds and the `virsh define` calls in the
journals. Inside a guest, where did its address come from? Delete one guest and create a third:
which address does it get?

**E10: Tenant self-service.** As the provider, create tenant-a and tenant-b in the cloud API.
As alice, create VMs until the quota stops you. As an auditor, find that refusal in
`/audit/events`. Then match each accepted `vm.create` to its `virsh start` in
`/audit/journal?node=hv101&tool=virsh`. What would a VM created with `hubctl` directly look
like in the two logs?


## Troubleshooting

| Problem | What to check |
|---|---|
| a VM stays at "booting" | `sudo ./cluster.sh logs tail NAME`; without nested KVM everything is very slow |
| `hub VM runs but does not answer SSH` | the images were built with another key: `rm` the VMs, `build`, `up` |
| `join failed: hub ... unreachable` | `sudo ./cluster.sh mtu-check`; on the hub, `/var/log/lab/api.log` |
| the PC reaches hub and spokes, but a spoke can't reach the hub | a firewall on the PC (Docker, ufw) drops bridged traffic; `cluster.sh` adds `iptables -I FORWARD -i br-under -o br-under -j ACCEPT`, or run that yourself |
| `join refused: ERROR bad-token` | `state/token` changed after the build; rebuild, or `hubctl token` to compare |
| `join refused: ERROR ip-in-use by X` | two machines with one address; if X is gone, `hubctl node del X` |
| a spoke is NotReady | `lab-agent status` and `/var/log/lab/agent.log` on it |
| `no Ready node has N MB RAM free` | `hubctl nodes`; use a smaller `--mem`, more spokes or a bigger `SPOKE_MEM` |
| a guest has no address | `hubctl vm info NAME` (type `none` gets none); inside, `cloud-init status --long` |
| guests on different spokes can't talk | `hubctl tenants` should list a tunnel per spoke; check the MTU on the switch |
| `build` refuses | VMs still use the old image: `rm` them first |
| a worker PC can't reach the hub | the main PC is up, `host show` lists the uplink NIC, same switch or VLAN, `net-up` done |
| cloud API doesn't answer | `sudo ./cluster.sh cloud logs api`; is port 36100 taken? |
| cloud jobs stay queued | `sudo ./cluster.sh cloud status`: the worker or Redis is not running |
