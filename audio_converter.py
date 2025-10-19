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
    converter = AudioConverter(audio_file)
    converter.process_audio()
    #converter.test_led()
    # arg = sys.argv[1] if len(sys.argv) > 1 else None
    # audio_file = arg if arg else "example.wav"  # Default audio file
    # converter = AudioConverter(audio_file)
    # converter.process_audio()
    # audio_file = arg if arg else "example.wav"  # Default audio file
    # converter = AudioConverter(audio_file)