from flask import Flask, request, jsonify
from audio_streamer import AudioStreamer

app = Flask(__name__)
global streamer
streamer = None

@app.route('/startmic', methods=['POST'])
def startmic():
    global streamer
    if request.method == 'POST':
        data = request.json  # Get JSON data from the request body
   
        print(f"Received webhook data: {data}")
       
        if streamer is not None:
            streamer = AudioStreamer()
            streamer.start() 
        return jsonify({"message": "Streaming started"}), 200
    else:
        return jsonify({"message": "Method not allowed"}), 405
    
@app.route('/stopmic', methods=['POST'])
def stopmic():
    if request.method == 'POST':
        data = request.json  # Get JSON data from the request body

        print(f"Received webhook data: {data}")

        if streamer is not None:
            streamer.stop()
        return jsonify({"message": "Streaming stopped"}), 200
    else:
        return jsonify({"message": "Method not allowed"}), 405    

@app.route('/')
def index():
      return jsonify({"message": "Server is up"}), 200
if __name__ == '__main__':
    app.run(debug=True, port=5000)