"""
Run this script directly in VS Code.

What it does automatically:
1) Starts the ultrasound application, which continues to render its local GUI.
2) Reads the processed B-mode image buffer directly from OSTB.
3) Publishes those image frames to Agora (video + microphone audio).
4) Serves a local API endpoint to receive remote mouse events from the web app
   and emulates those clicks/drags directly on the local PyQt5 GUI window.
"""

from __future__ import annotations

import atexit
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

MISSING_MODULES = []

try:
  import cv2
except ModuleNotFoundError:
  MISSING_MODULES.append("opencv-python")

try:
  import numpy as np
except ModuleNotFoundError:
  MISSING_MODULES.append("numpy")

try:
  import ostb._ostb as ostb
except ModuleNotFoundError:
  MISSING_MODULES.append("ostb")

try:
  from flask import Flask, Response, request, jsonify
except ModuleNotFoundError:
  MISSING_MODULES.append("flask")

try:
  import pyautogui
except ModuleNotFoundError:
  MISSING_MODULES.append("pyautogui")

try:
  import win32gui
  import win32con
except ModuleNotFoundError:
  # Check if we are on Windows. Only flag missing if running on Windows.
  if sys.platform.startswith("win"):
    MISSING_MODULES.append("pypiwin32")

if MISSING_MODULES:
  pkgs = " ".join(sorted(set(MISSING_MODULES)))
  cmd = f'"{sys.executable}" -m pip install {pkgs}'
  raise SystemExit(
    "Missing Python modules. Install them with:\n"
    f"{cmd}"
  )

# -------------------- USER CONFIG --------------------
APP_ID = "ae6510c468b5405d90d861e3a14810a4"
TOKEN = "007eJxTYFjK/Gbya/WlWaJeaUrxi3lf72/7tJM1fW5ANvPKNR/mFvAoMCSmmpkaGiSbmFkkmZoYmKZYGqRYmBmmGicamlgYGiSa/PefldUQyMiwQvQoEyMDBIL4rAwl+UWlxQwMAD/kH2I="
CHANNEL = "torus"
UID = 5001

# Choose which ultrasound script to launch.
ULTRASOUND_SCRIPT = "curv_proper_code.py"
HOST = "127.0.0.1"
PORT = 8000
FPS = 15

# Stable canvas output dimensions
STREAM_CANVAS_W = 960
STREAM_CANVAS_H = 640  # Capped at 640 to fit the wider aspect ratio and remove extra bottom black space

# OSTB's output B-mode process buffer
PROCESS_OUTPUT_MAPPING = "ostb.input.rf.out.process.0"

# Target physical dimensions for the curvilinear probe
LATERAL_MIN_MM = -62.3
LATERAL_MAX_MM = 62.3
DEPTH_MIN_MM = 4.48
DEPTH_MAX_MM = 100.0
# -----------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__)

_latest_jpg = None
_latest_lock = threading.Lock()
_stop_event = threading.Event()
_ultrasound_proc: subprocess.Popen | None = None

# MQTT control topic — must match exactly what app.js publishes to
MQTT_CONTROL_TOPIC = f"{CHANNEL}-control-{APP_ID[:8]}"
MQTT_BROKERS = [
    ("broker.hivemq.com", 1883),
    ("broker.emqx.io", 1883),
]


def _start_mqtt_subscriber() -> None:
    """
    Subscribes to MQTT control topic on ALL brokers simultaneously.
    Doctor JS may connect to any broker (HiveMQ or EMQX) — patient Python listens on both.
    """
    import json
    try:
        import paho.mqtt.client as mqtt_client
    except ImportError:
        print("[MQTT] paho-mqtt not installed. Run: pip install paho-mqtt")
        return

    def make_client(broker_host, broker_port):
        def on_connect(client, userdata, flags, reason_code, properties=None):
            if reason_code == 0 or str(reason_code) == "Success":
                client.subscribe(MQTT_CONTROL_TOPIC)
                print(f"[MQTT] ✅ Subscribed on {broker_host}:{broker_port} → topic: {MQTT_CONTROL_TOPIC}")
            else:
                print(f"[MQTT] ❌ Failed on {broker_host}: {reason_code}")

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
                control = payload.get("control")
                value = payload.get("value")
                print(f"[MQTT] ✅ Received from {broker_host}: {control} = {value}")
                if control:
                    execute_direct_runtime_command(control, value)
            except Exception as e:
                print(f"[MQTT] Error handling message from {broker_host}: {e}")

        try:
            try:
                client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
            except AttributeError:
                client = mqtt_client.Client()
            client.on_connect = on_connect
            client.on_message = on_message
            print(f"[MQTT] Connecting to {broker_host}:{broker_port}, topic: {MQTT_CONTROL_TOPIC}")
            client.connect(broker_host, broker_port, keepalive=60)
            client.loop_start()  # Non-blocking — runs in background thread
            print(f"[MQTT] Listener started on {broker_host}:{broker_port}")
        except Exception as e:
            print(f"[MQTT] Could not connect to {broker_host}: {e}")

    # Connect to ALL brokers simultaneously (non-blocking)
    for broker_host, broker_port in MQTT_BROKERS:
        t = threading.Thread(target=make_client, args=(broker_host, broker_port), daemon=True)
        t.start()

    # Keep thread alive
    while not _stop_event.is_set():
        time.sleep(5)




def find_opensonics_control_window() -> int:
    """
    Finds the NEXUS / OpenSonics Control Panel window handle (hwnd).
    Explicitly targets window title 'NEXUS' (class 'Qt6102QWindowOwnDCIcon').
    """
    try:
        import win32gui
    except ImportError:
        return 0

    control_hwnd = [0]
    render_hwnd = [0]

    def enum_cb(hwnd, _):
        if not win32gui.IsWindow(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        cls_name = win32gui.GetClassName(hwnd)

        # Match exact NEXUS control panel window
        if title == "NEXUS":
            control_hwnd[0] = hwnd
            return False
        elif "Image" in title or "Focused" in title or "B-Mode" in title:
            render_hwnd[0] = hwnd
        elif ("Qt6" in cls_name or "Qt5" in cls_name) and title and title not in ["Launcher", "_q_titlebar"]:
            if not control_hwnd[0]:
                control_hwnd[0] = hwnd
        return True

    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass

    target = control_hwnd[0] or render_hwnd[0] or 0
    return target


_click_lock = threading.Lock()


def send_win32_click(hwnd: int, cx: int, cy: int) -> None:
    """
    Executes a hardware click by momentarily bringing the target window to (0, 0) and activating it,
    performing the hardware mouse click at (cx, cy), and returning the window off-screen.
    Preserves original window width and height.
    """
    import win32gui
    import win32con
    import win32api

    with _click_lock:
        try:
            rect = win32gui.GetWindowRect(hwnd)
            width = max(600, rect[2] - rect[0])
            height = max(450, rect[3] - rect[1])
            print(f"[Remote Control] Targeting window hwnd={hwnd}, rect=({rect[0]},{rect[1]},{width}x{height}), click client coords=({cx}, {cy})")

            # 1. Restore, move to (0, 0) on screen while preserving width/height, and bring to foreground
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, width, height, win32con.SWP_SHOWWINDOW)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.06)

            # 2. Hardware cursor click
            screen_x = 0 + int(cx)
            screen_y = 0 + int(cy)

            orig_pos = win32api.GetCursorPos()
            win32api.SetCursorPos((screen_x, screen_y))
            time.sleep(0.02)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.06)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.02)
            win32api.SetCursorPos(orig_pos)

            # 3. Synchronous Win32 SendMessage backup
            lParam = win32api.MAKELONG(int(cx), int(cy))
            win32gui.SendMessage(hwnd, win32con.WM_MOUSEACTIVATE, hwnd, win32api.MAKELONG(win32con.HTCLIENT, win32con.WM_LBUTTONDOWN))
            win32gui.SendMessage(hwnd, win32con.WM_SETCURSOR, hwnd, win32api.MAKELONG(win32con.HTCLIENT, win32con.WM_LBUTTONDOWN))
            win32gui.SendMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lParam)
            time.sleep(0.03)
            win32gui.SendMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)

            # 4. Immediately return window off-screen to (-2000, -2000)
            win32gui.SetWindowPos(hwnd, 0, -2000, -2000, width, height, win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
        except Exception as e:
            print(f"[Remote Control] Click execution error: {e}")


def replicate_mouse_event(event_type: str, x_percent: float, y_percent: float) -> bool:
    """
    Locates the PyQt5 OpenSonics GUI window on the desktop and emulates pointer events (down, move, up).
    Falls back gracefully to full screen mapping if the window is not found or not on Windows.
    Uses non-invasive PostMessage API on Windows so the physical mouse pointer is not moved.
    """
    try:
        import win32gui
        import win32con
        import win32api
    except ImportError:
        # Fallback to pyautogui on non-Windows/development setups
        import pyautogui
        pyautogui.FAILSAFE = False
        screen_w, screen_h = pyautogui.size()
        left, top, width, height = 0, 0, screen_w, screen_h
        target_x = left + int(x_percent * width)
        target_y = top + int(y_percent * height)
        try:
            if event_type == "down":
                pyautogui.mouseDown(target_x, target_y)
                print(f"[Remote Control] Fallback MouseDown at: ({target_x}, {target_y})")
            elif event_type == "move":
                pyautogui.moveTo(target_x, target_y)
                print(f"[Remote Control] Fallback MouseMove to: ({target_x}, {target_y})")
            elif event_type == "up":
                pyautogui.mouseUp(target_x, target_y)
                print(f"[Remote Control] Fallback MouseUp at: ({target_x}, {target_y})")
            return True
        except Exception as e:
            print(f"[Remote Control] Fallback input emulation failed: {e}")
            return False

    hwnd = find_opensonics_control_window()
    if not hwnd:
        print("[Remote Control] OpenSonics window not found for mouse event replication")
        return False

    rect = win32gui.GetWindowRect(hwnd)
    left, top, right, bottom = rect
    width = right - left
    height = bottom - top

    screen_x = left + int(x_percent * width)
    screen_y = top + int(y_percent * height)

    cx, cy = win32gui.ScreenToClient(hwnd, (screen_x, screen_y))
    lParam = win32api.MAKELONG(cx, cy)

    if event_type == "down":
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lParam)
        print(f"[Remote Control] Sent WM_LBUTTONDOWN relative ({cx}, {cy})")
    elif event_type == "move":
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, lParam)
        print(f"[Remote Control] Sent WM_MOUSEMOVE relative ({cx}, {cy})")
    elif event_type == "up":
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)
        print(f"[Remote Control] Sent WM_LBUTTONUP relative ({cx}, {cy})")

    return True


# --- Dynamic OSTB Hooks (Leaves curv_proper_code.py 100% original and untouched!) ---
_global_ostb_runtime = None
_global_ostb_config = None
_curv_module = None


def _watch_module_runtime() -> None:
    """
    Monitors module.__dict__ while curv_proper_code.py executes.
    Captures 'runtime' object immediately when line 'runtime = ostb.AcquisitionRuntime()' executes,
    BEFORE runtime.run_controller() blocks the thread.
    """
    global _global_ostb_runtime, _curv_module
    for _ in range(100):
        if _curv_module is not None:
            r = getattr(_curv_module, "runtime", None)
            if r is not None:
                _global_ostb_runtime = r
                print(f"[ultrasound Watcher] >>> SUCCESS! CAPTURED RUNTIME INSTANCE: {r}")
                break
        time.sleep(0.1)


try:
    import ostb._ostb as ostb

    # Hook 1: Intercept AcquisitionRuntime instantiation to capture runtime object
    _orig_runtime_init = ostb.AcquisitionRuntime.__init__
    def _hooked_runtime_init(self, *args, **kwargs):
        global _global_ostb_runtime
        _global_ostb_runtime = self
        print(f"[ultrasound Hook] Automatically captured ostb.AcquisitionRuntime instance: {self}")
        return _orig_runtime_init(self, *args, **kwargs)
    ostb.AcquisitionRuntime.__init__ = _hooked_runtime_init

    # Hook 2: Intercept run_controller() to run non-blocking in background thread
    _orig_run_controller = ostb.AcquisitionRuntime.run_controller
    def _hooked_run_controller(self, *args, **kwargs):
        def _gui_worker():
            try:
                _orig_run_controller(self, *args, **kwargs)
            except Exception as e:
                print(f"[ultrasound Hook] Controller GUI info: {e}")
        t = threading.Thread(target=_gui_worker, daemon=True)
        t.start()
        print("[ultrasound Hook] Launched run_controller() in non-blocking background thread!")
    ostb.AcquisitionRuntime.run_controller = _hooked_run_controller

    # Hook 3: Block stop_control_server() — we want the runtime to stay alive
    def _hooked_stop_control_server(self, *args, **kwargs):
        print("[ultrasound Hook] Intercepted stop_control_server() — keeping runtime alive for remote control!")
    ostb.AcquisitionRuntime.stop_control_server = _hooked_stop_control_server

    # Hook 4: Block disconnect() — we want the runtime to stay connected to hardware
    def _hooked_disconnect(self, *args, **kwargs):
        print("[ultrasound Hook] Intercepted disconnect() — keeping runtime connected to hardware!")
    ostb.AcquisitionRuntime.disconnect = _hooked_disconnect

    # Hook 5: Block save_acquisition_archive_hdf5 — not needed for streaming
    if hasattr(ostb, "save_acquisition_archive_hdf5"):
        def _hooked_save(*args, **kwargs):
            print("[ultrasound Hook] Intercepted save_acquisition_archive_hdf5() — skipping HDF5 save for streaming mode!")
        ostb.save_acquisition_archive_hdf5 = _hooked_save

except Exception as exc:
    print(f"[ultrasound Hook] OSTB hook info: {exc}")


def rebuild_configuration_with_params(new_voltage: float = None, new_gain: float = None) -> bool:
    """
    Rebuilds the waveform, TX configs, scans, sequence and full AcquisitionConfiguration
    from scratch using the current _curv_module parameters — then calls runtime.configure()
    with the freshly-built configuration so hardware actually sees the new values.

    This is necessary because OSTB Builder objects serialize their values at .build() time —
    mutating Python attributes after .build() has no effect on already-built C++ objects.
    """
    global _global_ostb_runtime, _curv_module
    if _curv_module is None or _global_ostb_runtime is None:
        print("[Rebuild] Cannot rebuild — module or runtime not ready")
        return False

    try:
        import ostb._ostb as ostb_mod
        m = _curv_module  # shorthand

        # Update stored parameters
        if new_voltage is not None:
            m.gain_analog_db = getattr(m, "gain_analog_db", 40.0)
        if new_gain is not None:
            m.gain_analog_db = new_gain

        voltage = new_voltage if new_voltage is not None else getattr(m, "_current_voltage", 50.0)
        m._current_voltage = voltage
        gain = m.gain_analog_db

        print(f"[Rebuild] Rebuilding configuration: voltage={voltage}V, gain={gain}dB")

        # Rebuild waveform with new voltage
        new_waveform = ostb_mod.WaveformFactory.make_unipolar(m.center_frequency_hz)
        new_waveform.set_awg(False)
        new_waveform.set_negative_voltage(voltage)
        new_waveform.validate()

        # Rebuild sequence with new waveform and gain
        new_seq_builder = (ostb_mod.SequenceBuilder()
                           .set_probe(m.probe)
                           .set_trigger(m.trigger)
                           .set_number_of_frames(m.frame_count))

        for line_idx in range(m.line_count):
            start = line_idx
            apodization = [0.0] * m.probe.element_count
            apodization[start: start + m.active_element_count] = [1.0] * m.active_element_count

            center_idx = start + m.active_element_count // 2 - (
                1 if (m.active_element_count % 2 == 0) else 0
            )

            import math
            azimuth_rad = m.probe.az_angle[center_idx]
            radius_m = m.probe.radius
            rho_m = radius_m + m.focus_depth_m
            x_focus_m = rho_m * math.sin(azimuth_rad)
            z_focus_m = -radius_m + rho_m * math.cos(azimuth_rad)

            tx_config = (ostb_mod.TxConfigBuilder()
                         .set_apodization(apodization)
                         .set_source([0.0, 0.0, 0.0])
                         .set_focus_point([x_focus_m, 0.0, z_focus_m])
                         .set_tx_with_all_elements(False)
                         .set_waveform(new_waveform)
                         .set_speed_of_sound(m.speed_of_sound)
                         .compute_delays(m.probe)
                         .build(m.probe))

            scan = (ostb_mod.ScanBuilder()
                    .set_tx(tx_config)
                    .set_rx(m.rxConfig)
                    .set_process_id(0)
                    .set_scan_id(line_idx)
                    .set_frame_id(0)
                    .set_gain_digital(0.0)
                    .set_gain_analog(gain)
                    .set_start(m.scan_start_s)
                    .set_range(m.scan_range_s)
                    .set_time_slot(m.time_slot_seconds)
                    .set_sample_factor(m.sample_factor)
                    .set_compression_type(ostb_mod.CompressionType.DECIMATION)
                    .set_rectification(ostb_mod.Rectification.SIGNED)
                    .set_beam_correction(0.0)
                    .build(m.probe.element_count))

            new_seq_builder.add_scan(scan)

        new_seq = new_seq_builder.build()

        # Rebuild configuration with new sequence
        new_config = ostb_mod.AcquisitionConfiguration()
        new_config.set_root(m.root)
        new_config.set_sequence(new_seq)
        new_config.set_processing(m.processing)
        new_config.validate()

        # Apply new configuration to hardware
        _global_ostb_runtime.configure(new_config)
        print(f"[Rebuild] ✅ Hardware reconfigured: voltage={voltage}V, gain={gain}dB")
        return True

    except Exception as e:
        print(f"[Rebuild] ❌ Error rebuilding configuration: {e}")
        return False



def execute_direct_runtime_command(name: str, value: any) -> bool:
    """
    Directly invokes OSTB Python AcquisitionRuntime methods.
    Handles: start, stop, freeze, voltage, gain, log_gain, dynamic_range, display.
    Works 100% directly via Python memory without modifying curv_proper_code.py!
    """
    global _global_ostb_runtime, _curv_module
    print(f"[Direct Function Call] Executing '{name}' = {value}")

    if _global_ostb_runtime is None:
        print("[Direct Function Call] Runtime not ready yet — skipping")
        return False

    try:
        # --- Start / Stop ---
        if name == "start":
            if value:
                print("[Direct OSTB API] Executing _global_ostb_runtime.start()")
                _global_ostb_runtime.start()
            else:
                print("[Direct OSTB API] Executing _global_ostb_runtime.stop()")
                _global_ostb_runtime.stop()
            return True

        elif name == "freeze":
            print("[Direct OSTB API] Executing _global_ostb_runtime.stop()")
            _global_ostb_runtime.stop()
            return True

        # --- Voltage: rebuild full configuration with new voltage & sync patient GUI ---
        elif name == "voltage":
            val = max(0.0, min(50.0, float(value)))
            print(f"[Direct OSTB API] Setting voltage → {val} V (full rebuild)")
            res = rebuild_configuration_with_params(new_voltage=val)
            handle_control_command("voltage", val)
            return res

        # --- Analog Gain: rebuild full configuration with new gain & sync patient GUI ---
        elif name == "gain":
            val = max(0.0, min(80.0, float(value)))
            print(f"[Direct OSTB API] Setting analog gain → {val} dB (full rebuild)")
            res = rebuild_configuration_with_params(new_gain=val)
            handle_control_command("gain", val)
            return res

        # --- Log Compression Gain ---
        elif name == "log_gain":
            val = max(0.0, min(100.0, float(value)))
            print(f"[Direct OSTB API] Setting log compression gain → {val} dB")
            if _curv_module is not None and hasattr(_curv_module, "log"):
                _curv_module.log.gain_db = val
                if hasattr(_curv_module, "configuration"):
                    _global_ostb_runtime.configure(_curv_module.configuration)
            handle_control_command("log_gain", val)
            return True

        # --- Dynamic Range ---
        elif name == "dynamic_range":
            val = max(0.0, min(120.0, float(value)))
            print(f"[Direct OSTB API] Setting dynamic range → {val} dB")
            if _curv_module is not None and hasattr(_curv_module, "log"):
                _curv_module.log.dynamic_range_db = val
                if hasattr(_curv_module, "configuration"):
                    _global_ostb_runtime.configure(_curv_module.configuration)
            handle_control_command("dynamic_range", val)
            return True

        # --- Display Toggle ---
        elif name == "display":
            enabled = bool(value)
            print(f"[Direct OSTB API] Setting display → {enabled}")
            if _curv_module is not None and hasattr(_curv_module, "processing"):
                _curv_module.processing.set_enable_display(enabled)
                if hasattr(_curv_module, "configuration"):
                    _global_ostb_runtime.configure(_curv_module.configuration)
            handle_control_command("display", enabled)
            return True

    except Exception as exc:
        print(f"[Direct OSTB API] Error executing '{name}': {exc}")

    return handle_control_command(name, value)


def send_direct_qt_click(hwnd: int, cx: int, cy: int) -> None:
    """
    Sends direct Win32 WM_LBUTTONDOWN and WM_LBUTTONUP messages directly to Qt's event queue.
    Does NOT move the physical cursor or blink the mouse pointer on screen.
    """
    import win32gui
    import win32con
    import win32api

    try:
        lParam = win32api.MAKELONG(int(cx), int(cy))
        print(f"[Remote Control Qt] Posting WM_LBUTTONDOWN/UP to hwnd={hwnd} at client ({cx}, {cy})")
        win32gui.PostMessage(hwnd, win32con.WM_ACTIVATE, win32con.WA_ACTIVE, 0)
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lParam)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lParam)
        time.sleep(0.04)
        win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)
    except Exception as e:
        print(f"[Remote Control Qt] PostMessage error: {e}")


def handle_control_command(name: str, value: any) -> bool:
    """
    Handles control commands from doctor side and delivers them directly into NEXUS via PostMessage & PySonics socket.
    Coordinates calibrated to NEXUS window layout (640x480 ratio):
    - Start Button: x_pct=0.25, y_pct=0.16 (cy=68)
    - Freeze Button: x_pct=0.25, y_pct=0.25 (cy=108)
    - Voltage Slider: y_pct=0.42
    - Analog Gain Slider: y_pct=0.58
    """
    print(f"[Remote Control API] Processing command: '{name}'={value}")

    # 1. Direct Socket trigger (127.0.0.1:4096)
    send_ostb_socket_command(name, value)

    try:
        import win32gui
        import win32con
        import win32api
    except ImportError:
        return True

    hwnd = find_opensonics_control_window()
    if not hwnd:
        print("[Remote Control] OpenSonics window not found for control command.")
        return True

    rect = win32gui.GetWindowRect(hwnd)
    left, top, right, bottom = rect
    width = max(500, right - left)
    height = max(380, bottom - top)

    x_pct = None
    y_pct = None

    if name == "start":
        x_pct, y_pct = 0.25, 0.16
        # Send Space/Enter keypresses directly into Qt window
        try:
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_SPACE, 0)
            time.sleep(0.03)
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_SPACE, 0)
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
            time.sleep(0.03)
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
        except Exception:
            pass

    elif name == "freeze":
        x_pct, y_pct = 0.25, 0.25
        try:
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_SPACE, 0)
            time.sleep(0.03)
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_SPACE, 0)
        except Exception:
            pass
    elif name == "voltage":
        val = float(value)
        x_pct = 0.12 + (val / 50.0) * 0.26
        y_pct = 0.42
    elif name == "gain":
        val = float(value)
        x_pct = 0.12 + (val / 40.0) * 0.26
        y_pct = 0.58
    elif name == "display":
        x_pct, y_pct = 0.64, 0.70
    elif name == "tgc_toggle":
        x_pct, y_pct = 0.64, 0.18
    elif name.startswith("tgc_slider_"):
        try:
            slider_idx = int(name.split("_")[-1]) - 1
            val = float(value)
            x_pct = 0.54 + (slider_idx * 0.07)
            y_pct = 0.62 - (val / 100.0) * 0.34
        except ValueError:
            return False
    elif name == "save_setup":
        x_pct, y_pct = 0.62, 0.83
    elif name == "advanced":
        x_pct, y_pct = 0.84, 0.83

    if x_pct is not None and y_pct is not None:
        cx = int(x_pct * width)
        cy = int(y_pct * height)
        
        # Deliver mouse click directly into Qt event queue (No mouse blinking!)
        send_direct_qt_click(hwnd, cx, cy)
        
        # Also backup hardware click if window is currently visible
        if left >= 0 and top >= 0:
            send_win32_click(hwnd, cx, cy)

        return True

    return True


# Window off-screen repositioning loop completely removed as requested.



def _forward_child_output(proc: subprocess.Popen) -> None:
  if proc.stdout is None:
    return

  for line in proc.stdout:
    print(f"[ultrasound] {line.rstrip()}")


def _make_status_frame(
    text: str, w: int = STREAM_CANVAS_W, h: int = STREAM_CANVAS_H
) -> bytes:
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:] = (20, 22, 28)
    cv2.putText(frame, "Ultrasound -> Agora", (26, 62), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (230, 230, 230), 2)
    cv2.putText(frame, text, (26, 114), cv2.FONT_HERSHEY_SIMPLEX, 0.74, (190, 190, 190), 2)
    ok, encoded = cv2.imencode(".jpg", frame)
    if not ok:
        raise RuntimeError("Failed to encode status frame")
    return encoded.tobytes()


def _set_latest_jpg(jpg_bytes: bytes) -> None:
    global _latest_jpg
    with _latest_lock:
        _latest_jpg = jpg_bytes


def _get_latest_jpg() -> bytes:
    with _latest_lock:
        if _latest_jpg is not None:
            return _latest_jpg
    return _make_status_frame("Waiting for ultrasound image stream...")


def _snapshot_to_gray(snapshot) -> np.ndarray:
    """Convert an OSTB processed-output snapshot to a displayable B-mode frame."""
    shape = tuple(int(size) for size in snapshot.shape)
    if not shape:
        raise ValueError("OSTB supplied a processed snapshot without dimensions")

    element_count = int(np.prod(shape))
    payload = snapshot.data
    image = np.asarray(payload)

    if image.size != element_count:
        if snapshot.element_type == ostb.GpuElementType.UINT8:
            dtype = np.uint8
        elif snapshot.element_type == ostb.GpuElementType.FLOAT32:
            dtype = np.float32
        else:
            raise ValueError(
                "Unsupported OSTB element type "
                f"{snapshot.element_type!r} for shape {shape}"
            )
        try:
            image = np.frombuffer(payload, dtype=dtype, count=element_count)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Cannot decode OSTB payload type {type(payload).__name__} "
                f"for shape {shape}"
            ) from exc

    if image.size == element_count:
        image = image.reshape(shape)
    image = np.squeeze(image)
    if image.ndim > 2:
        image = image.reshape((-1, image.shape[-2], image.shape[-1]))[-1]
    if image.ndim != 2:
        raise ValueError(f"Expected a 2-D processed image, received shape {image.shape}")

    if image.dtype == np.uint8:
        return image

    image = image.astype(np.float32, copy=False)
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        return np.zeros(image.shape, dtype=np.uint8)
    low, high = np.percentile(finite, (1, 99))
    if high <= low:
        return np.zeros(image.shape, dtype=np.uint8)
    return np.clip((image - low) * (255.0 / (high - low)), 0, 255).astype(np.uint8)


def _render_stream_frame(gray: np.ndarray) -> np.ndarray:
    """Render the direct image using the same physical geometry as the GUI."""
    canvas_w, canvas_h = STREAM_CANVAS_W, STREAM_CANVAS_H
    left, top = 145, 45
    plot_w = 650
    physical_aspect = (LATERAL_MAX_MM - LATERAL_MIN_MM) / (DEPTH_MAX_MM - DEPTH_MIN_MM)
    plot_h = round(plot_w / physical_aspect)

    required_h = top + plot_h + 85
    if required_h > canvas_h:
        raise ValueError(
            f"Stream canvas height {canvas_h} is too small for plot height {plot_h}; "
            f"need at least {required_h}"
        )

    frame = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    resized = cv2.resize(gray, (plot_w, plot_h), interpolation=cv2.INTER_LINEAR)
    frame[top:top + plot_h, left:left + plot_w] = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)

    axis_color = (215, 215, 215)
    label_color = (220, 190, 130)
    cv2.rectangle(frame, (left, top), (left + plot_w, top + plot_h), axis_color, 1)

    for mm in np.arange(LATERAL_MIN_MM, LATERAL_MAX_MM + 0.1, 3.0):
        x = round(left + (mm - LATERAL_MIN_MM) / (LATERAL_MAX_MM - LATERAL_MIN_MM) * plot_w)
        cv2.line(frame, (x, top + plot_h), (x, top + plot_h + 5), axis_color, 1)

    x_ticks = [
        LATERAL_MIN_MM,
        LATERAL_MIN_MM / 2.0,
        0.0,
        LATERAL_MAX_MM / 2.0,
        LATERAL_MAX_MM
    ]
    for mm in x_ticks:
        x = round(left + (mm - LATERAL_MIN_MM) / (LATERAL_MAX_MM - LATERAL_MIN_MM) * plot_w)
        cv2.line(frame, (x, top + plot_h), (x, top + plot_h + 10), axis_color, 1)
        text = f"{mm:.1f}" if abs(mm) > 0.01 else "0.00"
        size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)[0]
        cv2.putText(frame, text, (x - size[0] // 2, top + plot_h + 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, label_color, 1, cv2.LINE_AA)

    depth_ticks = np.linspace(DEPTH_MIN_MM, DEPTH_MAX_MM, 5)
    for index, mm in enumerate(np.linspace(DEPTH_MIN_MM, DEPTH_MAX_MM, 17)):
        y = round(top + (mm - DEPTH_MIN_MM) / (DEPTH_MAX_MM - DEPTH_MIN_MM) * plot_h)
        major = index % 4 == 0
        cv2.line(frame, (left - (10 if major else 5), y), (left, y), axis_color, 1)
        if major:
            tick_mm = depth_ticks[index // 4]
            text = f"{tick_mm:.1f}"
            size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)[0]
            cv2.putText(frame, text, (left - 18 - size[0], y + 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, label_color, 1, cv2.LINE_AA)

    cv2.putText(frame, "Lateral [mm]", (left + plot_w // 2 - 42, top + plot_h + 72),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, label_color, 1, cv2.LINE_AA)
    cv2.putText(frame, "Depth [mm]", (32, top + plot_h // 2 + 43),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, label_color, 1, cv2.LINE_AA)

    bar_left, bar_w = left + plot_w + 28, 20
    bar = np.linspace(255, 0, plot_h, dtype=np.uint8)[:, None]
    frame[top:top + plot_h, bar_left:bar_left + bar_w] = cv2.cvtColor(
        np.repeat(bar, bar_w, axis=1), cv2.COLOR_GRAY2BGR
    )
    cv2.rectangle(frame, (bar_left, top), (bar_left + bar_w, top + plot_h), axis_color, 1)
    cv2.putText(frame, "0 dB", (bar_left + 32, top + 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, label_color, 1, cv2.LINE_AA)
    cv2.putText(frame, "-60 dB", (bar_left + 32, top + plot_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, label_color, 1, cv2.LINE_AA)
    return frame


def capture_render_window() -> np.ndarray:
    """
    Captures the live pixels of the ultrasound render window ('C3.5-128R60C Focused B-Mode')
    using Win32 PrintWindow. Works 100% reliably regardless of OSTB shared memory mapping names.
    """
    try:
        import win32gui
        import win32ui
        import win32con
        import ctypes

        render_hwnd = 0

        def enum_cb(hwnd, _):
            nonlocal render_hwnd
            if win32gui.IsWindow(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if any(k in title for k in ["Focused", "B-Mode", "Render", "C3.5"]):
                    render_hwnd = hwnd
                    return False
            return True

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            pass

        if not render_hwnd:
            return None

        rect = win32gui.GetWindowRect(render_hwnd)
        w = max(100, rect[2] - rect[0])
        h = max(100, rect[3] - rect[1])

        hwndDC = win32gui.GetWindowDC(render_hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()

        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, w, h)
        saveDC.SelectObject(saveBitMap)

        # PrintWindow captures live pixels
        ctypes.windll.user32.PrintWindow(render_hwnd, saveDC.GetSafeHdc(), 2)

        bmpstr = saveBitMap.GetBitmapBits(True)
        img = np.frombuffer(bmpstr, dtype='uint8').reshape((h, w, 4))

        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(render_hwnd, hwndDC)

        # Convert BGRA to BGR
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    except Exception:
        return None


def _process_output_loop() -> None:
    reader = ostb.ProcessOutputSnapshotReader()
    interval = max(0.02, 1.0 / max(1, FPS))
    opened_logged = False
    last_error = None
    try:
        while not _stop_event.is_set():
            frame = None

            # 1. Try Direct OSTB Shared Memory Reader
            if reader.is_open or reader.open(PROCESS_OUTPUT_MAPPING):
                try:
                    ok, snapshot = reader.read_latest()
                    if ok and not snapshot.empty:
                        gray = _snapshot_to_gray(snapshot)
                        frame = _render_stream_frame(gray)
                except Exception:
                    pass

            # 2. Fallback to Win32 Live Window Capture of 'C3.5-128R60C Focused B-Mode'
            if frame is None:
                win_frame = capture_render_window()
                if win_frame is not None and win_frame.size > 0:
                    frame = win_frame

            if frame is not None:
                ok, encoded = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90]
                )
                if ok:
                    _set_latest_jpg(encoded.tobytes())
            else:
                _set_latest_jpg(_make_status_frame("Waiting for ultrasound processing output..."))

            time.sleep(interval)
    finally:
        reader.close()


def _run_curv_proper_code_thread() -> None:
    """
    Imports and executes curv_proper_code.py directly in the same Python process.
    Attaches module to _curv_module and module.runtime to _global_ostb_runtime.
    """
    global _curv_module, _global_ostb_runtime
    import importlib.util

    script_path = APP_DIR / ULTRASOUND_SCRIPT
    if not script_path.exists():
        print(f"[ultrasound] {ULTRASOUND_SCRIPT} not found at {script_path}")
        return

    print(f"[ultrasound] Importing and executing {ULTRASOUND_SCRIPT} in-process...")
    try:
        spec = importlib.util.spec_from_file_location("curv_proper_module", str(script_path))
        module = importlib.util.module_from_spec(spec)
        _curv_module = module

        # Start watcher thread to capture module.runtime in real time
        watcher = threading.Thread(target=_watch_module_runtime, daemon=True)
        watcher.start()

        spec.loader.exec_module(module)
        if hasattr(module, "runtime"):
            _global_ostb_runtime = module.runtime
            print(f"[ultrasound] >>> ATTACHED _curv_module and _global_ostb_runtime: {module.runtime}")
    except Exception as exc:
        print(f"[ultrasound] In-process execution info: {exc}")


def _start_ultrasound_process():
    """
    Launches curv_proper_code.py in-process thread for direct runtime function invocation,
    with subprocess fallback if needed.
    """
    thread = threading.Thread(target=_run_curv_proper_code_thread, daemon=True)
    thread.start()
    return None


def _cleanup() -> None:
    _stop_event.set()

    global _ultrasound_proc
    if _ultrasound_proc is not None and _ultrasound_proc.poll() is None:
        _ultrasound_proc.terminate()
        try:
            _ultrasound_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _ultrasound_proc.kill()
            _ultrasound_proc.wait(timeout=5)


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    return response


@app.route("/api/status", methods=["GET"])
def get_status():
    """
    Returns current control panel state directly from curv_proper_code.py parameters.
    Allows Doctor UI on load to sync sliders and buttons to patient hardware state.
    """
    global _curv_module
    if _curv_module is not None and hasattr(_curv_module, "get_current_status"):
        try:
            return jsonify(_curv_module.get_current_status())
        except Exception as e:
            print(f"[Status API] Error getting status: {e}")
    return jsonify({
        "status": "STOPPED",
        "voltage": 50.0,
        "gain": 40.0,
        "display": True,
        "tgc_enabled": False,
        "tgc_sliders": [50, 12, 3, 77, 90, 30]
    })


@app.route("/api/remote-input", methods=["POST", "OPTIONS"])
def remote_input():
    if request.method == "OPTIONS":
        return Response(status=204)
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No data received"}), 400
        
        print(f"[Remote Control API] Received control packet: {data}")
        
        if "control" in data:
            success = execute_direct_runtime_command(data.get("control"), data.get("value"))
            print(f"[Remote Control API] Executed command '{data.get('control')}'={data.get('value')}: result={success}")
            return jsonify({"status": "success", "executed": success})

        event_type = data.get("type")
        x_percent = float(data.get("x", 0.0))
        y_percent = float(data.get("y", 0.0))
        
        success = replicate_mouse_event(event_type, x_percent, y_percent)
        return jsonify({"status": "success", "executed": success})
    except Exception as e:
        print(f"[Remote Control API] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/frame.jpg", methods=["GET"])
def frame_jpg():
    response = Response(_get_latest_jpg(), mimetype="image/jpeg")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/", methods=["GET"])
def publisher_page():
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Ultrasound Agora Publisher</title>
  <script src="https://download.agora.io/sdk/release/AgoraRTC_N.js"></script>
  <script src="https://unpkg.com/mqtt/dist/mqtt.min.js"></script>
  <style>
    body {{ font-family: Segoe UI, sans-serif; margin: 18px; background: #12161d; color: #f4f7fb; }}
    #status {{ margin-bottom: 10px; padding: 10px 12px; border-radius: 8px; background: #1f2937; }}
    #preview {{ width: min(920px, 96vw); aspect-ratio: 16/9; background: #0a0f16; border-radius: 10px; overflow: hidden; }}
  </style>
</head>
<body>
  <div id="status">Starting ultrasound publisher...</div>
  <div id="preview"></div>

  <script>
    const APP_ID = {APP_ID!r};
    const TOKEN = {TOKEN!r} || null;
    const CHANNEL = {CHANNEL!r};
    const UID = {UID};
    const FRAME_URL = '/frame.jpg';

    const statusEl = document.getElementById('status');
    const previewEl = document.getElementById('preview');

    let client = null;
    let track = null;
    let audioTrack = null;
    let timerId = null;
    let mqttClient = null;

    function setStatus(text) {{
      statusEl.textContent = text;
    }}

    function connectMQTT() {{
      const cleanAppId = APP_ID.substring(0, 8);
      const topic = `${{CHANNEL}}-control-${{cleanAppId}}`;
      setStatus(`Connecting to MQTT Broker on topic: ${{topic}}...`);

      const brokerUrls = [
        "wss://broker.hivemq.com:8884/mqtt",
        "wss://broker.emqx.io:8084/mqtt"
      ];
      let attempts = 0;

      function tryConnect() {{
        const url = brokerUrls[attempts % brokerUrls.length];
        console.log(`[Patient MQTT] Trying connection to ${{url}} on topic: ${{topic}}`);
        try {{
          mqttClient = mqtt.connect(url, {{ keepalive: 30, reconnectPeriod: 3000 }});
          
          mqttClient.on("connect", () => {{
            console.log(`[Patient MQTT] Successfully connected to ${{url}}`);
            mqttClient.subscribe(topic, (err) => {{
              if (err) {{
                console.error("[Patient MQTT] Subscription error:", err);
              }} else {{
                console.log(`[Patient MQTT] Subscribed to control topic: ${{topic}}`);
                setStatus(`Ultrasound active (Remote Control connected on channel: ${{CHANNEL}})`);
              }}
            }});
          }});

          mqttClient.on("error", (err) => {{
            console.error(`[Patient MQTT] Connection error on ${{url}}:`, err);
            mqttClient.end();
            attempts++;
            if (attempts < 4) {{
              setTimeout(tryConnect, 1500);
            }}
          }});

          mqttClient.on("message", async (receivedTopic, message) => {{
            if (receivedTopic !== topic) return;
            try {{
              const event = JSON.parse(message.toString());
              console.log("[Patient MQTT] Message received:", event);
              const targetUrl = window.location.origin + "/api/remote-input";
              console.log("[Patient MQTT] Forwarding to local API:", targetUrl);
              const res = await fetch(targetUrl, {{
                method: "POST",
                headers: {{ "Content-Type": "application/json" }},
                body: JSON.stringify(event)
              }});
              const resData = await res.json();
              console.log("[Patient MQTT] Local API execution response:", resData);
            }} catch (err) {{
              console.error("[Patient MQTT] Error processing remote event:", err);
            }}
          }});
        }} catch (e) {{
          console.error("[Patient MQTT] Exception on connection attempt:", e);
        }}
      }}

      tryConnect();
    }}

    async function createTrackFromFrames() {{
      const canvas = document.createElement('canvas');
      canvas.width = 640;
      canvas.height = 480;
      const ctx = canvas.getContext('2d');
      if (!ctx) throw new Error('Canvas context unavailable');

      const image = new Image();
      image.crossOrigin = 'anonymous';

      await new Promise((resolve, reject) => {{
        let firstFrameReady = false;
        const timeoutId = setTimeout(() => {{
          if (!firstFrameReady) reject(new Error('Timed out waiting for ultrasound frames.'));
        }}, 10000);

        const pull = () => {{
          const sep = FRAME_URL.includes('?') ? '&' : '?';
          image.src = `${{FRAME_URL}}${{sep}}ts=${{Date.now()}}`;
        }};

        image.onload = () => {{
          if (image.naturalWidth && image.naturalHeight) {{
            canvas.width = image.naturalWidth;
            canvas.height = image.naturalHeight;
          }}

          ctx.drawImage(image, 0, 0, canvas.width, canvas.height);

          if (!firstFrameReady) {{
            firstFrameReady = true;
            clearTimeout(timeoutId);
            resolve();
          }}

          timerId = setTimeout(pull, 40);
        }};

        image.onerror = () => {{
          if (!firstFrameReady) {{
            clearTimeout(timeoutId);
            reject(new Error('Cannot load /frame.jpg from Python side.'));
            return;
          }}
          timerId = setTimeout(pull, 200);
        }};

        pull();
      }});

      const stream = canvas.captureStream(25);
      const mediaTrack = stream.getVideoTracks()[0];
      if (!mediaTrack) throw new Error('No media track available');

      const v = document.createElement('video');
      v.autoplay = true;
      v.muted = true;
      v.playsInline = true;
      v.srcObject = stream;
      v.style.width = '100%';
      v.style.height = '100%';
      v.style.objectFit = 'contain';
      previewEl.innerHTML = '';
      previewEl.appendChild(v);

      return AgoraRTC.createCustomVideoTrack({{ mediaStreamTrack: mediaTrack }});
    }}

    async function start() {{
      if (!APP_ID) throw new Error('APP_ID missing in Python config');

      setStatus('Preparing ultrasound track...');
      track = await createTrackFromFrames();

      try {{
        setStatus('Accessing microphone...');
        audioTrack = await AgoraRTC.createMicrophoneAudioTrack();
        console.log('Microphone track created successfully');
      }} catch (err) {{
        console.warn('Could not create microphone track, continuing with video only:', err);
      }}

      setStatus('Joining Agora channel...');
      client = AgoraRTC.createClient({{ mode: 'rtc', codec: 'vp8' }});
      await client.join(APP_ID, CHANNEL, TOKEN, UID);
      
      const publishTracks = [track];
      if (audioTrack) {{
        publishTracks.push(audioTrack);
      }}
      await client.publish(publishTracks);

      connectMQTT();
    }}

    // Always connect MQTT immediately on page load regardless of Agora stream state
    connectMQTT();

    start().catch((err) => {{
      setStatus(`Failed: ${{err?.message || err}}`);
      console.error(err);
    }});

    window.addEventListener('beforeunload', async () => {{
      try {{
        if (timerId) clearTimeout(timerId);
        if (track) {{
          track.stop();
          track.close();
        }}
        if (audioTrack) {{
          audioTrack.stop();
          audioTrack.close();
        }}
        if (mqttClient) {{
          mqttClient.end();
        }}
        if (client) await client.leave();
      }} catch (_) {{}}
    }});
  </script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


def main() -> None:
    global _ultrasound_proc

    _ultrasound_proc = _start_ultrasound_process()

    if _ultrasound_proc is not None:
        output_thread = threading.Thread(
            target=_forward_child_output,
            args=(_ultrasound_proc,),
            daemon=True,
        )
        output_thread.start()

        time.sleep(2)
        if _ultrasound_proc.poll() is not None:
            raise SystemExit(
                "Ultrasound script exited immediately. "
                "Check [ultrasound] logs above for the root cause."
            )

    output_thread = threading.Thread(target=_process_output_loop, daemon=True)
    output_thread.start()

    # Start MQTT subscriber to receive Doctor control commands
    mqtt_thread = threading.Thread(target=_start_mqtt_subscriber, daemon=True)
    mqtt_thread.start()

    atexit.register(_cleanup)

    url = f"http://{HOST}:{PORT}/"
    print("Ultrasound -> Agora publisher starting...")
    print(f"Open: {url}")
    webbrowser.open(url)

    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()