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
from collections import deque
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
      
        self.echo_buffer = deque([np.zeros(self.CHUNK, dtype=np.int16)] * 3, maxlen=3)
        self.reverb_buffer = np.zeros(8820, dtype=np.float32)
        self.reverb_pos = 0

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
      

    def talk(self, audio_data, start_time):
        
        print(f"intial previous_jaw_value jaw_value: { self.previous_jaw_value }")
        self.jaw_motor.value 
        # Convert to numpy array
        #audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32)
       
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
    
    def ultimate_scary_effect(self, audio_data):
        """
        Combines multiple effects for maximum scariness:
        1. Lower pitch (demonic)
        2. Add distortion (possessed)
        3. Add echo (haunting)
        4. Add reverb (otherworldly)
        """
        
        
        audio = audio_data.astype(np.float32)
        
        # 1. PITCH SHIFT (make it deeper)
        shift_factor = 0.85
        num_samples = int(len(audio) * shift_factor)
        indices = np.linspace(0, len(audio) - 1, num_samples)
        audio = np.interp(indices, np.arange(len(audio)), audio)
        if len(audio) < len(audio_data):
            audio = np.pad(audio, (0, len(audio_data) - len(audio)), 'edge')
        else:
            audio = audio[:len(audio_data)]
        
        # can't hear audio when this is on
        # 2. DISTORTION (add grittiness)
        # audio = audio / 32768.0
        # threshold = 0.4
        # audio = np.clip(audio, -threshold, threshold) / threshold
        
        # 3. ECHO (haunting repetition)
        output = audio.copy()
        for i, old_audio in enumerate(self.echo_buffer):
            decay = 0.5 ** (i + 1)
            output += old_audio.astype(np.float32) / 32768.0 * decay
        self.echo_buffer.append((audio * 32768.0).astype(np.int16))
        
        # too choppy - decreasing delay did not help
        # 4. REVERB (spooky space)
        # result = np.zeros_like(output)
        # for i in range(len(output)):
        #     current = output[i]
        #     reverb_out = 0
        #     for delay in [2205, 4410]:
        #         if self.reverb_pos >= delay:
        #             reverb_out += self.reverb_buffer[self.reverb_pos - delay] * 0.3
        #     result[i] = current + reverb_out * 0.6
        #     self.reverb_buffer[self.reverb_pos] = current + reverb_out * 0.2
        #     self.reverb_pos = (self.reverb_pos + 1) % len(self.reverb_buffer)
        
        # Convert back and prevent clipping
        #output = np.clip(result * 32768.0, -32768, 32767)
        return output.astype(np.int16)
              
    def stream_mic(self, input_device_index=1,output_device_index=2, duration=20):
        """
        Stream audio using callback method (non-blocking).
        More efficient for continuous processing.
        """
           
        # Storage for audio data
        audio_buffer = []
        
       
        def scary_pitch_shift(audio_data, shift_factor=0.7):
            """
            Lower the pitch to make voice sound demonic/scary
            shift_factor < 1.0 = deeper/scarier (try 0.6 - 0.8)
            """
            # Convert to float for processing
            audio_float = audio_data.astype(np.float32)
            
            # Resample to lower pitch (makes voice deeper)
            num_samples = int(len(audio_float) * shift_factor)
            indices = np.linspace(0, len(audio_float) - 1, num_samples)
            lowered = np.interp(indices, np.arange(len(audio_float)), audio_float)
            
            # Pad back to original size
            if len(lowered) < len(audio_float):
                lowered = np.pad(lowered, (0, len(audio_float) - len(lowered)), 'edge')
            else:
                lowered = lowered[:len(audio_float)]
            
            return lowered.astype(np.int16)

        def audio_callback(in_data, frame_count, time_info, status):
            """This function is called for each audio chunk."""
            if status:
                print(f"Status: {status}")
            
            audio = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)    
            self.talk(audio, start_time =  datetime.now().timestamp())
            # filter stream data to lower voice for output
                # Apply scary pitch shift
            #scary_audio = scary_pitch_shift(audio, shift_factor=0.85)
            # Convert back to bytes
            #output = scary_audio.tobytes()
            
            scary_audio = self.ultimate_scary_effect(audio)
            return (scary_audio.tobytes(), pyaudio.paContinue)
            
            #return (output, pyaudio.paContinue)
        
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
 
