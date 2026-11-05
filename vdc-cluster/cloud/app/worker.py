# worker.py - runs the queued jobs, one at a time. The Job table holds every job's state;
# what hands jobs to this process depends on config.QUEUE_BACKEND:
#   redis   an RQ worker on the "vdc-cloud" queue; each RQ job calls run_job(id)  (default)
#   sqlite  this process polls the Job table itself
#   python -m app.worker          run until stopped
#   python -m app.worker --once   (sqlite) run what is queued now, then exit
# One worker is enough: hubctl takes a lock for every change anyway.
import argparse
import json
import os
import signal
import threading
import time
import traceback

from sqlmodel import Session, select, update

from . import config, tasks
from .db import Job, WorkerBeat, engine, init_db, now

STOP = False


def beat():
    with Session(engine) as s:
        b = s.get(WorkerBeat, 1) or WorkerBeat(id=1)
        b.ts, b.pid = now(), os.getpid()
        s.add(b); s.commit()


def claim(jid: str | None = None) -> Job | None:
    """a queued job (that one, or the oldest), marked started (the conditional update makes it ours)"""
    with Session(engine) as s:
        q = select(Job).where(Job.state == "queued")
        q = q.where(Job.id == jid) if jid else q.order_by(Job.created)
        j = s.exec(q).first()
        if j is None:
            return None
        n = s.exec(update(Job).where(Job.id == j.id, Job.state == "queued")
                   .values(state="started", started=now())).rowcount
        s.commit()
        if n != 1:
            return None
        s.refresh(j)
        s.expunge(j)
        return j


def finish(jid, state, result="", error=""):
    with Session(engine) as s:
        j = s.get(Job, jid)
        j.state, j.ended, j.result, j.error = state, now(), result, error[:2000]
        s.add(j); s.commit()


def run_one() -> bool:
    j = claim()
    if j is None:
        return False
    execute(j)
    return True


def run_job(jid: str):
    """the RQ entry point: run one job by its id"""
    j = claim(jid)
    if j is None:
        return {"skipped": jid}
    execute(j)
    return {"job": jid}


def execute(j: Job):
    f = tasks.TASKS.get(j.func)
    if f is None:
        finish(j.id, "failed", error=f"unknown task {j.func!r}")
        return
    print(f"{now():%F %T} {j.id} {j.func} started", flush=True)
    try:
        res = f(**json.loads(j.kwargs or "{}"))
        finish(j.id, "finished", result=json.dumps(res, default=str))
        print(f"{now():%F %T} {j.id} finished", flush=True)
    except Exception as e:                      # the task failed: keep its message for the job view
        finish(j.id, "failed", error=str(e) or e.__class__.__name__)
        print(f"{now():%F %T} {j.id} failed: {e}", flush=True)
        traceback.print_exc()


def run_pending() -> int:
    n = 0
    while run_one():
        n += 1
    return n


def recover():
    """jobs a previous worker left half-done (it was stopped or crashed)"""
    with Session(engine) as s:
        for j in s.exec(select(Job).where(Job.state == "started")):
            j.state, j.ended, j.error = "failed", now(), "the worker stopped while this job ran; start it again"
            s.add(j)
        s.commit()


def main():
    ap = argparse.ArgumentParser(description="vdc-cloud job worker")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    init_db()
    recover()

    def stop(*_):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if config.QUEUE_BACKEND == "redis":
        from rq import Worker
        from .services import redis_conn, rq_queue

        def beating():
            while True:
                beat(); time.sleep(5)
        threading.Thread(target=beating, daemon=True).start()
        print(f"{now():%F %T} worker {os.getpid()} ready (Redis queue {config.QUEUE})", flush=True)
        Worker([rq_queue()], connection=redis_conn()).work()
        return
    print(f"{now():%F %T} worker {os.getpid()} ready (SQLite queue)", flush=True)
    while not STOP:
        beat()
        if not run_one():
            if a.once:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
