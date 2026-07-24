import cv2
import mediapipe as mp
import numpy as np
import pygame
import time
import math
import os     # os mojule ka use files, folders (directories), paths aur operating system se related operations karne ke liye hota hai.
import wave   # wav file ko read aur write karne ke liye
import struct    # struct module ka use tab hota hai jab aapko binary data ko efficiently encode ya decode karna hota hai. jaise file formats, networking prtocols, ya low level programming mein.
import tkinter as tk   # tkinter python ka standard GUI module hai. iska use windows, buttons, text boxes, labels, menus, dialog boxes, etc. banave ke liye hota hai. 
from tkinter import filedialog   # eska use ptyhon mein open file , save file aur folder selection dialogs dikhane ke liye kiya jata hai. isse user graphival file browser ke through file ya folder select kar sakta hai.
import platform    # platform is a built in ptyhon module used to retrieve information about the operating system, hardwre, and python runtime environment.
import subprocess   # python ka built in module hai jo dusre progtams ya system commands ko python ke andar se run karne ke liye use hota hai. 
import threading     # threading ka use convcurrent (ek saath) tasks ko execute karne ke liye hota hai, jisse program zyada respondive aur efficient ban sakya hai.

# --- PYNPUT SETUP ---
HAS_PYNPUT = False
try:
    from pynput import keyboard
    HAS_PYNPUT = True
    print("Pynput loaded successfully.")
except ImportError:
    print("WARNING: 'pynput' not found. Install via 'pip install pynput'")

# --- 1. SETUP & GLOBALS ---
mp_drawing = mp.solutions.drawing_utils
mp_hands = mp.solutions.hands

cap = cv2.VideoCapture(0)
cap.set(3, 1280)
cap.set(4, 720)
cv2.namedWindow('Air Band', cv2.WINDOW_NORMAL)

custom_sounds = {}    
harm_sounds = {}      
drum_sounds = {}      
note_cache = {}       
menu_buttons = []
prev_hand_y = {} 

current_instrument = "Guitar" 
show_menu = False
active_string_idx = 0
current_fret = 0
last_strum_time = 0
last_played_string_idx = -1 
prev_wrist_y = 0
can_strum = True
current_pressure = 50.0
guitar_strum_state = {'prev_y': 0.5} 
active_harmonium_notes = {} 

ROW_HEIGHT = 70
MARGIN_TOP = 150

# --- 2. AUDIO ENGINE ---
class SoundEngine:
    def __init__(self):
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            pygame.mixer.set_num_channels(64) 
            self.sample_rate = 44100
            self.raw_buffers = {} 
        except Exception as e:
            print(f"Audio Error: {e}")

    def store_raw(self, key, numpy_array):
        self.raw_buffers[key] = numpy_array

    def generate_note(self, freq, duration=0.8, volume=0.7):
        n_samples = int(self.sample_rate * duration)
        t = np.linspace(0, duration, n_samples, False)
        wave_data = 0.6 * np.sin(2 * np.pi * freq * t)
        wave_data += 0.4 * (2 * (t * freq - np.floor(t * freq + 0.5)))
        decay = np.exp(-3 * t)
        wave_data = wave_data * decay * volume
        audio_int16 = (wave_data * 32767).astype(np.int16)
        stereo = np.column_stack((audio_int16, audio_int16))
        sound = pygame.sndarray.make_sound(stereo)
        self.store_raw(f"gtr_{int(freq)}", wave_data)
        return sound, f"gtr_{int(freq)}"
    
    def generate_drum(self, freq, duration=0.2, wave_type='sine'):
        if isinstance(duration, str): duration = 0.2
        n_samples = int(self.sample_rate * duration)
        t = np.linspace(0, duration, n_samples, False)
        if wave_type == 'noise':
            wave_data = np.random.uniform(-0.5, 0.5, n_samples) * np.exp(-10 * t)
        else:
            wave_data = np.sin(2 * np.pi * freq * t) * np.exp(-5 * t)
        audio_int16 = (wave_data * 32767).astype(np.int16)
        stereo = np.column_stack((audio_int16, audio_int16))
        sound = pygame.sndarray.make_sound(stereo)
        self.store_raw(f"drm_{int(freq)}", wave_data)
        return sound, f"drm_{int(freq)}"

    def generate_harmonium(self, freq, duration=1.0, volume=0.5):
        n_samples = int(self.sample_rate * duration)
        t = np.linspace(0, duration, n_samples, False)
        square = np.sign(np.sin(2 * np.pi * freq * t))
        sine = np.sin(2 * np.pi * freq * t)
        wave_data = (0.4 * square) + (0.6 * sine)
        wave_data = wave_data * volume
        audio_int16 = (wave_data * 32767).astype(np.int16)
        stereo = np.column_stack((audio_int16, audio_int16))
        sound = pygame.sndarray.make_sound(stereo)
        self.store_raw(f"harm_{int(freq)}", wave_data[:int(44100*0.5)]) 
        return sound, f"harm_{int(freq)}"

audio = SoundEngine()

# --- 3. PRESSURE SYSTEM ---
class PressureEngine:
    def __init__(self):
        self.prev_gray = None
        self.pressure = 50.0
        self.sensitivity = 70.0
        self.smoothing = 0.92

    def update(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (0, 0), fx=0.5, fy=0.5)
        if self.prev_gray is None:
            self.prev_gray = gray
            return 50.0
        flow = cv2.calcOpticalFlowFarneback(self.prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        dy = np.median(flow[..., 1])
        delta = -dy * self.sensitivity
        target = self.pressure + delta
        self.pressure = self.pressure * self.smoothing + target * (1 - self.smoothing)
        self.pressure = max(0, min(100, self.pressure))
        self.prev_gray = gray
        return int(self.pressure)

pressure_engine = PressureEngine()

# --- 4. RECORDER SYSTEM ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(BASE_DIR, "Recordings")
if not os.path.exists(RECORDINGS_DIR): os.makedirs(RECORDINGS_DIR)

def open_recordings_folder():
    path = RECORDINGS_DIR
    if platform.system() == "Windows":
        os.startfile(path)
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])

def save_mix_to_wav(events, duration, filename):
    if not events: return None
    print("--- Mixing Audio... ---")
    total_samples = int(44100 * (duration + 1.0))
    mix_buffer = np.zeros(total_samples, dtype=np.float32)
    
    count = 0
    for event in events:
        key = event['raw_key']
        if key in audio.raw_buffers:
            sample_data = audio.raw_buffers[key]
            start_idx = int(event['time'] * 44100)
            end_idx = start_idx + len(sample_data)
            if end_idx < total_samples:
                mix_buffer[start_idx:end_idx] += sample_data
            else:
                available = total_samples - start_idx
                mix_buffer[start_idx:total_samples] += sample_data[:available]
            count += 1
            
    if count == 0: return None
    max_val = np.max(np.abs(mix_buffer))
    if max_val > 1.0: mix_buffer /= max_val
    
    audio_int16 = (mix_buffer * 32767).astype(np.int16)
    filepath = os.path.join(RECORDINGS_DIR, filename)
    try:
        with wave.open(filepath, 'w') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(44100)
            wav_file.writeframes(audio_int16.tobytes())
        print(f"Saved: {filename}")
        return filepath
    except Exception as e:
        print(f"Save Failed: {e}")
        return None

class Looper:
    def __init__(self):
        self.tracks = []
        self.is_recording = False
        self.rec_start_time = 0
        self.current_events = []
        self.recording_label = ""
        self.loop_channel = pygame.mixer.Channel(5)
    
    def start_recording(self, label="Track"):
        self.is_recording = True
        self.rec_start_time = time.time()
        self.current_events = []
        self.recording_label = label
        print(f"RECORDING START: {label}")

    def log_event(self, sound_key, raw_key):
        if self.is_recording:
            timestamp = time.time() - self.rec_start_time
            self.current_events.append({'time': timestamp, 'key': sound_key, 'raw_key': raw_key})

    def stop_recording(self):
        if self.is_recording:
            self.is_recording = False
            duration = time.time() - self.rec_start_time
            print(f"RECORDING STOP. Events: {len(self.current_events)}")
            if len(self.current_events) > 0:
                filename = f"{self.recording_label}_{int(time.time())}.wav"
                filepath = save_mix_to_wav(self.current_events, duration, filename)
                if filepath:
                    try:
                        loop_sound = pygame.mixer.Sound(filepath)
                        self.tracks.append({
                            'label': f"{self.recording_label} {len(self.tracks)+1}",
                            'file': filepath,
                            'sound_obj': loop_sound,
                            'duration': duration,
                            'is_playing': False,
                            'channel': None 
                        })
                    except Exception as e: print(f"Load Error: {e}")

    def toggle_loop(self, track_idx):
        if track_idx == -1 and self.tracks:
            target_track = self.tracks[-1]
        elif 0 <= track_idx < len(self.tracks):
            target_track = self.tracks[track_idx]
        else:
            return

        if target_track:
            if target_track['is_playing']:
                target_track['is_playing'] = False
                if target_track['channel']:
                    target_track['channel'].stop()
                    target_track['channel'] = None
            else:
                target_track['is_playing'] = True
                channel = pygame.mixer.find_channel(force=True) 
                if channel:
                    channel.play(target_track['sound_obj'], loops=-1)
                    target_track['channel'] = channel

    def stop_all_loops(self):
        for track in self.tracks:
            track['is_playing'] = False
            if track['channel']:
                track['channel'].stop()
                track['channel'] = None
        print("ALL LOOPS STOPPED")

    def delete_track(self, track_idx):
        if 0 <= track_idx < len(self.tracks):
            if self.tracks[track_idx]['is_playing'] and self.tracks[track_idx]['channel']:
                self.tracks[track_idx]['channel'].stop()
            del self.tracks[track_idx]

looper = Looper()

# --- 5. FILE HELPERS ---
def load_wav_raw(filepath):
    try:
        with wave.open(filepath, 'rb') as wf:
            frames = wf.readframes(wf.getnframes())
            raw = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
            raw /= 32768.0
            if wf.getnchannels() == 2: raw = raw[::2]
            return raw
    except: return None

def locate_folder(folder_name):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    relative_path = os.path.join(script_dir, folder_name)
    if os.path.exists(relative_path): return relative_path
    return None

# --- 6. INSTRUMENT SETUP ---

# --- GUITAR CONFIG ---
GUITAR_PATH = locate_folder("Guitar")
if not GUITAR_PATH: GUITAR_PATH = r"D:\Projects\Minor\Audio Files\Guitar"

STRING_FILE_PREFIXES = {0: "Eminor_", 1: "B", 2: "G", 3: "D", 4: "A", 5: "Emajor_"}
STRINGS = {0: {'base_freq': 329.63, 'name': 'High E'}, 
           1: {'base_freq': 246.94, 'name': 'B String'}, 
           2: {'base_freq': 196.00, 'name': 'G String'}, 
           3: {'base_freq': 146.83, 'name': 'D String'}, 
           4: {'base_freq': 110.00, 'name': 'A String'}, 
           5: {'base_freq': 82.41, 'name': 'Low E'}}

if GUITAR_PATH and os.path.exists(GUITAR_PATH):
    for s_idx, prefix in STRING_FILE_PREFIXES.items():
        for fret in range(13):
            for ext in [".wav", ".mp3"]:
                for sep in ["", "_"]:
                    fname = f"{prefix}{sep}{fret}{ext}"
                    path = os.path.join(GUITAR_PATH, fname)
                    if os.path.exists(path):
                        try:
                            snd = pygame.mixer.Sound(path)
                            key = f"{s_idx}_{fret}"
                            if ext == ".wav":
                                raw = load_wav_raw(path)
                                if raw is not None:
                                    synth_key = f"gtr_{int(STRINGS[s_idx]['base_freq'])}_fret{fret}"
                                    audio.store_raw(synth_key, raw)
                                    custom_sounds[key] = (snd, synth_key)
                                else:
                                    synth_key = f"gtr_{int(STRINGS[s_idx]['base_freq'])}"
                                    if synth_key not in audio.raw_buffers: audio.generate_note(STRINGS[s_idx]['base_freq'])
                                    custom_sounds[key] = (snd, synth_key)
                            else:
                                synth_key = f"gtr_{int(STRINGS[s_idx]['base_freq'])}"
                                if synth_key not in audio.raw_buffers: audio.generate_note(STRINGS[s_idx]['base_freq'])
                                custom_sounds[key] = (snd, synth_key)
                            break
                        except: pass

def get_guitar_sound(string_idx, fret_num):
    key = f"{string_idx}_{fret_num}"
    if key in custom_sounds: return custom_sounds[key]
    base = STRINGS[string_idx]['base_freq']
    freq = base * (2 ** (fret_num / 12.0))
    if key not in note_cache: note_cache[key] = audio.generate_note(freq)
    return note_cache[key]

# --- DRUMS CONFIG ---
DRUM_PATH = locate_folder("Drums")
DRUM_FILE_MAP = {
    'Kick': 'kick', 'Snare': 'snare_normal', 'HiHat': 'hihat',
    'Crash': 'crash', 'Ride': 'ride', 'HighTom': 'tom_high',
    'MidTom': 'tom_mid', 'FloorTom': 'tom_low'
}

drum_sounds = {}
if DRUM_PATH:
    for key, fname_base in DRUM_FILE_MAP.items():
        loaded = False
        for ext in [".wav", ".mp3"]:
            fname = f"{fname_base}{ext}"
            path = os.path.join(DRUM_PATH, fname)
            if os.path.exists(path):
                try:
                    snd = pygame.mixer.Sound(path)
                    raw_key = f"drm_{key}"
                    if ext == ".wav":
                        raw = load_wav_raw(path)
                        if raw is not None:
                            audio.store_raw(raw_key, raw)
                            drum_sounds[key] = (snd, raw_key)
                            loaded = True
                            break
                    
                    synth_key = f"drm_synth_{key}"
                    drum_sounds[key] = (snd, synth_key) 
                    loaded = True
                    break
                except: pass
        
synth_map = {
    'Kick': (60, 0.3, 'sine'), 'Snare': (150, 0.2, 'sine'),
    'HiHat': (400, 0.1, 'noise'), 'Crash': (300, 0.8, 'noise'),
    'Ride': (350, 0.6, 'noise'), 'HighTom': (200, 0.3, 'sine'),
    'MidTom': (150, 0.3, 'sine'), 'FloorTom': (100, 0.4, 'sine')
}

for key, params in synth_map.items():
    synth_snd, synth_key = audio.generate_drum(params[0], duration=params[1], wave_type=params[2])
    if key not in drum_sounds:
        drum_sounds[key] = (synth_snd, synth_key)
    else:
        snd, r_key = drum_sounds[key]
        if r_key.startswith("drm_synth_") and r_key not in audio.raw_buffers:
             audio.generate_drum(params[0], duration=params[1], wave_type=params[2])

# --- UPDATED DRUM LAYOUT (REALISTIC ARC) ---
drum_zones = [
    # TOP ROW (Cymbals & Toms) - Close Arc
    {'pos': (300, 250), 'r': 85, 'color': (0, 215, 255), 'sound': 'Crash', 'label': 'Crash', 'type': 'cymbal'},
    {'pos': (500, 280), 'r': 80, 'color': (200, 200, 200), 'sound': 'HighTom', 'label': 'Hi-Tom', 'type': 'drum'},
    {'pos': (780, 280), 'r': 80, 'color': (200, 200, 200), 'sound': 'MidTom', 'label': 'Mid-Tom', 'type': 'drum'},
    {'pos': (980, 250), 'r': 85, 'color': (0, 215, 255), 'sound': 'Ride', 'label': 'Ride', 'type': 'cymbal'},
    
    # BOTTOM ROW (Snare, Kick, Floor) - Raised from bottom
    {'pos': (200, 450), 'r': 75, 'color': (0, 215, 255), 'sound': 'HiHat', 'label': 'Hi-Hat', 'type': 'cymbal'},
    {'pos': (380, 520), 'r': 90, 'color': (200, 200, 200), 'sound': 'Snare', 'label': 'Snare', 'type': 'drum'},
    {'pos': (640, 580), 'r': 110, 'color': (50, 50, 200), 'sound': 'Kick', 'label': 'Kick', 'type': 'kick'},
    {'pos': (900, 520), 'r': 95, 'color': (200, 200, 200), 'sound': 'FloorTom', 'label': 'Low Tom', 'type': 'drum'},
]
for d in drum_zones: 
    d['trigger'] = False
    d['ready'] = True 

# --- HARMONIUM CONFIG ---
harm_map = {
    'Z': ('C3', 130.8), 'X': ('D3', 146.8), 'C': ('E3', 164.8), 'V': ('F3', 174.6),
    'B': ('G3', 196.0), 'N': ('A3', 220.0), 'M': ('B3', 246.9),
    '<': ('C4', 261.6), '>': ('D4', 293.6), '?': ('E4', 329.6),
    'S': ('C#3', 138.6), 'D': ('D#3', 155.6), 'G': ('F#3', 185.0), 
    'H': ('G#3', 207.6), 'J': ('A#3', 233.1), 'L': ('C#4', 277.2), ':': ('D#4', 311.1)
}

HARM_PATH = locate_folder("Harmonium")
print("--- Loading Harmonium ---")
for k, (note, freq) in harm_map.items():
    loaded = False
    if HARM_PATH:
        candidates = [f"{note}.wav", f"{note}.mp3", f"harmonium-{note.lower()}.wav", f"harmonium-{note}.wav"]
        for fname in candidates:
            path = os.path.join(HARM_PATH, fname)
            if os.path.exists(path):
                try:
                    snd = pygame.mixer.Sound(path)
                    raw_key = f"harm_{note}"
                    synth_key = f"harm_synth_{int(freq)}"
                    if synth_key not in audio.raw_buffers: audio.generate_harmonium(freq)
                    
                    if fname.endswith(".wav"):
                        raw = load_wav_raw(path)
                        if raw is not None:
                            audio.store_raw(raw_key, raw)
                            harm_sounds[k] = (snd, raw_key)
                            loaded = True
                            print(f"Loaded: {fname} for key {k}")
                            break
                    harm_sounds[k] = (snd, synth_key)
                    loaded = True
                    print(f"Loaded (Play): {fname} for key {k}")
                    break
                except: pass
    if not loaded: harm_sounds[k] = audio.generate_harmonium(freq)

# --- 7. PYNPUT LISTENER ---
if HAS_PYNPUT:
    def on_press(key):
        try:
            if hasattr(key, 'char') and key.char:
                if current_instrument != "Harmonium": return
                k = key.char # Fixed: No .upper()
                if k in harm_sounds:
                    now = time.time()
                    if k not in active_harmonium_notes:
                        snd_tuple = harm_sounds[k]
                        snd = snd_tuple[0]
                        raw_key = snd_tuple[1]
                        channel = snd.play(loops=-1)
                        if channel:
                            active_harmonium_notes[k] = {
                                'channel': channel, 'last_seen': now, 
                                'start_time': now, 'raw': raw_key
                            }
                            looper.log_event("harm", raw_key)
        except: pass

    def on_release(key):
        try:
            if hasattr(key, 'char') and key.char:
                if current_instrument != "Harmonium": return
                k = key.char # Fixed: No .upper()
                if k in active_harmonium_notes:
                    active_harmonium_notes[k]['channel'].fadeout(50)
                    del active_harmonium_notes[k]
        except: pass

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

# --- 8. HELPERS ---
def count_fingers(hand_landmarks):
    lm = hand_landmarks.landmark
    fingers_open = [False] * 5
    if math.hypot(lm[4].x - lm[5].x, lm[4].y - lm[5].y) > 0.1: fingers_open[0] = True
    if lm[8].y < lm[6].y: fingers_open[1] = True
    if lm[12].y < lm[10].y: fingers_open[2] = True
    if lm[16].y < lm[14].y: fingers_open[3] = True
    if lm[20].y < lm[18].y: fingers_open[4] = True
    if fingers_open[0] and fingers_open[1] and not any(fingers_open[2:5]): return 7
    if fingers_open[0] and not any(fingers_open[1:5]): return 6
    return max(0, min(5, sum(fingers_open)))

def get_right_hand_cursor(landmarks_list, w, h):
    for hand in landmarks_list:
        ix, iy = int(hand.landmark[8].x * w), int(hand.landmark[8].y * h)
        tx, ty = hand.landmark[4].x, hand.landmark[4].y
        idx_x, idx_y = hand.landmark[8].x, hand.landmark[8].y
        dist = math.hypot(tx - idx_x, ty - idx_y)
        is_pinch = dist < 0.05
        return (ix, iy, is_pinch)
    return None

def process_harmonium_key_event_legacy(key_char):
    if key_char in harm_sounds:
        now = time.time()
        if key_char not in active_harmonium_notes:
            snd_tuple = harm_sounds[key_char]
            snd = snd_tuple[0]
            raw_key = snd_tuple[1]
            channel = snd.play(loops=-1)
            if channel:
                active_harmonium_notes[key_char] = {
                    'channel': channel, 
                    'last_seen': now, 
                    'start_time': now, 
                    'raw': raw_key
                }
                looper.log_event("harm", raw_key)
        else:
            active_harmonium_notes[key_char]['last_seen'] = now

# --- 9. DRAWING & PROCESSING ---
def draw_harmonium_ui(image, pressure):
    h, w, _ = image.shape
    bar_w = int((pressure / 100.0) * 400)
    cv2.rectangle(image, (w//2 - 200, h - 50), (w//2 - 200 + bar_w, h - 30), (0, 255, 255), -1)
    cv2.rectangle(image, (w//2 - 200, h - 50), (w//2 + 200, h - 30), (255, 255, 255), 2)
    cv2.putText(image, "BELLOWS (Tilt Camera)", (w//2 - 100, h - 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    cv2.putText(image, "HOLD SHIFT + KEYS (Z,X,C...)", (50, h - 100), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
    
    if not HAS_PYNPUT:
        now = time.time()
        keys_to_remove = []
        for k, data in active_harmonium_notes.items():
            duration_held = now - data['start_time']
            time_since_last = now - data['last_seen']
            timeout = 1.0 if duration_held < 1.0 else 0.3
            if time_since_last > timeout:
                data['channel'].fadeout(50)
                keys_to_remove.append(k)
        for k in keys_to_remove: del active_harmonium_notes[k]

    start_x = 100
    kw = 40
    sorted_keys = sorted(harm_map.keys(), key=lambda k: harm_map[k][1])
    x_off = 0
    for k in sorted_keys:
        note = harm_map[k][0]
        is_active = k in active_harmonium_notes
        if '#' not in note:
            x = start_x + (x_off * kw)
            y = h - 200
            col = (0, 255, 0) if is_active else (255,255,255)
            cv2.rectangle(image, (x, y), (x+kw-5, y+80), col, -1)
            cv2.putText(image, k, (x+10, y+60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,0), 1)
            x_off += 1
    x_off = 0
    for k in sorted_keys:
        note = harm_map[k][0]
        is_active = k in active_harmonium_notes
        if '#' in note:
            x = start_x + (x_off * kw) + (kw//2)
            y = h - 200
            col = (0, 255, 0) if is_active else (0,0,0)
            cv2.rectangle(image, (x, y), (x+kw-10, y+50), col, -1)
            cv2.putText(image, k, (x+5, y+40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,255,255), 1)
        else: x_off += 1

def draw_interface_guitar(image, active_idx, fret):
    h, w, _ = image.shape
    cv2.putText(image, f"MODE: {current_instrument}", (30, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    cv2.putText(image, "'M' Menu | 'R' Rec | 'L' Loop", (30, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)

    for i in range(6):
        y = MARGIN_TOP + (i * ROW_HEIGHT) + (ROW_HEIGHT // 2)
        color = (0, 255, 0) if i == active_idx else (80, 80, 80)
        thick = 4 if i == active_idx else 2
        cv2.line(image, (0, y), (int(w/2), y), color, thick)
        cv2.putText(image, STRINGS[i]['name'], (20, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    
    cv2.rectangle(image, (w-400, 0), (w, 160), (30, 30, 30), -1)
    cv2.putText(image, f"String: {STRINGS[active_idx]['name']}", (w-380, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    fret_text = f"Fret {fret}"
    if fret == 7: fret_text += " (L-Shape)"
    elif fret == 6: fret_text += " (Thumb)"
    cv2.putText(image, f"Note: {fret_text}", (w-380, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    key = f"{active_idx}_{fret}"
    src = "FILE" if key in custom_sounds else "SYNTH"
    cv2.putText(image, f"Source: {src}", (w-380, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    cv2.putText(image, "RIGHT HAND: Strum/Sweep", (w-380, 145), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
    
    cv2.line(image, (int(w*0.75)-50, h//2), (int(w*0.75)+50, h//2), (255, 0, 0), 4)
    cv2.putText(image, "STRUM LINE", (int(w*0.75)-40, h//2 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)

def draw_menu(image, right_hand_coords):
    h, w, _ = image.shape
    output = image.copy()
    overlay = image.copy()
    cv2.rectangle(overlay, (100, 100), (w-100, h-100), (20, 20, 20), -1)
    output = cv2.addWeighted(overlay, 0.95, output, 0.05, 0)
    cv2.putText(output, "LOOP STATION MENU", (w//2 - 150, 150), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
    
    global menu_buttons
    menu_buttons = []
    start_y = 220
    
    btn_rect = (w-200, 120, 80, 40)
    menu_buttons.append({'rect': btn_rect, 'text': 'X', 'action': 'close', 'color': (0, 0, 255)})
    stop_btn_rect = (w-370, 120, 150, 40)
    menu_buttons.append({'rect': stop_btn_rect, 'text': 'STOP ALL', 'action': 'stop_all', 'color': (200, 0, 0)})
    folder_btn_rect = (150, 120, 150, 40)
    menu_buttons.append({'rect': folder_btn_rect, 'text': 'OPEN REC', 'action': 'open_folder', 'color': (0, 100, 255)})

    if not looper.tracks:
        cv2.putText(output, "No recordings yet.", (150, start_y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (100, 100, 100), 2)
    else:
        for i, track in enumerate(looper.tracks):
            y = start_y + (i * 60)
            col = (0, 100, 0) if track['is_playing'] else (50, 50, 50)
            toggle_width = (w-150) - 150 - 70 
            cv2.rectangle(output, (150, y), (150 + toggle_width, y+50), col, -1)
            status = "LOOPING" if track['is_playing'] else "STOPPED"
            cv2.putText(output, f"{track['label']} ({track['duration']:.1f}s) - {status}", (170, y+35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            menu_buttons.append({'rect': (150, y, toggle_width, 50), 'text': '', 'action': 'toggle', 'idx': i, 'color': None})
            del_x = 150 + toggle_width + 10
            cv2.rectangle(output, (del_x, y), (del_x + 60, y+50), (0, 0, 200), -1)
            cv2.putText(output, "DEL", (del_x + 10, y+35), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            menu_buttons.append({'rect': (del_x, y, 60, 50), 'text': '', 'action': 'delete', 'idx': i, 'color': None})

    if right_hand_coords:
        rx, ry, is_pinch = right_hand_coords
        cursor_col = (0, 0, 255) if is_pinch else (0, 255, 0)
        for btn in menu_buttons:
            bx, by, bw, bh = btn['rect']
            if btn['color']: 
                cv2.rectangle(output, (bx, by), (bx+bw, by+bh), btn['color'], -1)
                cv2.putText(output, btn['text'], (bx+20, by+30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)
            if bx < rx < bx+bw and by < ry < by+bh:
                cv2.rectangle(output, (bx, by), (bx+bw, by+bh), (255, 255, 255), 2) 
                if is_pinch:
                    if btn['action'] == 'close':
                        global show_menu
                        show_menu = False
                    elif btn['action'] == 'stop_all':
                        looper.stop_all_loops()
                        time.sleep(0.3)
                    elif btn['action'] == 'open_folder':
                        open_recordings_folder()
                        time.sleep(0.3)
                    elif btn['action'] == 'toggle':
                        looper.toggle_loop(btn['idx'])
                        time.sleep(0.3)
                    elif btn['action'] == 'delete':
                        looper.delete_track(btn['idx'])
                        time.sleep(0.3)
        cv2.circle(output, (rx, ry), 10, cursor_col, -1)
    return output

def draw_hud(image):
    cv2.rectangle(image, (20, 10), (480, 90), (0, 0, 0), -1)
    cv2.putText(image, f"MODE: {current_instrument}", (30, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    cv2.putText(image, "'M' Menu | 'R' Rec | 'L' Loop | 'O' Stop All", (30, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)

    if looper.is_recording:
        cv2.circle(image, (50, 110), 20, (0, 0, 255), -1)
        cv2.putText(image, "REC", (80, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        cv2.rectangle(image, (0,0), (image.shape[1],image.shape[0]), (0,0,255), 10) 
    
    active_tracks = [t for t in looper.tracks if t['is_playing']]
    if active_tracks:
        y_pos = 150 if looper.is_recording else 110
        cv2.putText(image, f"Looping: {len(active_tracks)} tracks", (30, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)

def draw_3d_drum(image, drum):
    x, y = drum['pos']
    r = drum['r']
    text_y_offset = 5
    if drum['type'] == 'cymbal':
        color = (0, 255, 255) if drum['trigger'] else drum['color']
        cv2.ellipse(image, (x, y), (r, int(r*0.4)), 0, 0, 360, color, -1)
        cv2.ellipse(image, (x, y), (r, int(r*0.4)), 0, 0, 360, (50, 50, 50), 2)
        cv2.ellipse(image, (x, y-5), (int(r*0.2), int(r*0.1)), 0, 0, 360, (0,0,0), -1)
        text_y_offset = 40
    elif drum['type'] == 'kick':
        color = (255, 255, 255) if drum['trigger'] else drum['color']
        cv2.circle(image, (x, y), r, (50, 50, 50), -1)
        cv2.circle(image, (x, y), r-10, color, -1)
        cv2.circle(image, (x, y), r, (200, 200, 200), 4)
    else: 
        color = (255, 255, 255) if drum['trigger'] else drum['color']
        depth = 40
        cv2.rectangle(image, (x-r, y), (x+r, y+depth), (100, 100, 100), -1)
        cv2.ellipse(image, (x, y+depth), (r, int(r*0.4)), 0, 0, 180, (100, 100, 100), -1)
        cv2.ellipse(image, (x, y), (r, int(r*0.4)), 0, 0, 360, color, -1)
        cv2.ellipse(image, (x, y), (r, int(r*0.4)), 0, 0, 360, (150, 150, 150), 3)
    cv2.putText(image, drum['label'], (x-30, y+text_y_offset), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,0), 2)

def process_guitar(image, landmarks_list):
    global active_string_idx, current_fret, last_strum_time, prev_wrist_y, can_strum, last_played_string_idx
    h, w, _ = image.shape
    left, right = None, None
    for hand in landmarks_list:
        if hand.landmark[0].x < 0.5: left = hand
        else: right = hand

    if left:
        ly_px = int(left.landmark[9].y * h)
        active_string_idx = max(0, min(5, int((ly_px - MARGIN_TOP) / ROW_HEIGHT)))
        current_fret = count_fingers(left)
        cv2.circle(image, (int(left.landmark[9].x * w), ly_px), 15, (0, 255, 0), -1)

    if right:
        ry = right.landmark[0].y
        now = time.time()
        right_fingers = count_fingers(right)
        ry_px = int(right.landmark[9].y * h)
        right_str_idx = max(0, min(5, int((ry_px - MARGIN_TOP) / ROW_HEIGHT)))
        
        if right_fingers == 6: 
            speed = abs(ry - guitar_strum_state['prev_y'])
            if speed > 0.05: 
                if (now - last_strum_time > 0.05) or (right_str_idx != last_played_string_idx): 
                    sound_tuple = get_guitar_sound(right_str_idx, current_fret)
                    if sound_tuple:
                        snd, raw_key = sound_tuple
                        snd.play()
                        last_strum_time = now
                        last_played_string_idx = right_str_idx
                        looper.log_event("guitar", raw_key)
                        cv2.putText(image, "SWEEP!", (int(w*0.7), int(h/2)), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 255), 3)
                        y_sweep = MARGIN_TOP + (right_str_idx * ROW_HEIGHT) + (ROW_HEIGHT // 2)
                        cv2.circle(image, (int(w*0.75), y_sweep), 20, (0, 0, 255), -1)
        else:
            center_y = 0.5 
            prev_y = guitar_strum_state['prev_y']
            crossed_line = (prev_y < center_y and ry >= center_y) or (prev_y > center_y and ry <= center_y)
            if crossed_line and (now - last_strum_time > 0.2):
                sound_tuple = get_guitar_sound(active_string_idx, current_fret)
                if sound_tuple:
                    snd, raw_key = sound_tuple
                    snd.play()
                    last_strum_time = now
                    looper.log_event("guitar", raw_key)
                    cv2.putText(image, "STRUM!", (int(w*0.7), int(h/2)), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 255, 255), 3)

        guitar_strum_state['prev_y'] = ry

def process_drums(image, landmarks_list):
    h, w, _ = image.shape
    for drum in drum_zones:
        draw_3d_drum(image, drum)
        if drum['trigger']: drum['trigger'] = False
    
    for i, hand in enumerate(landmarks_list):
        ix, iy = int(hand.landmark[8].x * w), int(hand.landmark[8].y * h)
        cv2.circle(image, (ix, iy), 20, (0, 0, 255), -1)
        cv2.circle(image, (ix, iy), 23, (255, 255, 255), 2)
        current_y = hand.landmark[8].y
        prev_y = prev_hand_y.get(i, current_y)
        speed = (current_y - prev_y) 
        prev_hand_y[i] = current_y

        for drum in drum_zones:
            x, y = drum['pos']
            if abs(ix - x) < drum['r'] and abs(iy - y) < drum['r']:
                if speed > 0.02 and drum['ready']:
                    drum['trigger'] = True
                    drum['ready'] = False
                    snd_tuple = drum_sounds[drum['sound']]
                    snd_tuple[0].play() 
                    looper.log_event(drum['sound'], snd_tuple[1])
                if speed < 0.005: drum['ready'] = True
            else:
                drum['ready'] = True

# --- 10. MAIN LOOP ---
with mp_hands.Hands(min_detection_confidence=0.7, min_tracking_confidence=0.5, max_num_hands=2) as hands:
    while cap.isOpened():
        success, image = cap.read()
        if not success: break
        current_pressure = pressure_engine.update(image)
        image = cv2.flip(image, 1)
        image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        results = hands.process(image_rgb)
        
        if show_menu:
            cursor = None
            if results.multi_hand_landmarks:
                cursor = get_right_hand_cursor(results.multi_hand_landmarks, image.shape[1], image.shape[0])
                for hand_landmarks in results.multi_hand_landmarks:
                    mp_drawing.draw_landmarks(image, hand_landmarks, mp_hands.HAND_CONNECTIONS)
            image = draw_menu(image, cursor)
        else:
            draw_hud(image) 
            if current_instrument == "Guitar":
                draw_interface_guitar(image, active_string_idx, current_fret)
            elif current_instrument == "Harmonium":
                draw_harmonium_ui(image, current_pressure)

            if results.multi_hand_landmarks:
                if current_instrument != "Drums":
                    for hand_landmarks in results.multi_hand_landmarks:
                        mp_drawing.draw_landmarks(image, hand_landmarks, mp_hands.HAND_CONNECTIONS)
                if current_instrument == "Guitar": process_guitar(image, results.multi_hand_landmarks)
                elif current_instrument == "Drums": process_drums(image, results.multi_hand_landmarks)

        key = cv2.waitKey(5) & 0xFF
        if key == 27: break
        elif key == ord('g'): current_instrument = "Guitar"
        elif key == ord('d'): current_instrument = "Drums"
        elif key == ord('h'): current_instrument = "Harmonium"
        elif key == ord('r'): 
            if looper.is_recording: looper.stop_recording()
            else: looper.start_recording(current_instrument)
        elif key == ord('l'):
            if looper.tracks: looper.toggle_loop(-1)
        elif key == ord('o'): 
            looper.stop_all_loops()
        elif key == ord('m'): show_menu = not show_menu
        
        if current_instrument == "Harmonium":
            if not HAS_PYNPUT:
                char_key = chr(key).lower() if key < 256 else None
                key_str = chr(key) if key < 256 else ""
                if key_str in harm_map: process_harmonium_key_event_legacy(key_str)

        cv2.imshow('Air Band', image)

cap.release()
cv2.destroyAllWindows()