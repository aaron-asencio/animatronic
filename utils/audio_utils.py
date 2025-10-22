import pyaudio
import numpy as np
import time

class AudioUtils:
    
    def __init__(self, name):
        self.name = name
        
    @staticmethod
    def list_audio_devices():
        p = pyaudio.PyAudio()
       
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            print(f"{i}: {info['name']}")
            print(f"   Inputs: {info['maxInputChannels']}, Outputs: {info['maxOutputChannels']}")

    @staticmethod
    def list_audio_input_devices():
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
    
    @staticmethod
    def close_streams(input_stream, output_stream, p):
        input_stream.stop_stream()
        input_stream.close()
        output_stream.stop_stream()
        output_stream.close()
        p.terminate()
        print("\nStreaming stopped.")
    
    @staticmethod
    def bar_graph(audio_data, peak, start_time):
 
        rms = np.sqrt(np.mean(audio_data**2))
        # Normalize to 0-1 range
        rms_normalized = rms / 32768
        peak_normalized = peak / 32768
        
        # Visual amplitude bar
        bar_length = int(rms_normalized * 50)
        bar = '█' * bar_length + '░' * (50 - bar_length)
        
        elapsed = time.time() - start_time
        print(f"[{elapsed:5.1f}s] {bar} RMS: {rms_normalized:.3f} Peak: {peak_normalized:.3f}", end='\r')
