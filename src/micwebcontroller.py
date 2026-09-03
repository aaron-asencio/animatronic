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
previous_jaw_value = None
led_eye_light = None

# Jaw tuning — two independent profiles (File_Profile and Mic_Profile) loaded
# from the shared Config_Store and adjustable at runtime via POST /config.
# This process drives the live mic, so talk() reads the mic profile.
# sensitivity:   peak amplitude divisor; lower = more sensitive.
#                Voice peaks ~1000–8000; default 500 is a good starting point.
# noise_floor:   absolute peak value below which the jaw stays closed.
#                Eliminates jitter from mic background noise.
#                Set just above your ambient noise level (seen as ~150–400 in logs).
# drop_threshold: ratio below which a falling jaw value snaps closed.
#                Sharp drop (ratio < threshold) = silence between words → close jaw.
#                Gradual drop (ratio >= threshold) = trailing off → hold open.
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
    global previous_jaw_value, jaw_motor, led_eye_light
    if jaw_motor is None:
        from model.constants import MOUTH_MOTOR_PIN
        jaw_motor = DigitalOutputDevice(MOUTH_MOTOR_PIN)
        if led_eye_light is None:
            led_eye_light = LED(EYE_LIGHT_PIN)

    peak = np.max(np.abs(audio_data))

    # Noise floor gate: if the peak is just mic background noise, close the jaw
    # and bail out. This prevents jitter when no one is speaking.
    if peak < jaw_profiles[PROFILE_MIC]['noise_floor']:
        jaw_motor.value = 0.0
        previous_jaw_value = 0.0
        led_eye_light.off()
        print(f"Peak: {peak:.0f}; GATED (below noise floor {jaw_profiles[PROFILE_MIC]['noise_floor']})")
        return

    # Scale peak to 0–100 using tunable sensitivity divisor.
    jaw_value = float(min(peak / jaw_profiles[PROFILE_MIC]['sensitivity'] * 100, 100))

    # Drop-threshold: detect a sharp drop (silence between words) vs gradual
    # decay (natural end of a word). Close the jaw on sharp drops only.
    # ratio < drop_threshold  → sharp drop → close jaw
    # ratio >= drop_threshold → gradual drop → hold open (natural decay)
    if previous_jaw_value is not None and previous_jaw_value > 0 and jaw_value < previous_jaw_value:
        ratio = jaw_value / previous_jaw_value
        if ratio < jaw_profiles[PROFILE_MIC]['drop_threshold']:
            jaw_value = 0.0

    motor_value = jaw_value / 100.0
    print(f"Peak: {peak:.0f}; Jaw: {jaw_value:.1f}%; Motor: {motor_value:.2f}; Prev: {previous_jaw_value}")
    jaw_motor.value = motor_value
    led_eye_light.on() if jaw_value > 0 else led_eye_light.off()
    previous_jaw_value = jaw_value
        
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
    three jaw-tuning fields. The selector is checked against an explicit
    allowlist, and every supplied field is validated, before any profile is
    mutated. On any invalid input the handler returns early, so the stored
    profiles (in memory and on disk) are left unchanged.

    Args:
        (request body) profile:        Profile to update, one of ALLOWED_PROFILES
                                       ("file" or "mic").
        (request body) sensitivity:    Optional peak divisor; must be > 0.
                                       Lower values increase sensitivity.
        (request body) noise_floor:    Optional gate threshold; must be >= 0.
        (request body) drop_threshold: Optional snap-shut ratio; must be 0.0–1.0.

    Example:
        curl -X POST http://localhost:5000/config \\
             -H 'Content-Type: application/json' \\
             -d '{"profile": "mic", "sensitivity": 300, "drop_threshold": 0.15}'
    """
    data = request.json or {}

    # Allowlist dispatch — never index jaw_profiles with unvalidated input.
    profile_name = data.get('profile')
    if profile_name not in ALLOWED_PROFILES:
        return jsonify({'status': 'error',
                        'message': f'profile must be one of {list(ALLOWED_PROFILES)}'}), 400

    # Validate every supplied field before mutating anything.
    updates = {}
    if 'sensitivity' in data:
        value = float(data['sensitivity'])
        if value <= 0:
            return jsonify({'status': 'error', 'message': 'sensitivity must be > 0'}), 400
        updates['sensitivity'] = value

    if 'noise_floor' in data:
        value = float(data['noise_floor'])
        if value < 0:
            return jsonify({'status': 'error', 'message': 'noise_floor must be >= 0'}), 400
        updates['noise_floor'] = value

    if 'drop_threshold' in data:
        value = float(data['drop_threshold'])
        if not (0.0 <= value <= 1.0):
            return jsonify({'status': 'error', 'message': 'drop_threshold must be 0.0–1.0'}), 400
        updates['drop_threshold'] = value

    # Validation fully passed before any mutation → invalid requests leave the
    # stored profiles unchanged (the persisted file is only touched on success).
    global jaw_profiles
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