# Kubernetes HPA example with multiple metrics

This module demonstrates a Horizontal Pod Autoscaler (HPA) using four signals:

| Metric | Source | Target |
|---|---|---|
| CPU utilization | Metrics Server | 70% of requested CPU |
| Memory utilization | Metrics Server | 80% of requested memory |
| In-flight requests | Prometheus Adapter | Average of 4 per pod |
| Pending jobs | Prometheus Adapter | Average of 10 per pod |

Kubernetes evaluates each metric and scales to the largest replica recommendation, within the configured minimum and maximum. The queue is an in-memory per-pod demonstration; a restarted pod can lose queued jobs.

The directory and scripts follow the configuration, `k8s/`, and `scripts/` pattern used by [`cloud-dynamics-applications`](https://github.com/rm77/cloud2023/tree/master/kubernetes/research/cloud-dynamics-applications). Run these commands from this module directory.

## Requirements

- A running Kubernetes cluster and a working `kubectl` context
- Docker and kind, for building/loading this demo image
- nginx Ingress Controller
- Metrics Server
- Prometheus Operator/kube-prometheus-stack and Prometheus Adapter
- Helm and `jq` for the one-time Adapter registration step



The shared cluster setup is documented in [`kubernetes/setup-cluster/supporting`](https://github.com/rm77/cloud2023/tree/master/kubernetes/setup-cluster/supporting). This module expects the Prometheus Service `monitoring-kube-prometheus-prometheus` in namespace `monitoring`, with the ServiceMonitor label `release: monitoring` selected by Prometheus.

To simplify run from within `kubernetes/setup-cluster/supporting`

```
cd kubernetes/setup-cluster/supporting

sh 02-install-metrics-server.sh  
sh 03-install-ingress.sh         
sh 04-cek-ingress.sh         
sh 05-install-prometheus.sh  
sh 06-install-linkerd.sh    
sh 07-install-netobserv.sh  
 
```


Make sure Metrics Server is installed and check it with:

```sh
kubectl top nodes
```

Check the Prometheus Service and its ready EndpointSlices:

```sh
kubectl get svc -n monitoring
kubectl get endpointslice -n monitoring \
  -l kubernetes.io/service-name=monitoring-kube-prometheus-prometheus
```

## 1. Prepare custom metrics once per cluster

The supporting setup installs Metrics Server and Prometheus, but it does not install/register these custom metrics in Prometheus Adapter. Run the preparation script before deploying the module:

```sh
sh prepare/register-metrics.sh
```

It checks Metrics Server and Prometheus, then installs Prometheus Adapter if no Helm-managed adapter exists, or merges this module's rules into the selected Helm release. If there is more than one Adapter release, provide its Helm release and namespace:
<!--

```sh
sh prepare/register-metrics.sh <adapter-release> <namespace>
```
The script needs `kubectl`, Helm, and `jq`. The JSON file `prepare/prometheus-adapter-values.json` is Helm configuration, not a Kubernetes manifest, so do not pass it to `kubectl apply`.
-->

## 2. Configure the module

Defaults are in `config/environment.env`:
provided is `config/environment.env.original`, you should copy to `config/environment.env` first

```sh
HOST_IP=127.0.0.1
CLUSTER_NAME=mylab99
NAMESPACE=hpa-multi-metric
INGRESS_HTTP_PORT=80
```

Export variables before running scripts to override defaults. For example, for a remotely reachable host where nginx Ingress listens on port 28080:

```sh
export HOST_IP=10.28.84.254
export INGRESS_HTTP_PORT=28080
```

Use the IP address of the machine reachable by the client. The `sslip.io` hostname resolves to that IP; it does not create a network route or open a firewall port. Ensure the cluster's Ingress listener and firewall allow the configured port. For public access, the IP must be publicly reachable. The Kubernetes Ingress resource itself remains on HTTP port 80; `INGRESS_HTTP_PORT` is the host-side port used by the scripts and URL.

## 3. Build, deploy, and check

Build the container and load it into the selected kind cluster:

```sh
./scripts/build-and-load.sh
```

Deploy the namespace, app, Service, ServiceMonitor, HPA, and sslip.io Ingress:

```sh
./scripts/deploy.sh
```

The script prints the application URL. With defaults it is:

```text
http://hpa-multi-metric-demo.127.0.0.1.sslip.io/
```

For the example remote IP and port above, it is:

```text
http://hpa-multi-metric-demo.10.28.84.254.sslip.io:28080/
```

Smoke-test the external path:

```sh
./scripts/smoke-test.sh
```

If you changed `NAMESPACE`, use that namespace in kubectl commands. These scripts render namespace and hostname placeholders with POSIX `sed`; the manifests do not need hand edits or `yq`.

## 4. Watch how the HPA reacts to load

After deployment, open two terminals to watch the HPA and its pods. Run these before starting a load generator:

**Terminal 1 — HPA metrics and replica count**

```sh
kubectl get hpa hpa-multi-metric-demo -n hpa-multi-metric --watch
```

**Terminal 2 — pod creation and readiness**

```sh
kubectl get pods -n hpa-multi-metric --watch
```

In the HPA output, `TARGETS` shows the current metric value alongside its target (for example, `cpu: 95%/70%`, `inflight_requests: 12/4`, or `queue_depth: 25/10`). A value above its target asks the HPA to add replicas. `REPLICAS` shows the replica count reported for the target Deployment; the pod watch shows when new pods are created and become ready.

Start one of the load generators in a third terminal (examples below). Watch the current metric values rise and the HPA replica count change. The HPA considers all available metrics and uses the largest replica recommendation, up to 12. After stopping the load, scale-down is intentionally delayed by the 120-second stabilization window.

At startup, custom metrics can temporarily appear as `<unknown>` while Prometheus scrapes the app and the Adapter retrieves the series. If they remain unknown, use the verification commands below and inspect the HPA events with `kubectl describe`.

To see the HPA's computed desired replicas and scaling events in more detail:

```sh
kubectl describe hpa hpa-multi-metric-demo -n hpa-multi-metric
```

## 5. Verify the metrics

Wait for Prometheus to scrape the app, then inspect APIService and custom metric values:

```sh
kubectl get apiservice v1beta1.custom.metrics.k8s.io
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/hpa-multi-metric/pods/*/inflight_requests"
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/hpa-multi-metric/pods/*/queue_depth"
kubectl describe hpa hpa-multi-metric-demo -n hpa-multi-metric
```

The app exports `demo_inflight_requests` and `demo_queue_depth`. It also provides `/cpu`, `/memory`, `/slow`, and `/queue` endpoints to create load.

## 6. Generate load

Run load generators from a second terminal. These use the in-cluster Service, so they work independently of the external Ingress hostname.

Queue backlog:

```sh
kubectl run queue-load -n hpa-multi-metric --rm -it --restart=Never --image=curlimages/curl -- \
  sh -c 'while true; do curl -s "http://hpa-multi-metric-demo.hpa-multi-metric.svc.cluster.local/queue?count=10"; sleep 0.5; done'
```

In-flight requests:

```sh
kubectl run concurrency-load -n hpa-multi-metric --rm -it --restart=Never --image=curlimages/curl -- \
  sh -c 'while true; do i=0; while [ "$i" -lt 20 ]; do curl -s "http://hpa-multi-metric-demo.hpa-multi-metric.svc.cluster.local/slow?seconds=2" >/dev/null & i=$((i+1)); done; wait; done'
```

CPU pressure:

```sh
kubectl run cpu-load -n hpa-multi-metric --rm -it --restart=Never --image=curlimages/curl -- \
  sh -c 'while true; do i=0; while [ "$i" -lt 8 ]; do curl -s "http://hpa-multi-metric-demo.hpa-multi-metric.svc.cluster.local/cpu?seconds=1" >/dev/null & i=$((i+1)); done; wait; done'
```

Memory pressure:

```sh
kubectl run memory-load -n hpa-multi-metric --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s "http://hpa-multi-metric-demo.hpa-multi-metric.svc.cluster.local/memory?mb=64&seconds=20"
```

Stop generators with Ctrl-C. Keep the HPA and pod watches open to see the metrics fall and the replica count scale down after the stabilization window. `maxReplicas` caps scaling at 12.

## Remove the module

This removes the module's namespace and everything deployed in it:

```sh
./scripts/remove.sh
```

The shared Prometheus Adapter and its registered rules remain installed because they are cluster-level preparation shared by modules.
