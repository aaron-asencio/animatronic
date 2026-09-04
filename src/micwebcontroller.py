from flask import Flask, jsonify, request
import pyaudio
import threading
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
from config_store import ConfigStore, ALLOWED_PROFILES, PROFILE_MIC

app = Flask(__name__)

# Audio parameters
CHUNK = 1024  # Frames per buffer
FORMAT = pyaudio.paInt16  # 16-bit audio
CHANNELS = 1  # Mono
# Global state
audio_state = {
    'stream': None,
    'audio': None,
    'is_streaming': False,
    'thread': None,
    'echo_buffer': deque([np.zeros(CHUNK, dtype=np.int16)] * 3, maxlen=3)
}

# Audio configuration
stream_timeout_seconds = 30

RATE = 48000  # Sample rate (Hz)

input_device_index = 1   # capture device index
output_device_index = 2  # playback device index

jaw_motor = None
led_eye_light = None

# Adaptive jaw envelope state (mic passthrough).
jaw_open = False
below_count = 0
avg_level = 0.0

# Jaw tuning — two independent profiles (File_Profile and Mic_Profile) loaded
# from the shared Config_Store and adjustable at runtime via POST /config.
# This process drives the live mic, so talk() reads the mic profile live from
# jaw_profiles so runtime /config updates take effect immediately.
# silence_floor:     absolute RMS below which the jaw is always closed.
# open_ratio:        open when level > open_ratio * running average.
# close_ratio:       close when level < close_ratio * running average
#                    (must be < open_ratio for hysteresis).
# ema_alpha:         smoothing factor (0, 1] for the running-average envelope.
# close_hold_frames: consecutive close-condition frames before closing (debounce).
config_store = ConfigStore()
jaw_profiles = config_store.load_profiles()   # {"file": {...}, "mic": {...}}

# ── Voice effects ────────────────────────────────────────────────────────────
# Each effect can be toggled on/off and has an intensity/amount parameter.
# Adjust live via POST /effects, or pick a named style via POST /effects
# with {"style": "demon"} which loads a preset into effects_config.
#
#   pitch      — resample to shift pitch. amount < 1.0 = deeper, > 1.0 = higher.
#   distortion — hard-clip / drive for a gritty, possessed edge. amount 0.0–1.0.
#   echo       — repeats from the echo buffer. amount = decay 0.0–1.0.
#   reverb     — short feedback-delay wash for an otherworldly space. 0.0–1.0.
#   tremolo    — amplitude wobble (pulsing). amount = depth 0.0–1.0.
#   bitcrush   — reduce bit depth for a broken/robotic texture. amount 0.0–1.0.
#   ring_mod   — ring modulation (metallic/robot). amount = wet mix 0.0–1.0.
effects_config = {
    'pitch':      {'enabled': True,  'amount': 0.85},
    'distortion': {'enabled': False, 'amount': 0.40},
    'echo':       {'enabled': True,  'amount': 0.50},
    'reverb':     {'enabled': False, 'amount': 0.30},
    'tremolo':    {'enabled': False, 'amount': 0.50},
    'bitcrush':   {'enabled': False, 'amount': 0.40},
    'ring_mod':   {'enabled': False, 'amount': 0.50},
}

# Named style presets. Selecting a style overwrites effects_config wholesale.
STYLE_PRESETS = {
    'natural': {
        'pitch':      {'enabled': False, 'amount': 1.00},
        'distortion': {'enabled': False, 'amount': 0.00},
        'echo':       {'enabled': False, 'amount': 0.00},
        'reverb':     {'enabled': False, 'amount': 0.00},
        'tremolo':    {'enabled': False, 'amount': 0.00},
        'bitcrush':   {'enabled': False, 'amount': 0.00},
        'ring_mod':   {'enabled': False, 'amount': 0.00},
    },
    'demon': {
        'pitch':      {'enabled': True,  'amount': 0.70},
        'distortion': {'enabled': True,  'amount': 0.55},
        'echo':       {'enabled': True,  'amount': 0.35},
        'reverb':     {'enabled': True,  'amount': 0.25},
        'tremolo':    {'enabled': False, 'amount': 0.00},
        'bitcrush':   {'enabled': False, 'amount': 0.00},
        'ring_mod':   {'enabled': False, 'amount': 0.00},
    },
    'ghost': {
        'pitch':      {'enabled': True,  'amount': 0.92},
        'distortion': {'enabled': False, 'amount': 0.00},
        'echo':       {'enabled': True,  'amount': 0.65},
        'reverb':     {'enabled': True,  'amount': 0.55},
        'tremolo':    {'enabled': True,  'amount': 0.35},
        'bitcrush':   {'enabled': False, 'amount': 0.00},
        'ring_mod':   {'enabled': False, 'amount': 0.00},
    },
    'robot': {
        'pitch':      {'enabled': False, 'amount': 1.00},
        'distortion': {'enabled': True,  'amount': 0.30},
        'echo':       {'enabled': False, 'amount': 0.00},
        'reverb':     {'enabled': False, 'amount': 0.00},
        'tremolo':    {'enabled': False, 'amount': 0.00},
        'bitcrush':   {'enabled': True,  'amount': 0.50},
        'ring_mod':   {'enabled': True,  'amount': 0.70},
    },
    'chipmunk': {
        'pitch':      {'enabled': True,  'amount': 1.45},
        'distortion': {'enabled': False, 'amount': 0.00},
        'echo':       {'enabled': False, 'amount': 0.00},
        'reverb':     {'enabled': False, 'amount': 0.00},
        'tremolo':    {'enabled': False, 'amount': 0.00},
        'bitcrush':   {'enabled': False, 'amount': 0.00},
        'ring_mod':   {'enabled': False, 'amount': 0.00},
    },
    'possessed': {
        'pitch':      {'enabled': True,  'amount': 0.60},
        'distortion': {'enabled': True,  'amount': 0.75},
        'echo':       {'enabled': True,  'amount': 0.70},
        'reverb':     {'enabled': True,  'amount': 0.50},
        'tremolo':    {'enabled': True,  'amount': 0.45},
        'bitcrush':   {'enabled': False, 'amount': 0.00},
        'ring_mod':   {'enabled': False, 'amount': 0.00},
    },
}

# Persistent state for time-based effects (tremolo phase, ring-mod phase).
effect_state = {
    'tremolo_phase': 0.0,
    'ring_phase': 0.0,
}

def stream_mic():
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
        talk(audio, start_time =  datetime.now().timestamp())
        
        processed = apply_effects(audio)
        return (processed.tobytes(), pyaudio.paContinue)
        
    # Open stream with callback
    stream = audio_state['audio'].open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    output=True,
                    input_device_index=input_device_index,
                    output_device_index=output_device_index,
                    frames_per_buffer=CHUNK,
                    stream_callback=audio_callback)
            
    stream.start_stream()
        # Keep stream active
    
    print(f"\nCaptured {len(audio_buffer)} chunks")
    return stream


def talk(audio_data, start_time):
    global jaw_motor, led_eye_light, jaw_open, below_count, avg_level
    if jaw_motor is None:
        jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)
    if led_eye_light is None:
        led_eye_light = LED(EYE_LIGHT_PIN)

    # Read the live mic profile so runtime POST /config updates take effect.
    mic = jaw_profiles[PROFILE_MIC]
    silence_floor = mic["silence_floor"]
    open_ratio = mic["open_ratio"]
    close_ratio = mic["close_ratio"]
    ema_alpha = mic["ema_alpha"]
    close_hold_frames = mic["close_hold_frames"]

    data = np.asarray(audio_data, dtype=np.float32)
    if data.size:
        level = float(np.sqrt(np.mean(data * data)))
    else:
        level = 0.0

    if level >= silence_floor:
        if avg_level <= 0.0:
            avg_level = level
        else:
            avg_level += ema_alpha * (level - avg_level)

    open_thresh = open_ratio * avg_level
    close_thresh = close_ratio * avg_level

    if not jaw_open:
        if level >= silence_floor and level >= open_thresh:
            jaw_open = True
            below_count = 0
    else:
        if level < silence_floor or level < close_thresh:
            below_count += 1
            if below_count >= close_hold_frames:
                jaw_open = False
                below_count = 0
        else:
            below_count = 0

    if jaw_open:
        jaw_motor.on()
        led_eye_light.on()
    else:
        jaw_motor.off()
        led_eye_light.off()

    state = 'OPEN' if jaw_open else 'closed'
    print(f"RMS: {level:.0f}; avg: {avg_level:.0f}; Jaw: {state}; below: {below_count}")
        
# ── Individual effect functions ──────────────────────────────────────────────
# Each takes a float32 array (int16 range, ±32768) and returns float32.

def _fx_pitch(audio, amount, n_out):
    """Resample to shift pitch. amount < 1 = deeper, > 1 = higher.

    n_out is the number of samples to return (keeps chunk size stable).
    """
    if amount <= 0:
        return audio
    n_src = int(len(audio) / amount)
    n_src = max(1, n_src)
    indices = np.linspace(0, len(audio) - 1, n_src)
    shifted = np.interp(indices, np.arange(len(audio)), audio)
    if len(shifted) < n_out:
        shifted = np.pad(shifted, (0, n_out - len(shifted)), 'edge')
    return shifted[:n_out]


def _fx_distortion(audio, amount):
    """Hard-clip drive. Higher amount = more aggressive clipping."""
    # Map amount 0–1 to a clip threshold: high amount = low threshold = more grit.
    threshold = 1.0 - (amount * 0.85)  # 1.0 (clean) down to 0.15 (harsh)
    norm = audio / 32768.0
    driven = np.clip(norm, -threshold, threshold) / threshold
    return driven * 32768.0


def _fx_echo(audio, amount):
    """Layer decayed copies from the echo buffer."""
    output = audio.copy()
    for i, old in enumerate(audio_state['echo_buffer']):
        decay = amount ** (i + 1)
        output += old.astype(np.float32) * decay
    audio_state['echo_buffer'].append(audio.astype(np.int16))
    return output


def _fx_reverb(audio, amount):
    """Cheap feedback-delay reverb using the persistent reverb buffer."""
    buf = audio_state.setdefault('reverb_buffer', np.zeros(9600, dtype=np.float32))
    pos = audio_state.get('reverb_pos', 0)
    out = np.empty_like(audio)
    blen = len(buf)
    fb = 0.35 + amount * 0.4          # feedback amount
    wet = amount * 0.6               # wet mix
    for i in range(len(audio)):
        delayed = buf[pos]
        val = audio[i] + delayed * wet
        buf[pos] = audio[i] + delayed * fb
        out[i] = val
        pos = (pos + 1) % blen
    audio_state['reverb_pos'] = pos
    return out


def _fx_tremolo(audio, amount):
    """Amplitude wobble — a pulsing, unsettling modulation."""
    rate_hz = 6.0
    depth = amount
    phase = effect_state['tremolo_phase']
    t = np.arange(len(audio))
    lfo = 1.0 - depth * 0.5 * (1.0 + np.sin(2 * np.pi * rate_hz * t / RATE + phase))
    effect_state['tremolo_phase'] = (phase + 2 * np.pi * rate_hz * len(audio) / RATE) % (2 * np.pi)
    return audio * lfo


def _fx_bitcrush(audio, amount):
    """Reduce effective bit depth for a broken/lo-fi texture."""
    # amount 0–1 → bits 16 down to 3
    bits = int(round(16 - amount * 13))
    bits = max(2, min(16, bits))
    step = 2 ** (16 - bits)
    return np.round(audio / step) * step


def _fx_ring_mod(audio, amount):
    """Ring modulation — multiply by a carrier sine for a metallic/robot tone."""
    carrier_hz = 120.0
    phase = effect_state['ring_phase']
    t = np.arange(len(audio))
    carrier = np.sin(2 * np.pi * carrier_hz * t / RATE + phase)
    effect_state['ring_phase'] = (phase + 2 * np.pi * carrier_hz * len(audio) / RATE) % (2 * np.pi)
    wet = audio * carrier
    return audio * (1.0 - amount) + wet * amount


def apply_effects(audio_data):
    """Apply the enabled effects in order, driven by effects_config.

    Signal chain: pitch → ring_mod → bitcrush → distortion → tremolo → echo → reverb.
    Returns int16 with clipping protection.
    """
    n_out = len(audio_data)
    audio = audio_data.astype(np.float32)

    cfg = effects_config
    if cfg['pitch']['enabled']:
        audio = _fx_pitch(audio, cfg['pitch']['amount'], n_out)
    if cfg['ring_mod']['enabled']:
        audio = _fx_ring_mod(audio, cfg['ring_mod']['amount'])
    if cfg['bitcrush']['enabled']:
        audio = _fx_bitcrush(audio, cfg['bitcrush']['amount'])
    if cfg['distortion']['enabled']:
        audio = _fx_distortion(audio, cfg['distortion']['amount'])
    if cfg['tremolo']['enabled']:
        audio = _fx_tremolo(audio, cfg['tremolo']['amount'])
    if cfg['echo']['enabled']:
        audio = _fx_echo(audio, cfg['echo']['amount'])
    if cfg['reverb']['enabled']:
        audio = _fx_reverb(audio, cfg['reverb']['amount'])

    # Prevent wraparound distortion when effects sum above int16 range.
    audio = np.clip(audio, -32768, 32767)
    return audio.astype(np.int16)

#@app.route('/start', methods=['POST'])
def start_streaming():
    """Start streaming microphone to speaker"""
    if audio_state['is_streaming']:
        return jsonify({'status': 'error', 'message': 'Already streaming'}), 400
    
    try:
        # Initialize PyAudio
        audio_state['audio'] = pyaudio.PyAudio()
        audio_state['is_streaming'] = True
        
        # Start streaming thread
        audio_state['thread'] = threading.Thread(target=stream_mic)
        audio_state['thread'].start()
        
        return jsonify({'status': 'success', 'message': 'Streaming started (mic to speaker)'})
    
    except Exception as e:
        audio_state['is_streaming'] = False
        return jsonify({'status': 'error', 'message': str(e)}), 500

#@app.route('/stop', methods=['POST'])
def stop_streaming():
    """Stop streaming microphone to speaker"""
    if not audio_state['is_streaming']:
        return jsonify({'status': 'error', 'message': 'Not streaming'}), 400
    
    try:
        # Stop streaming
        audio_state['is_streaming'] = False
        
        # Wait for thread to finish
        if audio_state['thread']:
            audio_state['thread'].join()
        
        # Terminate PyAudio
        if audio_state['audio']:
            audio_state['audio'].terminate()
        
        audio_state['stream'] = None
        
        return jsonify({
            'status': 'success', 
            'message': 'Streaming stopped'
        })
    
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/config', methods=['POST'])
def set_config():
    """Update jaw tuning for one profile at runtime and persist both.

    Accepts a JSON body with a "profile" selector plus any combination of the
    five adaptive jaw-tuning fields. The selector is checked against an explicit
    allowlist, and every supplied field is validated, before any profile is
    mutated. On any invalid input the handler returns early, so the stored
    profiles (in memory and on disk) are left unchanged.

    Args:
        (request body) profile:           Profile to update, one of
                                          ALLOWED_PROFILES ("file" or "mic").
        (request body) silence_floor:     Optional absolute RMS gate; must be >= 0.
        (request body) open_ratio:        Optional open multiplier; must be > 0.
        (request body) close_ratio:       Optional close multiplier; must be > 0
                                          and less than the effective open_ratio.
        (request body) ema_alpha:         Optional smoothing factor; must be in (0, 1].
        (request body) close_hold_frames: Optional debounce frame count; int >= 1.

    Example:
        curl -X POST http://localhost:5000/config \\
             -H 'Content-Type: application/json' \\
             -d '{"profile": "mic", "silence_floor": 700, "open_ratio": 1.2}'
    """
    global jaw_profiles
    data = request.json or {}

    # Allowlist dispatch — never index jaw_profiles with unvalidated input.
    profile_name = data.get('profile')
    if profile_name not in ALLOWED_PROFILES:
        return jsonify({'status': 'error',
                        'message': f'profile must be one of {list(ALLOWED_PROFILES)}'}), 400

    # Validate every supplied field before mutating anything.
    updates = {}
    if 'silence_floor' in data:
        value = float(data['silence_floor'])
        if value < 0:
            return jsonify({'status': 'error', 'message': 'silence_floor must be >= 0'}), 400
        updates['silence_floor'] = value

    if 'open_ratio' in data:
        value = float(data['open_ratio'])
        if value <= 0:
            return jsonify({'status': 'error', 'message': 'open_ratio must be > 0'}), 400
        updates['open_ratio'] = value

    if 'close_ratio' in data:
        value = float(data['close_ratio'])
        if value <= 0:
            return jsonify({'status': 'error', 'message': 'close_ratio must be > 0'}), 400
        updates['close_ratio'] = value

    if 'ema_alpha' in data:
        value = float(data['ema_alpha'])
        if not (0.0 < value <= 1.0):
            return jsonify({'status': 'error', 'message': 'ema_alpha must be in (0, 1]'}), 400
        updates['ema_alpha'] = value

    if 'close_hold_frames' in data:
        value = int(float(data['close_hold_frames']))
        if value < 1:
            return jsonify({'status': 'error', 'message': 'close_hold_frames must be >= 1'}), 400
        updates['close_hold_frames'] = value

    # Cross-field hysteresis check: the effective close_ratio (updated value if
    # present, else the current stored value) must stay below the effective
    # open_ratio. Enforced across partial updates so a single-field change can't
    # invert the relationship. Checked before any mutation.
    current = jaw_profiles[profile_name]
    effective_open = updates.get('open_ratio', current['open_ratio'])
    effective_close = updates.get('close_ratio', current['close_ratio'])
    if effective_close >= effective_open:
        return jsonify({'status': 'error',
                        'message': 'close_ratio must be less than open_ratio'}), 400

    # Validation fully passed before any mutation → invalid requests leave the
    # stored profiles unchanged (the persisted file is only touched on success).
    jaw_profiles = config_store.update_profile(profile_name, updates)
    print(f"Jaw profile '{profile_name}' updated: {jaw_profiles[profile_name]}")
    return jsonify({'status': 'success', 'profile': profile_name,
                    'updated': updates, 'profiles': jaw_profiles})


@app.route('/effects', methods=['POST'])
def set_effects():
    """Update voice effects at runtime.

    Three ways to use it:

    1. Load a named style preset:
        {"style": "demon"}
       Valid styles: natural, demon, ghost, robot, chipmunk, possessed

    2. Toggle a single effect on/off:
        {"effect": "echo", "enabled": true}

    3. Set a single effect's intensity:
        {"effect": "distortion", "amount": 0.7}

    You can combine enabled + amount in one call:
        {"effect": "reverb", "enabled": true, "amount": 0.5}

    Examples:
        curl -X POST http://localhost:5000/effects \\
             -H 'Content-Type: application/json' -d '{"style": "possessed"}'
        curl -X POST http://localhost:5000/effects \\
             -H 'Content-Type: application/json' \\
             -d '{"effect": "bitcrush", "enabled": true, "amount": 0.6}'
    """
    data = request.json or {}

    # 1. Style preset — overwrites the whole config.
    if 'style' in data:
        style = str(data['style']).lower()
        if style not in STYLE_PRESETS:
            valid = ', '.join(STYLE_PRESETS.keys())
            return jsonify({'status': 'error',
                            'message': f'Unknown style "{style}". Valid: {valid}'}), 400
        # Deep copy so later edits don't mutate the preset.
        for name, params in STYLE_PRESETS[style].items():
            effects_config[name] = dict(params)
        print(f"Voice style set to '{style}': {effects_config}")
        return jsonify({'status': 'success', 'style': style, 'effects': effects_config})

    # 2 & 3. Single-effect toggle / amount.
    if 'effect' in data:
        name = str(data['effect']).lower()
        if name not in effects_config:
            valid = ', '.join(effects_config.keys())
            return jsonify({'status': 'error',
                            'message': f'Unknown effect "{name}". Valid: {valid}'}), 400

        if 'enabled' in data:
            effects_config[name]['enabled'] = bool(data['enabled'])
        if 'amount' in data:
            amount = float(data['amount'])
            if amount < 0:
                return jsonify({'status': 'error', 'message': 'amount must be >= 0'}), 400
            effects_config[name]['amount'] = amount

        print(f"Effect '{name}' updated: {effects_config[name]}")
        return jsonify({'status': 'success', 'effect': name, 'config': effects_config[name]})

    return jsonify({'status': 'error',
                    'message': 'Provide "style", or "effect" with "enabled"/"amount"'}), 400


@app.route('/handler', methods=['POST'])
def handler():
    # get the body of the request
    data = request.json
    if data:
        action = data.get('action')
        print(f"Handler action received: {action}")
        if action == 'start':
            return start_streaming()
        elif action == 'stop':
            return stop_streaming()

    return jsonify({'status': 'error', 'message': 'Unknown action'}), 400

@app.route('/status', methods=['GET'])
def get_status():
    """Return streaming status, both jaw profiles, and voice effects config.

    Returns:
        A JSON response with the current streaming flag, both jaw-tuning
        profiles (File_Profile and Mic_Profile) under "profiles", the active
        voice effects config, and the list of available style presets.
    """
    return jsonify({
        'is_streaming': audio_state['is_streaming'],
        'profiles': jaw_profiles,   # {"file": {...}, "mic": {...}}
        'effects': effects_config,
        'styles': list(STYLE_PRESETS.keys()),
    })

if __name__ == '__main__':
    app.run(debug=True, threaded=True)