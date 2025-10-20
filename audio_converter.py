import pyaudio
import wave
import numpy as np
import time
from gpiozero import PWMLED
from gpiozero import LED
from gpiozero import Device

from model.constants import EYE_LIGHT_PIN, MOUTH_MOTOR_PIN

class AudioConverter:
    def __init__(self, audio_file):
        self.audio_file = audio_file
        #self.eye_light = PWMLED(EYE_LIGHT_PIN)
        self.led_eye_light = LED(MOUTH_MOTOR_PIN)
        self.jaw_motor = LED(EYE_LIGHT_PIN)
        
        # print the Device.pin_factory being used
        print(f"Eye light pin factory: {Device.pin_factory}")

        # self.mouth_motor = PWMLED(MOUTH_MOTOR_PIN)
         # Audio parameters
        self.CHUNK = 1024  # Frames per buffer
        self.FORMAT = pyaudio.paInt16  # 16-bit audio
        self.CHANNELS = 1  # Mono
        
        self.RATE = 48000  # Sample rate (Hz)

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
     
    def list_audio_devices(self):
        p = pyaudio.PyAudio()
       
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            print(f"{i}: {info['name']}")
            print(f"   Inputs: {info['maxInputChannels']}, Outputs: {info['maxOutputChannels']}") 
    
    def list_audio_input_devices(self):
        """List all available audio input devices."""
        p = pyaudio.PyAudio()
       
        print("-" * 80)
        
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            print(f"Device {i}: {info['name']}")
            
            try:
                max_inputs = int(float(info['maxInputChannels']))
            except (ValueError, TypeError):
                max_inputs = 0
            if max_inputs > 0:  # Only show input devices
                print("Available Audio Devices:")
                print(f"  Max Input Channels: {max_inputs}")
                try:
                    default_rate = int(float(info['defaultSampleRate']))
                except (ValueError, TypeError):
                    default_rate = 0
                print(f"  Default Sample Rate: {default_rate} Hz")
                print()
        
        p.terminate()    
        
    def stream_with_realtime_processing(self, device_index=None, duration=10):
        """
        Stream audio from USB mic with real-time amplitude monitoring.
        """

        
        p = pyaudio.PyAudio()
        
        stream = p.open(format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.RATE,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=self.CHUNK)
        
        print(f"Streaming audio for {duration} seconds...")
        print("Real-time amplitude monitoring:")
        print("-" * 60)
        
        start_time = time.time()
        
        try:
            while (time.time() - start_time) < duration:
                # Read audio data
                data = stream.read(self.CHUNK, exception_on_overflow=False)
                
                # Convert to numpy array
                audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32)
     
                rms = np.sqrt(np.mean(audio_data**2))
                print(f"Audio Data: {np.mean(audio_data**2)}")
                print(f"Audio Data: {np.sqrt(np.mean(audio_data**2))}")
                peak = np.max(np.abs(audio_data))
                
                # Normalize to 0-1 range
                rms_normalized = rms / 32768
                peak_normalized = peak / 32768
                
                # Visual amplitude bar
                bar_length = int(rms_normalized * 50)
                bar = '█' * bar_length + '░' * (50 - bar_length)
                
                elapsed = time.time() - start_time
                print(f"[{elapsed:5.1f}s] {bar} RMS: {rms_normalized:.3f} Peak: {peak_normalized:.3f}", end='\r')
                
        except KeyboardInterrupt:
            print("\nStopped by user")
        
        stream.stop_stream()
        stream.close()
        p.terminate()
        print("\nStreaming stopped.")

   
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
    
        
    def process_audio(self):
        # Open the audio file
        wf = wave.open(self.audio_file, 'rb')

        # Initialize PyAudio
        p = pyaudio.PyAudio()

        # Open stream based on the wave file's properties
        stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                        channels=wf.getnchannels(),
                        rate=wf.getframerate(),
                        output=True)  # Set to True to play audio through speakers

        CHUNK = 1024

        print(f"Processing audio file: {self.audio_file}")
        print(f"Sample rate: {wf.getframerate()} Hz")
        print(f"Channels: {wf.getnchannels()}")
        print(f"Duration: {wf.getnframes() / wf.getframerate():.2f} seconds")
        print("\nPress Ctrl+C to stop\n")

        try:
            # Read data in chunks
            data = wf.readframes(CHUNK)
            prev_jaw_value = 0
            jaw_value = 0
            while data:
                # Convert bytes to numpy array for analysis
                audio_data = np.frombuffer(data, dtype=np.int16)
                
                # Calculate amplitude (volume level)
                amplitude = np.abs(audio_data).mean()
                
                # Map amplitude to motor/light range (0-255 for PWM)
                motor_value = int(min(amplitude / 50, 255))
                
                # Display the value
                print(f"Amplitude: {amplitude:6.1f} | Motor Value: {motor_value:3d} | {'█' * (motor_value // 10)}")
                # self.eye_light.pulse(fade_in_time=0.1, fade_out_time=0.1, n=1, background=True)
                # Fade the eyes in and out based on amplitude
                # track the previous jaw value and if the new value is 30% less or more, turn off the light
                if 'prev_jaw_value' in locals():
                    if abs(jaw_value - prev_jaw_value) > 30:
                        self.jaw_motor.off()
                prev_jaw_value = jaw_value
                

                jaw_value = int(min(amplitude / 50, 150))
                
                jaw_value = round(jaw_value / 100)
                print(f"Jaw Value: {jaw_value}" )
                self.led_eye_light.value = jaw_value 
                #self.eye_light.on()
                # Send to motor controller here
                #self.jaw_motor.value = motor_value / 255.0
                
                # Play the audio chunk (optional - remove if you don't want playback)
                stream.write(data)
                
                # Read next chunk
                data = wf.readframes(CHUNK)

        except KeyboardInterrupt:
            print("\nStopping...")

        finally:
            # Clean up
            stream.stop_stream()
            stream.close()
            p.terminate()
            wf.close()
            print("Done!")

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
    c.stream_with_realtime_processing(device_index = 1, duration=5)
    
    #converter.test_led()
    # arg = sys.argv[1] if len(sys.argv) > 1 else None
    # audio_file = arg if arg else "example.wav"  # Default audio file
    # converter = AudioConverter(audio_file)
    # converter.process_audio()
    # audio_file = arg if arg else "example.wav"  # Default audio file
    # converter = AudioConverter(audio_file)