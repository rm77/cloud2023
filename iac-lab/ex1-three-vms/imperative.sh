#!/bin/sh
# imperative.sh - example 1 the way staff would do it on the lab PC: one command per VM.
#   sudo sh ex1-three-vms/imperative.sh tenant-a
# Run it twice and see the second run fail: the guests already exist.
set -e
t=${1:?usage: imperative.sh TENANT}
sudo ./cluster.sh hubctl vm add "$t" app1  --image alpine --mem 512
sudo ./cluster.sh hubctl vm add "$t" app2  --image alpine --mem 512
sudo ./cluster.sh hubctl vm add "$t" test1 --image cirros
