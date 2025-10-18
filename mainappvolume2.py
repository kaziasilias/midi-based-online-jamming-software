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
        self.ui.routingmanagerButton.clicked.connect(self.open_routing_manager)
        # WebRTC objects
        self.offer_sent = False
        self.pc = None
        self.channel = None
        # MIDI
        self.midi_input = None
        self.midi_output = None
        self.ui.muteButton.clicked.connect(self.toggle_local_mute)

        try:
            if self.main_app.selected_input:
                self.midi_input = mido.open_input(self.main_app.selected_input)
            if self.main_app.selected_output:
                self.midi_output = mido.open_output(self.main_app.selected_output)
        except Exception as e:
            QMessageBox.critical(self, "MIDI Error", f"Could not open MIDI port:\n{e}")

        # Poll MIDI input
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.poll_midi_input)
        self.timer.start(10)
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
        """Calculate RTT between this user and the sender, then update label."""
        sent_time = data.get("timestamp")
        sender = data.get("from")
        if not sent_time or not sender:
            return

        rtt = (time.time() - sent_time) * 1000  # ms

        # 🎨 color-coding for musical thresholds
        if rtt < 30:
            color = "green"
        elif rtt < 80:
            color = "orange"
        else:
            color = "red"

        if sender in self.user_latency_labels:
            label = self.user_latency_labels[sender]
            label.setText(f"Latency: {rtt:.1f} ms")
            label.setStyleSheet(f"color: {color}; font-size: 10px;")

        print(f"📥 Pong from {sender}: {rtt:.1f} ms")

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
            if data.get("user") == self.main_app.username:
                return
            # 🕒 Handle ping/pong control messages first
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

            elif data.get("type") == "pong" and data.get("to") == self.main_app.username:
                self.handle_pong(data)
                return
            midi_event = json.loads(message)
            user = midi_event.get("user", "Unknown")
            note = midi_event.get("note")
            velocity = midi_event.get("velocity")
            msg_type = midi_event.get("type")
            print("Received MIDI event:", midi_event)
            self.add_user_ui(user)
            # Update velocity bar
            if note is not None and velocity is not None:
                if user in self.user_velocity_bars:
                    self.user_velocity_bars[user].setValue(velocity)
                    QtCore.QTimer.singleShot(
                        300, lambda: self.user_velocity_bars[user].setValue(0)
                    )

            # ✅ Forward to local MIDI output
            if self.midi_output and note is not None and velocity is not None:
                from mido import Message
                try:
                    self.midi_output.send(
                        Message(msg_type, note=note, velocity=velocity)
                    )
                except Exception as e:
                    print("MIDI out error:", e)

            self.ui.logArea.append(f"{user}: {msg_type} note={note} vel={velocity}")
            if hasattr(self, "piano") and data["user"] != self.main_app.username:
                if data["velocity"] > 0:
                    self.piano._highlight(data["note"], True)
                    QtCore.QTimer.singleShot(200, lambda n=data["note"]: self.piano._highlight(n, False))
        except Exception as e:
            print("Failed to parse MIDI message:", e)

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

        # Setup headers (optional if done in UI)
        self.ui.routingTable.setHorizontalHeaderLabels(["User", "Output", "Channel"])

        # Fill the table now
        self.refresh_table()

        # Connect buttons
        self.ui.refreshButton.clicked.connect(self.refresh_table)
        self.ui.saveButton.clicked.connect(self.save_and_close)

    def refresh_table(self):
        """Populate routing table with connected users and available outputs."""
        if not self.main_app.room_window:
            return

        users = list(self.main_app.room_window.user_ui_elements.keys())
        outputs = self.get_midi_outputs()

        self.ui.routingTable.setRowCount(len(users))
        for row, user in enumerate(users):
            # User name (non-editable)
            item = QtWidgets.QTableWidgetItem(user)
            item.setFlags(QtCore.Qt.ItemIsEnabled)
            self.ui.routingTable.setItem(row, 0, item)

            # Output dropdown
            combo = QComboBox()
            combo.addItems(outputs)
            self.ui.routingTable.setCellWidget(row, 1, combo)

            # Channel spinbox
            spin = QSpinBox()
            spin.setRange(1, 16)
            self.ui.routingTable.setCellWidget(row, 2, spin)

    def get_midi_outputs(self):
        """Detect available MIDI output ports."""
        import mido
        return mido.get_output_names()

    def save_and_close(self):
        """Save routing configuration."""
        routing_config = {}
        for row in range(self.ui.routingTable.rowCount()):
            user = self.ui.routingTable.item(row, 0).text()
            output_widget = self.ui.routingTable.cellWidget(row, 1)
            channel_widget = self.ui.routingTable.cellWidget(row, 2)
            output = output_widget.currentText()
            channel = channel_widget.value()
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
            self.main_app.selected_input = selected_input
            self.main_app.selected_output = selected_output
            QMessageBox.information(self, "MIDI", f"Applied:\nInput: {selected_input}\nOutput: {selected_output}")
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
