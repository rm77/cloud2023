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

## Requirements

- Kubernetes cluster and `kubectl`
- Docker and `kind` for the example image workflow (or another way to build/publish the image)
- Prometheus Operator stack (for example `kube-prometheus-stack`), Prometheus Adapter, and Helm

Metrics Server supplies the CPU and memory resource metrics; it does not supply the two application metrics. Resource metric support is limited to CPU and memory, while application signals use a custom metrics API. See the [Kubernetes resource metrics pipeline documentation](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/).

The Prometheus Adapter values assume Prometheus is reachable at `http://kube-prometheus-stack-prometheus.monitoring.svc:9090` and discovers `ServiceMonitor` resources labeled `release: kube-prometheus-stack` in `default`. Adjust those for your installation.

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

## 3. Configure Prometheus Adapter

Install the adapter with the custom metric rules:

```sh
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install prometheus-adapter prometheus-community/prometheus-adapter \
  --namespace monitoring --create-namespace \
  --values k8s/prometheus-adapter-values.yaml
```

Verify the API and metrics are available after Prometheus scrapes the app:

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
