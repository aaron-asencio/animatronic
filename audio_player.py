import numpy as np
import pyaudio as pa
import wave
import time
from gpiozero import PWMLED
from gpiozero import LED
from gpiozero import DigitalOutputDevice
from datetime import datetime
from utils.audio_utils import AudioUtils
from model.constants import EYE_LIGHT_PIN, MOUTH_MOTOR_PIN
import logging
import sys

class AudioPlayer:
    def __init__(self):
        logging.basicConfig(filename='app.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.led_eye_light = LED(EYE_LIGHT_PIN)
        #self.jaw_motor = LED(MOUTH_MOTOR_PIN)
        self.jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)
         # Audio parameters
        self.CHUNK = 1024  # Frames per buffer
        self.FORMAT = pa.paInt16  # 16-bit audio
        self.CHANNELS = 1  # Mono
        
        self.RATE = 48000  # Sample rate (Hz)
        self.previous_jaw_value = None
        self.drop_threshold = .20
    
    def talk(self, audio_data, start_time):
       
                   
        print(f"intial previous_jaw_value jaw_value: { self.previous_jaw_value }")
        self.jaw_motor.value 
        # copy audio_data otherwise the buffer gets messed up
        data = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
        peak = np.max(np.abs(data))
        jaw_value = int(min(peak / 50, 100))
        
        # # jaw value is less than previous but may be enough to keep jaw open. if it drops enough, we want the jaw to no stay open
        if(self.previous_jaw_value is not None and jaw_value < self.previous_jaw_value  ):
            diff = jaw_value / self.previous_jaw_value
            
            if(diff > self.drop_threshold):
                print(f"Drop greater than {self.drop_threshold}")
                jaw_value = 0 # setting to a percentage like jaw_value = jaw_value * .75 didn't improve noticably
        
        normalized_jaw_value = round(jaw_value / 100)
        print(f"Peak: {peak}; Jaw Value: {jaw_value}; Normalized jaw value: {normalized_jaw_value}; Previous jaw: {self.previous_jaw_value}" )
        self.jaw_motor.value = normalized_jaw_value 
        #AudioUtils.bar_graph(data, peak, start_time) # this breaks call from nodered
        previous_jaw_value = jaw_value   
            

    def play_audio_file(self, audio_file, output_device_index=2):
        """
        Stream audio using callback method (non-blocking).
        More efficient for continuous processing.
        """
        p = pa.PyAudio()
 
        wf = wave.open(audio_file, 'rb')   
        
        def audio_callback(in_data, frame_count, time_info, status):
            data = wf.readframes(frame_count)
                
            self.talk(data, start_time =  datetime.now().timestamp())
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
    music = ['beetel-exorcist.wav', 'blah.wav', 'krusty-laugh.wav', 'sb_party_switch.wav','spongebob-torture.mp3',
             'vader-beaten.wav', 'vader-father.wav', 'were-waiting.wav', 'yoda-900.wav', 'yoda-agent-evil.wav',
             'yoda-fear.wav','hello-everyone.wav','happy-halloween.wav','walk.wav','how-yall.wav','cant-hear.wav']
    p = AudioPlayer()
    path = "/home/aaron/Music/"
    audio_file_name =  "krusty-laugh.wav"
    arg1 = sys.argv[1]
    split = arg1.split("=")
    if len(split) == 2:
        
        value = split[1]
        audio_file_name = value
        
        match value:
            case "exorcist":
                audio_file_name = "beetel-exorcist.wav"
            case "blah":
                audio_file_name = "blah.wav"
            case "krusty":
                audio_file_name = "krusty-laugh.wav"
            # case "startParty": # sound is sped up
            #     audio_file_name = "sb_party_switch.wav"
            # case "torture": # must be wav
            #     audio_file_name = "spongebob-torture.mp3"
            case "vaderBeaten":
                audio_file_name = "vader-beaten.wav"
            case "vaderFather":
                audio_file_name = "vader-father.wav"
            case "waiting":
                audio_file_name = "were-waiting.wav"
            case "yoda-900":
                audio_file_name = "yoda-900.wav"
            case "yoda-agent-evil":
                audio_file_name = "yoda-agent-evil.wav"
            case "yoda-fear":
                audio_file_name = "yoda-fear.wav"
          

            case _:
                raise ValueError("Unknown audio file name: " + audio_file_name)
        
    
    #audio_file = "/home/aaron/Music/krusty-laugh.wav"  # Replace with your audio file path
    # good ones: like-this-one.wav, beetel-exorcist.wav, blah.wav, evil-laugh.wav, krusty-laugh.wav, were-waiting.wav
    # waiting.wav not good
    audio_file = path + audio_file_name
    #print(f"Audio file: {audio_file_name}")
    #audio_file = "/home/aaron/Music/sb_party_switch.wav"
    #raise ValueError(f"audio_file_name: [{audio_file}]")
    try:
        print(f"Playing audio file: {audio_file}")
        logging.info(f"Playing audio file: {audio_file}")
        p.play_audio_file(audio_file)
    except Exception as e:
        print(f"Error playing audio file: {e}")
        logging.error(f"Error playing audio file: {e}")
    