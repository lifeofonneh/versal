import threading
from flask import Flask, jsonify
from versal_pipeline import run_pipeline
import asyncio

app = Flask(__name__)

_running = False  # simple flag — prevents stacking runs

@app.route("/")
def index():
    return "Versal Lead Machine is live.", 200

@app.route("/run")
def run():
    global _running
    if _running:
        return jsonify({"status": "skipped", "reason": "pipeline already running"}), 200

    def background():
        global _running
        _running = True
        try:
            asyncio.run(run_pipeline(test_mode=False))
        finally:
            _running = False

    threading.Thread(target=background, daemon=True).start()
    return jsonify({"status": "started"}), 200

@app.route("/status")
def status():
    return jsonify({"running": _running}), 200
