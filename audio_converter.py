import pyaudio
import wave
import numpy as np
import time

# Path to your audio file
AUDIO_FILE = "your_audio_file.wav"

# Open the audio file
wf = wave.open(AUDIO_FILE, 'rb')

# Initialize PyAudio
p = pyaudio.PyAudio()

# Open stream based on the wave file's properties
stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True)  # Set to True to play audio through speakers

CHUNK = 1024

print(f"Processing audio file: {AUDIO_FILE}")
print(f"Sample rate: {wf.getframerate()} Hz")
print(f"Channels: {wf.getnchannels()}")
print(f"Duration: {wf.getnframes() / wf.getframerate():.2f} seconds")
print("\nPress Ctrl+C to stop\n")

try:
    # Read data in chunks
    data = wf.readframes(CHUNK)
    
    while data:
        # Convert bytes to numpy array for analysis
        audio_data = np.frombuffer(data, dtype=np.int16)
        
        # Calculate amplitude (volume level)
        amplitude = np.abs(audio_data).mean()
        
        # Map amplitude to motor/light range (0-255 for PWM)
        motor_value = int(min(amplitude / 50, 255))
        
        # Display the value
        print(f"Amplitude: {amplitude:6.1f} | Motor Value: {motor_value:3d} | {'█' * (motor_value // 10)}")
        
        # Send to motor controller here
        # Example: arduino.write(motor_value)
        
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
