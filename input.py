import sounddevice as sd
import soundfile as sf

def record_voice(filename, duration=30, samplerate=16000):
    print("🎤 Gapiring...")
    audio = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=1, dtype='int16')
    sd.wait()
    sf.write(filename, audio, samplerate)
    print(f"✅ Saqlandi: {filename}")

record_voice("input_ana.wav")
