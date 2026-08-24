from flask import Flask, jsonify, request
import pyaudio
import threading
import wave
import numpy as np
import time
from gpiozero import PWMLED
from gpiozero import LED
from gpiozero import DigitalOutputDevice
from datetime import datetime
from utils.audio_utils import AudioUtils
from model.constants import MOUTH_MOTOR_PIN
from collections import deque

app = Flask(__name__)

# Audio parameters
CHUNK = 1024  # Frames per buffer
FORMAT = pyaudio.paInt16  # 16-bit audio
CHANNELS = 1  # Mono
# Global state
audio_state = {
    'stream': None,
    'audio': None,
    'is_streaming': False,
    'thread': None,
    'echo_buffer': deque([np.zeros(CHUNK, dtype=np.int16)] * 3, maxlen=3)
}

# Audio configuration
stream_timeout_seconds = 30  # 1 hour timeout

RATE = 48000  # Sample rate (Hz)

drop_threshold = .20
input_device_index = 1  # Set your input device index
output_device_index = 2  # Set your output device index

jaw_motor = None
previous_jaw_value = None

def stream_mic():
    """
    Stream audio using callback method (non-blocking).
    More efficient for continuous processing.
    """
        
    # Storage for audio data
    audio_buffer = []
    
    def audio_callback(in_data, frame_count, time_info, status):
        """This function is called for each audio chunk."""
        if status:
            print(f"Status: {status}")
        
        audio = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)    
        talk(audio, start_time =  datetime.now().timestamp())
        
        scary_audio = ultimate_scary_effect(audio)
        return (scary_audio.tobytes(), pyaudio.paContinue)
        
    # Open stream with callback
    stream = audio_state['audio'].open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    output=True,
                    input_device_index=input_device_index,
                    output_device_index=output_device_index,
                    frames_per_buffer=CHUNK,
                    stream_callback=audio_callback)
            
    stream.start_stream()
        # Keep stream active
    
    print(f"\nCaptured {len(audio_buffer)} chunks")
    return stream


def talk(audio_data, start_time):
    global previous_jaw_value, jaw_motor
    if jaw_motor is None:
        from model.constants import MOUTH_MOTOR_PIN
        jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)
    print(f"intial previous_jaw_value jaw_value: { previous_jaw_value }")
    jaw_motor.value 
    # Convert to numpy array
    #audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    
    peak = np.max(np.abs(audio_data))
    jaw_value = int(min(peak / 50, 100))
    
    # jaw value is less than previous but may be enough to keep jaw open. if it drops enough, we want the jaw to no stay open
    if(previous_jaw_value is not None and jaw_value < previous_jaw_value  ):
        diff = jaw_value / previous_jaw_value
        
        if(diff > drop_threshold):
            print(f"Drop greater than {drop_threshold}")
            jaw_value = 0 # setting to a percentage like jaw_value = jaw_value * .75 didn't improve noticably
    
    normalized_jaw_value = round(jaw_value / 100)
    print(f"Peak: {peak}; Jaw Value: {jaw_value}; Normalized jaw value: {normalized_jaw_value}; Previous jaw: {previous_jaw_value}" )
    jaw_motor.value = normalized_jaw_value 
    #AudioUtils.bar_graph(audio_data, peak, start_time) 
    previous_jaw_value = jaw_value   
        
def ultimate_scary_effect(audio_data):

    audio = audio_data.astype(np.float32)
    
    # PITCH SHIFT (make it deeper)
    shift_factor = 0.85
    num_samples = int(len(audio) * shift_factor)
    indices = np.linspace(0, len(audio) - 1, num_samples)
    audio = np.interp(indices, np.arange(len(audio)), audio)
    if len(audio) < len(audio_data):
        audio = np.pad(audio, (0, len(audio_data) - len(audio)), 'edge')
    else:
        audio = audio[:len(audio_data)]
    
 
    # ECHO (haunting repetition)
    output = audio.copy()
    for i, old_audio in enumerate(audio_state['echo_buffer']):
        decay = 0.5 ** (i + 1)
        output += old_audio.astype(np.float32) / 32768.0 * decay
    audio_state['echo_buffer'].append((audio * 32768.0).astype(np.int16))

    return output.astype(np.int16)

#@app.route('/start', methods=['POST'])
def start_streaming():
    """Start streaming microphone to speaker"""
    if audio_state['is_streaming']:
        return jsonify({'status': 'error', 'message': 'Already streaming'}), 400
    
    try:
        # Initialize PyAudio
        audio_state['audio'] = pyaudio.PyAudio()
        audio_state['is_streaming'] = True
        
        # Start streaming thread
        audio_state['thread'] = threading.Thread(target=stream_mic)
        audio_state['thread'].start()
        
        return jsonify({'status': 'success', 'message': 'Streaming started (mic to speaker)'})
    
    except Exception as e:
        audio_state['is_streaming'] = False
        return jsonify({'status': 'error', 'message': str(e)}), 500

#@app.route('/stop', methods=['POST'])
def stop_streaming():
    """Stop streaming microphone to speaker"""
    if not audio_state['is_streaming']:
        return jsonify({'status': 'error', 'message': 'Not streaming'}), 400
    
    try:
        # Stop streaming
        audio_state['is_streaming'] = False
        
        # Wait for thread to finish
        if audio_state['thread']:
            audio_state['thread'].join()
        
        # Terminate PyAudio
        if audio_state['audio']:
            audio_state['audio'].terminate()
        
        audio_state['stream'] = None
        
        return jsonify({
            'status': 'success', 
            'message': 'Streaming stopped'
        })
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/handler', methods=['POST'])
def handler():
    # get the body of the request
    data = request.json
    if data:
        action = data.get('action')
        print(f"Handler action received: {action}")
        if action == 'start':
            return start_streaming()
        elif action == 'stop':
            return stop_streaming()

    return jsonify({'status': 'error', 'message': 'Unknown action'}), 400

@app.route('/status', methods=['GET'])
def get_status():
    """Get current streaming status"""
    return jsonify({
        'is_streaming': audio_state['is_streaming']
    })

if __name__ == '__main__':
    app.run(debug=True, threaded=True)