# mainappvolume2.py
import sys, asyncio, json, time
from PyQt5 import QtWidgets, QtCore
from PyQt5.QtWidgets import QApplication, QMainWindow, QMessageBox, QWidget
from midiuser_ui import Ui_MainWindow
from midisettings_ui import Ui_Form as Ui_SettingsForm
from roomform_ui import Ui_roomwindow
from roomsettings_ui import Ui_roomSettingsWindow
from routingmanager_ui import Ui_routingDialog
from PyQt5.QtWidgets import QDialog, QComboBox, QSpinBox
import mido
import time
import statistics
import re
from PyQt5.QtWidgets import QPushButton, QHBoxLayout, QWidget
from mido import Message
import websockets
import csv
from datetime import datetime
from qasync import QEventLoop
#from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription
from username import Ui_Form as Ui_UsernameForm

# WebRTC signaling server
SIGNALING_SERVER = "ws://155.207.200.166:8080/ws"  # change to your VPS


import subprocess, threading, queue

def force_winmm_refresh():
    """
    Αναγκάζει το Windows WinMM να ξανακαταγράψει τις MIDI συσκευές.
    Χρειάζεται μόνο σε Windows — σε άλλα OS δεν κάνει τίποτα.
    """
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        # Στέλνουμε WM_DEVICECHANGE μήνυμα για να αναγκάσουμε re-enumeration
        # Ο πιο αξιόπιστος τρόπος είναι να ανοίξουμε και να κλείσουμε ένα dummy port
        # ώστε το WinMM να ανανεώσει το internal cache
        count = winmm.midiInGetNumDevs()
        print(f"🔄 WinMM refresh: {count} MIDI input(s) detected")
    except Exception as e:
        print(f"⚠️ WinMM refresh failed: {e}")
class VirtualMidiBridge:
    def __init__(self, exe_path: str):
        self.p = subprocess.Popen(
            [exe_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        self._q = queue.Queue()
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()
        self._lock = threading.Lock()

        # consume initial READY from helper
        try:
            first = self._q.get(timeout=2)
            print("BRIDGE INIT <-", first)
        except Exception:
            print("⚠️ No READY received from bridge")

        # MIDI outputs opened by Python toward the virtual ports
        self.output_cache = {}

    def _reader(self):
        for line in self.p.stdout:
            self._q.put(line.rstrip("\n"))

    def _cmd(self, line: str) -> str:
        with self._lock:
            #print("BRIDGE CMD ->", line)
            self.p.stdin.write(line + "\n")
            self.p.stdin.flush()
            resp = self._q.get()
            #print("BRIDGE RESP <-", resp)
            return resp

    def create(self, name: str) -> bool:
        resp = self._cmd(f"CREATE|{name}")
        if not resp.startswith("OK"):
            return False

        # Give Windows a little time to expose the MIDI port
        time.sleep(0.5)
        return True

    def close(self, name: str) -> bool:
        # close Python-side mido output first
        out = self.output_cache.pop(name, None)
        if out:
            try:
                out.close()
            except Exception:
                pass

        resp = self._cmd(f"CLOSE|{name}")
        return resp.startswith("OK")

    def send_hex(self, name: str, hex_bytes: str) -> bool:
        """
        Send MIDI through midiroomsports.exe.
        This avoids mido.open_output(name), which may not see dynamic ports.
        """
        try:
            resp = self._cmd(f"SEND|{name}|{hex_bytes}")
            return resp.startswith("OK")
        except Exception as e:
            print(f"❌ Failed to send MIDI through bridge to {name}: {e}")
            return False

    def send_hex_nowait(self, name: str, hex_bytes: str):
        """Fire-and-forget MIDI send — does not wait for OK response."""
        try:
            with self._lock:
                self.p.stdin.write(f"SEND|{name}|{hex_bytes}\n")
                self.p.stdin.flush()
        except Exception as e:
            print(f"❌ Bridge nowait send failed for {name}: {e}")

    def shutdown(self):
        for name in list(self.output_cache.keys()):
            try:
                self.output_cache[name].close()
            except Exception:
                pass
        self.output_cache.clear()

        try:
            self._cmd("EXIT")
        except Exception:
            pass

        try:
            self.p.terminate()
        except Exception:
            pass
class UsernameDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_UsernameForm()
        self.ui.setupUi(self)

        self.username = None

        self.ui.continueButton.clicked.connect(self.on_continue)
        self.ui.usernameLine.returnPressed.connect(self.on_continue)

    def on_continue(self):
        text = self.ui.usernameLine.text().strip()

        if not text:
            QtWidgets.QMessageBox.warning(self, "Error", "Please enter a username.")
            return

        self.username = text
        self.accept()
class PianoWidget(QtWidgets.QWidget):
    """
    A two-octave piano: properly overlapped black keys above white keys.
    - No labels on keys
    - Only local key presses highlight (remote notes do NOT)
    - Calls a provided send_midi(note, velocity) callback
    """
    WHITE_ORDER = [0, 2, 4, 5, 7, 9, 11]          # C D E F G A B
    BLACK_MAP   = {0:1, 1:3, 3:6, 4:8, 5:10}      # which white-index has a black key after it (no sharps after E/B)

    def __init__(self, parent=None, start_note=60, white_keys=21, on_send_midi=None):
        super().__init__(parent)
        self.start_note = start_note          # C4 by default
        self.white_keys = white_keys          # 14 white keys = two octaves
        self.on_send_midi = on_send_midi      # callback(room.send_midi)

        # key geometry
        self.WHITE_W, self.WHITE_H = 32, 150
        self.BLACK_W, self.BLACK_H = 20, 95
        self.setFixedSize(self.white_keys * self.WHITE_W, self.WHITE_H)

        # containers
        self.white_btns = {}   # note -> QPushButton
        self.black_btns = {}   # note -> QPushButton

        # No layout: absolute positioning to overlap blacks over whites
        # ---- place white keys ----
        for i in range(self.white_keys):
            octave   = i // 7
            w_index  = i % 7
            midi     = self.start_note + self.WHITE_ORDER[w_index] + 12 * octave

            btn = QtWidgets.QPushButton("", self)
            btn.setGeometry(i * self.WHITE_W, 0, self.WHITE_W, self.WHITE_H)
            btn.setFocusPolicy(QtCore.Qt.NoFocus)
            btn.setStyleSheet("background:#fff; border:1px solid #000;")
            btn.pressed.connect(lambda n=midi: self._local_press(n))
            btn.released.connect(lambda n=midi: self._local_release(n))
            self.white_btns[midi] = btn

        # ---- place black keys (between whites, except after E/B) ----
        for i in range(self.white_keys):
            w_index = i % 7
            if w_index in self.BLACK_MAP:
                octave = i // 7
                midi   = self.start_note + self.BLACK_MAP[w_index] + 12 * octave

                # center black over gap between white i and i+1
                x = i * self.WHITE_W + (self.WHITE_W - self.BLACK_W // 2)
                btn = QtWidgets.QPushButton("", self)
                btn.setGeometry(x, 0, self.BLACK_W, self.BLACK_H)
                btn.raise_()
                btn.setFocusPolicy(QtCore.Qt.NoFocus)
                btn.setStyleSheet("background:#000; border:1px solid #333;")
                btn.pressed.connect(lambda n=midi: self._local_press(n))
                btn.released.connect(lambda n=midi: self._local_release(n))
                self.black_btns[midi] = btn

    # ---- local press/release: send MIDI + highlight locally only ----
    def _local_press(self, note):
        if callable(self.on_send_midi):
            self.on_send_midi(note, 100)
        self._highlight(note, True)

    def _local_release(self, note):
        if callable(self.on_send_midi):
            self.on_send_midi(note, 0)
        self._highlight(note, False)

    def _highlight(self, note, on):
        # set color back based on key type
        if note in self.white_btns:
            btn = self.white_btns[note]
            if on:
                btn.setStyleSheet("background: #ffea75; border:2px solid #d9a400;")  # yellow
            else:
                btn.setStyleSheet("background:#fff; border:1px solid #000;")
        elif note in self.black_btns:
            btn = self.black_btns[note]
            if on:
                btn.setStyleSheet("background: #555; border:2px solid #333;")        # lighter when pressed
            else:
                btn.setStyleSheet("background:#000; border:1px solid #333;")



class RoomWindow(QWidget):
    def __init__(self, main_app, room_name="lobby", is_creator=False):
        super().__init__()
        self.local_mute = False
        self.main_app = main_app
        self.room_name = room_name
        self.metronome_running = False
        self.metronome_bpm = 120
        self.metronome_start_time = None
        self.metronome_last_beat = -1
        self.is_creator = is_creator
        self.ui = Ui_roomwindow()
        self.ui.setupUi(self)
        self.ui.bpmSpinBox.setSuffix(" BPM")
        self.ui.StartButton.clicked.connect(self.start_recording)
        self.ui.StopButton.clicked.connect(self.stop_recording)
        self.ui.StopButton.setEnabled(False)
        self.ui.startMetronomeButton.clicked.connect(self.start_metronome_clicked)
        self.ui.stopMetronomeButton.clicked.connect(self.stop_metronome_clicked)
        self.ui.applyBpmButton.clicked.connect(self.apply_bpm_clicked)
        self.ui.bpmSpinBox.setValue(self.metronome_bpm)
        self.piano = PianoWidget(
            self,
            start_note=24,  # C1
            white_keys=49  # 88-key piano
        )
        self.metronome_port_name = f"{self.main_app.username}_metronome"[:31]

        if getattr(self.main_app, "vmidi", None):
            ok = self.main_app.vmidi.create(self.metronome_port_name)
            if ok:
                self.main_app.created_vmidi_ports.add(self.metronome_port_name)
                print(f"✅ Created metronome MIDI port: {self.metronome_port_name}")
            else:
                print(f"❌ Failed to create metronome MIDI port: {self.metronome_port_name}")
        self.metronome_timer = QtCore.QTimer()
        self.metronome_timer.timeout.connect(self.metronome_tick)
        self.metronome_timer.start(10)
        self.recording = False
        self.recorded_events = []
        self.recording_start_time = None
        self.ui.pianoScrollArea.setWidgetResizable(False)
        self.ui.pianoScrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        self.ui.pianoScrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.ui.pianoScrollArea.setWidget(self.piano)
        self.setWindowTitle(f"MIDIROOMS - {room_name}")
        self.ui.LeaveButton.clicked.connect(self.leave_room)
        self.ui.logArea.clear()
        self.user_ui_elements = {}
        self.user_velocity_bars = {}
        self.user_latency_labels = {}
        self.velocity_reset_timers = {}  # username -> QTimer
        self.piano_off_timers = {}  # note -> QTimer (optional, prevents singleShot spam)
        self.connected_users = []
        self.midi_seq = 0
        self.last_seq_from_peer = {}
        self.add_user_ui(self.main_app.username)  # show myself
        self.ui.muteButton.clicked.connect(self.toggle_local_mute)
        self.ui.routingmanagerButton.clicked.connect(self.open_routing_manager)
        # WebRTC objects
        self.offer_sent = False
        self.pc = None
        self.channel = None
        # Use preselected ports from settings
        # Use preselected ports from settings
        self.midi_inputs = []  # list of (stream_id, mido_input_port)
        self.stream_devices = {}  # stream_id -> device name
        self.clock_offset_from_peer = {}
        self.remote_midi_queue = []
        self.remote_playback_delay = 0.005  # 5 ms
        self.peer_jitter = {}
        self.rtt_samples = {}
        self.remote_playback_timer = QtCore.QTimer()
        self.remote_playback_timer.timeout.connect(self.process_remote_midi_queue)
        self.remote_playback_timer.start(2)

        from mido import open_input, open_output

        # Get selected inputs (new) or fallback to old single input
        selected_inputs = getattr(self.main_app, "selected_inputs", None)
        if not selected_inputs:
            one = getattr(self.main_app, "selected_input", None)
            selected_inputs = [one] if one else []

        self.failed_inputs = set()
        if getattr(self.main_app, "midi_service_was_refreshed", False):
            print("⏳ Waiting after MIDI service refresh...")
            QtWidgets.QApplication.processEvents()
            time.sleep(3.0)

            # Force mido/WinMM to re-enumerate fresh
            try:
                print("🔄 Fresh MIDI inputs:", mido.get_input_names())
                print("🔄 Fresh MIDI outputs:", mido.get_output_names())
            except Exception as e:
                print("⚠️ MIDI rescan failed:", e)

            self.main_app.midi_service_was_refreshed = False

        # Poll MIDI input
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.poll_midi_input)
        self.timer.start(1)
        self.ping_timer = QtCore.QTimer()
        self.ping_timer.timeout.connect(lambda: asyncio.ensure_future(self.send_ping()))
        self.ping_timer.start(3000)  # every 3 seconds
        self.hotplug_timer = QtCore.QTimer()
        self.hotplug_timer.timeout.connect(self.scan_new_midi_inputs)
        self.hotplug_timer.start(3000)  # every 3 seconds

        # περιοδικό re-announce streams — ώστε αν χαθεί το πρώτο να ξαναστείλουμε
        self.announce_timer = QtCore.QTimer()
        self.announce_timer.timeout.connect(self.send_streams_announce)
        self.announce_timer.start(10000)  # κάθε 10 δευτερόλεπτα

        QtCore.QTimer.singleShot(3500, self.init_midi_inputs)
    def init_midi_inputs(self):
        import mido

        print("🚀 Initializing MIDI inputs AFTER delay...")

        self.midi_inputs = []
        self.stream_devices = {}

        selected_inputs = getattr(self.main_app, "selected_inputs", []) or []

        available_inputs = mido.get_input_names()
        print("🎹 Available inputs after delay:", available_inputs)

        for idx, dev_name in enumerate(selected_inputs, start=1):
            inport = self.open_midi_input_with_retry(dev_name)

            if not inport:
                print(f"❌ Could not open MIDI input: {dev_name}")
                continue

            stream_id = self.main_app.make_stream_id(dev_name, idx)

            self.midi_inputs.append((stream_id, inport))
            self.stream_devices[stream_id] = dev_name

            print(f"✅ Opened MIDI input: {dev_name}")

    def send_to_room(self, payload: dict):
        """Στέλνει message σε όλους στο δωμάτιο μέσω WebSocket (TCP)."""
        if not self.main_app.ws:
            return
        msg = {**payload, "room": self.room_name, "tcp_relay": True}
        try:
            asyncio.get_event_loop().create_task(
                self.main_app.ws.send(json.dumps(msg))
            )
        except Exception as e:
            print("⚠️ send_to_room failed:", e)
    def open_midi_input_with_retry(self, dev_name, retries=12, delay_ms=1000):
        from mido import open_input

        for attempt in range(1, retries + 1):
            try:
                available = mido.get_input_names()
                print(f"🔎 Available MIDI inputs attempt {attempt}: {available}")

                real_name = next((n for n in available if n == dev_name), None)

                if not real_name:
                    # fallback για cases τύπου "MPK mini 3 0" vs renamed instance
                    base = dev_name.lower().split(" 0")[0]
                    real_name = next((n for n in available if base in n.lower()), None)

                if not real_name:
                    print(f"⚠️ {dev_name} not visible yet")
                    time.sleep(delay_ms / 1000)
                    continue

                print(f"🎹 Trying to open MIDI input {real_name} ({attempt}/{retries})")
                return open_input(real_name)

            except Exception as e:
                print(f"⚠️ Failed attempt {attempt}/{retries} opening {dev_name}: {e}")
                time.sleep(delay_ms / 1000)

        return None


    def send_streams_announce(self):
        if not self.main_app.ws:
            return
        streams = []

        if getattr(self, "stream_devices", None):
            for stream_id, dev_name in self.stream_devices.items():
                streams.append({"stream": stream_id, "device": dev_name})
        else:
            selected_inputs = getattr(self.main_app, "selected_inputs", []) or []
            for idx, dev_name in enumerate(selected_inputs, start=1):
                stream_id = self.main_app.make_stream_id(dev_name, idx)
                streams.append({"stream": stream_id, "device": dev_name})

        self.send_to_room({
            "type": "streams_announce",
            "user": self.main_app.username,
            "streams": streams
        })
        print("📣 Sent streams_announce via TCP")

    def bump_velocity_bar(self, username: str, velocity: int):
        bar = self.user_velocity_bars.get(username)
        if not bar:
            return

        bar.setValue(int(velocity))

        t = self.velocity_reset_timers.get(username)
        if t is None:
            t = QtCore.QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(
                lambda u=username: self.user_velocity_bars.get(u) and self.user_velocity_bars[u].setValue(0))
            self.velocity_reset_timers[username] = t

        # restart the same timer instead of creating new ones
        t.start(300)

    def preferred_remote_port(self, user: str, stream: str) -> str | None:
        meta = getattr(self.main_app, "stream_meta", {}).get((user, stream), {})
        room = meta.get("room", self.room_name)
        device = meta.get("device", "UnknownDevice")

        pool = getattr(self.main_app, "remote_port_pool", [])
        if not pool:
            return None

        key_str = f"{room}|{user}|{device}|{stream}"
        idx = abs(hash(key_str)) % len(pool)
        return pool[idx]

    def open_routing_manager(self):
        dlg = RoutingManager(self.main_app)
        self.main_app.routing_manager_dialog = dlg
        dlg.exec_()
        self.main_app.routing_manager_dialog = None

    def scan_new_midi_inputs(self):
        import mido
        from mido import open_input

        def should_ignore_input(name: str) -> bool:
            n = (name or "").lower()
            vmidi_lower = {v.lower() for v in getattr(self.main_app, "created_vmidi_ports", set())}
            if n in vmidi_lower:
                return True
            return False

        current_names = [n for n in mido.get_input_names() if not should_ignore_input(n)]
        selected_inputs = set(getattr(self.main_app, "selected_inputs", []) or [])
        if not selected_inputs:
            return  # ✅ don't auto-open devices unless user selected them in settings

        if selected_inputs:
            current_names = [n for n in current_names if n in selected_inputs]

        opened_names = set(n.lower() for n in self.stream_devices.values() if n)

        new_devices = [d for d in current_names if d.lower() not in opened_names]
        failed = getattr(self, "failed_inputs", set())
        new_devices = [d for d in new_devices if d.lower() not in failed]

        if not new_devices:
            return

        for dev_name in new_devices:
            try:
                def make_stream_id(dev_name: str, idx: int) -> str:
                    base = "".join(c if c.isalnum() else "_" for c in (dev_name or "device")).strip("_")
                    base = "_".join(base.split("_"))
                    base = base[:24] if base else f"dev{idx}"
                    return f"{base}_{idx}"

                stream_id = make_stream_id(dev_name, len(self.midi_inputs) + 1)

                inport = self.open_midi_input_with_retry(dev_name, retries=1, delay_ms=100)
                if not inport:
                    print(f"⚠️ Hotplug: {dev_name} δεν είναι ακόμα διαθέσιμη, θα ξαναδοκιμαστεί")
                    continue  # ΔΕΝ βάζουμε στο failed_inputs — ο επόμενος κύκλος θα ξαναδοκιμάσει
                self.midi_inputs.append((stream_id, inport))
                self.stream_devices[stream_id] = dev_name
                print(f"➕ Hotplug: opened {stream_id} = {dev_name}")

                # ειδοποίηση επανασύνδεσης στο UI log
                self.ui.logArea.append(
                    f"✅ MIDI device reconnected: {dev_name} — ready to use"
                )
                # ενημέρωσε τον πίνακα αν είναι ανοιχτός
                if getattr(self.main_app, "routing_manager_dialog", None):
                    self.main_app.routing_manager_dialog.refresh_table()

                # announce updated stream list to peers
                self.send_streams_announce()

            except Exception as e:
                print(f"⚠️ Hotplug: failed to open {dev_name}: {e}")

    def try_reopen_device(self, dev_name: str, attempt: int = 1):
        """
        Ασύγχρονη επαναφορά συσκευής — δεν μπλοκάρει το Qt thread.
        Χρησιμοποιεί singleShot για να επαναλαμβάνεται χωρίς blocking.
        """
        MAX_ATTEMPTS = 20  # δοκίμασε έως 20 φορές (60 δευτερόλεπτα συνολικά)

        # αν έχει ήδη ξανανοιχτεί, σταμάτα
        already_open = any(
            self.stream_devices.get(sid, "").lower() == dev_name.lower()
            for sid, _ in self.midi_inputs
        )
        if already_open:
            return

        # αν δεν είναι επιλεγμένη, σταμάτα
        selected = set(getattr(self.main_app, "selected_inputs", []) or [])
        if dev_name not in selected:
            return

        if attempt > MAX_ATTEMPTS:
            self.ui.logArea.append(f"❌ Could not reconnect {dev_name} after {MAX_ATTEMPTS} attempts.")
            print(f"❌ Giving up on {dev_name}")
            return

        # Force WinMM να ξαναδεί τις συσκευές
        force_winmm_refresh()

        try:
            available = mido.get_input_names()
        except Exception:
            available = []

        # ψάξε exact match ή partial match
        real_name = next((n for n in available if n == dev_name), None)
        if not real_name:
            base = dev_name.lower().split(" 0")[0]
            real_name = next((n for n in available if base in n.lower()), None)

        if not real_name:
            # δεν φαίνεται ακόμα — ξαναδοκίμασε σε 3 δευτερόλεπτα
            print(f"⏳ [{attempt}/{MAX_ATTEMPTS}] {dev_name} not visible yet, retrying...")
            QtCore.QTimer.singleShot(
                3000, lambda: self.try_reopen_device(dev_name, attempt + 1)
            )
            return

        # φαίνεται — προσπάθησε να την ανοίξεις
        try:
            from mido import open_input
            inport = open_input(real_name)

            stream_id = self.main_app.make_stream_id(dev_name, len(self.midi_inputs) + 1)
            self.midi_inputs.append((stream_id, inport))
            self.stream_devices[stream_id] = dev_name
            self.failed_inputs.discard(dev_name.lower())

            self.ui.logArea.append(f"✅ MIDI device reconnected: {dev_name} — ready to use")
            print(f"✅ Reopened {dev_name} as {stream_id}")

            if getattr(self.main_app, "routing_manager_dialog", None):
                self.main_app.routing_manager_dialog.refresh_table()

            self.send_streams_announce()

        except Exception as e:
            # η συσκευή φαίνεται αλλά δεν ανοίγει ακόμα — ξαναδοκίμασε
            print(f"⏳ [{attempt}/{MAX_ATTEMPTS}] {dev_name} visible but not openable yet: {e}")
            QtCore.QTimer.singleShot(
                3000, lambda: self.try_reopen_device(dev_name, attempt + 1)
            )
    async def send_ping(self):
        """Periodically send ping messages to every other connected user."""
        if not self.main_app.ws:
            return

        now = time.time()
        # make sure connected_users exists
        for peer in getattr(self, "connected_users", []):
            if peer == self.main_app.username:
                continue  # skip self
            payload = {
                "type": "sync_ping",
                "from": self.main_app.username,
                "to": peer,
                "t0": time.perf_counter(),
            }
            try:
                self.send_to_room(payload)
                print(f"📤 Sent ping from {self.main_app.username} → {peer}")
            except Exception as e:
                print("⚠️ Failed to send ping:", e)

    def handle_pong(self, data):
        return

    def on_datachannel(self, channel):
        return
        self.channel = channel
        print(f"📥 DataChannel received by {self.main_app.username}")

        @channel.on("open")
        def on_open():
            print("✅ DataChannel opened (receiver)")
            self.send_streams_announce()

        channel.on("message", self.on_midi_message)

        # ✅ IMPORTANT: sometimes channel is already open before handlers attach
        if getattr(channel, "readyState", None) == "open":
            print("✅ DataChannel already open (receiver) — announcing streams now")
            self.send_streams_announce()
        else:
            # also send once after a tiny delay as a safety net
            QtCore.QTimer.singleShot(200, self.send_streams_announce)

    def leave_room(self):
        if self.main_app.ws:
            asyncio.get_event_loop().create_task(
                self.main_app.ws.send(json.dumps({
                    "type": "leave",
                    "room": self.room_name,
                    "user": self.main_app.username
                }))
            )

        # κλείσε channel και pc πριν τα μηδενίσεις
        try:
            if self.channel:
                self.channel.close()
        except Exception:
            pass
        try:
            if self.pc:
                asyncio.ensure_future(self.pc.close())
        except Exception:
            pass

        self.pc = None
        self.channel = None
        self.offer_sent = False
        # Stop timers first
        try:
            self.timer.stop()
            self.ping_timer.stop()
        except Exception:
            pass
        try:
            self.hotplug_timer.stop()
        except Exception:
            pass
        try:
            self.announce_timer.stop()
        except Exception:
            pass
        # Close ALL remote vmidi ports created for this room session
        try:
            if getattr(self.main_app, "vmidi", None):
                for (u, s), route in list(self.main_app.routing_config.items()):
                    # 🔒 Do NOT close my local port
                    if u == self.main_app.username:
                        continue

                    vm = route.get("vmidi")
                    if vm:
                        try:
                            self.main_app.vmidi.close(vm)
                            self.main_app.created_vmidi_ports.discard(vm)
                        except Exception:
                            pass
                print("🧹 Closed all room vmidi ports")
        except Exception:
            pass

        # Close main midi input/output if open
        # Close all MIDI inputs
        try:
            for _, inport in self.midi_inputs:
                try:
                    inport.close()
                except Exception:
                    pass
        except Exception:
            pass
        self.midi_inputs = []
        try:
            self.main_app.routing_config.clear()
            self.main_app.locked_routes.clear()
            self.main_app.seen_streams.clear()
            self.main_app.stream_meta.clear()
            self.main_app.stream_pretty.clear()
        except Exception:
            pass
        self.main_app.show()
        try:
            if getattr(self.main_app, "vmidi", None) and getattr(self, "metronome_port_name", None):
                self.main_app.vmidi.close(self.metronome_port_name)
                self.main_app.created_vmidi_ports.discard(self.metronome_port_name)
                print(f"🧹 Closed metronome port: {self.metronome_port_name}")
        except Exception as e:
            print("⚠️ Failed to close metronome port:", e)
        try:
            for out in getattr(self, "local_output_cache", {}).values():
                try:
                    out.close()
                except Exception:
                    pass
            self.local_output_cache.clear()
        except Exception:
            pass
        self.close()

    async def start_webrtc(self):
        # Το όνομα διατηρείται για συμβατότητα — πλέον στέλνει μόνο το join.
        # Όλη η επικοινωνία γίνεται μέσω WebSocket (TCP), όχι WebRTC.
        if not self.main_app.ws:
            print("No signaling connection")
            return

        await self.main_app.ws.send(json.dumps({
            "type": "join",
            "room": self.room_name,
            "user": self.main_app.username
        }))
        print(f"🟢 {self.main_app.username} joined room '{self.room_name}' (TCP mode)")

    def poll_midi_input(self):
        if not self.midi_inputs:
            return
        for stream_id, inport in list(self.midi_inputs):
            try:
                pending = list(inport.iter_pending())
                input_ts = time.perf_counter()
            except Exception as e:
                print(f"⚠️ MIDI input disconnected: {stream_id}: {e}")

                try:
                    inport.close()
                except Exception:
                    pass

                lost_dev = self.stream_devices.get(stream_id)

                self.midi_inputs = [
                    (sid, port) for sid, port in self.midi_inputs if sid != stream_id
                ]

                self.stream_devices.pop(stream_id, None)

                # αφαίρεσε από failed_inputs ώστε το hotplug να μπορεί να την ξανανοίξει
                if lost_dev:
                    self.failed_inputs.discard(lost_dev.lower())
                    print(f"🔌 Συσκευή αποσυνδέθηκε: {lost_dev} — αναμένουμε επανασύνδεση")
                    self.ui.logArea.append(
                        f"⚠️ MIDI device disconnected: {lost_dev} — waiting for reconnect..."
                    )
                    if getattr(self.main_app, "routing_manager_dialog", None):
                        self.main_app.routing_manager_dialog.refresh_table()
                    # ξεκίνα αυτόματη ασύγχρονη επαναφορά
                    QtCore.QTimer.singleShot(
                        3000, lambda d=lost_dev: self.try_reopen_device(d)
                    )

                continue

            for msg in pending:

                if msg.type in ("note_on", "note_off"):
                    input_ts = time.perf_counter()
                    me = self.main_app.username
                    note = getattr(msg, "note", None)
                    velocity = getattr(msg, "velocity", None)
                    self.record_midi_event(
                        user=me,
                        stream=stream_id,
                        msg_type=msg.type,
                        note=note,
                        velocity=velocity,
                        channel=getattr(msg, "channel", 0)
                    )
                    # --- Local visualization ---
                    self.ui.logArea.append(f"{me}/{stream_id}: {msg.type} note={note} vel={velocity}")
                    if hasattr(self, "piano"):
                        self.piano._highlight(note, velocity > 0)
                    if not self.local_mute:
                        self.bump_velocity_bar(me, velocity)
                    if self.local_mute:
                        continue
                    # --- Send to others over WebRTC ---
                    # Skip network-originated messages (to prevent echo)
                    if getattr(msg, "_from_network", False):
                        continue  # don't resend notes that came from network

                    if self.main_app.ws:
                        self.midi_seq += 1
                        midi_event = {
                            "user": me,
                            "stream": stream_id,
                            "seq": self.midi_seq,
                            "timestamp": time.perf_counter(),
                            "input_ts": input_ts,
                            "note": note,
                            "velocity": velocity,
                            "type": msg.type,
                            "room": self.room_name,
                            "tcp_midi": True,  # flag για τον server να το κάνει relay
                        }
                        asyncio.get_event_loop().create_task(
                            self.main_app.ws.send(json.dumps(midi_event))
                        )
                    else:
                        print("⏳ WebSocket not connected, skipping send")

    def assign_leader(self, target_user: str):
        # only current leader can assign
        if getattr(self.main_app, "room_leader", None) != self.main_app.username:
            return
        if not self.main_app.ws:
            return

        asyncio.get_event_loop().create_task(
            self.main_app.ws.send(json.dumps({
                "type": "assign_leader",
                "room": self.room_name,
                "target": target_user
            }))
        )

    def update_leader_ui(self, leader: str | None):
        # store on main_app so add_user_ui() can read it
        self.main_app.room_leader = leader

        is_leader = (leader == self.main_app.username)

        for user, container in self.user_ui_elements.items():
            # find the Assign Leader button inside this user's widget
            btns = container.findChildren(QtWidgets.QPushButton)
            for b in btns:
                if b.text() in ("Assign Leader", "Kick"):
                    b.setVisible(is_leader and user != self.main_app.username)
        leader_controls_enabled = is_leader
        self.ui.bpmSpinBox.setEnabled(leader_controls_enabled)
        self.ui.applyBpmButton.setEnabled(leader_controls_enabled)
        self.ui.startMetronomeButton.setEnabled(leader_controls_enabled)
        self.ui.stopMetronomeButton.setEnabled(leader_controls_enabled)
    def on_midi_message(self, message):
        port_name = None
        try:
            data = json.loads(message)
            recv_ts = time.perf_counter()
            if "input_ts" in data:
                sender = data.get("user")
                offset = self.clock_offset_from_peer.get(sender, 0.0)
                sender_time_on_my_clock = data["input_ts"] - offset
                print(f"PYTHON INPUT→RECEIVE = {(recv_ts - sender_time_on_my_clock) * 1000:.2f} ms")

            if data.get("type") == "metronome_start":
                self.start_local_metronome(
                    data.get("bpm", 120),
                    data.get("start_time")
                )
                print("▶️ Received metronome_start:", data)
                return

            if data.get("type") == "metronome_stop":
                self.stop_local_metronome()
                print("⏹️ Received metronome_stop")
                return

            if data.get("type") == "metronome_bpm_update":
                bpm = data.get("bpm", 120)
                self.metronome_bpm = bpm
                self.ui.bpmSpinBox.setValue(bpm)
                print("🎚️ Received BPM update:", bpm)
                return
            # --- Handle streams announcement (auto-routing) ---
            if data.get("type") == "streams_announce":
                sender = data.get("user")
                room = data.get("room", self.room_name)
                streams = data.get("streams", [])

                if not sender or sender == self.main_app.username:
                    return

                for s in streams:
                    stream = s.get("stream", "kbd1")
                    device = s.get("device", "Unknown Device")
                    key = (sender, stream)
                    if hasattr(self.main_app, "seen_streams"):
                        self.main_app.seen_streams.add(key)
                    # Store metadata for routing manager (optional but recommended)
                    if hasattr(self.main_app, "stream_meta"):
                        self.main_app.stream_meta[key] = {
                            "device": device,
                            "room": room
                        }
                    # Human label for routing manager (can be long)
                    self.main_app.stream_pretty[key] = f"{sender} — {device} ({stream})"

                    # Actual Windows/FL port name (must be short)
                    port_name = self.main_app.make_remote_port_name(sender, device, stream)
                    self.main_app.stream_portname[key] = port_name

                    existing = self.main_app.routing_config.get(key, {})
                    existing_port = existing.get("vmidi")
                    port_actually_exists = existing_port in getattr(self.main_app, "created_vmidi_ports", set())

                    if not existing_port or not port_actually_exists:
                        if getattr(self.main_app, "vmidi", None) is None:
                            print("❌ vmidi bridge not running; cannot create ports.")
                            continue
                        self.main_app.ensure_vmidi_bridge()
                        ok = self.main_app.vmidi.create(port_name)
                        if ok:
                            self.main_app.routing_config[key] = {"vmidi": port_name, "channel": 1}
                            self.main_app.locked_routes.add(key)
                            self.main_app.created_vmidi_ports.add(port_name)
                            self.main_app.debug_dump_midi_ports(f"after create remote {port_name}")

                            if getattr(self.main_app, "routing_manager_dialog", None):
                                self.main_app.routing_manager_dialog.refresh_table()

                            print(f"🔒 Auto-created vmidi port for {key}: {port_name}")
                        else:
                            print(f"❌ Failed to create vmidi port: {port_name}")
                if getattr(self.main_app, "routing_manager_dialog", None):
                    self.main_app.routing_manager_dialog.refresh_table()
                return

            # --- Handle ping/pong control ---
            if data.get("type") == "sync_ping" and data.get("to") == self.main_app.username:

                reply = {
                    "type": "sync_pong",
                    "from": self.main_app.username,
                    "to": data["from"],
                    "t0": data["t0"],
                    "t1": time.perf_counter(),
                }

                self.send_to_room(reply)

                return

            if data.get("type") == "sync_pong" and data.get("to") == self.main_app.username:
                t2 = time.perf_counter()
                t0 = data.get("t0")
                t1 = data.get("t1")
                sender = data.get("from")
                rtt = t2 - t0
                offset = t1 - ((t0 + t2) / 2)
                self.clock_offset_from_peer[sender] = offset
                rtt_ms = rtt * 1000
                oneway_ms = rtt_ms / 2
                self.rtt_samples.setdefault(sender, []).append(oneway_ms)
                self.rtt_samples[sender] = self.rtt_samples[sender][-20:]
                if len(self.rtt_samples[sender]) > 2:
                    jitter_ms = statistics.stdev(self.rtt_samples[sender])
                    self.peer_jitter[sender] = jitter_ms / 1000.0  # store in seconds
                else:
                    jitter_ms = 0.0

                if sender in self.user_latency_labels:
                    self.user_latency_labels[sender].setText(
                        f"Latency: {oneway_ms:.1f} ms  |  Jitter: {jitter_ms:.1f} ms"
                    )
                print(f"🕒 Sync {sender} | RTT={rtt_ms:.1f} ms | one-way={oneway_ms:.1f} ms | offset={offset * 1000:.1f} ms | jitter={jitter_ms:.1f} ms")

                return

            # --- Skip my own loopback ---
            if data.get("user") == self.main_app.username:
                return

            # Mark this message as coming from the network
            data["_from_network"] = True
            user = data.get("user", "Unknown")
            stream = data.get("stream", "kbd1")  # default for older senders
            seq = data.get("seq")
            if seq is not None:
                key = (user, stream)
                last = self.last_seq_from_peer.get(key)

                if last is not None and seq != last + 1:
                    missing = seq - last - 1
                    if missing > 0:
                        print(f"⚠️ Missing {missing} MIDI packets from {user}/{stream}: expected {last + 1}, got {seq}")

                self.last_seq_from_peer[key] = seq
            note = data.get("note")
            velocity = data.get("velocity")
            msg_type = data.get("type")
            self.record_midi_event(
                user=user,
                stream=stream,
                msg_type=msg_type,
                note=note,
                velocity=velocity,
                channel=0
            )
            if hasattr(self.main_app, "seen_streams"):
                self.main_app.seen_streams.add((user, stream))

            print("🎵 Received MIDI event:", data)
            self.add_user_ui(user)

            # Update velocity bar
            if note is not None:
                effective_velocity = velocity if velocity is not None else 0

                if velocity is not None:
                    self.bump_velocity_bar(user, velocity)

                sender_ts = data.get("timestamp", time.perf_counter())
                offset = self.clock_offset_from_peer.get(user, 0.0)
                jitter_s = self.peer_jitter.get(user, 0.005)
                adaptive_delay = max(0.008, 2.0 * jitter_s)  # ελάχιστο 8ms buffer
                play_time = sender_ts - offset + adaptive_delay
                # μην αφήσεις play_time στο παρελθόν — αλλιώς χάνεται σε batch
                play_time = max(play_time, time.perf_counter() + 0.002)

                is_noteoff = (msg_type == "note_off" or
                              (msg_type == "note_on" and effective_velocity == 0))

                if not hasattr(self, "last_noteon_playtime"):
                    self.last_noteon_playtime = {}

                key_note = (user, stream, note)

                if is_noteoff:
                    # εγγύηση: note_off ΠΟΤΕ πριν το note_on του ίδιου note
                    last_on = self.last_noteon_playtime.get(key_note, 0.0)
                    if play_time <= last_on:
                        play_time = last_on + 0.001  # 1ms μετά το note_on
                else:
                    self.last_noteon_playtime[key_note] = play_time

                self.remote_midi_queue.append({
                    "play_time": play_time,
                    "user": user,
                    "stream": stream,
                    "type": msg_type,
                    "note": note,
                    "velocity": effective_velocity,
                })

                self.remote_midi_queue.sort(key=lambda e: e["play_time"])
            # --- GUI Piano highlight ---
            self.ui.logArea.append(f"{user}/{stream}: {msg_type} note={note} vel={velocity}")
            if hasattr(self, "piano"):
                if velocity is not None and velocity > 0:
                    self.piano._highlight(note, True)
                    QtCore.QTimer.singleShot(200, lambda n=note: self.piano._highlight(n, False))

        except Exception as e:
            print("❌ Failed to parse MIDI message:", e)

    def process_remote_midi_queue(self):
        if not self.remote_midi_queue:
            return

        now = time.perf_counter()

        ready = []
        while self.remote_midi_queue and self.remote_midi_queue[0]["play_time"] <= now:
            ready.append(self.remote_midi_queue.pop(0))

        for event in ready:
            actual_play_ts = time.perf_counter()
            user = event["user"]
            stream = event["stream"]
            msg_type = event["type"]
            note = event["note"]
            velocity = event["velocity"]

            try:
                msg = Message(msg_type, note=note, velocity=velocity)

                route = self.main_app.routing_config.get((user, stream))
                vmidi_name = None
                channel = 1

                if route:
                    vmidi_name = route.get("vmidi")
                    channel = route.get("channel", 1)

                msg.channel = (int(channel) - 1) if channel else 0

                if vmidi_name and getattr(self.main_app, "vmidi", None):
                    b = msg.bytes()
                    hex_bytes = " ".join(f"{x:02X}" for x in b)
                    # note_off και velocity=0 στέλνονται αξιόπιστα για να μην κολλάνε νότες
                    if msg_type == "note_off" or (msg_type == "note_on" and velocity == 0):
                        self.main_app.vmidi.send_hex(vmidi_name, hex_bytes)
                    else:
                        self.main_app.vmidi.send_hex_nowait(vmidi_name, hex_bytes)

            except Exception as e:
                print("⚠️ Scheduled MIDI playback error:", e)
    def send_midi(self, note, velocity):
        if not self.main_app.ws:
            return
        self.midi_seq += 1
        midi_event = {
            "user": self.main_app.username,
            "stream": "gui",
            "seq": self.midi_seq,
            "timestamp": time.perf_counter(),
            "note": note,
            "velocity": velocity,
            "type": "note_on" if velocity > 0 else "note_off",
            "room": self.room_name,
            "tcp_midi": True,
        }
        msg_type = "note_on" if velocity > 0 else "note_off"
        self.ui.logArea.append(f"{self.main_app.username}/gui: {msg_type} note={note} vel={velocity}")
        asyncio.get_event_loop().create_task(
            self.main_app.ws.send(json.dumps(midi_event))
        )
    def update_user_list(self, users):
        """Synchronize UI with the current list of connected users."""
        self.connected_users = users
        if self.main_app.ws:
            self.send_streams_announce()

        # 1️⃣ Add any new users not yet in the UI
        for username in users:
            if username not in self.user_ui_elements:
                self.add_user_ui(username)
                QtCore.QTimer.singleShot(500, self.send_streams_announce)

        # 2️⃣ Remove any users who have left
        for username in list(self.user_ui_elements.keys()):
            if username not in users:
                container = self.user_ui_elements.pop(username)
                container.setParent(None)
                container.deleteLater()

                # Also clean up velocity + latency bars
                self.user_velocity_bars.pop(username, None)
                self.user_latency_labels.pop(username, None)
                # Close any vmidi ports that belonged to this user
                to_close = []
                for (u, s), route in list(self.main_app.routing_config.items()):
                    if u == username:
                        vm = route.get("vmidi")
                        if vm:
                            to_close.append(vm)
                        # remove route + discovered stream
                        self.main_app.routing_config.pop((u, s), None)
                        self.main_app.seen_streams.discard((u, s))
                        self.main_app.locked_routes.discard((u, s))
                        self.main_app.stream_pretty.pop((u, s), None)
                        self.main_app.stream_meta.pop((u, s), None)
                        self.main_app.stream_portname.pop((u, s), None)

                for vm in to_close:
                    try:
                        if self.main_app.vmidi:
                            self.main_app.vmidi.close(vm)
                        self.main_app.created_vmidi_ports.discard(vm)
                        print(f"🧹 Closed vmidi port (user left): {vm}")
                    except Exception as e:
                        print("⚠️ Failed to close vmidi port:", vm, e)

                if getattr(self.main_app, "routing_manager_dialog", None):
                    self.main_app.routing_manager_dialog.refresh_table()

        # 🧠 If only the room creator remains, reset WebRTC to accept new joiners
        if self.is_creator and len(users) <= 1:
            print("🔄 All peers left — resetting WebRTC state for future connections.")
            if self.pc:
                asyncio.ensure_future(self.pc.close())
            self.pc = None
            self.channel = None
            self.offer_sent = False

        print(f"👥 Updated user list: {users}")

    def kick_user(self, target_user: str):
        # only leader can kick
        if getattr(self.main_app, "room_leader", None) != self.main_app.username:
            return
        if not self.main_app.ws:
            return

        # confirmation dialog
        reply = QMessageBox.question(
            self,
            "Kick user",
            f"Kick {target_user} from the room?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        asyncio.get_event_loop().create_task(
            self.main_app.ws.send(json.dumps({
                "type": "kick",
                "room": self.room_name,
                "target": target_user
            }))
        )

    def add_user_ui(self, username):
        if username in self.user_ui_elements:
            return

        container = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(container)

        # --- Row 1: name + buttons (horizontal) ---
        top_row = QtWidgets.QHBoxLayout()

        name_label = QtWidgets.QLabel(username)
        top_row.addWidget(name_label)

        top_row.addStretch(1)

        # Assign leader button (only shown to current leader, and not for self)
        assign_btn = QtWidgets.QPushButton("Assign Leader")
        assign_btn.setFixedHeight(22)
        assign_btn.setVisible(False)  # will be controlled by update_leader_ui()
        assign_btn.clicked.connect(lambda _, u=username: self.assign_leader(u))
        top_row.addWidget(assign_btn)
        kick_btn = QtWidgets.QPushButton("Kick")
        kick_btn.setFixedHeight(22)
        kick_btn.setVisible(False)  # controlled by update_leader_ui()
        kick_btn.clicked.connect(lambda _, u=username: self.kick_user(u))
        top_row.addWidget(kick_btn)

        outer.addLayout(top_row)

        # --- Row 2: velocity bar ---
        bar = QtWidgets.QProgressBar()
        bar.setValue(0)
        outer.addWidget(bar)

        # --- Row 3: latency label ---
        latency_label = QtWidgets.QLabel("Latency: -- ms")
        latency_label.setStyleSheet("color: gray; font-size: 10px;")
        if username == self.main_app.username:
            latency_label.hide()
        outer.addWidget(latency_label)

        self.ui.uservelocityLayout.addWidget(container)

        # Store
        self.user_ui_elements[username] = container
        self.user_velocity_bars[username] = bar
        self.user_latency_labels[username] = latency_label

        # Apply visibility rule now (in case leader already known)
        self.update_leader_ui(getattr(self.main_app, "room_leader", None))

    async def start_offer(self):
        # WebRTC απενεργοποιημένο — όλα πάνε μέσω TCP.
        return

        # Ensure PeerConnection exists
        if not self.pc:
            print("🔧 Creating new RTCPeerConnection before sending offer.")
            self.pc = RTCPeerConnection()

        # Create DataChannel safely
        if not self.channel:
            self.channel = self.pc.createDataChannel(
                "midi",
                ordered=False,
                maxRetransmits=0
            )

            @self.channel.on("open")
            def on_open():
                print(f"✅ DataChannel opened for {self.main_app.username}")
                self.send_streams_announce()
            self.channel.on("message", self.on_midi_message)

        # Create SDP offer
        print(f"🟢 {self.main_app.username} creating OFFER now (peer present)")
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        # Send offer to the signaling server
        await self.main_app.ws.send(json.dumps({
            "type": "offer",
            "room": self.room_name,
            "sdp": self.pc.localDescription.sdp,
            "sdpType": self.pc.localDescription.type
        }))

        self.offer_sent = True

    def toggle_local_mute(self):
        self.local_mute = not self.local_mute
        state = "🔇 Muted" if self.local_mute else "🔊 Active"
        self.ui.muteButton.setText(f"Mute Local ({state})")
        print(f"[RoomWindow] Local mute set to {self.local_mute}")

    def is_room_leader(self):
        return getattr(self.main_app, "room_leader", None) == self.main_app.username

    def start_metronome_clicked(self):
        if not self.is_room_leader():
            print("⚠️ Only room leader can start metronome")
            return

        bpm = self.ui.bpmSpinBox.value()
        start_time = time.time() + 3.0

        payload = {
            "type": "metronome_start",
            "bpm": bpm,
            "start_time": start_time,
            "leader": self.main_app.username
        }

        self.start_local_metronome(bpm, start_time)

        self.send_to_room(payload)

        print("▶️ Metronome start sent:", payload)

    def stop_metronome_clicked(self):
        if not self.is_room_leader():
            print("⚠️ Only room leader can stop metronome")
            return

        self.stop_local_metronome()

        payload = {
            "type": "metronome_stop",
            "leader": self.main_app.username
        }

        self.send_to_room(payload)

        print("⏹️ Metronome stop sent")

    def apply_bpm_clicked(self):
        if not self.is_room_leader():
            print("⚠️ Only room leader can change BPM")
            return

        bpm = self.ui.bpmSpinBox.value()
        self.metronome_bpm = bpm

        payload = {
            "type": "metronome_bpm_update",
            "bpm": bpm,
            "leader": self.main_app.username
        }

        self.send_to_room(payload)

        print("🎚️ BPM update sent:", bpm)

    def start_local_metronome(self, bpm, start_time):
        self.metronome_bpm = bpm
        self.metronome_start_time = start_time
        self.metronome_last_beat = -1
        self.metronome_running = True

    def stop_local_metronome(self):
        self.metronome_running = False
        self.metronome_start_time = None
        self.metronome_last_beat = -1

    def metronome_tick(self):
        if not self.metronome_running or self.metronome_start_time is None:
            return

        now = time.time()

        if now < self.metronome_start_time:
            return

        beat_interval = 60.0 / self.metronome_bpm
        elapsed = now - self.metronome_start_time
        beat = int(elapsed / beat_interval)

        if beat != self.metronome_last_beat:
            self.metronome_last_beat = beat

            beat_in_bar = beat % 4

            if beat_in_bar == 0:
                note = 76
                velocity = 110
                print("🔔 METRONOME strong beat")
            else:
                note = 72
                velocity = 80
                print("tick")

            if getattr(self.main_app, "vmidi", None) and getattr(self, "metronome_port_name", None):
                msg_on = Message("note_on", note=note, velocity=velocity, channel=9)
                msg_off = Message("note_off", note=note, velocity=0, channel=9)

                hex_on = " ".join(f"{x:02X}" for x in msg_on.bytes())
                hex_off = " ".join(f"{x:02X}" for x in msg_off.bytes())

                self.main_app.vmidi.send_hex_nowait(self.metronome_port_name, hex_on)

                QtCore.QTimer.singleShot(
                    60,
                    lambda port=self.metronome_port_name, h=hex_off:
                    self.main_app.vmidi.send_hex(port, h)
                )

    def start_recording(self):
        self.recording = True
        self.recorded_events = []
        self.recording_start_time = time.time()

        self.ui.StartButton.setEnabled(False)
        self.ui.StopButton.setEnabled(True)

        print("🔴 Recording started")

    def stop_recording(self):
        if not self.recording:
            return

        self.recording = False
        self.ui.StartButton.setEnabled(True)
        self.ui.StopButton.setEnabled(False)

        if not self.recorded_events:
            print("⚠️ No MIDI events recorded")
            return

        filename = f"midirooms_recording_{self.room_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mid"

        mid = mido.MidiFile()
        track = mido.MidiTrack()
        mid.tracks.append(track)

        ticks_per_beat = mid.ticks_per_beat
        tempo = mido.bpm2tempo(getattr(self, "metronome_bpm", 120))

        track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))

        last_time = 0.0

        for event in self.recorded_events:
            delta_seconds = event["time"] - last_time
            last_time = event["time"]

            delta_ticks = int(mido.second2tick(delta_seconds, ticks_per_beat, tempo))

            msg = Message(
                event["type"],
                note=event["note"],
                velocity=event["velocity"],
                channel=event.get("channel", 0),
                time=max(0, delta_ticks)
            )

            track.append(msg)

        mid.save(filename)

        print(f"💾 Recording saved: {filename}")
        QtWidgets.QMessageBox.information(
            self,
            "Recording Saved",
            f"Recording saved locally as:\n{filename}"
        )

    def process_remote_midi_queue(self):
        if not self.remote_midi_queue:
            return

        now = time.perf_counter()

        ready = []
        while self.remote_midi_queue and self.remote_midi_queue[0]["play_time"] <= now:
            event = self.remote_midi_queue.pop(0)

            lateness_ms = (now - event["play_time"]) * 1000
            if lateness_ms > 35:
                print(f"⏭️ Dropped late MIDI note: {lateness_ms:.1f} ms")
                continue

            ready.append(event)

        for event in ready:
            user = event["user"]
            stream = event["stream"]
            msg_type = event["type"]
            note = event["note"]
            velocity = event["velocity"]

            try:
                msg = Message(msg_type, note=note, velocity=velocity)

                route = self.main_app.routing_config.get((user, stream))
                vmidi_name = None
                channel = 1

                if route:
                    vmidi_name = route.get("vmidi")
                    channel = route.get("channel", 1)

                msg.channel = (int(channel) - 1) if channel else 0

                if vmidi_name and getattr(self.main_app, "vmidi", None):
                    b = msg.bytes()
                    hex_bytes = " ".join(f"{x:02X}" for x in b)
                    ok = self.main_app.vmidi.send_hex(vmidi_name, hex_bytes)
                    vmidi_start = time.perf_counter()
                    vmidi_ms = (time.perf_counter() - vmidi_start) * 1000
                    print(f"VMIDI SEND = {vmidi_ms:.2f} ms")

                    if ok:
                        print(f"🎧 Scheduled {user}/{stream} → {vmidi_name} ch {channel}")
                    else:
                        print(f"❌ Scheduled route failed {user}/{stream} → {vmidi_name}")

            except Exception as e:
                print("⚠️ Scheduled MIDI playback error:", e)
    def record_midi_event(self, user, stream, msg_type, note, velocity, channel=0):
        if not getattr(self, "recording", False):
            return

        # Do not record metronome notes
        if stream == "metronome" or user == "metronome":
            return

        if note is None or velocity is None:
            return

        now = time.time()
        relative_time = now - self.recording_start_time

        self.recorded_events.append({
            "time": relative_time,
            "user": user,
            "stream": stream,
            "type": msg_type,
            "note": note,
            "velocity": velocity,
            "channel": channel
        })
class RoutingManager(QDialog):
    def __init__(self, main_app):
        super().__init__()
        self.main_app = main_app
        self.room_window = getattr(main_app, "room_window", None)
        self.ui = Ui_routingDialog()
        self.ui.setupUi(self)
        self.setWindowTitle("MIDI Port Manager")

        # ── Μεγάλωσε το παράθυρο ──
        self.resize(1000, 600)
        self.setMinimumSize(900, 500)

        # ── Table: stretch να γεμίζει το παράθυρο ──
        table = self.ui.routingTable
        table.setGeometry(QtCore.QRect(10, 10, 980, 520))  # override fixed geometry
        table.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding
        )

        # ── Row height μικρότερο ──
        table.verticalHeader().setDefaultSectionSize(32)
        table.verticalHeader().hide()  # κρύψε τον αριθμό γραμμής αριστερά

        # ── 5 columns με stretch ──
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(
            ["Type", "User / Device", "Stream / Port", "Virtual MIDI Port", "Active"]
        )
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)  # Type
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)  # User/Device
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)  # Stream/Port
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)  # Virtual MIDI Port
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)  # Active

        # ── Κουμπιά κάτω δεξιά ──
        self.ui.horizontalLayoutWidget.setGeometry(QtCore.QRect(700, 545, 280, 40))

        self.ui.refreshButton.clicked.connect(self.refresh_table)
        self.ui.cancelButton.clicked.connect(self.close)

        self.refresh_table()

    def get_midi_outputs(self):
        """Detect available MIDI output ports."""
        try:
            return mido.get_output_names()
        except Exception as e:
            print("⚠️ Could not get MIDI outputs:", e)
            return []

    def refresh_table(self):
        self.room_window = getattr(self.main_app, "room_window", None)
        table = self.ui.routingTable
        table.setRowCount(0)  # καθάρισε πρώτα

        me = self.main_app.username

        # ── ΤΜΗΜΑ Α: Δικές μου συσκευές ──────────────────────────────
        try:
            all_inputs = mido.get_input_names()
        except Exception:
            all_inputs = []

        vmidi_ports_lower = {v.lower() for v in getattr(self.main_app, "created_vmidi_ports", set())}
        metronome_name = (getattr(
            getattr(self.main_app, "room_window", None), "metronome_port_name", ""
        ) or "").lower()

        def is_vmidi_or_metronome(name: str) -> bool:
            n = name.lower()
            # exact match με created ports
            if n in vmidi_ports_lower:
                return True
            # partial match — το Windows προσθέτει " 0" ή παρόμοιο suffix
            for vp in vmidi_ports_lower:
                if n.startswith(vp) or vp.startswith(n):
                    return True
            # metronome με οποιοδήποτε suffix
            if metronome_name and (n.startswith(metronome_name) or metronome_name.startswith(n)):
                return True
            return False

        physical_inputs = [n for n in all_inputs if not is_vmidi_or_metronome(n)]

        selected = set(getattr(self.main_app, "selected_inputs", []) or [])

        for dev_name in physical_inputs:
            row = table.rowCount()
            table.insertRow(row)

            # Col 0: Type
            item_type = QtWidgets.QTableWidgetItem("🎹 Local")
            item_type.setFlags(item_type.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, 0, item_type)

            # Col 1: Device name
            item_dev = QtWidgets.QTableWidgetItem(dev_name)
            item_dev.setFlags(item_dev.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, 1, item_dev)

            # Col 2: stream_id αν είναι ανοιχτή
            stream_id = ""
            if self.room_window:
                for sid, dname in self.room_window.stream_devices.items():
                    if dname == dev_name:
                        stream_id = sid
                        break
            item_stream = QtWidgets.QTableWidgetItem(stream_id if stream_id else "—")
            item_stream.setFlags(item_stream.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, 2, item_stream)

            # Col 3: N/A για local
            item_out = QtWidgets.QTableWidgetItem("(local input)")
            item_out.setFlags(item_out.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, 3, item_out)

            # Col 4: Active checkbox
            chk = QtWidgets.QCheckBox()
            chk.blockSignals(True)  # μην πυροδοτείς signal κατά το setChecked
            chk.setChecked(dev_name in selected)
            chk.blockSignals(False)  # ξαναενεργοποίησε τα signals
            chk.stateChanged.connect(lambda state, d=dev_name: self.toggle_local_device(d, state))
            cell_widget = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(cell_widget)
            layout.addWidget(chk)
            layout.setAlignment(QtCore.Qt.AlignCenter)
            layout.setContentsMargins(0, 0, 0, 0)
            table.setCellWidget(row, 4, cell_widget)

        # ── ΤΜΗΜΑ Β: Remote peers ──────────────────────────────────────
        seen = set(getattr(self.main_app, "seen_streams", set()))
        announced = set(getattr(self.main_app, "stream_pretty", {}).keys())
        routed = set(getattr(self.main_app, "routing_config", {}).keys())
        remote_streams = sorted((seen | announced | routed))
        remote_streams = [(u, s) for (u, s) in remote_streams if u != me]

        locked = getattr(self.main_app, "locked_routes", set())
        routing = getattr(self.main_app, "routing_config", {})
        pretty_map = getattr(self.main_app, "stream_pretty", {})

        for (user, stream) in remote_streams:
            key = (user, stream)
            row = table.rowCount()
            table.insertRow(row)

            # Col 0: Type
            item_type = QtWidgets.QTableWidgetItem("🌐 Remote")
            item_type.setFlags(item_type.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, 0, item_type)

            # Col 1: User
            item_user = QtWidgets.QTableWidgetItem(user)
            item_user.setFlags(item_user.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, 1, item_user)

            # Col 2: Stream pretty name
            pretty = pretty_map.get(key, stream)
            item_stream = QtWidgets.QTableWidgetItem(pretty)
            item_stream.setFlags(item_stream.flags() & ~QtCore.Qt.ItemIsEditable)
            item_stream.setData(QtCore.Qt.UserRole, stream)
            if key in locked:
                item_stream.setToolTip("🔒 Auto-allocated")
            table.setItem(row, 2, item_stream)

            # Col 3: Virtual MIDI port name
            route = routing.get(key, {})
            assigned = route.get("vmidi", "")
            out_text = assigned if assigned else "(allocating...)"
            item_out = QtWidgets.QTableWidgetItem(out_text)
            item_out.setFlags(item_out.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, 3, item_out)

            # Col 4: Active — πάντα ✓ για remote (δεν μπορείς να τα κλείσεις χειροκίνητα)
            item_active = QtWidgets.QTableWidgetItem("✓")
            item_active.setFlags(item_active.flags() & ~QtCore.Qt.ItemIsEditable)
            item_active.setTextAlignment(QtCore.Qt.AlignCenter)
            table.setItem(row, 4, item_active)

    def toggle_local_device(self, dev_name: str, state: int):
        """Ενεργοποιεί ή απενεργοποιεί μια τοπική MIDI συσκευή κατά τη διάρκεια του session."""
        selected = set(getattr(self.main_app, "selected_inputs", []) or [])
        room_win = getattr(self.main_app, "room_window", None)

        if state == QtCore.Qt.Checked:
            # ── ΕΝΕΡΓΟΠΟΙΗΣΗ ──
            selected.add(dev_name)
            self.main_app.selected_inputs = list(selected)
            print(f"➕ Ενεργοποίηση συσκευής: {dev_name}")

            if room_win:
                # άμεση ασύγχρονη επαναφορά
                QtCore.QTimer.singleShot(
                    500, lambda d=dev_name: room_win.try_reopen_device(d)
                )

        else:
            # ── ΑΠΕΝΕΡΓΟΠΟΙΗΣΗ ──
            selected.discard(dev_name)
            self.main_app.selected_inputs = list(selected)
            print(f"➖ Απενεργοποίηση συσκευής: {dev_name}")

            if room_win:
                # βρες και κλείσε το port
                to_remove = []
                for sid, port in list(room_win.midi_inputs):
                    if room_win.stream_devices.get(sid, "").lower() == dev_name.lower():
                        try:
                            port.close()
                        except Exception:
                            pass
                        to_remove.append(sid)

                room_win.midi_inputs = [
                    (s, p) for s, p in room_win.midi_inputs if s not in to_remove
                ]
                for sid in to_remove:
                    room_win.stream_devices.pop(sid, None)
                    room_win.failed_inputs.discard(dev_name.lower())

                # ενημέρωσε τους peers ότι αυτό το stream δεν υπάρχει πια
                room_win.send_streams_announce()
                print(f"🔌 Έκλεισε MIDI input: {dev_name}")

        # ανανέωσε τον πίνακα
        QtCore.QTimer.singleShot(200, self.refresh_table)


class RoomSettingsWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_roomSettingsWindow()
        self.ui.setupUi(self)
        self.setWindowTitle("Create Room")

        self.ui.cancelButton.clicked.connect(self.close)
        self.ui.createRoomButton.clicked.connect(self.accept_settings)


    def accept_settings(self):
        room_name = self.ui.roomNameLineEdit.text().strip()
        max_participants = self.ui.maxParticipantsSpinBox.value()
        recording_enabled = self.ui.recordingCheckBox.isChecked()

        if not room_name:
            QMessageBox.warning(self, "Error", "Please enter a room name")
            return
        if not self.parent().username:
            QMessageBox.warning(self, "No Username", "Username not initialized.")
            return

        if self.parent():
            self.parent().ensure_vmidi_bridge()
            self.parent().selected_room = room_name
            self.parent().max_participants = max_participants
            self.parent().recording_enabled = recording_enabled
            self.parent().hide()
            self.parent().room_window = RoomWindow(self.parent(), room_name=room_name, is_creator=True)
            self.parent().room_window.show()
            self.parent().room_window.add_user_ui(self.parent().username)
            asyncio.get_event_loop().create_task(self.parent().room_window.start_webrtc())

        self.close()


class MidiUserApp(QMainWindow):
    def __init__(self):
        super().__init__()
        import os
        import re
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.username = ""
        # Hide old username input widgets from the main window
        self.ui.usernamelabel.hide()
        self.ui.usernamelineEdit.hide()

        self.username_display = QtWidgets.QLabel(self.ui.centralwidget)
        self.username_display.setObjectName("username_display")
        self.username_display.setGeometry(QtCore.QRect(860, 10, 220, 24))
        self.username_display.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.username_display.setStyleSheet("font-weight: bold;")
        self.username_display.setText("User: -")
        self.selected_room = "lobby"
        self.selected_inputs = []
        self.ui.joinRoomButton.clicked.connect(lambda: asyncio.create_task(self.connect_to_room()))
        self.ui.serverListWidget.itemDoubleClicked.connect(lambda item: asyncio.create_task(self.connect_to_room()))
        self.ui.createRoomButton.clicked.connect(self.open_room_settings)
        self.ui.actionMIDIsettings.triggered.connect(self.open_settings_window)
        asyncio.get_event_loop().create_task(self.listen_for_rooms())
        self.ws = None
        self.room_window = None
        self.room_leader = None
        # ---- MIDIrooms auto-routing state ----
        self.remote_port_pool = [f"MIDIrooms_Remote_{i}" for i in range(1, 65)]
        # ---- virtualMIDI bridge ----
        self.vmidi = None
        self.created_vmidi_ports = set()  # all ports created via bridge (local + remote)
        self.pid = os.getpid()
        self.routing_config = getattr(self, "routing_config", {})
        self.locked_routes = getattr(self, "locked_routes", set())
        self.seen_streams = getattr(self, "seen_streams", set())
        self.stream_meta = getattr(self, "stream_meta", {})
        self.stream_pretty = getattr(self, "stream_pretty", {})
        self.stream_portname = getattr(self, "stream_portname", {})
        self.ensure_vmidi_bridge()
        self.instance_suffix = f"{os.getpid() % 100:02d}"

    def debug_dump_midi_ports(self, label=""):
        try:
            ins = mido.get_input_names()
        except Exception as e:
            ins = [f"<error reading inputs: {e}>"]

        try:
            outs = mido.get_output_names()
        except Exception as e:
            outs = [f"<error reading outputs: {e}>"]

        print(f"\n===== MIDI PORT SNAPSHOT {label} =====")
        print("CREATED BY APP:")
        for x in sorted(getattr(self, "created_vmidi_ports", set())):
            print("  APP:", x)

        print("INPUTS:")
        for x in ins:
            print("  IN :", x)

        print("OUTPUTS:")
        for x in outs:
            print("  OUT:", x)

        print("=====================================\n")

    def ensure_vmidi_bridge(self):
        if getattr(self, "vmidi", None):
            return
        try:
            import os
            exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "midiroomsports.exe")
            print("DEBUG helper exe path:", exe_path)
            self.vmidi = VirtualMidiBridge(exe_path)
            print("✅ vmidi bridge started")
        except Exception as e:
            self.vmidi = None
            print("❌ Could not start midiroomsports.exe:", e)

    def refresh_username_label(self):
        if hasattr(self, "username_display"):
            self.username_display.setText(f"User: {self.username or '-'}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "username_display"):
            self.username_display.setGeometry(self.width() - 240, 10, 220, 24)
    def sanitize_vmidi_name(self, s: str, max_len: int = 31) -> str:
        s = (s or "").strip()
        s = re.sub(r"[^A-Za-z0-9_]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return (s[:max_len] or "midiport")

    def _tag(self, s: str, n: int) -> str:
        s = re.sub(r"[^A-Za-z0-9]+", "", (s or "").strip()).lower()
        return (s[:n] or "x")

    def _dev_tag(self, dev_name: str, n: int = 4) -> str:
        dn = (dev_name or "").lower()
        if "mpk" in dn: return "mpk"
        if "akai" in dn: return "akai"
        if "launchkey" in dn: return "lk"
        if "novation" in dn: return "nov"
        if "keyboard" in dn or "key" in dn: return "key"
        if "pad" in dn: return "pad"
        if "drum" in dn: return "drm"
        return self._tag(dev_name, n)

    def _stream_idx(self, stream_id: str, fallback: int = 1) -> int:
        m = re.search(r"_(\d+)$", stream_id or "")
        return int(m.group(1)) if m else fallback

    def _short_user(self, username: str, n: int = 8) -> str:
        return self._tag(username or "user", n)

    def _short_dev(self, dev_name: str, n: int = 4) -> str:
        return self._dev_tag(dev_name, n)

    def _suffix(self) -> str:
        s = getattr(self, "instance_suffix", "X")
        s = re.sub(r"[^A-Za-z0-9]+", "", str(s))[:2]
        return s or "X"

    def make_local_port_name_default(self) -> str:
        user = self._short_user(getattr(self, "username", "") or "user", 8)
        suf = self._suffix()
        return f"{user}_main_{suf}"[:31]

    def make_remote_port_name(self, sender: str, device: str, stream_id: str) -> str:
        user = self._short_user(sender, 8)
        dev = self._short_dev(device, 4)
        i = self._stream_idx(stream_id, 1)
        suf = self._suffix()
        return f"{user}_{dev}_{i}_{suf}"[:31]

    def make_stream_id(self, dev_name: str, idx: int) -> str:
        base = "".join(c if c.isalnum() else "_" for c in (dev_name or "device")).strip("_")
        base = "_".join(base.split("_"))
        base = base[:24] if base else f"dev{idx}"
        return f"{base}_{idx}"

    async def handle_offer(self, data):
        return

        print("📩 handle_offer called for", self.username)

        # ensure pc exists
        if not self.room_window.pc:
            print("⚠️ handle_offer called before PC initialized!")
        else:
            print("📩 handle_offer using existing PC:", self.room_window.pc)

        desc = RTCSessionDescription(sdp=data["sdp"], type="offer")
        await self.room_window.pc.setRemoteDescription(desc)
        print("📡 Remote description set (offer)")

        answer = await self.room_window.pc.createAnswer()
        await self.room_window.pc.setLocalDescription(answer)
        print("📡 Local answer created")

        await self.ws.send(json.dumps({
            "type": "answer",
            "room": data["room"],
            "sdp": self.room_window.pc.localDescription.sdp,
            "sdpType": self.room_window.pc.localDescription.type
        }))
        print("📤 Sent ANSWER to server")

    async def handle_answer(self, data):
        return

        print("📩 handle_answer called for", self.username)
        desc = RTCSessionDescription(sdp=data["sdp"], type=data.get("sdpType", "answer"))
        await self.room_window.pc.setRemoteDescription(desc)
        print("📡 Remote description set (answer)")

    async def handle_candidate(self, data):
        return
        
        print("📩 handle_candidate called for", self.username)
        from aiortc import RTCIceCandidate
        c = data.get("candidate")
        if c and self.room_window.pc:
            ice = RTCIceCandidate(
                sdpMid=c.get("sdpMid"),
                sdpMLineIndex=c.get("sdpMLineIndex"),
                candidate=c.get("candidate")
            )
            await self.room_window.pc.addIceCandidate(ice)
            print("🧊 Added remote ICE candidate")

    async def listen_for_rooms(self):
        while True:
            try:
                async with websockets.connect(SIGNALING_SERVER) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({
                        "type": "hello",
                        "user": self.username or "Unknown"
                    }))

                    async for message in ws:
                        data = json.loads(message)
                        print("📨 Received message from server:", data.get("type"))
                        if data.get("tcp_midi") or data.get("tcp_relay"):
                            if self.room_window:
                                self.room_window.on_midi_message(json.dumps(data))
                            continue
                        if data.get("type") == "room_list":
                            rooms = data.get("rooms", [])
                            QtCore.QMetaObject.invokeMethod(
                                self,
                                "_update_room_list",
                                QtCore.Qt.QueuedConnection,
                                QtCore.Q_ARG(list, rooms)
                            )
                        elif data.get("type") == "user_list":
                            users = data.get("users", [])
                            if self.room_window:
                                # Update current connected users
                                self.room_window.update_user_list(users)

                                # If I'm the creator and a peer is present, kick off the offer once.
                                if self.room_window.is_creator and not self.room_window.offer_sent and len(users) >= 2:
                                    asyncio.get_event_loop().create_task(self.room_window.start_offer())
                        elif data["type"] == "leader":
                            leader = data.get("leader")
                            if self.room_window and data.get("room") == self.room_window.room_name:
                                self.room_leader = leader
                                self.room_window.update_leader_ui(leader)

                        elif data["type"] == "kicked":
                            if self.room_window and data.get("room") == self.room_window.room_name:
                                by = data.get("by", "leader")
                                QMessageBox.warning(self.room_window, "Kicked", f"You were kicked by {by}.")
                                self.room_window.leave_room()
                        elif data["type"] == "offer":
                            print("📨 Received OFFER message:", data)
                            await self.handle_offer(data)

                        elif data["type"] == "answer":
                            await self.handle_answer(data)

                        elif data["type"] == "candidate":
                            await self.handle_candidate(data)


            except Exception as e:
                print("listen_for_rooms error:", e)
            await asyncio.sleep(2)  # retry

    @QtCore.pyqtSlot(list)
    def _update_room_list(self, rooms):
        self.ui.serverListWidget.clear()
        self.ui.serverListWidget.addItems(rooms)

    async def connect_to_room(self):
        if not self.username:
            QMessageBox.warning(self, "No Username", "Username not initialized.")
            return

        selected = self.ui.serverListWidget.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Room", "Please select a room first.")
            return

        self.selected_room = selected[0].text()
        self.ensure_vmidi_bridge()

        self.hide()
        self.room_window = RoomWindow(self, room_name=self.selected_room, is_creator=False)
        self.room_window.show()
        self.room_window.add_user_ui(self.username)

        loop = asyncio.get_event_loop()
        loop.create_task(self.room_window.start_webrtc())
        await asyncio.sleep(0.2)

    def refresh_midi_state(self):
        print("🔄 Refreshing MIDI state...")

        # Close cached Python-side mido outputs to virtual ports.
        # This does NOT close/destroy the virtual ports themselves.
        try:
            if getattr(self, "vmidi", None):
                for name, out in list(self.vmidi.output_cache.items()):
                    try:
                        out.close()
                    except Exception:
                        pass
                self.vmidi.output_cache.clear()
        except Exception as e:
            print("⚠️ refresh_midi_state output cache cleanup error:", e)

        try:
            print("INPUTS after refresh:", mido.get_input_names())
            print("OUTPUTS after refresh:", mido.get_output_names())
        except Exception as e:
            print("⚠️ refresh_midi_state mido query error:", e)

        print("✅ MIDI state refresh done")
    def open_settings_window(self):
        self.settings_window = SettingsWindow(self)
        self.settings_window.show()

    def open_room_settings(self):
        dlg = RoomSettingsWindow(self)
        dlg.show()

    def closeEvent(self, event):
        # Close ALL vmidi ports we created (local + remote)
        try:
            if getattr(self, "vmidi", None):
                for name in list(self.created_vmidi_ports):
                    try:
                        self.vmidi.close(name)
                    except Exception:
                        pass
                self.created_vmidi_ports.clear()

                try:
                    self.vmidi.shutdown()
                except Exception:
                    pass
        except Exception:
            pass

        event.accept()


class SettingsWindow(QMainWindow):
    def __init__(self, main_app=None):
        super().__init__()
        self.ui = Ui_SettingsForm()
        self.ui.setupUi(self)
        self.setWindowTitle("Settings")
        self.main_app = main_app
        self.ui.applyButton.clicked.connect(self.apply_settings)
        self.ui.cancelButton.clicked.connect(self.close)
        self.load_midi_devices()

    def load_midi_devices(self):
        import mido
        from PyQt5 import QtCore, QtWidgets

        self.ui.inputsList.clear()

        created = getattr(self.main_app, "created_vmidi_ports", set()) if self.main_app else set()

        # Only show REAL inputs (exclude our created ports)
        input_names = [
            n for n in mido.get_input_names()
            if n not in created and not n.lower().startswith("midirooms.")
        ]

        selected_inputs = getattr(self.main_app, "selected_inputs", [])

        for name in input_names:
            item = QtWidgets.QListWidgetItem(name)
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked if name in selected_inputs else QtCore.Qt.Unchecked)
            self.ui.inputsList.addItem(item)

    def showEvent(self, event):
        super().showEvent(event)
        if self.main_app:
            self.main_app.refresh_midi_state()
        self.load_midi_devices()

    def _get_checked_inputs_from_ui(self) -> list[str]:
        selected = []
        for i in range(self.ui.inputsList.count()):
            item = self.ui.inputsList.item(i)
            if item.checkState() == QtCore.Qt.Checked:
                selected.append(item.text())
        return selected

    def apply_settings(self):
        selected_inputs = self._get_checked_inputs_from_ui()
        if self.main_app:
            self.main_app.selected_inputs = selected_inputs
            self.main_app.midi_service_was_refreshed = True
            self.main_app.midi_refresh_time = time.time()

            QMessageBox.information(
                self,
                "MIDI Settings Applied",
                f"Inputs: {selected_inputs}\nLocal DAW monitoring should use the physical MIDI input directly."
            )
        self.close()


if __name__ == "__main__":
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)   # set timer resolution to 1 ms
    app = QtWidgets.QApplication(sys.argv)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    dlg = UsernameDialog()
    if dlg.exec_() != QtWidgets.QDialog.Accepted:
        sys.exit(0)

    window = MidiUserApp()
    window.username = dlg.username
    window.refresh_username_label()
    window.show()

    with loop:
        loop.run_forever()
    ctypes.windll.winmm.timeEndPeriod(1)