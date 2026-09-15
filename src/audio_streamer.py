import pyaudio
import wave
import numpy as np
import time
from gpiozero import PWMLED
from gpiozero import LED
from gpiozero import DigitalOutputDevice
from datetime import datetime
from utils.audio_utils import AudioUtils
from constants import EYE_LIGHT_PIN, MOUTH_MOTOR_PIN
from config_store import load_profile, PROFILE_MIC
from collections import deque
import sys

class AudioStreamer:
    def __init__(self, silence_floor=None, open_ratio=None, close_ratio=None,
                 ema_alpha=None, close_hold_frames=None):
        """Initialise the mic streamer and jaw motor.

        The jaw is a binary (on/off) DC motor driven from the mic envelope.
        Rather than fixed thresholds, it adapts to a running average of recent
        loudness so it articulates on the syllable-level swells and dips of
        continuous speech (which rarely falls to true silence):

          - A per-window RMS ``level`` is measured, and an exponential moving
            average ``self._avg`` tracks recent levels.
          - While closed, the jaw OPENS when level rises above
            ``open_ratio * avg`` (a swell) and is also above ``silence_floor``.
          - While open, the jaw CLOSES once level falls below
            ``close_ratio * avg`` (a dip) OR below ``silence_floor``, held for
            ``close_hold_frames`` consecutive frames.
          - ``silence_floor`` is an absolute RMS gate: below it the jaw is
            always closed and the average is not pulled down by it, so genuine
            pauses close the mouth and quiet noise never opens it.

        All five tuning parameters default to the persisted Mic profile from
        the shared config store; any argument passed explicitly (non-None)
        overrides the corresponding stored value.

        Args:
            silence_floor:     Absolute RMS below which the jaw is always closed.
            open_ratio:        Open when level > open_ratio * running average.
            close_ratio:       Close when level < close_ratio * running average
                               (close_ratio should be < open_ratio for
                               hysteresis).
            ema_alpha:         Smoothing factor (0-1) for the running-average
                               envelope; higher adapts faster.
            close_hold_frames: Consecutive frames satisfying the close condition
                               before the jaw actually closes (debounce).
        """
        self.jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)
        self.led_eye_light = LED(EYE_LIGHT_PIN)
        self.stream_timeout_seconds = 30

        # Audio parameters
        self.CHUNK = 1024
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = 48000

        # Adaptive jaw tuning for the binary DC motor (mic passthrough). Load
        # the persisted Mic profile and apply any explicit constructor overrides.
        profile = load_profile(PROFILE_MIC)
        self.silence_floor = silence_floor if silence_floor is not None else profile["silence_floor"]
        self.open_ratio = open_ratio if open_ratio is not None else profile["open_ratio"]
        self.close_ratio = close_ratio if close_ratio is not None else profile["close_ratio"]
        self.ema_alpha = ema_alpha if ema_alpha is not None else profile["ema_alpha"]
        self.close_hold_frames = close_hold_frames if close_hold_frames is not None else profile["close_hold_frames"]
        self.jaw_open = False
        self._below_count = 0
        self._avg = 0.0

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
        data = np.asarray(audio_data, dtype=np.float32)
        if data.size:
            level = float(np.sqrt(np.mean(data * data)))
        else:
            level = 0.0

        if level >= self.silence_floor:
            if self._avg <= 0.0:
                self._avg = level
            else:
                self._avg += self.ema_alpha * (level - self._avg)

        open_thresh = self.open_ratio * self._avg
        close_thresh = self.close_ratio * self._avg

        if not self.jaw_open:
            if level >= self.silence_floor and level >= open_thresh:
                self.jaw_open = True
                self._below_count = 0
        else:
            if level < self.silence_floor or level < close_thresh:
                self._below_count += 1
                if self._below_count >= self.close_hold_frames:
                    self.jaw_open = False
                    self._below_count = 0
            else:
                self._below_count = 0

        if self.jaw_open:
            self.jaw_motor.on()
            self.led_eye_light.on()
        else:
            self.jaw_motor.off()
            self.led_eye_light.off()

        AudioUtils.bar_graph(audio_data, level, start_time)
        state = 'OPEN' if self.jaw_open else 'closed'
        print(f"RMS: {level:.0f}; avg: {self._avg:.0f}; Jaw: {state}; below: {self._below_count}")
        

    
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