# mainappvolume2.py
import sys, asyncio, json, time
from PyQt5 import QtWidgets, QtCore
from PyQt5.QtWidgets import QApplication, QMainWindow, QMessageBox, QWidget
from midiuser_ui import Ui_MainWindow
from midisettings_ui import Ui_Form
from roomform_ui import Ui_roomwindow
from roomsettings_ui import Ui_roomSettingsWindow
from routingmanager_ui import Ui_routingDialog
from PyQt5.QtWidgets import QDialog, QComboBox, QSpinBox
import mido
import time
from PyQt5.QtWidgets import QPushButton, QHBoxLayout, QWidget
from mido import Message
import websockets
from qasync import QEventLoop
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription

# WebRTC signaling server
SIGNALING_SERVER = "ws://localhost:8080/ws"  # change to your VPS

# ICE servers: STUN first, TURN fallback
ice_config = RTCConfiguration(iceServers=[
    RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
    RTCIceServer(
        urls=["turn:your.vps.address:3478"],
        username="webrtcuser",
        credential="strongpassword"
    )
])


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
        self.is_creator = is_creator
        self.ui = Ui_roomwindow()
        self.ui.setupUi(self)
        self.piano = PianoWidget(self)
        layout = QtWidgets.QHBoxLayout(self.ui.PianoWidget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.piano)
        self.setWindowTitle(f"Jam Room - {room_name}")
        self.ui.LeaveButton.clicked.connect(self.leave_room)
        self.ui.logArea.clear()
        self.user_ui_elements = {}
        self.user_velocity_bars = {}
        self.user_latency_labels = {}
        self.connected_users = []
        self.add_user_ui(self.main_app.username)  # show myself
        self.ui.muteButton.clicked.connect(self.toggle_local_mute)
        self.ui.routingmanagerButton.clicked.connect(self.open_routing_manager)
        # WebRTC objects
        self.offer_sent = False
        self.pc = None
        self.channel = None
        # Use preselected ports from settings
        self.midi_input = None
        self.midi_output = None

        from mido import open_input, open_output

        # Initialize MIDI I/O based on saved settings
        self.midi_input = None
        self.midi_output = None

        selected_in = getattr(self.main_app, "selected_input", None)
        selected_out = getattr(self.main_app, "selected_output", None)

        try:
            if selected_in:
                self.midi_input = open_input(selected_in)
                print(f"🎹 Using selected MIDI input: {selected_in}")
            if selected_out:
                self.midi_output = open_output(selected_out)
                print(f"🎧 Using selected MIDI output: {selected_out}")
        except Exception as e:
            QMessageBox.critical(self, "MIDI Error", f"Could not open MIDI port:\n{e}")

        # Poll MIDI input
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.poll_midi_input)
        self.timer.start(2)
        self.ping_timer = QtCore.QTimer()
        self.ping_timer.timeout.connect(lambda: asyncio.ensure_future(self.send_ping()))
        self.ping_timer.start(3000)  # every 3 seconds

    def open_routing_manager(self):
        dlg = RoutingManager(self.main_app)
        dlg.exec_()

    async def send_ping(self):
        """Periodically send ping messages to every other connected user."""
        if not self.channel or getattr(self.channel, "readyState", None) != "open":
            return

        now = time.time()
        # make sure connected_users exists
        for peer in getattr(self, "connected_users", []):
            if peer == self.main_app.username:
                continue  # skip self
            payload = {
                "type": "ping",
                "from": self.main_app.username,
                "to": peer,
                "timestamp": now,
            }
            try:
                self.channel.send(json.dumps(payload))
                print(f"📤 Sent ping from {self.main_app.username} → {peer}")
            except Exception as e:
                print("⚠️ Failed to send ping:", e)

    def handle_pong(self, data):
        sent_time = data.get("timestamp")
        sender = data.get("from")

        rtt = (time.time() - sent_time) * 1000  # ms

        # NEW: store RTT samples per peer
        if not hasattr(self, "rtt_samples"):
            self.rtt_samples = {}

        if sender not in self.rtt_samples:
            self.rtt_samples[sender] = []

        self.rtt_samples[sender].append(rtt)

        # Limit to last 100 samples (optional)
        self.rtt_samples[sender] = self.rtt_samples[sender][-100:]

        # Compute jitter
        import statistics
        if len(self.rtt_samples[sender]) > 2:
            jitter = statistics.stdev(self.rtt_samples[sender])
        else:
            jitter = 0.0

        # Update label
        if sender in self.user_latency_labels:
            label = self.user_latency_labels[sender]
            label.setText(f"Latency: {rtt:.1f} ms  |  Jitter: {jitter:.1f} ms")

        # Start WebRTC

    def on_datachannel(self, channel):
        self.channel = channel
        self.channel.on("message", self.on_midi_message)

    def leave_room(self):
        if self.pc:
            asyncio.ensure_future(self.pc.close())
        self.pc = None
        self.channel = None
        self.offer_sent = False

        if self.main_app.ws:
            asyncio.get_event_loop().create_task(
                self.main_app.ws.send(json.dumps({
                    "type": "leave",
                    "room": self.room_name,
                    "user": self.main_app.username
                }))
            )

        if self.channel:
            self.channel.close()
        if self.pc:
            asyncio.ensure_future(self.pc.close())

        self.main_app.show()
        self.close()

    async def start_webrtc(self):
        if not self.main_app.ws:
            print("No signaling connection")
            return

        # 1) JOIN FIRST so server knows our username & room
        await self.main_app.ws.send(json.dumps({
            "type": "join",
            "room": self.room_name,
            "user": self.main_app.username
        }))

        # 2) Create the PeerConnection immediately (with ICE config)
        self.pc = RTCPeerConnection(ice_config)

        # 3) ICE candidates handler
        @self.pc.on("icecandidate")
        async def on_icecandidate(event):
            if event.candidate:
                await self.main_app.ws.send(json.dumps({
                    "type": "candidate",
                    "room": self.room_name,
                    "candidate": {
                        "candidate": event.candidate.candidate,
                        "sdpMid": event.candidate.sdpMid,
                        "sdpMLineIndex": event.candidate.sdpMLineIndex,
                    }
                }))

        # 4) If the OTHER side creates the DataChannel (joiner path)
        @self.pc.on("datachannel")
        def on_datachannel(channel):
            print(f"📥 DataChannel received by {self.main_app.username}")
            self.channel = channel

            @channel.on("open")
            def on_open():
                print(f"✅ DataChannel opened for {self.main_app.username}")

            self.channel.on("message", self.on_midi_message)

        # 5) Do NOT send an offer here. The creator will send it later,
        #    when a second user is present (user_list handler will trigger it).
        if self.is_creator:
            print(f"🟡 {self.main_app.username} (creator) joined and is waiting for a peer before sending OFFER.")
        else:
            print(f"🟡 {self.main_app.username} (joiner) waiting for OFFER")

    def poll_midi_input(self):
        if not self.midi_input:
            return

        for msg in self.midi_input.iter_pending():
            print("🎹 Got MIDI message:", msg)

            if msg.type in ("note_on", "note_off"):
                me = self.main_app.username
                note = getattr(msg, "note", None)
                velocity = getattr(msg, "velocity", None)

                # --- Local visualization ---
                if hasattr(self, "piano"):
                    self.piano._highlight(note, velocity > 0)

                if me in self.user_velocity_bars:
                    self.user_velocity_bars[me].setValue(velocity)
                    QtCore.QTimer.singleShot(300, lambda: self.user_velocity_bars[me].setValue(0))

                # --- Local playback (respect mute) ---
                if not getattr(self, "local_mute", False) and self.midi_output:
                    from mido import Message
                    try:
                        self.midi_output.send(Message(msg.type, note=note, velocity=velocity))
                    except Exception as e:
                        print("⚠️ MIDI out error (local):", e)
                else:
                    if getattr(self, "local_mute", False):
                        print("🔇 Local playback muted, skipping local output")
                # --- Send to others over WebRTC ---
                # Skip network-originated messages (to prevent echo)
                if getattr(msg, "_from_network", False):
                    continue  # don't resend notes that came from network

                if self.channel and getattr(self.channel, "readyState", None) == "open":
                    midi_event = {
                        "user": me,
                        "note": note,
                        "velocity": velocity,
                        "type": msg.type,
                    }
                    self.channel.send(json.dumps(midi_event))
                    print("📤 Sent MIDI event:", midi_event)
                else:
                    print("⏳ Channel not open, skipping send")


    def on_midi_message(self, message):
        try:
            data = json.loads(message)

            # --- Handle ping/pong control ---
            if data.get("type") == "ping" and data.get("to") == self.main_app.username:
                reply = {
                    "type": "pong",
                    "from": self.main_app.username,
                    "to": data["from"],
                    "timestamp": data["timestamp"],
                }
                if self.channel and getattr(self.channel, "readyState", None) == "open":
                    self.channel.send(json.dumps(reply))
                return

            if data.get("type") == "pong" and data.get("to") == self.main_app.username:
                self.handle_pong(data)
                return

            # --- Skip my own loopback ---
            if data.get("user") == self.main_app.username:
                return

            # Mark this message as coming from the network
            data["_from_network"] = True

            user = data.get("user", "Unknown")
            note = data.get("note")
            velocity = data.get("velocity")
            msg_type = data.get("type")

            print("🎵 Received MIDI event:", data)
            self.add_user_ui(user)

            # Update velocity bar
            if user in self.user_velocity_bars and velocity is not None:
                self.user_velocity_bars[user].setValue(velocity)
                QtCore.QTimer.singleShot(300, lambda: self.user_velocity_bars[user].setValue(0))

            # --- Local playback for remote notes ---
            # --- Routed playback for remote notes ---
            if note is not None and velocity is not None:
                from mido import Message, open_output
                try:
                    msg = Message(msg_type, note=note, velocity=velocity)
                    msg.dict().update({"_from_network": True})


                    # Look up routing config
                    routed_port = None
                    if hasattr(self.main_app, "routing_config"):
                        route = self.main_app.routing_config.get(user)
                        if route:
                            port_name = route.get("output")
                            channel = route.get("channel", 1)
                            msg.channel = channel - 1
                            if port_name:
                                routed_port = open_output(port_name)

                    if routed_port:
                        routed_port.send(msg)
                        routed_port.close()
                        print(f"🎧 Routed {user}'s note to {port_name} (ch {channel})")
                    elif self.midi_output:
                        self.midi_output.send(msg)
                        print(f"🎧 Default output used for {user}")
                except Exception as e:
                    print("⚠️ MIDI out error:", e)

            # --- GUI Piano highlight ---
            self.ui.logArea.append(f"{user}: {msg_type} note={note} vel={velocity}")
            if hasattr(self, "piano"):
                if velocity > 0:
                    self.piano._highlight(note, True)
                    QtCore.QTimer.singleShot(200, lambda n=note: self.piano._highlight(n, False))

        except Exception as e:
            print("❌ Failed to parse MIDI message:", e)

    def send_midi(self, note, velocity):
        if self.channel and self.channel.readyState == "open":
            midi_event = {
                "user": self.main_app.username,
                "note": note,
                "velocity": velocity
            }
            self.channel.send(json.dumps(midi_event))

    def update_user_list(self, users):
        """Synchronize UI with the current list of connected users."""
        self.connected_users = users

        # 1️⃣ Add any new users not yet in the UI
        for username in users:
            if username not in self.user_ui_elements:
                self.add_user_ui(username)

        # 2️⃣ Remove any users who have left
        for username in list(self.user_ui_elements.keys()):
            if username not in users:
                container = self.user_ui_elements.pop(username)
                container.setParent(None)
                container.deleteLater()

                # Also clean up velocity + latency bars
                self.user_velocity_bars.pop(username, None)
                self.user_latency_labels.pop(username, None)
        # 🧠 If only the room creator remains, reset WebRTC to accept new joiners
        if self.is_creator and len(users) <= 1:
            print("🔄 All peers left — resetting WebRTC state for future connections.")
            if self.pc:
                asyncio.ensure_future(self.pc.close())
            self.pc = None
            self.channel = None
            self.offer_sent = False

        print(f"👥 Updated user list: {users}")

    def add_user_ui(self, username):
        if username in self.user_ui_elements:
            return

        # Create a container for the whole user section
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)

        name_label = QtWidgets.QLabel(username)
        bar = QtWidgets.QProgressBar()
        bar.setValue(0)

        latency_label = QtWidgets.QLabel("Latency: -- ms")
        latency_label.setStyleSheet("color: gray; font-size: 10px;")
        if username == self.main_app.username:
            latency_label.hide()

        layout.addWidget(name_label)
        layout.addWidget(bar)
        layout.addWidget(latency_label)

        self.ui.uservelocityLayout.addWidget(container)

        # Now store the whole container, not just the bar
        self.user_ui_elements[username] = container
        self.user_velocity_bars[username] = bar
        self.user_latency_labels[username] = latency_label

    async def start_offer(self):
        # Prevent duplicate offers
        if self.offer_sent:
            return

        # Ensure PeerConnection exists
        if not self.pc:
            print("🔧 Creating new RTCPeerConnection before sending offer.")
            self.pc = RTCPeerConnection()

        # Create DataChannel safely
        if not self.channel:
            self.channel = self.pc.createDataChannel("midi")

            @self.channel.on("open")
            def on_open():
                print(f"✅ DataChannel opened for {self.main_app.username}")

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


class RoutingManager(QDialog):
    def __init__(self, main_app):
        super().__init__()
        self.main_app = main_app
        self.ui = Ui_routingDialog()
        self.ui.setupUi(self)
        self.setWindowTitle("Routing Manager")

        self.ui.routingTable.setHorizontalHeaderLabels(["User", "Output", "Channel"])

        # Connect buttons
        self.ui.refreshButton.clicked.connect(self.refresh_table)
        self.ui.saveButton.clicked.connect(self.save_and_close)

        # Load current config
        self.refresh_table()

    def get_midi_outputs(self):
        """Detect available MIDI output ports."""
        try:
            return mido.get_output_names()
        except Exception as e:
            print("⚠️ Could not get MIDI outputs:", e)
            return []

    def refresh_table(self):
        """Populate routing table with connected users and available outputs."""
        if not self.main_app.room_window:
            return

        users = list(self.main_app.room_window.user_ui_elements.keys())
        outputs = self.get_midi_outputs()
        self.ui.routingTable.setRowCount(len(users))

        for row, user in enumerate(users):
            # User column
            user_item = QtWidgets.QTableWidgetItem(user)
            user_item.setFlags(QtCore.Qt.ItemIsEnabled)
            self.ui.routingTable.setItem(row, 0, user_item)

            # Output dropdown
            output_combo = QComboBox()
            output_combo.addItems(outputs)

            existing_output = ""
            if user in self.main_app.routing_config:
                existing_output = self.main_app.routing_config[user].get("output", "")
            elif getattr(self.main_app, "selected_output", None):
                existing_output = self.main_app.selected_output  # fallback

            idx = output_combo.findText(existing_output)
            if idx >= 0:
                output_combo.setCurrentIndex(idx)

            self.ui.routingTable.setCellWidget(row, 1, output_combo)

            # Channel spin box
            spin = QSpinBox()
            spin.setRange(1, 16)
            if user in self.main_app.routing_config:
                existing_ch = self.main_app.routing_config[user].get("channel", 1)
                spin.setValue(existing_ch)
            self.ui.routingTable.setCellWidget(row, 2, spin)

    def save_and_close(self):
        """Save routing configuration."""
        routing_config = {}
        for row in range(self.ui.routingTable.rowCount()):
            user_item = self.ui.routingTable.item(row, 0)
            if not user_item:
                continue
            user = user_item.text()

            output_widget = self.ui.routingTable.cellWidget(row, 1)
            channel_widget = self.ui.routingTable.cellWidget(row, 2)
            output = output_widget.currentText() if output_widget else ""
            channel = channel_widget.value() if channel_widget else 1
            routing_config[user] = {"output": output, "channel": channel}

        self.main_app.routing_config = routing_config
        print("🎛️ Routing Config Updated:", routing_config)
        self.accept()


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
            username = self.parent().ui.usernamelineEdit.text().strip()
            if not username:
                QMessageBox.warning(self, "No Username", "Please enter a username.")
                return
            self.parent().username = username

        if self.parent():
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
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.username = ""
        self.selected_room = "lobby"
        self.selected_input = ""
        self.selected_output = ""
        self.ui.joinRoomButton.clicked.connect(lambda: asyncio.create_task(self.connect_to_room()))
        self.ui.createRoomButton.clicked.connect(self.open_room_settings)
        self.ui.actionMIDIsettings.triggered.connect(self.open_settings_window)
        asyncio.get_event_loop().create_task(self.listen_for_rooms())
        self.ws = None
        self.routing_config = {}
        self.room_window = None

    async def handle_offer(self, data):
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
        print("📩 handle_answer called for", self.username)
        desc = RTCSessionDescription(sdp=data["sdp"], type=data.get("sdpType", "answer"))
        await self.room_window.pc.setRemoteDescription(desc)
        print("📡 Remote description set (answer)")

    async def handle_candidate(self, data):
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
                        if data["type"] == "room_list":
                            rooms = data.get("rooms", [])
                            QtCore.QMetaObject.invokeMethod(
                                self,
                                "_update_room_list",
                                QtCore.Qt.QueuedConnection,
                                QtCore.Q_ARG(list, rooms)
                            )
                        elif data["type"] == "user_list":
                            users = data.get("users", [])
                            if self.room_window:
                                # Update current connected users
                                self.room_window.update_user_list(users)

                                # If I'm the creator and a peer is present, kick off the offer once.
                                if self.room_window.is_creator and not self.room_window.offer_sent and len(users) >= 2:
                                    asyncio.get_event_loop().create_task(self.room_window.start_offer())


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
        username = self.ui.usernamelineEdit.text().strip()
        if not username:
            QMessageBox.warning(self, "No Username", "Please enter a username.")
            return

        selected = self.ui.serverListWidget.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Room", "Please select a room first.")
            return

        self.username = username
        self.selected_room = selected[0].text()

        self.hide()
        self.room_window = RoomWindow(self, room_name=self.selected_room, is_creator=False)
        self.room_window.show()
        self.room_window.add_user_ui(self.username)

        loop = asyncio.get_event_loop()
        loop.create_task(self.room_window.start_webrtc())
        # Wait a brief moment so the peer connection exists before offers arrive
        await asyncio.sleep(0.2)

    def open_settings_window(self):
        self.settings_window = SettingsWindow(self)
        self.settings_window.show()

    def open_room_settings(self):
        dlg = RoomSettingsWindow(self)
        dlg.show()


class SettingsWindow(QMainWindow):
    def __init__(self, main_app=None):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)
        self.setWindowTitle("Settings")
        self.ui.inputCombo.addItems(mido.get_input_names())
        self.ui.outputCombo.addItems(mido.get_output_names())
        self.main_app = main_app
        self.ui.applyButton.clicked.connect(self.apply_settings)
        self.ui.cancelButton.clicked.connect(self.close)

    def apply_settings(self):
        selected_input = self.ui.inputCombo.currentText()
        selected_output = self.ui.outputCombo.currentText()

        if self.main_app:
            # Save globally on the main app for later use
            self.main_app.selected_input = selected_input
            self.main_app.selected_output = selected_output

            # Also store under consistent names used by RoomWindow
            self.main_app.midi_input_name = selected_input
            self.main_app.midi_output_name = selected_output

            QMessageBox.information(
                self,
                "MIDI Settings Applied",
                f"Input: {selected_input}\nOutput: {selected_output}"
            )

        self.close()


def main():
    app = QApplication(sys.argv)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MidiUserApp()
    window.show()

    with loop:
        loop.run_forever()



if __name__ == "__main__":
    main()
