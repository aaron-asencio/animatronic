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
from config_store import load_profile, PROFILE_FILE
import logging
import sys

class AudioPlayer:
    def __init__(self, silence_floor=None, open_ratio=None, close_ratio=None,
                 ema_alpha=None, close_hold_frames=None):
        """Initialise the audio player, jaw motor, and eye LED.

        The jaw is a binary (on/off) DC motor driven from the audio envelope.
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

        All five tuning parameters default to the persisted File profile from
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
        logging.basicConfig(filename='app.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.led_eye_light = LED(EYE_LIGHT_PIN)
        self.jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)

        # Audio parameters
        self.CHUNK = 1024       # Frames per buffer
        self.FORMAT = pa.paInt16
        self.CHANNELS = 1       # Mono
        self.RATE = 48000       # Sample rate (Hz)

        # Adaptive jaw tuning for the binary DC motor. Load the persisted File
        # profile and apply any explicit constructor overrides.
        profile = load_profile(PROFILE_FILE)
        self.silence_floor = silence_floor if silence_floor is not None else profile["silence_floor"]
        self.open_ratio = open_ratio if open_ratio is not None else profile["open_ratio"]
        self.close_ratio = close_ratio if close_ratio is not None else profile["close_ratio"]
        self.ema_alpha = ema_alpha if ema_alpha is not None else profile["ema_alpha"]
        self.close_hold_frames = close_hold_frames if close_hold_frames is not None else profile["close_hold_frames"]
        self.jaw_open = False          # current jaw state
        self._below_count = 0          # consecutive frames meeting close cond
        self._avg = 0.0                # running-average RMS envelope

    def talk(self, audio_data, start_time):
        data = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
        # RMS (energy) of this window.
        if data.size:
            level = float(np.sqrt(np.mean(data * data)))
        else:
            level = 0.0

        # Update the running-average envelope only when there is real signal,
        # so silent gaps don't drag the average toward zero (which would make
        # the next quiet sound look like a big relative swell).
        if level >= self.silence_floor:
            if self._avg <= 0.0:
                self._avg = level          # seed on first real signal
            else:
                self._avg += self.ema_alpha * (level - self._avg)

        open_thresh = self.open_ratio * self._avg
        close_thresh = self.close_ratio * self._avg

        if not self.jaw_open:
            # Open on a swell above the adaptive threshold, but never on quiet
            # below the absolute floor.
            if level >= self.silence_floor and level >= open_thresh:
                self.jaw_open = True
                self._below_count = 0
        else:
            # Close on a dip below the adaptive threshold or below the floor,
            # after close_hold_frames consecutive qualifying frames.
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

        state = 'OPEN' if self.jaw_open else 'closed'
        print(f"RMS: {level:.0f}; avg: {self._avg:.0f}; Jaw: {state}; below: {self._below_count}")
            

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
                        frames_per_buffer=1024,
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
    