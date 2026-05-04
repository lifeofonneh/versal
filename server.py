import threading
import asyncio
import logging
from flask import Flask, jsonify
from versal_pipeline import run_pipeline

app = Flask(__name__)
logger = logging.getLogger(__name__)

_running = False

def background():
    global _running
    _running = True
    try:
        asyncio.run(run_pipeline(test_mode=False))
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
    finally:
        _running = False

@app.route("/")
def index():
    return "Versal Lead Machine is live.", 200

@app.route("/run")
def run():
    global _running
    if _running:
        return jsonify({"status": "skipped", "reason": "pipeline already running"}), 200
    t = threading.Thread(target=background, daemon=False)  # daemon=False keeps process alive
    t.start()
    return jsonify({"status": "started"}), 200

@app.route("/status")
def status():
    return jsonify({"running": _running}), 200

# Keep-alive ping so Render doesn't spin down mid-pipeline
@app.route("/ping")
def ping():
    return "pong", 200
