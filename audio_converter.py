import pyaudio
import wave
import numpy as np
import time
from gpiozero import PWMLED
from gpiozero import LED
from gpiozero import Device

# import AudioUtils
from utils.audio_utils import AudioUtils


from model.constants import EYE_LIGHT_PIN, MOUTH_MOTOR_PIN

class AudioConverter:
    def __init__(self, audio_file):
        self.audio_file = audio_file
        self.led_eye_light = LED(EYE_LIGHT_PIN)
        self.jaw_motor = LED(MOUTH_MOTOR_PIN)
        
        # print the Device.pin_factory being used
        print(f"Eye light pin factory: {Device.pin_factory}")

        # self.mouth_motor = PWMLED(MOUTH_MOTOR_PIN)
         # Audio parameters
        self.CHUNK = 1024  # Frames per buffer
        self.FORMAT = pyaudio.paInt16  # 16-bit audio
        self.CHANNELS = 1  # Mono
        
        self.RATE = 48000  # Sample rate (Hz)
        self.previous_jaw_value = None

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
     
    
        
    def stream_with_realtime_processing(self, input_device_index=None, output_device_index=None, duration=10):
        """
        Stream audio from USB mic with real-time amplitude monitoring.
        """
      
        p = pyaudio.PyAudio()
        
        input_stream = p.open(format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.RATE,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=self.CHUNK)
        
        
        output_stream = p.open(format=self.FORMAT,
                       channels=self.CHANNELS,
                       rate=self.RATE,
                       output=True,
                       frames_per_buffer=self.CHUNK,
                       output_device_index=2) 
                       
        
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
        
        input_stream.stop_stream()
        input_stream.close()
        output_stream.stop_stream()
        output_stream.close()
        p.terminate()
        print("\nStreaming stopped.")

    def talk(self, data, start_time):
        
        print(f"intial previous_jaw_value jaw_value: { self.previous_jaw_value }")
        self.led_eye_light.value 
        # Convert to numpy array
        audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32)
       
        peak = np.max(np.abs(audio_data))

        # over 51 works about the same as 51      
        jaw_value = int(min(peak / 50, 100))
        
        # jaw value is less than previous but may be enough to keep jaw open. if it drops enough, we want the jaw to no stay open
        if(self.previous_jaw_value is not None and jaw_value < self.previous_jaw_value  ):
            diff = jaw_value / self.previous_jaw_value
            print(f"diff is {diff}")
            if(diff > .20):
                print(f"greater than 25% ")
                jaw_value = 0 # setting to a percentage like jaw_value = jaw_value * .75 didn't improve noticably
       
          
        normalized_jaw_value = round(jaw_value / 100)
        print(f"Peak: {peak}; Jaw Value: {jaw_value}; Normalized jaw value: {normalized_jaw_value}; Previous jaw: {self.previous_jaw_value}" )
        self.led_eye_light.value = normalized_jaw_value 
        AudioUtils.bar_graph(audio_data, peak, start_time)
        self.previous_jaw_value = jaw_value   
    
           
    def stream_with_callback(self, device_index=None, duration=10):
        """
        Stream audio using callback method (non-blocking).
        More efficient for continuous processing.
        """
        CHUNK = 1024
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 44100
        
        # Storage for audio data
        audio_buffer = []
        
        def audio_callback(in_data, frame_count, time_info, status):
            """This function is called for each audio chunk."""
            if status:
                print(f"Status: {status}")
            
            # Convert to numpy array
            audio_data = np.frombuffer(in_data, dtype=np.int16)
            
            # Process audio (example: calculate RMS)
            rms = np.sqrt(np.mean(audio_data**2)) / 32768
            
            # Store data
            audio_buffer.append(audio_data)
            
            # Visual feedback
            bar_length = int(rms * 30)
            bar = '█' * bar_length
            print(f"RMS: {bar:<30} {rms:.3f}", end='\r')
            
            return (in_data, pyaudio.paContinue)
        
        p = pyaudio.PyAudio()
        
        # Open stream with callback
        stream = p.open(format=FORMAT,
                        channels=CHANNELS,
                        rate=RATE,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=CHUNK,
                        stream_callback=audio_callback)
        
        print(f"Streaming with callback for {duration} seconds...")
        stream.start_stream()
        
        # Keep stream active
        time.sleep(duration)
        
        stream.stop_stream()
        stream.close()
        p.terminate()
        
        print(f"\nCaptured {len(audio_buffer)} chunks")
        return np.concatenate(audio_buffer)

if __name__ == "__main__":
    import sys

    audio_file = "/home/aaron/Music/evil-laugh.wav"  # Replace with your audio file path
    # good ones: like-this-one.wav, beetel-exorcist.wav, blah.wav, evil-laugh.wav, krusty-laugh.wav, were-waiting.wav
    # waiting.wav not good
    c = AudioConverter(audio_file)
    # c.process_audio()
    #c.list_audio_devices()
    # 1 not working
    device_index = 1
    c.stream_with_realtime_processing(input_device_index = 1, output_device_index=2, duration=3)
    
    #converter.test_led()
    # arg = sys.argv[1] if len(sys.argv) > 1 else None
    # audio_file = arg if arg else "example.wav"  # Default audio file
    # converter = AudioConverter(audio_file)
    # converter.process_audio()
    # audio_file = arg if arg else "example.wav"  # Default audio file
    # converter = AudioConverter(audio_file)