from flask import Flask
import time

app = Flask(__name__)


@app.route("/")
def root():
    return "experiment-app OK\n"


@app.route("/cpu")
def cpu():
    x = 0
    for i in range(2_000_000):
        x += i * i

    return {
        "status": "ok",
        "result": x
    }


@app.route("/sleep")
def sleep():
    time.sleep(0.05)

    return {
        "status": "ok",
        "delay": 0.05
    }


@app.route("/health")
def health():
    return {
        "status": "healthy"
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
