import os
import sys
import time
import json
import queue
import threading
import ctypes
import customtkinter as ctk
import pygame
import pygame._sdl2.audio as sdl2_audio
import pygame.midi
import sounddevice as sd
import soundfile as sf
import soundcard as sc
import numpy as np
import pystray
from PIL import Image, ImageDraw
from tkinter import filedialog, messagebox

# Single Instance Lock (Windows Named Mutex)
MUTEX_NAME = "Global\\LaunchpadSoundboard_SingleInstance_v5"
kernel32 = ctypes.windll.kernel32
mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    root = ctk.CTk()
    root.withdraw()
    messagebox.showinfo("Soundboard Already Running", "Launchpad Soundboard is already running in your system tray!")
    root.destroy()
    sys.exit(0)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

# Base Directory Resolution for Frozen PyInstaller Executable
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TRIM_DIR = os.path.join(BASE_DIR, "trimmed_audio")

if not os.path.exists(TRIM_DIR):
    os.makedirs(TRIM_DIR)

COLOR_MAP = {
    "Red": 15,
    "Green": 60,
    "Amber": 63,
    "Yellow": 62,
    "Off": 12
}

COLOR_HEX = {
    "Red": "#ff3366",
    "Green": "#00ffcc",
    "Amber": "#ffaa00",
    "Yellow": "#ffff33",
    "Off": "#1e1e1e"
}

def create_tray_icon_image():
    img = Image.new('RGBA', (64, 64), color=(26, 26, 35, 255))
    draw = ImageDraw.Draw(img)
    for r in range(3):
        for c in range(3):
            draw.rectangle([12 + c*16, 12 + r*16, 22 + c*16, 22 + r*16], fill=(0, 255, 204, 255))
    return img

class SoundboardApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.is_loading = True
        
        self.title("Launchpad Soundboard v5")
        self.geometry("1020x1080")
        
        self.pads = {}
        self.volume = 1.0
        
        # Audio & MIDI setup
        pygame.init()
        pygame.midi.init()
        
        self.broadcast_device_idx = None
        self.in_stream = None
        self.out_stream = None
        self.passthrough_buffer = np.zeros((0, 1), dtype=np.float32)
        self.passthrough_lock = threading.Lock()
        self.passthrough_active = True
        
        # System Audio Recorder & Pad Selection State
        self.recording_active = False
        self.recorded_chunks = []
        self.selecting_pad_mode = False
        self.pending_recording_filepath = None
        
        self.init_audio_system(None)
        
        self.midi_out = None
        self.midi_in = None
        
        self.setup_ui()
        self.init_midi_system()
        
        self.load_config()
        self.is_loading = False
        
        if self.passthrough_active:
            self.after(500, self.start_passthrough)

        self.setup_tray()
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.poll_midi()

        # Check command line flags or config setting to launch straight to tray
        cli_minimized = any(arg.lower() in ["--minimized", "--tray", "-m"] for arg in sys.argv[1:])
        if cli_minimized or self.start_minimized_var.get():
            self.after(10, self.withdraw)

    def setup_tray(self):
        def on_show(icon, item):
            self.after(0, self.show_from_tray)
            
        def on_quit(icon, item):
            self.after(0, self.quit_app)
            
        menu = pystray.Menu(
            pystray.MenuItem("Show Soundboard", on_show, default=True),
            pystray.MenuItem("Quit", on_quit)
        )
        
        self.tray_icon = pystray.Icon("soundboard", create_tray_icon_image(), "Launchpad Soundboard", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def hide_to_tray(self):
        self.withdraw()
        self.status_msg_label.configure(text="App minimized to system tray.", text_color="#00ffcc")

    def show_from_tray(self):
        self.deiconify()
        self.focus_force()

    def quit_app(self):
        if hasattr(self, 'tray_icon') and self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception: pass
        self.on_closing()
        
    def init_audio_system(self, monitor_devicename=None):
        try:
            if pygame.mixer.get_init():
                pygame.mixer.quit()
            
            if monitor_devicename == "Default" or monitor_devicename is None:
                pygame.mixer.init(frequency=44100, size=-16, channels=2)
            else:
                pygame.mixer.init(frequency=44100, size=-16, channels=2, devicename=monitor_devicename)
                
            pygame.mixer.set_num_channels(128)
            
            for key, pad in self.pads.items():
                if pad.get("file_path") and os.path.exists(pad["file_path"]):
                    try:
                        snd = pygame.mixer.Sound(pad["file_path"])
                        eff_vol = pad.get("volume", 1.0) * self.volume
                        snd.set_volume(eff_vol)
                        pad["sound"] = snd
                    except Exception as e:
                        print(f"Error loading sound {pad['file_path']}: {e}")
        except Exception as e:
            print(f"Failed to init monitor audio: {e}")
            if monitor_devicename is not None and monitor_devicename != "Default":
                self.init_audio_system("Default")

    def get_clean_devices(self, kind='input'):
        try:
            devices = sd.query_devices()
            clean_list = []
            seen_names = set()
            
            # WASAPI (Host API 2) ONLY to ensure 100% sample rate & driver compatibility
            for idx, dev in enumerate(devices):
                if dev['hostapi'] == 2:
                    is_valid = (kind == 'input' and dev['max_input_channels'] > 0) or \
                               (kind == 'output' and dev['max_output_channels'] > 0)
                    if is_valid:
                        name = dev['name']
                        if any(b in name for b in ["Mapper", "Primary Sound", "Steam", "VDVAD"]):
                            continue
                        display_name = f"{idx}: {name}"
                        if name not in seen_names:
                            seen_names.add(name)
                            clean_list.append((idx, display_name))
            return clean_list if clean_list else [(None, "No devices found")]
        except Exception as e:
            print(f"Error querying devices: {e}")
            return [(None, "Default")]

    def on_monitor_change(self, value):
        self.init_audio_system(value)
        self.save_config()

    def on_broadcast_change(self, value):
        try:
            self.broadcast_device_idx = int(value.split(":")[0])
            if self.passthrough_active:
                self.restart_passthrough()
        except Exception:
            self.broadcast_device_idx = None
        self.save_config()

    def on_input_change(self, value):
        print(f"Changing Physical Mic Input to: {value}")
        if self.passthrough_active:
            self.restart_passthrough()
        self.save_config()

    def on_volume_change(self, value):
        self.volume = float(value)
        for pad in self.pads.values():
            if pad.get("sound"):
                eff_vol = pad.get("volume", 1.0) * self.volume
                pad["sound"].set_volume(eff_vol)
        self.save_config()

    def on_start_minimized_change(self):
        self.save_config()

    def setup_ui(self):
        # Header
        self.header = ctk.CTkFrame(self, fg_color="transparent")
        self.header.pack(pady=10, padx=20, fill="x")
        
        self.title_label = ctk.CTkLabel(self.header, text="Launchpad Soundboard v5", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.pack(side="left")
        
        # System Audio Record Button
        self.rec_btn = ctk.CTkButton(
            self.header,
            text="⏺ Record System Audio",
            fg_color="#7a00ff",
            hover_color="#5c00c8",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.toggle_system_recording
        )
        self.rec_btn.pack(side="right", padx=10)

        # STOP ALL Button
        self.stop_btn = ctk.CTkButton(
            self.header,
            text="⏹ STOP ALL",
            fg_color="#ff3333",
            hover_color="#cc0000",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.stop_all_sounds
        )
        self.stop_btn.pack(side="right", padx=5)
        
        # Settings Panel
        self.settings_frame = ctk.CTkFrame(self)
        self.settings_frame.pack(pady=10, padx=20, fill="x")
        
        inputs = self.get_clean_devices('input')
        outputs = self.get_clean_devices('output')
        
        # 1. Monitor Output
        self.mon_label = ctk.CTkLabel(self.settings_frame, text="1. What YOU Hear (Headphones):", font=ctk.CTkFont(weight="bold"))
        self.mon_label.grid(row=0, column=0, padx=10, pady=6, sticky="w")
        
        mon_options = [opt[1] for opt in outputs]
        self.mon_var = ctk.StringVar(value=mon_options[0] if mon_options else "Default")
        self.mon_menu = ctk.CTkOptionMenu(
            self.settings_frame, values=mon_options, variable=self.mon_var,
            command=self.on_monitor_change, width=320
        )
        self.mon_menu.grid(row=0, column=1, padx=10, pady=6)

        # 2. Broadcast Output
        self.broad_label = ctk.CTkLabel(self.settings_frame, text="2. What DISCORD Hears (CABLE Input):", font=ctk.CTkFont(weight="bold"))
        self.broad_label.grid(row=1, column=0, padx=10, pady=6, sticky="w")
        
        broad_options = [opt[1] for opt in outputs]
        cable_opt = next((opt for opt in broad_options if "cable" in opt.lower()), broad_options[0] if broad_options else "")
        
        self.broad_var = ctk.StringVar(value=cable_opt)
        self.broad_menu = ctk.CTkOptionMenu(
            self.settings_frame, values=broad_options, variable=self.broad_var,
            command=self.on_broadcast_change, width=320
        )
        self.broad_menu.grid(row=1, column=1, padx=10, pady=6)
        if cable_opt:
            self.on_broadcast_change(cable_opt)

        # 3. Physical Microphone & Passthrough
        self.in_label = ctk.CTkLabel(self.settings_frame, text="3. Physical Microphone:", font=ctk.CTkFont(weight="bold"))
        self.in_label.grid(row=2, column=0, padx=10, pady=6, sticky="w")
        
        in_options = [opt[1] for opt in inputs]
        self.input_var = ctk.StringVar(value=in_options[0] if in_options else "")
        self.input_menu = ctk.CTkOptionMenu(
            self.settings_frame, values=in_options, variable=self.input_var,
            command=self.on_input_change, width=320
        )
        self.input_menu.grid(row=2, column=1, padx=10, pady=6)
        
        self.passthrough_btn = ctk.CTkButton(
            self.settings_frame, text="Mic Passthrough: ON", fg_color="#00ffcc",
            hover_color="#00cc99", text_color="black", font=ctk.CTkFont(weight="bold"), command=self.toggle_passthrough
        )
        self.passthrough_btn.grid(row=2, column=2, padx=10, pady=6)
        
        # 4. Master Volume Slider
        self.vol_label = ctk.CTkLabel(self.settings_frame, text="4. Master Volume:", font=ctk.CTkFont(weight="bold"))
        self.vol_label.grid(row=3, column=0, padx=10, pady=6, sticky="w")
        
        self.vol_slider = ctk.CTkSlider(
            self.settings_frame, from_=0.0, to=1.0, number_of_steps=100,
            command=self.on_volume_change, width=320
        )
        self.vol_slider.set(1.0)
        self.vol_slider.grid(row=3, column=1, padx=10, pady=6)

        # 5. Start Minimized Checkbox
        self.start_minimized_var = ctk.BooleanVar(value=False)
        self.start_minimized_checkbox = ctk.CTkCheckBox(
            self.settings_frame, text="Start Minimized to System Tray (for Windows Startup)",
            variable=self.start_minimized_var, command=self.on_start_minimized_change,
            font=ctk.CTkFont(weight="bold")
        )
        self.start_minimized_checkbox.grid(row=4, column=0, columnspan=2, padx=10, pady=6, sticky="w")

        # Status & MIDI Status
        self.status_msg_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=13, weight="bold"))
        self.status_msg_label.pack(pady=2)

        self.midi_status_label = ctk.CTkLabel(self, text="MIDI: Disconnected", text_color="#ff4444")
        self.midi_status_label.pack(pady=2)
        
        # Grid Frame Container
        self.grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.grid_frame.pack(pady=10, padx=20, expand=True)

        # Top Circle Buttons (CC 104..111)
        for col in range(8):
            cc_val = 104 + col
            key = f"top_{col}"
            btn = ctk.CTkButton(
                self.grid_frame, text="", width=65, height=40, corner_radius=20,
                fg_color="#1e1e1e", hover_color="#333333", border_width=1, border_color="#555555",
                font=ctk.CTkFont(size=9),
                command=lambda k=key: self.on_pad_click(k)
            )
            btn.bind("<Button-3>", lambda e, k=key: self.open_pad_dialog(k))
            btn.grid(row=0, column=col, padx=4, pady=4)
            
            self.pads[key] = {
                "file_path": None, "button": btn, "sound": None,
                "midi_type": "cc", "midi_val": cc_val, "color_name": "Amber",
                "is_stop_btn": False, "volume": 1.0, "custom_name": None
            }

        # Main 8x8 Grid + Right Side Circle Buttons
        for row in range(8):
            for col in range(8):
                idx = (row * 8) + col
                midi_note = (row * 16) + col
                key = f"grid_{idx}"
                btn = ctk.CTkButton(
                    self.grid_frame, text="", width=65, height=65, corner_radius=10,
                    fg_color="#1e1e1e", hover_color="#333333", border_width=1, border_color="#333333",
                    font=ctk.CTkFont(size=9),
                    command=lambda k=key: self.on_pad_click(k)
                )
                btn.bind("<Button-3>", lambda e, k=key: self.open_pad_dialog(k))
                btn.grid(row=row+1, column=col, padx=4, pady=4)
                
                self.pads[key] = {
                    "file_path": None, "button": btn, "sound": None,
                    "midi_type": "note", "midi_val": midi_note, "color_name": "Red",
                    "is_stop_btn": False, "volume": 1.0, "custom_name": None
                }

            # Right Side Circle Button
            side_midi_note = (row * 16) + 8
            key = f"side_{row}"
            btn = ctk.CTkButton(
                self.grid_frame, text="", width=40, height=65, corner_radius=20,
                fg_color="#1e1e1e", hover_color="#333333", border_width=1, border_color="#555555",
                font=ctk.CTkFont(size=9),
                command=lambda k=key: self.on_pad_click(k)
            )
            btn.bind("<Button-3>", lambda e, k=key: self.open_pad_dialog(k))
            btn.grid(row=row+1, column=8, padx=8, pady=4)
            
            self.pads[key] = {
                "file_path": None, "button": btn, "sound": None,
                "midi_type": "note", "midi_val": side_midi_note, "color_name": "Yellow",
                "is_stop_btn": False, "volume": 1.0, "custom_name": None
            }

        # Footer
        self.footer_label = ctk.CTkLabel(
            self, text="⏺ Click 'Record System Audio' to rip desktop audio live directly into a sound pad!",
            text_color="#00ffcc"
        )
        self.footer_label.pack(pady=10)

    def get_pad_display_name(self, pad):
        if pad.get("custom_name"):
            return pad["custom_name"][:10]
        if pad.get("file_path"):
            filename = os.path.basename(pad["file_path"])
            return os.path.splitext(filename)[0][:8]
        return ""

    def toggle_system_recording(self):
        if self.recording_active:
            self.stop_system_recording()
        else:
            self.start_system_recording()

    def start_system_recording(self):
        self.recording_active = True
        self.selecting_pad_mode = False
        self.pending_recording_filepath = None
        self.rec_btn.configure(text="⏹ Recording System... (Click Stop)", fg_color="#ff3333")
        self.status_msg_label.configure(text="Recording system audio live...", text_color="#ffaa00")
        self.recorded_chunks = []
        
        def record_thread():
            try:
                spk = sc.default_speaker()
                loopback = next((m for m in sc.all_microphones(include_loopback=True) if m.name == spk.name and m.isloopback), None)
                if not loopback:
                    loopback = sc.all_microphones(include_loopback=True)[0]
                    
                with loopback.recorder(samplerate=44100) as rec:
                    while self.recording_active:
                        chunk = rec.record(numframes=2205)
                        self.recorded_chunks.append(chunk)
            except Exception as e:
                print(f"System Audio Recording error: {e}")
                
        threading.Thread(target=record_thread, daemon=True).start()

    def stop_system_recording(self):
        self.recording_active = False
        self.rec_btn.configure(text="⏺ Record System Audio", fg_color="#7a00ff")
        
        if self.recorded_chunks:
            full_arr = np.concatenate(self.recorded_chunks, axis=0)
            dur = len(full_arr) / 44100.0
            
            # Save raw recording to a temporary file in TRIM_DIR without prompting
            temp_filepath = os.path.join(TRIM_DIR, f"temp_rec_{int(time.time())}.wav")
            sf.write(temp_filepath, full_arr, 44100)
            
            self.pending_recording_filepath = temp_filepath
            self.selecting_pad_mode = True
            self.status_msg_label.configure(
                text=f"⏺ Recorded {dur:.1f}s! CLICK ANY PAD ON SCREEN OR LAUNCHPAD TO ASSIGN & TRIM...",
                text_color="#00ffcc"
            )

    def stop_all_sounds(self):
        pygame.mixer.stop()
        sd.stop()
        for key, pad in self.pads.items():
            if pad["is_stop_btn"]:
                pad["button"].configure(text="STOP", fg_color="#aa0000", text_color="white")
                self.send_pad_midi_light(pad, COLOR_MAP["Red"])
            elif pad.get("file_path"):
                d_name = self.get_pad_display_name(pad)
                pad["button"].configure(text=d_name, fg_color=COLOR_HEX[pad["color_name"]], text_color="black" if pad["color_name"] in ["Yellow", "Green"] else "white")
                self.send_pad_midi_light(pad, COLOR_MAP[pad["color_name"]])

    def open_pad_dialog(self, key):
        pad = self.pads[key]
        
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Pad Options ({key})")
        dialog.geometry("380x470")
        dialog.grab_set()
        
        lbl = ctk.CTkLabel(dialog, text=f"Pad Settings: {key}", font=ctk.CTkFont(weight="bold"))
        lbl.pack(pady=6)

        # Custom Rename Field
        name_lbl = ctk.CTkLabel(dialog, text="Pad Label / Custom Name:")
        name_lbl.pack(pady=1)
        name_entry = ctk.CTkEntry(dialog, width=280, placeholder_text="Enter custom label...")
        name_entry.insert(0, pad.get("custom_name") or "")
        name_entry.pack(pady=3)
        
        action_var = ctk.StringVar(value="STOP Button" if pad["is_stop_btn"] else "Play Sound")
        action_lbl = ctk.CTkLabel(dialog, text="Button Action:")
        action_lbl.pack(pady=1)
        action_menu = ctk.CTkOptionMenu(dialog, values=["Play Sound", "STOP Button"], variable=action_var)
        action_menu.pack(pady=3)

        color_lbl = ctk.CTkLabel(dialog, text="Select LED Color:")
        color_lbl.pack(pady=1)
        color_var = ctk.StringVar(value=pad["color_name"])
        color_menu = ctk.CTkOptionMenu(dialog, values=list(COLOR_MAP.keys()), variable=color_var)
        color_menu.pack(pady=3)
        
        # Audio-Specific Volume Slider
        current_pad_vol = pad.get("volume", 1.0)
        vol_val_label = ctk.CTkLabel(dialog, text=f"Clip Volume: {int(current_pad_vol * 100)}%", font=ctk.CTkFont(weight="bold"))
        vol_val_label.pack(pady=1)
        
        def update_pad_vol_label(val):
            vol_val_label.configure(text=f"Clip Volume: {int(float(val) * 100)}%")
            
        pad_vol_slider = ctk.CTkSlider(dialog, from_=0.0, to=1.0, number_of_steps=100, command=update_pad_vol_label, width=280)
        pad_vol_slider.set(current_pad_vol)
        pad_vol_slider.pack(pady=3)
        
        def save_pad_settings():
            is_stop = (action_var.get() == "STOP Button")
            pad["is_stop_btn"] = is_stop
            pad["color_name"] = color_var.get()
            pad["volume"] = float(pad_vol_slider.get())
            
            c_name = name_entry.get().strip()
            pad["custom_name"] = c_name if c_name else None
            
            if pad.get("sound"):
                eff_vol = pad["volume"] * self.volume
                pad["sound"].set_volume(eff_vol)
            
            if is_stop:
                pad["button"].configure(text="STOP", fg_color="#aa0000", text_color="white")
                self.send_pad_midi_light(pad, COLOR_MAP["Red"])
            elif pad.get("file_path"):
                col_name = pad["color_name"]
                d_name = self.get_pad_display_name(pad)
                pad["button"].configure(text=d_name, fg_color=COLOR_HEX[col_name], text_color="black" if col_name in ["Yellow", "Green"] else "white")
                self.send_pad_midi_light(pad, COLOR_MAP[col_name])
            else:
                pad["button"].configure(text="", fg_color="#1e1e1e", text_color="white")
                self.send_pad_midi_light(pad, COLOR_MAP["Off"])
                
            self.save_config()
            dialog.destroy()
            
        def change_sound():
            filepath = filedialog.askopenfilename(title="Select Audio File", filetypes=[("Audio Files", "*.wav *.mp3 *.ogg")])
            if filepath:
                self.load_sound_to_pad(key, filepath)
            dialog.destroy()

        def open_trimmer():
            dialog.destroy()
            self.open_audio_trimmer(key)

        def clear_pad():
            pad["file_path"] = None
            pad["sound"] = None
            pad["is_stop_btn"] = False
            pad["volume"] = 1.0
            pad["custom_name"] = None
            pad["button"].configure(text="", fg_color="#1e1e1e", text_color="white")
            self.send_pad_midi_light(pad, COLOR_MAP["Off"])
            self.save_config()
            dialog.destroy()

        btn_sound = ctk.CTkButton(dialog, text="📁 Choose Audio File", command=change_sound)
        btn_sound.pack(pady=3)

        btn_trim = ctk.CTkButton(dialog, text="✂ Trim Audio Clip", fg_color="#7a00ff", hover_color="#5c00c8", command=open_trimmer)
        btn_trim.pack(pady=3)

        btn_save = ctk.CTkButton(dialog, text="✔ Apply & Save", command=save_pad_settings)
        btn_save.pack(pady=3)
        
        btn_clear = ctk.CTkButton(dialog, text="🗑 Clear Pad", fg_color="#ff4444", hover_color="#cc3333", command=clear_pad)
        btn_clear.pack(pady=3)

    def open_audio_trimmer(self, key=None, initial_filepath=None):
        filepath = initial_filepath
        if not filepath and key:
            filepath = self.pads[key].get("file_path")
            
        if not filepath or not os.path.exists(filepath):
            filepath = filedialog.askopenfilename(title="Select Audio File to Trim", filetypes=[("Audio Files", "*.wav *.mp3 *.ogg")])
            if not filepath:
                return

        try:
            data, sr = sf.read(filepath)
        except Exception as e:
            self.status_msg_label.configure(text=f"Error reading file for trim: {e}", text_color="#ff4444")
            return
            
        total_duration = len(data) / float(sr)
        
        trim_win = ctk.CTkToplevel(self)
        trim_win.title(f"Audio Trimmer - {os.path.basename(filepath)}")
        trim_win.geometry("540x500")
        trim_win.grab_set()
        
        lbl_title = ctk.CTkLabel(trim_win, text=f"Trim Clip: {os.path.basename(filepath)}", font=ctk.CTkFont(size=14, weight="bold"))
        lbl_title.pack(pady=8)
        
        canvas_width = 480
        canvas_height = 110
        wf_canvas = ctk.CTkCanvas(trim_win, width=canvas_width, height=canvas_height, bg="#141419", highlightthickness=1, highlightbackground="#333333")
        wf_canvas.pack(pady=8, padx=20)
        
        if len(data.shape) > 1:
            mono = data.mean(axis=1)
        else:
            mono = data
            
        num_samples = len(mono)
        step = max(1, num_samples // canvas_width)
        peaks = [np.max(np.abs(mono[i:i+step])) for i in range(0, num_samples, step)][:canvas_width]
        max_peak = max(peaks) if len(peaks) > 0 and max(peaks) > 0 else 1.0

        lbl_dur = ctk.CTkLabel(trim_win, text=f"Total Duration: {total_duration:.2f} seconds")
        lbl_dur.pack(pady=2)
        
        lbl_start = ctk.CTkLabel(trim_win, text="Start Time: 0.00s")
        lbl_start.pack(pady=1)
        start_slider = ctk.CTkSlider(trim_win, from_=0.0, to=total_duration, number_of_steps=500, width=460)
        start_slider.set(0.0)
        start_slider.pack(pady=2)

        lbl_end = ctk.CTkLabel(trim_win, text=f"End Time: {total_duration:.2f}s")
        lbl_end.pack(pady=1)
        end_slider = ctk.CTkSlider(trim_win, from_=0.0, to=total_duration, number_of_steps=500, width=460)
        end_slider.set(total_duration)
        end_slider.pack(pady=2)

        playback_state = {"playing": False, "start_time": 0.0, "duration": 0.0}

        def redraw_waveform():
            wf_canvas.delete("all")
            mid_y = canvas_height / 2
            
            for x, peak in enumerate(peaks):
                bar_h = (peak / max_peak) * (canvas_height / 2 - 4)
                wf_canvas.create_line(x, mid_y - bar_h, x, mid_y + bar_h, fill="#00ffcc", width=1)
                
            s_pct = start_slider.get() / total_duration if total_duration > 0 else 0
            e_pct = end_slider.get() / total_duration if total_duration > 0 else 1
            s_x = int(s_pct * canvas_width)
            e_x = int(e_pct * canvas_width)
            
            wf_canvas.create_rectangle(s_x, 0, e_x, canvas_height, fill="#00ffcc", stipple="gray25", outline="")
            wf_canvas.create_line(s_x, 0, s_x, canvas_height, fill="#ff3366", width=2)
            wf_canvas.create_line(e_x, 0, e_x, canvas_height, fill="#ff3366", width=2)

        def update_labels(_=None):
            s = start_slider.get()
            e = end_slider.get()
            if s >= e:
                s = max(0.0, e - 0.1)
                start_slider.set(s)
            lbl_start.configure(text=f"Start Time: {s:.2f}s")
            lbl_end.configure(text=f"End Time: {e:.2f}s (Selection: {max(0.0, e - s):.2f}s)")
            redraw_waveform()

        start_slider.configure(command=update_labels)
        end_slider.configure(command=update_labels)
        update_labels()

        def update_playback_needle():
            if not playback_state["playing"]:
                return
            elapsed = time.time() - playback_state["start_time"]
            if elapsed > playback_state["duration"]:
                playback_state["playing"] = False
                redraw_waveform()
                return
                
            curr_sec = start_slider.get() + elapsed
            pct = curr_sec / total_duration if total_duration > 0 else 0
            needle_x = int(pct * canvas_width)
            
            redraw_waveform()
            wf_canvas.create_line(needle_x, 0, needle_x, canvas_height, fill="#ffffff", width=2)
            trim_win.after(20, update_playback_needle)

        def preview_clip():
            s_sec = start_slider.get()
            e_sec = end_slider.get()
            s_frame = int(s_sec * sr)
            e_frame = int(e_sec * sr)
            clip = data[s_frame:e_frame]
            
            sd.stop()
            sd.play(clip, samplerate=sr)
            
            playback_state["playing"] = True
            playback_state["start_time"] = time.time()
            playback_state["duration"] = e_sec - s_sec
            update_playback_needle()

        def save_trimmed_clip():
            sd.stop()
            playback_state["playing"] = False
            s_frame = int(start_slider.get() * sr)
            e_frame = int(end_slider.get() * sr)
            clip = data[s_frame:e_frame]
            
            base_name = os.path.splitext(os.path.basename(filepath))[0]
            if base_name.startswith("temp_rec_"):
                base_name = "recording"
            default_trimmed_name = f"{base_name}_{int(start_slider.get())}s_{int(end_slider.get())}s.wav"
            
            # Single File Save Picker for Final Trimmed Audio Clip
            save_filepath = filedialog.asksaveasfilename(
                title="Save Audio Clip",
                initialdir=TRIM_DIR,
                initialfile=default_trimmed_name,
                defaultextension=".wav",
                filetypes=[("WAV Audio Files", "*.wav"), ("All Files", "*.*")]
            )
            
            if not save_filepath:
                return
                
            sf.write(save_filepath, clip, sr)
            
            # Clean up temp file if the source was a temporary recording
            if "temp_rec_" in os.path.basename(filepath) and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception: pass

            target_key = key
            if not target_key:
                target_key = "grid_0"
                for k, p in self.pads.items():
                    if not p.get("file_path") and not p.get("is_stop_btn"):
                        target_key = k
                        break
                        
            self.load_sound_to_pad(target_key, save_filepath)
            self.status_msg_label.configure(text=f"Clip saved & assigned to {target_key}!", text_color="#00ffcc")
            trim_win.destroy()

        btn_preview = ctk.CTkButton(trim_win, text="▶ Preview Selection", fg_color="#00ffcc", text_color="black", command=preview_clip)
        btn_preview.pack(pady=8)

        btn_save = ctk.CTkButton(trim_win, text="✂ Save & Assign to Pad", fg_color="#1f538d", command=save_trimmed_clip)
        btn_save.pack(pady=4)

    def on_pad_click(self, key):
        # Interactive Pad Selection Mode after Recording System Audio!
        if self.selecting_pad_mode:
            self.selecting_pad_mode = False
            pending_path = self.pending_recording_filepath
            self.pending_recording_filepath = None
            self.status_msg_label.configure(text="", text_color="#00ffcc")
            if pending_path and os.path.exists(pending_path):
                self.open_audio_trimmer(key=key, initial_filepath=pending_path)
            return

        pad = self.pads[key]
        if pad["is_stop_btn"]:
            self.stop_all_sounds()
            return
            
        if not pad["file_path"]:
            filepath = filedialog.askopenfilename(title="Select Audio File", filetypes=[("Audio Files", "*.wav *.mp3 *.ogg")])
            if filepath:
                self.load_sound_to_pad(key, filepath)
        else:
            self.play_pad(key)

    def load_sound_to_pad(self, key, filepath):
        pad = self.pads[key]
        pad["file_path"] = filepath
        try:
            snd = pygame.mixer.Sound(filepath)
            eff_vol = pad.get("volume", 1.0) * self.volume
            snd.set_volume(eff_vol)
            pad["sound"] = snd
            
            d_name = self.get_pad_display_name(pad)
            col_name = pad["color_name"]
            pad["button"].configure(text=d_name, fg_color=COLOR_HEX[col_name], text_color="black" if col_name in ["Yellow", "Green"] else "white")
            self.send_pad_midi_light(pad, COLOR_MAP[col_name])
            self.save_config()
        except Exception as e:
            print(f"Error loading sound: {e}")

    def play_pad(self, key):
        pad = self.pads[key]
        if pad["is_stop_btn"]:
            self.stop_all_sounds()
            return
            
        if pad.get("sound"):
            eff_vol = pad.get("volume", 1.0) * self.volume
            
            # 1. Play on Headphones (Monitor)
            pad["sound"].set_volume(eff_vol)
            pad["sound"].play()
            
            # 2. Play on CABLE Input (Broadcast) using WASAPI native sample rate & clip volume!
            if self.broadcast_device_idx is not None:
                try:
                    out_info = sd.query_devices(self.broadcast_device_idx)
                    out_sr = int(out_info['default_samplerate'])
                    arr = pygame.sndarray.array(pad["sound"])
                    
                    mixer_init = pygame.mixer.get_init()
                    in_sr = mixer_init[0] if mixer_init else 44100
                    
                    if eff_vol != 1.0:
                        arr = (arr * eff_vol).astype(arr.dtype)
                        
                    if in_sr != out_sr and len(arr) > 0:
                        num_target = int(len(arr) * (out_sr / float(in_sr)))
                        old_idx = np.linspace(0, 1, len(arr))
                        new_idx = np.linspace(0, 1, num_target)
                        if arr.ndim == 1:
                            arr = np.interp(new_idx, old_idx, arr).astype(arr.dtype)
                        else:
                            resampled = np.zeros((num_target, arr.shape[1]), dtype=arr.dtype)
                            for ch in range(arr.shape[1]):
                                resampled[:, ch] = np.interp(new_idx, old_idx, arr[:, ch])
                            arr = resampled
                            
                    sd.play(arr, samplerate=out_sr, device=self.broadcast_device_idx)
                except Exception as e:
                    print(f"Error playing to broadcast: {e}")

            pad["button"].configure(fg_color="#ffffff", text_color="black")
            self.send_pad_midi_light(pad, COLOR_MAP["Green"])
            
            length_ms = int(pad["sound"].get_length() * 1000)
            self.after(min(length_ms, 3000), lambda k=key: self.reset_pad_ui(k))

    def reset_pad_ui(self, key):
        pad = self.pads[key]
        if pad["is_stop_btn"]:
            pad["button"].configure(text="STOP", fg_color="#aa0000", text_color="white")
            self.send_pad_midi_light(pad, COLOR_MAP["Red"])
        elif pad.get("file_path"):
            col_name = pad["color_name"]
            d_name = self.get_pad_display_name(pad)
            pad["button"].configure(text=d_name, fg_color=COLOR_HEX[col_name], text_color="black" if col_name in ["Yellow", "Green"] else "white")
            self.send_pad_midi_light(pad, COLOR_MAP[col_name])

    def toggle_passthrough(self):
        if self.passthrough_active:
            self.stop_passthrough()
        else:
            self.start_passthrough()
        self.save_config()

    def start_passthrough(self):
        try:
            selected_in = self.input_var.get()
            input_idx = int(selected_in.split(":")[0]) if ":" in selected_in else None
            output_idx = self.broadcast_device_idx
            
            if input_idx is None or output_idx is None:
                self.status_msg_label.configure(text="Select valid input & broadcast devices.", text_color="#ffaa00")
                return

            in_info = sd.query_devices(input_idx)
            out_info = sd.query_devices(output_idx)
            
            in_sr = int(in_info['default_samplerate'])
            in_ch = in_info['max_input_channels']
            
            out_sr = int(out_info['default_samplerate'])
            out_ch = out_info['max_output_channels']
            
            self.passthrough_buffer = np.zeros((0, 1), dtype=np.float32)
            self.passthrough_lock = threading.Lock()

            def in_callback(indata, frames, time_info, status):
                mono = indata.mean(axis=1, keepdims=True) if indata.shape[1] > 1 else indata
                if in_sr != out_sr:
                    num_target = int(len(mono) * (out_sr / in_sr))
                    mono = np.interp(np.linspace(0, 1, num_target), np.linspace(0, 1, len(mono)), mono.ravel()).reshape(-1, 1).astype(np.float32)
                with self.passthrough_lock:
                    self.passthrough_buffer = np.vstack((self.passthrough_buffer, mono))

            def out_callback(outdata, frames, time_info, status):
                req = len(outdata)
                with self.passthrough_lock:
                    if len(self.passthrough_buffer) >= req:
                        chunk = self.passthrough_buffer[:req]
                        self.passthrough_buffer = self.passthrough_buffer[req:]
                    else:
                        chunk = self.passthrough_buffer
                        self.passthrough_buffer = np.zeros((0, 1), dtype=np.float32)
                if len(chunk) > 0:
                    if outdata.shape[1] > 1:
                        outdata[:len(chunk), :] = np.repeat(chunk, outdata.shape[1], axis=1)
                        if len(chunk) < req: outdata[len(chunk):, :] = 0
                    else:
                        outdata[:len(chunk)] = chunk
                        if len(chunk) < req: outdata[len(chunk):] = 0
                else:
                    outdata.fill(0)

            self.in_stream = sd.InputStream(device=input_idx, channels=in_ch, samplerate=in_sr, callback=in_callback)
            self.out_stream = sd.OutputStream(device=output_idx, channels=out_ch, samplerate=out_sr, callback=out_callback)
            
            self.in_stream.start()
            self.out_stream.start()
            
            self.passthrough_active = True
            self.passthrough_btn.configure(text="Mic Passthrough: ON", fg_color="#00ffcc", text_color="black")
            self.status_msg_label.configure(text=f"Mic Passthrough active ({in_info['name']})!", text_color="#00ffcc")
        except Exception as e:
            print(f"Error starting mic passthrough: {e}")
            self.status_msg_label.configure(text=f"Passthrough Error: {e}", text_color="#ff4444")
            self.stop_passthrough()

    def stop_passthrough(self):
        if self.in_stream:
            try:
                self.in_stream.stop()
                self.in_stream.abort()
                self.in_stream.close()
            except Exception: pass
            self.in_stream = None
            
        if self.out_stream:
            try:
                self.out_stream.stop()
                self.out_stream.abort()
                self.out_stream.close()
            except Exception: pass
            self.out_stream = None

        with self.passthrough_lock:
            self.passthrough_buffer = np.zeros((0, 1), dtype=np.float32)
            
        self.passthrough_active = False
        self.passthrough_btn.configure(text="Mic Passthrough: OFF", fg_color="#ff4444", text_color="white")

    def restart_passthrough(self):
        self.stop_passthrough()
        self.start_passthrough()

    def init_midi_system(self):
        try:
            out_id = None
            in_id = None
            for i in range(pygame.midi.get_count()):
                interf, name, is_in, is_out, opened = pygame.midi.get_device_info(i)
                name_str = name.decode('utf-8')
                if "Launchpad" in name_str or "MIDI" in name_str:
                    if is_in == 1: in_id = i
                    if is_out == 1: out_id = i
                        
            if out_id is not None and in_id is not None:
                self.midi_out = pygame.midi.Output(out_id)
                self.midi_in = pygame.midi.Input(in_id)
                self.midi_out.write_short(0xB0, 0, 0)
                info = pygame.midi.get_device_info(out_id)
                self.midi_status_label.configure(text=f"MIDI Connected: {info[1].decode('utf-8')}", text_color="#00ffcc")
                
                self.refresh_all_midi_lights()
            else:
                self.midi_status_label.configure(text="Launchpad not found.", text_color="#ffaa00")
        except Exception as e:
            print(f"MIDI Error: {e}")

    def send_pad_midi_light(self, pad, color_vel):
        if self.midi_out:
            try:
                if pad["midi_type"] == "note":
                    self.midi_out.write_short(0x90, pad["midi_val"], color_vel)
                elif pad["midi_type"] == "cc":
                    self.midi_out.write_short(0xB0, pad["midi_val"], color_vel)
            except Exception:
                pass

    def refresh_all_midi_lights(self):
        for pad in self.pads.values():
            if pad["is_stop_btn"]:
                self.send_pad_midi_light(pad, COLOR_MAP["Red"])
            elif pad.get("file_path"):
                self.send_pad_midi_light(pad, COLOR_MAP[pad["color_name"]])

    def poll_midi(self):
        if self.midi_in and self.midi_in.poll():
            midi_events = self.midi_in.read(10)
            for event in midi_events:
                status, note_or_cc, velocity, _ = event[0]
                
                if status == 0x90 and velocity > 0:
                    for key, pad in self.pads.items():
                        if pad["midi_type"] == "note" and pad["midi_val"] == note_or_cc:
                            self.on_pad_click(key)
                            
                elif status == 0xB0 and velocity > 0:
                    for key, pad in self.pads.items():
                        if pad["midi_type"] == "cc" and pad["midi_val"] == note_or_cc:
                            self.on_pad_click(key)

        self.after(10, self.poll_midi)

    def save_config(self):
        if getattr(self, 'is_loading', True):
            return
            
        config_data = {
            "monitor_device": self.mon_var.get(),
            "broadcast_device": self.broad_var.get(),
            "input_device": self.input_var.get(),
            "passthrough_active": self.passthrough_active,
            "start_minimized": self.start_minimized_var.get(),
            "volume": self.volume,
            "pads": {}
        }
        for key, pad in self.pads.items():
            if pad.get("file_path") or pad.get("is_stop_btn") or pad.get("volume", 1.0) != 1.0 or pad.get("custom_name"):
                config_data["pads"][key] = {
                    "file_path": pad.get("file_path"),
                    "color_name": pad.get("color_name", "Red"),
                    "is_stop_btn": pad.get("is_stop_btn", False),
                    "volume": pad.get("volume", 1.0),
                    "custom_name": pad.get("custom_name")
                }
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(config_data, f, indent=2)
        except Exception as e:
            print(f"Error saving config: {e}")

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r") as f:
                config_data = json.load(f)
                
            if "volume" in config_data:
                self.volume = float(config_data["volume"])
                self.vol_slider.set(self.volume)
                
            if "monitor_device" in config_data:
                saved_mon = config_data["monitor_device"].split(":")[-1].strip()
                for opt in self.mon_menu._values:
                    if saved_mon in opt:
                        self.mon_var.set(opt)
                        self.init_audio_system(opt)
                        break
                        
            if "broadcast_device" in config_data:
                saved_broad = config_data["broadcast_device"].split(":")[-1].strip()
                for opt in self.broad_menu._values:
                    if saved_broad in opt:
                        self.broad_var.set(opt)
                        try:
                            self.broadcast_device_idx = int(opt.split(":")[0])
                        except Exception: pass
                        break

            if "input_device" in config_data:
                saved_in = config_data["input_device"].split(":")[-1].strip()
                for opt in self.input_menu._values:
                    if saved_in in opt:
                        self.input_var.set(opt)
                        break

            if "passthrough_active" in config_data:
                self.passthrough_active = config_data["passthrough_active"]

            if "start_minimized" in config_data:
                self.start_minimized_var.set(bool(config_data["start_minimized"]))

            pads_data = config_data.get("pads", {})
            for key, data in pads_data.items():
                if key in self.pads:
                    pad = self.pads[key]
                    pad["is_stop_btn"] = data.get("is_stop_btn", False)
                    pad["color_name"] = data.get("color_name", "Red")
                    pad["volume"] = float(data.get("volume", 1.0))
                    pad["custom_name"] = data.get("custom_name")
                    
                    if pad["is_stop_btn"]:
                        pad["button"].configure(text="STOP", fg_color="#aa0000", text_color="white")
                    elif data.get("file_path") and os.path.exists(data["file_path"]):
                        pad["file_path"] = data["file_path"]
                        try:
                            snd = pygame.mixer.Sound(data["file_path"])
                            eff_vol = pad["volume"] * self.volume
                            snd.set_volume(eff_vol)
                            pad["sound"] = snd
                            
                            d_name = self.get_pad_display_name(pad)
                            col_name = pad["color_name"]
                            pad["button"].configure(
                                text=d_name,
                                fg_color=COLOR_HEX[col_name],
                                text_color="black" if col_name in ["Yellow", "Green"] else "white"
                            )
                        except Exception as e:
                            print(f"Error restoring pad {key}: {e}")

            self.refresh_all_midi_lights()
        except Exception as e:
            print(f"Error loading config: {e}")

    def on_closing(self):
        self.stop_all_sounds()
        self.save_config()
        if self.midi_out:
            try:
                self.midi_out.write_short(0xB0, 0, 0)
                self.midi_out.close()
            except Exception: pass
        if self.midi_in:
            try: self.midi_in.close()
            except Exception: pass
        pygame.midi.quit()
        pygame.quit()
        self.destroy()

if __name__ == "__main__":
    app = SoundboardApp()
    app.mainloop()
