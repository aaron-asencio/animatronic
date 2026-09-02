from flask import Flask, request, jsonify
from audio_streamer import AudioStreamer

app = Flask(__name__)

streamer: AudioStreamer | None = None


@app.route('/startmic', methods=['POST'])
def startmic():
    global streamer
    data = request.json or {}
    print(f"startmic received: {data}")

    if streamer is not None and streamer.stream is not None and streamer.stream.is_active():
        return jsonify({"message": "Already streaming"}), 400

    streamer = AudioStreamer()
    streamer.start()
    return jsonify({"message": "Streaming started"}), 200


@app.route('/stopmic', methods=['POST'])
def stopmic():
    data = request.json or {}
    print(f"stopmic received: {data}")

    if streamer is None:
        return jsonify({"message": "Not streaming"}), 400

    streamer.stop()
    return jsonify({"message": "Streaming stopped"}), 200


@app.route('/config', methods=['POST'])
def set_config():
    """Update jaw tuning parameters on the live AudioStreamer instance.

    Accepts JSON body with any combination of:
        sensitivity    (int/float) — peak divisor; lower = more sensitive
        drop_threshold (float)     — close ratio 0.0–1.0

    Changes take effect immediately on the running stream — no restart needed.

    Example:
        curl -X POST http://localhost:5000/config \\
             -H 'Content-Type: application/json' \\
             -d '{"sensitivity": 300, "drop_threshold": 0.15}'
    """
    if streamer is None:
        return jsonify({'status': 'error', 'message': 'No streamer initialised — start mic first'}), 400

    data = request.json or {}
    updated = {}

    if 'sensitivity' in data:
        value = float(data['sensitivity'])
        if value <= 0:
            return jsonify({'status': 'error', 'message': 'sensitivity must be > 0'}), 400
        streamer.sensitivity = value
        updated['sensitivity'] = value

    if 'noise_floor' in data:
        value = float(data['noise_floor'])
        if value < 0:
            return jsonify({'status': 'error', 'message': 'noise_floor must be >= 0'}), 400
        streamer.noise_floor = value
        updated['noise_floor'] = value

    if 'drop_threshold' in data:
        value = float(data['drop_threshold'])
        if not (0.0 <= value <= 1.0):
            return jsonify({'status': 'error', 'message': 'drop_threshold must be 0.0–1.0'}), 400
        streamer.drop_threshold = value
        updated['drop_threshold'] = value

    print(f"Jaw config updated: sensitivity={streamer.sensitivity}, drop_threshold={streamer.drop_threshold}")
    return jsonify({
        'status': 'success',
        'updated': updated,
        'config': {
            'sensitivity': streamer.sensitivity,
            'drop_threshold': streamer.drop_threshold,
        },
    })


@app.route('/status', methods=['GET'])
def status():
    """Return streaming state and current jaw tuning config."""
    is_streaming = (
        streamer is not None
        and streamer.stream is not None
        and streamer.stream.is_active()
    )
    config = (
        {'sensitivity': streamer.sensitivity, 'noise_floor': streamer.noise_floor, 'drop_threshold': streamer.drop_threshold}
        if streamer is not None else {}
    )
    return jsonify({'is_streaming': is_streaming, 'config': config})


@app.route('/')
def index():
    return jsonify({"message": "Server is up"}), 200


if __name__ == '__main__':
    app.run(debug=True, port=5000)
