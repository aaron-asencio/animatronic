import pyaudio
import wave
import numpy as np
import time
from gpiozero import PWMLED
from gpiozero import LED
from gpiozero import DigitalOutputDevice
from datetime import datetime
from utils.audio_utils import AudioUtils
from model.constants import EYE_LIGHT_PIN, MOUTH_MOTOR_PIN
import sys

class AudioStreamer:
    def __init__(self):
       
        self.led_eye_light = LED(EYE_LIGHT_PIN)
        self.jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)
  
         # Audio parameters
        self.CHUNK = 1024  # Frames per buffer
        self.FORMAT = pyaudio.paInt16  # 16-bit audio
        self.CHANNELS = 1  # Mono
        
        self.RATE = 48000  # Sample rate (Hz)
        self.previous_jaw_value = None
        self.drop_threshold = .20

    def test_led(self):
        print("Testing LED light...")
        for i in range(5):
            self.led_eye_light.value = 0.05
            time.sleep(0.2)
            self.led_eye_light.value = 0.0
            time.sleep(0.2)
            # self.led_eye_light.on()
            # time.sleep(0.2)
            # self.led_eye_light.off()
            #time.sleep(0.2)
        print("LED test complete.")
     
    def open_streams(self,  input_device_index=1, output_device_index=2):
        p = pyaudio.PyAudio()
        input_stream = p.open(format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.RATE,
                        input=True,
                        input_device_index=input_device_index,
                        frames_per_buffer=self.CHUNK)
        
        
        output_stream = p.open(format=self.FORMAT,
                       channels=self.CHANNELS,
                       rate=self.RATE,
                       output=True,
                       frames_per_buffer=self.CHUNK,
                       output_device_index=output_device_index) 
        return p, input_stream, output_stream
      

    def talk(self, data, start_time):
        
        print(f"intial previous_jaw_value jaw_value: { self.previous_jaw_value }")
        self.jaw_motor.value 
        # Convert to numpy array
        audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32)
       
        peak = np.max(np.abs(audio_data))
        jaw_value = int(min(peak / 50, 100))
        
        # jaw value is less than previous but may be enough to keep jaw open. if it drops enough, we want the jaw to no stay open
        if(self.previous_jaw_value is not None and jaw_value < self.previous_jaw_value  ):
            diff = jaw_value / self.previous_jaw_value
            
            if(diff > self.drop_threshold):
                print(f"Drop greater than {self.drop_threshold}")
                jaw_value = 0 # setting to a percentage like jaw_value = jaw_value * .75 didn't improve noticably
       
        normalized_jaw_value = round(jaw_value / 100)
        print(f"Peak: {peak}; Jaw Value: {jaw_value}; Normalized jaw value: {normalized_jaw_value}; Previous jaw: {self.previous_jaw_value}" )
        self.jaw_motor.value = normalized_jaw_value 
        AudioUtils.bar_graph(audio_data, peak, start_time) 
        self.previous_jaw_value = jaw_value   
        
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
    def stream_mic_blocking(self, input_device_index=1, output_device_index=2, duration=10):

        p, input_stream, output_stream = self.open_streams(input_device_index, output_device_index,)
                       
        print(f"Streaming audio for {duration} seconds...")
        print("Real-time amplitude monitoring:")
        print("-" * 60)
        
        start_time = time.time()
        
        try:
            while (time.time() - start_time) < duration:
                # Read audio data
                data = input_stream.read(self.CHUNK, exception_on_overflow=False)
                self.talk(data, start_time)
                # Write to speakers
                output_stream.write(data)

        except KeyboardInterrupt:
            print("\nStopped by user")
        
        AudioUtils.close_streams(input_stream, output_stream, p)    
              
    def stream_mic(self, input_device_index=1,output_device_index=2, duration=10):
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
                
            self.talk(in_data, start_time =  datetime.now().timestamp())
            return (in_data, pyaudio.paContinue)
        
        p = pyaudio.PyAudio()
        
        # Open stream with callback
        stream = p.open(format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.RATE,
                        input=True,
                        output=True,
                        input_device_index=input_device_index,
                        output_device_index=output_device_index,
                        frames_per_buffer=self.CHUNK,
                        stream_callback=audio_callback)
        
      
        print(f"Streaming with callback for {duration} seconds...")
        stream.start_stream()
      
        # Keep stream active
        time.sleep(duration)
        
        stream.stop_stream()
        stream.close()
        p.terminate()
   
        
        print(f"\nCaptured {len(audio_buffer)} chunks")
        #return np.concatenate(audio_buffer)

if __name__ == "__main__":
   
    c = AudioStreamer()
    # c.stream_with_realtime_processing(input_device_index = 1, output_device_index=2, duration=5)
    c.stream_mic(input_device_index = 1, output_device_index=2, duration=5)
    # path = "/home/aaron/Music/"
    # audio_file_name = sys.argv[1]
    # #audio_file = "/home/aaron/Music/krusty-laugh.wav"  # Replace with your audio file path
    # # good ones: like-this-one.wav, beetel-exorcist.wav, blah.wav, evil-laugh.wav, krusty-laugh.wav, were-waiting.wav
    # # waiting.wav not good
    # audio_file = path + audio_file_name
    # print(f"Audio file: {audio_file}")
    # c.play_audio_file(audio_file)
 
