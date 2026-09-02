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
from config_store import load_profile, PROFILE_MIC

class AudioStreamer:
    def __init__(self, sensitivity=None, noise_floor=None, drop_threshold=None):
        """Initialise the mic streamer and jaw motor.

        Jaw-tuning parameters default to the persisted Mic_Profile loaded from
        the Config_Store. Any explicit argument, when given, overrides the
        corresponding value from the profile.

        Args:
            sensitivity:     Optional override for the peak amplitude divisor.
                             Lower = more sensitive; voice audio typically peaks
                             1000–8000. When None, the Mic_Profile value is used.
            noise_floor:     Optional override for the absolute peak value below
                             which the jaw stays closed. Eliminates jitter from
                             mic background noise. When None, the Mic_Profile
                             value is used.
            drop_threshold:  Optional override for the ratio below which a
                             falling jaw value snaps closed. When None, the
                             Mic_Profile value is used.
        """
        self.jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)
        self.stream_timeout_seconds = 30

        # Audio parameters
        self.CHUNK = 1024
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = 48000

        # Jaw tuning — defaults come from the persisted Mic_Profile; explicit
        # constructor args override. Adjustable at runtime via /config.
        profile = load_profile(PROFILE_MIC)
        self.sensitivity = sensitivity if sensitivity is not None else profile["sensitivity"]
        self.noise_floor = noise_floor if noise_floor is not None else profile["noise_floor"]
        self.drop_threshold = drop_threshold if drop_threshold is not None else profile["drop_threshold"]
        print(f"AudioStreamer jaw tuning (Mic_Profile): sensitivity={self.sensitivity}, "
              f"noise_floor={self.noise_floor}, drop_threshold={self.drop_threshold}")
        self.previous_jaw_value = None

        self.echo_buffer = deque([np.zeros(self.CHUNK, dtype=np.int16)] * 3, maxlen=3)
        self.reverb_buffer = np.zeros(8820, dtype=np.float32)
        self.reverb_pos = 0
        self.stream = None
        self.p = pyaudio.PyAudio()

    # def test_led(self):
        
    #     print("Testing LED light...")
    #     for i in range(5):
    #         self.led_eye_light.value = 0.05
    #         time.sleep(0.2)
    #         self.led_eye_light.value = 0.0
    #         time.sleep(0.2)
    #         # self.led_eye_light.on()
    #         # time.sleep(0.2)
    #         # self.led_eye_light.off()
    #         #time.sleep(0.2)
    #     print("LED test complete.")
    
   

    def talk(self, audio_data, start_time):
        peak = np.max(np.abs(audio_data))

        # Noise floor gate: close jaw and bail if this is just background noise.
        if peak < self.noise_floor:
            self.jaw_motor.value = 0.0
            self.previous_jaw_value = 0.0
            print(f"Peak: {peak:.0f}; GATED (below noise floor {self.noise_floor})")
            return

        # Scale peak to 0–100 using tunable sensitivity divisor.
        jaw_value = float(min(peak / self.sensitivity * 100, 100))

        # Drop-threshold: sharp drop (ratio < threshold) → close jaw.
        # Gradual drop (ratio >= threshold) → hold open (natural speech decay).
        if self.previous_jaw_value is not None and self.previous_jaw_value > 0 and jaw_value < self.previous_jaw_value:
            ratio = jaw_value / self.previous_jaw_value
            if ratio < self.drop_threshold:
                jaw_value = 0.0

        motor_value = jaw_value / 100.0
        print(f"Peak: {peak:.0f}; Jaw: {jaw_value:.1f}%; Motor: {motor_value:.2f}; Prev: {self.previous_jaw_value}")
        self.jaw_motor.value = motor_value
        AudioUtils.bar_graph(audio_data, peak, start_time)
        self.previous_jaw_value = jaw_value
        

    
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
              
    def stream_mic(self, input_device_index=1,output_device_index=2):
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
            self.talk(audio, start_time =  datetime.now().timestamp())
            
            scary_audio = self.ultimate_scary_effect(audio)
            return (scary_audio.tobytes(), pyaudio.paContinue)
            
        # Open stream with callback
        self.stream = self.p.open(format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.RATE,
                        input=True,
                        output=True,
                        input_device_index=input_device_index,
                        output_device_index=output_device_index,
                        frames_per_buffer=self.CHUNK,
                        stream_callback=audio_callback)
              
        self.stream.start_stream()
           # Keep stream active
      
        print(f"\nCaptured {len(audio_buffer)} chunks")
        return self.stream
  
    def handler(self, duration_seconds=5):
        stream_ref =self.stream_mic(input_device_index = 1, output_device_index=2)
        time.sleep(duration_seconds)
        stream_ref.stop_stream()
        stream_ref.close()
        self.p.terminate()
        
    def start(self):
        if self.stream is not None and self.stream.is_active():
            print("Stream already running. Stopping existing stream...")
            self.stop()
        self.stream_mic(input_device_index = 1, output_device_index=2)

        
    def stop(self):
        if self.stream is not None and self.stream.is_active():
            print("Stopping stream...")
            self.stream.stop_stream()
            self.stream.close()
            self.p.terminate()
        else:
            print("No active stream to stop.")    
            
if __name__ == "__main__":
    print("Calling AudioStreamer.")   
    c = AudioStreamer()
    # #c.handler(5)
    c.start()
    time.sleep(3)
    c.stop()
    #c.stream_mic(input_device_index = 1, output_device_index=2,)
    # try:
    #     while True:
    #         time.sleep(0.1)
    # except KeyboardInterrupt:
    #     print("Stopping stream...")
    #     c.stream.stop_stream()
    #     c.stream.close()
    #     c.p.terminate()
    #     sys.exit()