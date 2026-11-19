# vdc-k8s: a Kubernetes service for vdc-cluster

A tenant asks for a Kubernetes cluster with one API call and gets k3s on VMs of its own: a
control-plane VM `k8s-<cluster>-cp1` and workers `k8s-<cluster>-w1`, `-w2`, .... The add-on
creates them through the cloud API with the tenant's own login, so the tenant's quota, ownership
and VM-hour billing apply as for any VM, and they show up in the tenant's VM list. It then sets
the nodes up over SSH, gives the cluster MetalLB with extra addresses of the tenant subnet for
`LoadBalancer` Services, and opens annotated Services to campus on a tenant port or a floating
IP. Like a public cloud's managed Kubernetes, but each cluster's nodes are visible VMs that
students can log in to.

It is separate from the kit and changes nothing in it.

| File | What it is |
|---|---|
| `vdc_k8s.py` | the service (Python 3.8+, standard library only) |
| `node/node-setup.sh` | makes a VM a k3s server or agent (Alpine or Ubuntu/Debian) |
| `node/vdc-node` | the helper it installs on each node: kubeconfig, join token, kubectl, MetalLB |
| `image/build.sh`, `image/k3s-node.pkr.hcl` | optional: bake a node image with k3s already in it |
| `examples/k8s_clusters.py` | a custom metric for `vdc_meter.py`: a fee per cluster, or per node |
| `test_k8s.py` | tests against a stand-in API and stand-in nodes: `python3 test_k8s.py` |

## Running it

On the main PC (it needs the routes to the tenant subnets), with the cloud API running, as any
user with an `ssh` client:

```sh
python3 vdc_k8s.py --api http://127.0.0.1:36100
# vdc-k8s on http://0.0.0.0:8092/addon/k8s  ->  API http://127.0.0.1:36100; nodes: medium alpine, k3s stable, MetalLB v0.16.1; ...
```

It keeps everything in `state/` next to it: `k8s.db`, its own SSH key (`id_k8s`) and the nodes'
host keys. `--check` tells whether the API answers.

| Option | Default | |
|---|---|---|
| `--api` | `$VDC_API`, else `http://127.0.0.1:36100` | the cloud API |
| `--bind`, `--port` | `0.0.0.0`, `8092` | where it listens |
| `--node-image` | `alpine` | image of the nodes: an Alpine or Ubuntu cloud-init image of the kit |
| `--node-flavor` | `medium` | machine type of a node, unless the request names one |
| `--max-clusters` | `1` | clusters per tenant |
| `--max-workers` | `3` | workers per cluster |
| `--k3s-channel` | `stable` | which k3s: a channel (`stable`, `latest`, `v1.33`) ... |
| `--k3s-version` | | ... or a fixed version, e.g. `v1.33.5+k3s1` (the same for every cluster of a class) |
| `--metallb` | `v0.16.1` | MetalLB version |
| `--state` | `./state` | database, SSH key, known hosts |

The environment can set them too: `VDC_K8S_PORT`, `_IMAGE`, `_FLAVOR`, `_MAX_CLUSTERS`,
`_MAX_WORKERS`, `_CHANNEL`, `_VERSION`, `_METALLB`, `_STATE`.

A k3s server wants 2 GB of memory; the kit's `medium` machine type has 1 GB, which runs a small
cluster but leaves little room. Add a bigger machine type as the provider
(`PUT /provider/flavors/k8s-node` with 2 vCPU and 2048 MB) and start the add-on with
`--node-flavor k8s-node`, or let tenants name it per cluster. A cluster of one control plane and
two workers is then 6 GB of the tenant's quota.

With the web console, start `portal.py` with `--addon k8s=http://127.0.0.1:8092`.

## Using it

Every call carries the caller's cloud API token (`Authorization: Bearer <token>`). A tenant user
sees and manages only their own tenant's clusters; the provider sees all of them and can delete
any; an auditor can only read.

```sh
A=http://<lab-pc>:8092/addon/k8s
curl -s $A/clusters -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"name": "demo", "workers": 2}'
# {"job": "cluster-create-...", "status_url": "/addon/k8s/jobs/cluster-create-..."}
```

| Call | |
|---|---|
| `POST /addon/k8s/clusters` | create a cluster (`202` + job; a few minutes) |
| `GET /addon/k8s/clusters` · `/clusters/{name}` | my clusters, their nodes and exposed Services; `?state=true` asks Kubernetes for the nodes' state |
| `PATCH /addon/k8s/clusters/{name}` `{"workers": 3}` | add or remove workers (`202` + job) |
| `DELETE /addon/k8s/clusters/{name}` | delete it: nodes, addresses, ports, floating IPs (`202` + job) |
| `GET /addon/k8s/clusters/{name}/kubeconfig?access=tunnel` | a kubeconfig and how to use it (`tunnel`, `internal` or `floating`) |
| `POST /addon/k8s/clusters/{name}/kubectl` `{"args": ["get", "pods", "-A"]}` | run kubectl on the control plane; `"stdin"` takes a manifest for `-f -` |
| `GET /addon/k8s/clusters/{name}/services` | the cluster's Services, with their LoadBalancer address |
| `POST /addon/k8s/clusters/{name}/sync` | open or close campus access to match the Services' annotations |
| `GET /addon/k8s/jobs` · `/jobs/{id}` | jobs, each with a log of what was run where |
| `GET /addon/k8s/config` | the settings above, for a front end |

### A cluster

```json
{"name": "demo", "workers": 2, "lb_ips": 1, "flavor": "k8s-node", "api": "tunnel", "ingress": false}
```

Only `name` is needed: 1-8 letters, digits or `-`. Cluster names are unique in the whole cloud,
like VM names, because the nodes are VMs named after the cluster.

| Field | Default | |
|---|---|---|
| `workers` | 1 | worker VMs (0 runs everything on the control plane) |
| `lb_ips` | 1 | extra addresses of the tenant subnet for MetalLB, one per `LoadBalancer` Service (`max_ips` of the tenant's quota) |
| `flavor` | `--node-flavor` | machine type of every node |
| `api` | `tunnel` | `floating`: the API server also gets a floating IP, so kubectl works from campus without a tunnel |
| `ingress` | `false` | `true` keeps k3s' Traefik ingress controller (it takes one of the `lb_ips`) |

What the add-on does, step by step (the job log shows each one):

1. adds its SSH key to the tenant (`POST /tenant/keys`, shown as `vdc-k8s`);
2. reserves the extra addresses (`POST /tenant/ips`) and, with `api: floating`, a floating IP;
3. creates the VMs (`POST /tenant/vms`) and waits for them;
4. on the control plane: `node-setup.sh` as a k3s server (`--flannel-backend=host-gw`, no ServiceLB);
5. on each worker, in parallel: `node-setup.sh` as an agent with the control plane's join token;
6. installs MetalLB with the extra addresses as its pool (L2 mode).

The nodes all sit on the tenant's own network, so the pods' traffic between nodes is plain
routing (flannel `host-gw`), with no third tunnel inside the two the lab already has.

### kubectl

The API server listens on the control plane's address, inside the tenant network. Three ways in:

| `access` | `server` in the kubeconfig | |
|---|---|---|
| `tunnel` (default) | `https://127.0.0.1:6443` | through the tenant's jump host: the answer has the command, e.g. `ssh -N -L 6443:<cp1 address>:6443 -p 45222 t-tenant-a@<lab-pc>`; keep it running |
| `internal` | `https://<cp1 address>:6443` | from a VM of the tenant, or staff on the main PC |
| `floating` | `https://<floating IP>:6443` | from campus directly (cluster created with `api: floating`) |

```sh
curl -s "$A/clusters/demo/kubeconfig" -H "Authorization: Bearer $TOKEN" | jq -r .kubeconfig > demo.yaml
ssh -N -L 6443:<cp1 address>:6443 -p 45222 t-tenant-a@<lab-pc> &       # the line in "how"
KUBECONFIG=demo.yaml kubectl get nodes
```

Or without any of that, through the add-on: `POST /clusters/demo/kubectl` with
`{"args": ["get", "nodes", "-o", "wide"]}`.

The kubeconfig is the cluster's admin account: it can do anything in the cluster. The tenant's
own SSH keys also open the node VMs.

### Adding and removing nodes

A node is a VM of the tenant. Workers are added and removed by scaling the cluster, as a tenant
user, or with **Scale** on the cluster's page in the console:

```sh
curl -s -X PATCH $A/clusters/demo -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"workers": 3}'
```

- **More workers:** new VMs `k8s-demo-w<N>` (the lowest free numbers), set up as k3s agents and
  joined with the control plane's token; they are ready when the job finishes
  (`?state=true` on the cluster shows them `Ready` in Kubernetes).
- **Fewer workers:** the highest-numbered workers are drained first (their pods move to the
  others), deleted from Kubernetes, then their VMs are deleted.
- `workers` goes from 0 (everything on the control plane) to `--max-workers`. Every node also
  counts against the tenant's VM, vCPU and memory quota, and the job fails with the API's
  `quota exceeded` when it doesn't fit.
- The control plane stays one VM (`-cp1`); the add-on doesn't add control-plane nodes.
- New nodes get the cluster's machine type. A bigger machine type for new clusters is
  `--node-flavor` or `flavor` when creating; an existing cluster keeps its own.

Nodes are VMs, so the room for them comes from the cluster's nodes in the kit. When the hub
can't place a VM (`no Ready node has N MB free`), add a spoke (`sudo ./cluster.sh spoke add`),
or free memory on the nodes.

### Services from campus

A `LoadBalancer` Service gets one of the cluster's extra addresses from MetalLB and is reachable
inside the tenant at once. To reach it from campus, annotate it:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: web
  annotations:
    vdc/expose: port          # or: floating
spec:
  type: LoadBalancer
  selector: {app: web}
  ports: [{port: 80}]
```

| `vdc/expose` | From campus | Uses |
|---|---|---|
| `port` | `<lab-pc>:<campus port>`, one per Service port | the tenant's `max_ports` |
| `floating` | `<floating IP>:<port>`, every port | the tenant's `max_floating_ips` |

The add-on makes the campus side match the annotations whenever it syncs: after every
`kubectl` call through it, and on `POST /clusters/{name}/sync`. After `kubectl apply` from your
own machine, call sync once; its answer lists each exposed Service with its `connect` address,
or the reason it couldn't be opened (a quota, for instance; it is tried again on the next sync).
Removing the annotation, deleting the Service, or deleting the cluster closes it again.

## The node image

Without anything else, every node downloads k3s on its first start, and the control plane
downloads MetalLB, so the tenant needs internet then. To start nodes faster, bake an image with
k3s, its air-gap images and the MetalLB manifest already in it:

```sh
image/build.sh /path/to/alpine-cloudinit.qcow2              # K3S_VERSION=v1.33.5+k3s1 to fix the version
sudo ./cluster.sh image add k3s-alpine /path/to/vdc-k8s/state/image/k3s-alpine.qcow2 --user alpine   # in the kit
python3 vdc_k8s.py --node-image k3s-alpine
```

Nodes from that image still pull the two MetalLB images (quay.io) and your applications' images
from their registries. `build.sh` needs `packer`, `qemu-system-x86_64` and `xorriso`.

## Billing

The nodes are VMs, so the kit already bills their hours at their machine type's price. A public
cloud also charges a fee per cluster; to do the same, add a metric to `vdc_meter.py` (vdc-portal):

```json
{"name": "k8s-cluster", "label": "Kubernetes cluster", "source": "command",
 "command": "VDC_K8S=http://127.0.0.1:8092 python3 /path/to/vdc-k8s/examples/k8s_clusters.py clusters",
 "kind": "gauge", "per": "hour", "unit": "cluster-hour", "price": 0.10}
```

`nodes` instead of `clusters` meters each cluster's number of nodes.

## Troubleshooting

| Problem | What to check |
|---|---|
| a job fails at "waiting for SSH" | the VMs booted? (`hubctl vm console NAME` in the kit); the add-on runs on the main PC |
| `can't become root without a password` | the image's default user has no password-less `doas`/`sudo` |
| `can't reach get.k3s.io (internet off?)` | switch the tenant's internet on, or bake the node image |
| `the k3s server did not come up` | on the node: `/var/log/k3s.log`; too little memory (use a bigger machine type) |
| `the MetalLB controller did not start` | the nodes can't pull from quay.io; `kubectl -n metallb-system get pods` |
| a LoadBalancer Service stays `<pending>` | all `lb_ips` are in use: one address per Service |
| an annotated Service isn't reachable from campus | `POST .../sync` and read its answer (`error`); `edge status` in the kit lists the port or floating IP |
| `quota exceeded` from the cloud API | the tenant's VM, address, port or floating IP quota: the provider raises it |
| `cluster demo exists` | cluster names are unique in the whole cloud: pick another |
| a cluster is `error` | its job's log; delete it and create it again |
