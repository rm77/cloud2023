#!/bin/sh
# A custom metric for vdc_meter.py from the cluster's command line: the network traffic of every
# guest, in GB (received + sent) since the last round. In metrics.json:
#
#   {"name": "traffic", "label": "Network traffic", "source": "command",
#    "command": "sh examples/traffic.sh /path/to/vdc-cluster",
#    "kind": "amount", "unit": "GB", "price": 0.05}
#
# It asks the hub which guest is on which node for which tenant (hubctl vms) and each node for
# its guests' byte counters (virsh domstats --interface), over SSH with the kit's key, so the
# meter runs as root on the main PC. The first round only records the counters; a counter that
# went down (the guest was restarted) counts again from zero. Prints TENANT GUEST GB per line.
set -eu
KIT=$(cd "${1:?usage: traffic.sh KIT_FOLDER}" && pwd)
LAST=${TRAFFIC_STATE:-$(pwd)/state/traffic.last}     # GUEST BYTES of the last round
HUB=${HUB_IP:-$(sed -n 's/^HUB_IP=//p' "$KIT/cluster.conf" | tr -d '"' | tail -n 1)}
[ -n "$HUB" ] || { echo "traffic.sh: no HUB_IP in $KIT/cluster.conf" >&2; exit 1; }

hub() {
  ssh -n -F /dev/null -i "$KIT/state/id_lab" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 "root@$HUB" "$@"
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
hub hubctl vms | awk 'NR > 1 {print $1, $2, $3}' >"$tmp/guests"      # GUEST TENANT NODE

for node in $(awk '{print $3}' "$tmp/guests" | sort -u); do
  hub hubctl exec "$node" virsh domstats --interface || echo "traffic.sh: node $node did not answer" >&2
done | awk '/^Domain:/ {d = $2; gsub(/[^A-Za-z0-9_.-]/, "", d)}
            /^ *net\.[0-9]+\.(rx|tx)\.bytes=/ {split($1, a, "="); b[d] += a[2]}
            END {for (d in b) print d, b[d]}' >"$tmp/now"                # GUEST BYTES

mkdir -p "$(dirname "$LAST")"
touch "$LAST"
awk 'FILENAME == ARGV[1] {last[$1] = $2; next}
     FILENAME == ARGV[2] {tenant[$1] = $2; next}
     ($1 in tenant) && ($1 in last) {
       d = $2 - last[$1]; if (d < 0) d = $2
       if (d > 0) printf "%s %s %.6f\n", tenant[$1], $1, d / 1e9 }' "$LAST" "$tmp/guests" "$tmp/now"
# keep the last counters of guests not seen this round (a node that did not answer), so their
# traffic is charged once the node answers again
awk 'FILENAME == ARGV[1] {seen[$1] = 1; print; next} !($1 in seen)' "$tmp/now" "$LAST" >"$tmp/keep"
cp "$tmp/keep" "$LAST"
