import os
import queue
import threading
import time

from flask import Flask, Response, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

app = Flask(__name__)

INFLIGHT = Gauge("demo_inflight_requests", "Requests currently being processed")
QUEUE_DEPTH = Gauge("demo_queue_depth", "Jobs waiting in this pod's in-memory queue")
JOB_QUEUE = queue.Queue(maxsize=1000)
JOB_TIME_SECONDS = float(os.environ.get("JOB_TIME_SECONDS", "0.5"))
MEMORY_LOADS = []


def update_queue_depth():
    QUEUE_DEPTH.set(JOB_QUEUE.qsize())


def queue_worker():
    while True:
        JOB_QUEUE.get()
        update_queue_depth()
        time.sleep(JOB_TIME_SECONDS)
        JOB_QUEUE.task_done()


threading.Thread(target=queue_worker, daemon=True).start()
update_queue_depth()


@app.before_request
def count_inflight_start():
    if request.path not in ("/metrics", "/healthz"):
        INFLIGHT.inc()


@app.teardown_request
def count_inflight_end(_error):
    if request.path not in ("/metrics", "/healthz"):
        INFLIGHT.dec()


@app.get("/healthz")
def healthz():
    return "ok\n"


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.get("/slow")
def slow():
    seconds = min(max(request.args.get("seconds", default=1.0, type=float), 0), 10)
    time.sleep(seconds)
    return jsonify(message="completed", delay_seconds=seconds)


@app.get("/cpu")
def cpu():
    seconds = min(max(request.args.get("seconds", default=1.0, type=float), 0), 10)
    deadline = time.monotonic() + seconds
    value = 0
    while time.monotonic() < deadline:
        value = (value + 1) % 1000003
    return jsonify(message="completed", iterations=value)


@app.get("/memory")
def memory():
    megabytes = min(max(request.args.get("mb", default=32, type=int), 1), 64)
    seconds = min(max(request.args.get("seconds", default=10, type=int), 1), 20)
    allocation = bytearray(megabytes * 1024 * 1024)
    MEMORY_LOADS.append(allocation)
    time.sleep(seconds)
    MEMORY_LOADS.remove(allocation)
    return jsonify(message="completed", allocated_mb=megabytes, held_seconds=seconds)


@app.get("/queue")
def enqueue():
    count = min(max(request.args.get("count", default=1, type=int), 1), 100)
    accepted = 0
    for _ in range(count):
        try:
            JOB_QUEUE.put_nowait(1)
            accepted += 1
        except queue.Full:
            break
    update_queue_depth()
    return jsonify(accepted=accepted, queue_depth=JOB_QUEUE.qsize())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
