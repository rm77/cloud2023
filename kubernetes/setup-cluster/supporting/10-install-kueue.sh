VERSION=v0.20.3
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/${VERSION}/manifests.yaml
kubectl wait deploy/kueue-controller-manager -nkueue-system --for=condition=available --timeout=5m
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/${VERSION}/prometheus.yaml
kubectl get servicemonitor -n kueue-system

#https://kueue.sigs.k8s.io/docs/tasks/manage/observability/setup_prometheus/
#

