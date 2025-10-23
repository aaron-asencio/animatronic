import numpy as np
import pyaudio as pa
import wave
import time
from gpiozero import PWMLED
from gpiozero import LED
from gpiozero import Device
from datetime import datetime
from utils.audio_utils import AudioUtils
from model.constants import EYE_LIGHT_PIN, MOUTH_MOTOR_PIN
import sys

class AudioPlayer:
    def __init__(self):
        self.led_eye_light = LED(EYE_LIGHT_PIN)
        self.jaw_motor = LED(MOUTH_MOTOR_PIN)
  
         # Audio parameters
        self.CHUNK = 1024  # Frames per buffer
        self.FORMAT = pa.paInt16  # 16-bit audio
        self.CHANNELS = 1  # Mono
        
        self.RATE = 48000  # Sample rate (Hz)
        self.previous_jaw_value = None
        self.drop_threshold = .20
    
    def play_audio_file(self, audio_file, output_device_index=2):
        """
        Stream audio using callback method (non-blocking).
        More efficient for continuous processing.
        """
        p = pa.PyAudio()
 
        wf = wave.open(audio_file, 'rb')   
        
        def audio_callback(in_data, frame_count, time_info, status):
            data = wf.readframes(frame_count)
                
            #self.talk(data, start_time =  datetime.now().timestamp())
            if len(data) == 0:
              return (data, pa.paComplete)
            return (data, pa.paContinue)
       
        # Open stream with callback based on the wave file's properties
        stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                        channels=wf.getnchannels(),
                        rate=wf.getframerate(),
                        output=True,
                        output_device_index=output_device_index,
                        stream_callback=audio_callback)  # Set to True to play audio through speakers
        
        stream.start_stream()
        # Keep alive while playing
        while stream.is_active():
            time.sleep(0.1)
        stream.stop_stream()
        stream.close()
        p.terminate()
   
if __name__ == "__main__":        
    p = AudioPlayer()
    path = "/home/aaron/Music/"
    audio_file_name = sys.argv[1]
    #audio_file = "/home/aaron/Music/krusty-laugh.wav"  # Replace with your audio file path
    # good ones: like-this-one.wav, beetel-exorcist.wav, blah.wav, evil-laugh.wav, krusty-laugh.wav, were-waiting.wav
    # waiting.wav not good
    audio_file = path + audio_file_name
    print(f"Audio file: {audio_file}")
    p.play_audio_file(audio_file)