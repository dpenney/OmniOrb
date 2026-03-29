import pyaudio

p = pyaudio.PyAudio()
print("\n--- PyAudio Device List ---")
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    name = info.get('name')
    inputs = info.get('maxInputChannels')
    
    # Check if this name matches what we expect
    if "googlevoicehat" in name or "SoundCard" in name or "voicehat" in name:
        match = " [MATCH!]"
    else:
        match = ""
        
    print(f"Index {i}: {name} (Max Inputs: {inputs}){match}")

p.terminate()
print("\n--- End of List ---")
