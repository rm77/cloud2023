# Kubernetes HPA example: resource, concurrency, and queue metrics

Repository location: `kubernetes/research/hpa-multi-metric-example/`. Keep this example's `app/`, `k8s/`, and `README.md` files together in that directory. Run the relative commands below from this directory. From the repository root, enter it with:

```sh
cd kubernetes/research/hpa-multi-metric-example
```

This example demonstrates HPA signals beyond HTTP requests per second:

- **CPU utilization** and **memory utilization** from Metrics Server (`metrics.k8s.io`)
- **In-flight requests per pod** as a Prometheus custom Pods metric
- **Pending jobs per pod** as another Prometheus custom Pods metric

The included HPA lists all four signals. Kubernetes calculates a replica recommendation for each metric and uses the largest recommendation, subject to the HPA's replica limits. This lets resource pressure or application-level pressure trigger scaling. See the [Kubernetes HPA documentation](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/).

The queue is intentionally an in-memory, per-pod demonstration. It is useful for learning how queue-depth metrics can drive an HPA, but it is not a durable production queue: restarting or removing a pod can discard its pending jobs. For a real shared queue such as RabbitMQ or Kafka, expose queue backlog as an external metric through an appropriate adapter or KEDA.

## Prerequisites

The cluster infrastructure and shared monitoring stack are provided by the project setup under [`kubernetes/setup-cluster/supporting`](https://github.com/rm77/cloud2023/tree/master/kubernetes/setup-cluster/supporting). Its [`02-install-metrics-server.sh`](https://github.com/rm77/cloud2023/blob/master/kubernetes/setup-cluster/supporting/02-install-metrics-server.sh) installs Metrics Server and configures `--kubelet-insecure-tls`; [`05-install-prometheus.sh`](https://github.com/rm77/cloud2023/blob/master/kubernetes/setup-cluster/supporting/05-install-prometheus.sh) installs `kube-prometheus-stack` in namespace `monitoring` with Helm release `monitoring`. Complete the shared setup before running this example.

Make sure the existing Prometheus Adapter is available and configured to serve custom metrics. The two supporting scripts above do not install the Adapter. The included `k8s/prometheus-adapter-values.yaml` provides the rules for this workload; merge them into the existing Adapter's configuration rather than installing a second Adapter release.

Metrics Server supplies CPU and memory resource metrics. The Prometheus Adapter supplies the application-specific custom metrics used below. See the [Kubernetes resource metrics pipeline documentation](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/) for the distinction between these APIs.

The provided Kubernetes files already match the supporting setup: the Adapter values point to `monitoring-kube-prometheus-prometheus` in namespace `monitoring`, and the workload's `ServiceMonitor` uses label `release: monitoring`. Confirm the Prometheus service name with `kubectl get svc -n monitoring` if your installation differs.

## 1. Check Metrics Server

Make sure Metrics Server is installed and running in the cluster. Check that resource metrics are available with:

```sh
kubectl top nodes
```

If this command returns node CPU and memory usage, Metrics Server is providing the resource metrics needed by the CPU and memory HPA targets.

## 2. Build and deploy the workload

```sh
docker build -t hpa-multi-metric-demo:v1 ./app
kind load docker-image hpa-multi-metric-demo:v1
kubectl apply -f k8s/app.yaml
kubectl rollout status deployment/hpa-multi-metric-demo
```

If you are not using kind, make the image available to the cluster and omit `kind load`.

The app exports:

- `demo_inflight_requests`: current requests being processed by each pod
- `demo_queue_depth`: pending jobs in that pod's local demo queue

It also has `/cpu`, `/memory`, and `/slow` endpoints to generate CPU activity, temporary memory pressure, and concurrent requests. The `/queue?count=N` endpoint enqueues demo jobs.

## 3. Add this workload's rules to the existing Prometheus Adapter

Add the two custom rules in `k8s/prometheus-adapter-values.yaml` to the existing Adapter configuration; do not install a second Adapter release. Preserve the Adapter's existing values and merge these rules into its custom rules list. The file already contains the Prometheus service address used by the supporting setup. If the Adapter is managed by Helm, apply the merged values to its existing release and namespace.

After updating the existing adapter configuration and allowing Prometheus to scrape the app, verify the custom metrics API and values:

```sh
kubectl get apiservice v1beta1.custom.metrics.k8s.io
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/default/pods/*/inflight_requests"
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/default/pods/*/queue_depth"
```

If a metric is missing, check the `ServiceMonitor` selection, the Prometheus URL, adapter logs, and that the series has `namespace` and `pod` labels.

## 4. Apply the multi-metric HPA

```sh
kubectl apply -f k8s/hpa-multi-metric.yaml
kubectl get hpa hpa-multi-metric-demo -w
```

The example targets 70% CPU utilization, 80% memory utilization, 4 in-flight requests per pod, and 10 queued jobs per pod. Set CPU and memory requests because utilization targets are calculated relative to resource requests. Adjust the custom metric targets to match measured behavior.

Inspect individual metric readings and the HPA's events:

```sh
kubectl describe hpa hpa-multi-metric-demo
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/default/pods/*/inflight_requests"
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/default/pods/*/queue_depth"
```

## 5. Generate different kinds of load

### Queue backlog

In a separate terminal, repeatedly enqueue batches:

```sh
kubectl run queue-load --rm -it --restart=Never --image=curlimages/curl -- \
  sh -c 'while true; do curl -s "http://hpa-multi-metric-demo.default.svc.cluster.local/queue?count=10"; sleep 0.5; done'
```

Watch the queue depth per pod and the HPA:

```sh
kubectl get hpa,pods -w
```

### In-flight requests

Create concurrent requests that remain active for two seconds:

```sh
kubectl run concurrency-load --rm -it --restart=Never --image=curlimages/curl -- \
  sh -c 'while true; do i=0; while [ "$i" -lt 20 ]; do curl -s "http://hpa-multi-metric-demo.default.svc.cluster.local/slow?seconds=2" >/dev/null & i=$((i+1)); done; wait; done'
```

### CPU pressure

Generate CPU work in parallel:

```sh
kubectl run cpu-load --rm -it --restart=Never --image=curlimages/curl -- \
  sh -c 'while true; do i=0; while [ "$i" -lt 8 ]; do curl -s "http://hpa-multi-metric-demo.default.svc.cluster.local/cpu?seconds=1" >/dev/null & i=$((i+1)); done; wait; done'
```

### Memory pressure

Hold one 64 MiB allocation for 20 seconds:

```sh
kubectl run memory-load --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s "http://hpa-multi-metric-demo.default.svc.cluster.local/memory?mb=64&seconds=20"
```

Stop load generators with Ctrl-C. These generators are intentionally simple lab tools; keep runs short and observe the replica ceiling. Concurrent memory requests can exceed the container limit and cause an OOM restart.

## How HPA combines metrics

Each metric independently suggests a replica count. If CPU recommends 3, memory recommends 2, in-flight work recommends 5, and queue depth recommends 7, HPA chooses 7 (unless capped by `maxReplicas`). A metric that cannot be fetched can affect scale-down decisions, so inspect HPA conditions and events when metrics are unavailable. See [how HPA combines multiple metrics](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/).

## Bundle contents

- `app/`: instrumented Flask service with `/cpu`, `/memory`, `/slow`, and `/queue` endpoints
- `k8s/app.yaml`: Deployment, Service, and ServiceMonitor
- `k8s/hpa-multi-metric.yaml`: one HPA combining resource and custom pod metrics
- `k8s/prometheus-adapter-values.yaml`: adapter rules for concurrency and queue depth
