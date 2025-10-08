# mainappvolume2.py
import sys, asyncio, json, time
from PyQt5 import QtWidgets, QtCore
from PyQt5.QtWidgets import QApplication, QMainWindow, QMessageBox, QWidget
from midiuser_ui import Ui_MainWindow
from midisettings_ui import Ui_Form
from roomform_ui import Ui_roomwindow
from roomsettings_ui import Ui_roomSettingsWindow
import mido
from mido import Message
import websockets
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer
from qasync import QEventLoop
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


class RoomWindow(QWidget):
    def __init__(self, main_app, room_name="lobby"):
        super().__init__()
        self.main_app = main_app
        self.room_name = room_name
        self.ui = Ui_roomwindow()
        self.ui.setupUi(self)
        self.setWindowTitle(f"Jam Room - {room_name}")
        self.ui.LeaveButton.clicked.connect(self.leave_room)
        self.ui.logArea.clear()
        self.user_ui_elements = {}
        self.user_latency_labels = {}
        self.add_user_ui(self.main_app.username)  # show myself

        # WebRTC objects
        self.pc = None
        self.channel = None

        # MIDI
        self.midi_input = None
        self.midi_output = None
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

        # Start WebRTC

    def leave_room(self):
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

        # Tell the server we want to join a room
        await self.main_app.ws.send(json.dumps({
            "type": "join",
            "room": self.room_name,
            "user": self.main_app.username
        }))
        self.add_user_ui(self.main_app.username)
        # Create peer connection + data channel
        self.pc = RTCPeerConnection()
        self.channel = self.pc.createDataChannel("midi")
        self.channel.on("message", self.on_midi_message)

    def poll_midi_input(self):
        if not self.midi_input or not self.channel or self.channel.readyState != "open":
            return
        for msg in self.midi_input.iter_pending():
            self.ui.logArea.append(f"🎹 {msg}")
            midi_event = {
                "user": self.main_app.username,
                "note": getattr(msg, "note", None),
                "velocity": getattr(msg, "velocity", None),
                "type": msg.type
            }
            self.channel.send(json.dumps(midi_event))

    def on_midi_message(self, message):
        try:
            midi_event = json.loads(message)
            user = midi_event.get("user", "Unknown")
            note = midi_event.get("note")
            velocity = midi_event.get("velocity")
            msg_type = midi_event.get("type")

            self.add_user_ui(user)

            # Update velocity bar
            if note is not None and velocity is not None:
                if user in self.user_ui_elements:
                    self.user_ui_elements[user].setValue(velocity)
                    QtCore.QTimer.singleShot(
                        300, lambda: self.user_ui_elements[user].setValue(0)
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

    def add_user_ui(self, username):
        if username in self.user_ui_elements:
            return

        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)

        bar = QtWidgets.QProgressBar()
        bar.setValue(0)

        label = QtWidgets.QLabel(username)
        latency_label = QtWidgets.QLabel("Latency: -- ms")
        latency_label.setStyleSheet("color: gray; font-size: 10px;")

        layout.addWidget(bar)
        layout.addWidget(label)
        layout.addWidget(latency_label)

        self.ui.uservelocityLayout.addWidget(container)

        self.user_ui_elements[username] = bar
        self.user_latency_labels[username] = latency_label


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
            self.parent().room_window = RoomWindow(self.parent(), room_name=room_name)
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

        self.ui.joinRoomButton.clicked.connect(self.connect_to_room)
        self.ui.createRoomButton.clicked.connect(self.open_room_settings)
        self.ui.actionMIDIsettings.triggered.connect(self.open_settings_window)
        asyncio.get_event_loop().create_task(self.listen_for_rooms())
        self.ws = None
        self.room_window = None

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
                                for u in users:
                                    self.room_window.add_user_ui(u)

            except Exception as e:
                print("listen_for_rooms error:", e)
            await asyncio.sleep(2)  # retry

    @QtCore.pyqtSlot(list)
    def _update_room_list(self, rooms):
        self.ui.serverListWidget.clear()
        self.ui.serverListWidget.addItems(rooms)

    def connect_to_room(self):
        username = self.ui.usernamelineEdit.text().strip()
        if not username:
            QMessageBox.warning(self, "No Username", "Please enter a username.")
            return

        selected_items = self.ui.serverListWidget.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Room", "Please select a room first.")
            return

        self.username = username
        self.selected_room = selected_items[0].text()
        self.hide()

        self.room_window = RoomWindow(self, room_name=self.selected_room)
        self.room_window.show()
        self.room_window.add_user_ui(self.username)
        asyncio.get_event_loop().create_task(self.room_window.start_webrtc())

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
