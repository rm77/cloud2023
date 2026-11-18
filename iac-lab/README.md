# iac-lab: infrastructure as code on vdc-cluster

Two examples, both through the cluster's cloud API as a normal tenant:

1. **Imperative vs declarative:** three VMs (two Alpine, one CirrOS), built once with a list of
   commands and once from a file that says what should exist.
2. **A web server:** one Alpine VM, declared with `vdc.py`, configured as an nginx web server
   with Ansible.

## Before you start

You need a tenant user in the cloud API (user, password and address come from the lab staff),
an SSH key pair (`ssh-keygen -t ed25519` if you have none) and Python 3.10 or newer.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export VDC_API=http://10.28.84.254:36100      # the lab PC's campus address
export VDC_USER=alice
export VDC_PASS='...'                          # never put it in a file you commit
```

VM names are unique in the whole cluster. When several groups share it, put your group in
the names (`g3-app1`): in the files below and in `imperative.py`.

## Example 1: imperative vs declarative

The goal is the same both ways: `app1` and `app2` (Alpine, flavor `small`) and `test1`
(CirrOS, flavor `tiny`).

### The imperative way: say how

`ex1-three-vms/imperative.py` is a list of commands, run in order:

```python
commands = [
    ("create app1",  "POST", "/tenant/vms", {"name": "app1",  "flavor": "small", "image": "alpine"}),
    ("create app2",  "POST", "/tenant/vms", {"name": "app2",  "flavor": "small", "image": "alpine"}),
    ("create test1", "POST", "/tenant/vms", {"name": "test1", "flavor": "tiny",  "image": "cirros"}),
]
```

```bash
python ex1-three-vms/imperative.py      # create app1: done ... create test1: done
python ex1-three-vms/imperative.py      # create app1: FAILED - 409 VM name app1 is taken
```

The second run fails: the script doesn't look at what exists. (`imperative.sh` is the same
with `hubctl` commands, for staff on the lab PC.)

Delete the three VMs before the declarative way (in `/docs`, or `DELETE /tenant/vms/app1` ...).

### The declarative way: say what

`ex1-three-vms/tenant.yaml`:

```yaml
vms:
  - {name: app1,  flavor: small, image: alpine}
  - {name: app2,  flavor: small, image: alpine}
  - {name: test1, flavor: tiny,  image: cirros}
```

```bash
python vdc.py plan  -f ex1-three-vms/tenant.yaml   # + vm app1, + vm app2, + vm test1
python vdc.py apply -f ex1-three-vms/tenant.yaml   # asks, then creates them (-y: don't ask)
python vdc.py apply -f ex1-three-vms/tenant.yaml   # no changes: the tenant matches the file
python vdc.py show                                 # status, power, address, ssh command
```

`vdc.py` reads the file (desired state) and the API (actual state), and acts only on the
difference:

| | | |
|---|---|---|
| `+` create | `-` delete | `~` replace: flavor or image changed (a VM can't be changed in place) |
| `>` start | `.` stop | `!` recreate: the VM is in error |

It remembers what this file created in `ex1-three-vms/.vdc-state.json`, like Terraform's
state file, and deletes only those VMs. Other VMs of the tenant are listed as "not managed by
this file" and left alone.

### Try this

1. **Drift.** Delete `test1` in `/docs` (`DELETE /tenant/vms/test1`). Run the imperative script:
   what happens? Run `vdc.py plan`: what does it see? `apply` repairs it.
2. **Power.** Add `power: off` to `app2`. Plan, apply. Then remove it again.
3. **Change.** Give `app1` the flavor `medium`. Why does the plan say "replace"?
4. **Remove** `app2` from the file. What does the plan do with it, and why doesn't it touch
   `web1` from example 2?

### Questions

- What does each way need to know about the current state of the tenant?
- Which way can you run every hour from a timer, safely? Why?
- `vdc.py` keeps a state file of what it created. What goes wrong if two students share a
  tenant but each has their own state file? And if one deletes the file?

## Example 2: a web server

One Alpine VM, then nginx on it. Both steps are declarative: the file says which VM should
exist, the playbook says how it should be configured.

**Step 1: the VM.** `ex2-web-server/tenant.yaml` puts your key in the tenant first, then
creates `web1`:

```yaml
keys:
  - ~/.ssh/id_ed25519.pub
vms:
  - {name: web1, flavor: small, image: alpine}
```

```bash
python vdc.py apply -f ex2-web-server/tenant.yaml
python vdc.py show                         # web1 ... ssh -p 460xx alpine@10.28.84.254
```

**Step 2: the inventory.** Each VM has its own SSH port on the lab PC's campus address.
`inventory.py` asks the API for them:

```bash
python inventory.py ex2-web-server/hosts.yml
```

**Step 3: the configuration.** `ex2-web-server/site.yml` installs nginx, puts a page in place
and keeps nginx running:

```bash
cd ex2-web-server
ansible-galaxy collection install community.general     # apk and doas, once
ansible -i hosts.yml web -m ping
ansible-playbook -i hosts.yml site.yml                  # changed=5 (4 tasks + the reload)
ansible-playbook -i hosts.yml site.yml                  # changed=0: nothing to do
```

Alpine guests become root with `doas`; Python is there because cloud-init needs it. The
tenant's internet switch must be on for `apk` (it is by default).

**See it.** From a computer on campus:
```bash
ssh -p 460xx alpine@10.28.84.254 wget -qO- localhost
```
or on the internet for a while: `POST /tenant/vms/web1/tunnels` with `{"protocol": "http"}`
returns a public `https://….trycloudflare.com` address.

### Try this

1. Change `message` in `site.yml` and run it. Which tasks report `changed`?
2. Log in and edit `/var/lib/nginx/html/index.html` by hand. Run
   `ansible-playbook -i hosts.yml site.yml --check --diff`: Ansible's own drift detection.
3. Delete `web1` and run steps 1 to 3 again. How long does it take to get the same server
   back? What would be lost if this were a real server?

## Files

| File | |
|---|---|
| `vdc.py` | plan / apply / destroy / show for a `tenant.yaml` |
| `vdc_api.py` | the API client: login, calls, waiting for jobs |
| `inventory.py` | an Ansible inventory from `GET /tenant/access` |
| `ex1-three-vms/imperative.py` | example 1, imperative, through the API (`imperative.sh`: with hubctl) |
| `ex1-three-vms/tenant.yaml` | example 1, declarative |
| `ex2-web-server/tenant.yaml` | example 2: the VM and the key |
| `ex2-web-server/site.yml` | example 2: nginx |
