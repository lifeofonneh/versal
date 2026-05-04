"""
Tiny Flask server — cron-job.org hits /run and triggers the pipeline.
Deploy this as a Render Web Service (free tier).
"""

import asyncio, threading
from flask import Flask, jsonify
from versal_pipeline import run_pipeline

app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "Versal Lead Machine is alive ✅"})

@app.route("/run")
def run():
    def background():
        asyncio.run(run_pipeline(test_mode=False))
    threading.Thread(target=background).start()
    return jsonify({"status": "Pipeline started ✅"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
