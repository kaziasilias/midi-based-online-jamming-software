# serverakosvolume2.py
import asyncio
import json
from aiohttp import web

# Room storage: room_name -> set of WebSocket clients
rooms = {}
clients = set()   # connected clients not in a room
usernames = {}  # maps ws -> username
room_leader = {}  # room_name -> username

async def broadcast_leader(room):
    if room not in rooms:
        return
    leader = room_leader.get(room)
    msg = json.dumps({"type": "leader", "room": room, "leader": leader})
    dead = []
    for peer in list(rooms[room]):
        try:
            await peer.send_str(msg)
        except Exception:
            dead.append(peer)
    for peer in dead:
        rooms[room].discard(peer)
        usernames.pop(peer, None)

async def broadcast_room_list():
    room_names = list(rooms.keys())
    message = json.dumps({"type": "room_list", "rooms": room_names})

    dead = []

    # Send to users in rooms
    for peers in rooms.values():
        for ws in list(peers):
            try:
                await ws.send_str(message)
            except Exception:
                dead.append(ws)

    # Send to idle clients
    for ws in list(clients):
        try:
            await ws.send_str(message)
        except Exception:
            dead.append(ws)

    # Cleanup dead connections
    for ws in dead:
        clients.discard(ws)
        for peers in rooms.values():
            peers.discard(ws)

async def broadcast_user_list(room):
    """Send the updated user list to all clients in a specific room."""
    if room not in rooms:
        return
    user_list = [usernames.get(peer, "Unknown") for peer in rooms[room]]
    message = json.dumps({"type": "user_list", "users": user_list})

    dead = []
    for peer in list(rooms[room]):
        try:
            await peer.send_str(message)
        except Exception:
            dead.append(peer)

    # Cleanup broken sockets
    for peer in dead:
        rooms[room].discard(peer)
        usernames.pop(peer, None)

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # απενεργοποίηση Nagle algorithm (TCP_NODELAY) για χαμηλό latency
    try:
        import socket as _socket
        sock = request.transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
    except Exception as e:
        print("⚠️ Could not set TCP_NODELAY:", e)

    await ws.send_str(json.dumps({"type": "room_list", "rooms": list(rooms.keys())}))
    clients.add(ws)

    room = None
    username = None

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue

            data = json.loads(msg.data)
            mtype = data.get("type")

            # ---------- JOIN ----------
            if mtype == "join":
                room = data["room"]
                username = data.get("user", "Unknown")
                usernames[ws] = username

                clients.discard(ws)

                if room not in rooms:
                    rooms[room] = set()
                rooms[room].add(ws)

                # ✅ Tip #3: creator/first joiner becomes leader
                # (In your app, the creator is the first joiner.)
                if room not in room_leader:
                    room_leader[room] = username

                await broadcast_user_list(room)
                await broadcast_leader(room)
                await broadcast_room_list()

                print(f"👤 {username} joined room '{room}' (leader={room_leader.get(room)})")
                continue

            # ---------- LEAVE ----------
            if mtype == "leave":
                target_room = data.get("room")
                leaving_user = usernames.get(ws, "Unknown")

                if target_room and target_room in rooms and ws in rooms[target_room]:
                    rooms[target_room].discard(ws)
                    clients.add(ws)

                    # Keep username available for future actions/rejoin.
                    usernames[ws] = leaving_user

                    # Leader left -> pick random remaining user (fallback)
                    if room_leader.get(target_room) == leaving_user:
                        if rooms[target_room]:
                            # random leader among remaining
                            new_ws = next(iter(rooms[target_room]))
                            room_leader[target_room] = usernames.get(new_ws, "Unknown")
                        else:
                            room_leader.pop(target_room, None)

                    # delete empty room
                    if not rooms[target_room]:
                        rooms.pop(target_room, None)
                        room_leader.pop(target_room, None)
                    else:
                        await broadcast_user_list(target_room)
                        await broadcast_leader(target_room)

                    await broadcast_room_list()

                print(f"👋 {leaving_user} left room '{target_room}'")
                continue

            # ---------- LEADER CHOOSES NEXT LEADER ----------
            # This is what your GUI will call.
            if mtype == "assign_leader":
                target_room = data.get("room")
                target_user = data.get("target")

                sender = usernames.get(ws, None)
                if not target_room or target_room not in rooms or not sender:
                    continue

                # only current leader can assign
                if room_leader.get(target_room) != sender:
                    continue

                # target must be currently in the room
                ok = False
                for peer in rooms[target_room]:
                    if usernames.get(peer) == target_user:
                        ok = True
                        break
                if not ok:
                    continue

                room_leader[target_room] = target_user
                await broadcast_leader(target_room)
                print(f"👑 Leader changed in '{target_room}': {sender} -> {target_user}")
                continue

            # ---------- KICK (leader only) ----------
            if mtype == "kick":
                target_room = data.get("room")
                target_user = data.get("target")

                sender = usernames.get(ws, None)
                if not target_room or target_room not in rooms or not sender:
                    continue

                # only leader can kick
                if room_leader.get(target_room) != sender:
                    continue

                # find ws for target
                target_ws = None
                for peer in rooms[target_room]:
                    if usernames.get(peer) == target_user:
                        target_ws = peer
                        break

                if target_ws:
                    try:
                        await target_ws.send_str(json.dumps({
                            "type": "kicked",
                            "room": target_room,
                            "by": sender
                        }))
                    except Exception:
                        pass

                    rooms[target_room].discard(target_ws)
                    usernames.pop(target_ws, None)

                    # if room empty -> delete
                    if not rooms[target_room]:
                        rooms.pop(target_room, None)
                        room_leader.pop(target_room, None)
                    else:
                        # if kicked user was leader (rare), pick random remaining
                        if room_leader.get(target_room) == target_user:
                            new_ws = next(iter(rooms[target_room]))
                            room_leader[target_room] = usernames.get(new_ws, "Unknown")
                        await broadcast_user_list(target_room)
                        await broadcast_leader(target_room)

                    await broadcast_room_list()

                continue

            # ---------- TCP relay (MIDI notes + control messages) ----------
            if data.get("tcp_midi") or data.get("tcp_relay"):
                target_room = data.get("room", room)
                if target_room and target_room in rooms:
                    for peer in list(rooms.get(target_room, set())):
                        if peer is not ws:
                            try:
                                await peer.send_str(msg.data)
                            except Exception:
                                pass
                continue
            # ---------- WebRTC signaling relay ----------
            if mtype in ["offer", "answer", "candidate"]:
                target_room = data.get("room")
                for peer in rooms.get(target_room, set()):
                    if peer != ws:
                        await peer.send_str(json.dumps(data))
                continue

            # ---------- relay everything else ----------
            if room and room in rooms:
                for peer in list(rooms[room]):
                    if peer is not ws:
                        await peer.send_str(msg.data)

    finally:
        # cleanup on disconnect/crash
        clients.discard(ws)
        leaving_user = usernames.pop(ws, None)

        empty_rooms = []
        for r, peers in list(rooms.items()):
            if ws in peers:
                peers.discard(ws)
                print(f"❌ {leaving_user or 'Unknown'} disconnected from room '{r}'")

                # leader left -> random remaining
                if room_leader.get(r) == leaving_user:
                    if peers:
                        new_ws = next(iter(peers))
                        room_leader[r] = usernames.get(new_ws, "Unknown")
                    else:
                        room_leader.pop(r, None)

                if peers:
                    await broadcast_user_list(r)
                    await broadcast_leader(r)
                else:
                    empty_rooms.append(r)

        for r in empty_rooms:
            rooms.pop(r, None)
            room_leader.pop(r, None)

        await broadcast_room_list()

    return ws

app = web.Application()
app.router.add_get("/ws", websocket_handler)

if __name__ == "__main__":
    web.run_app(app, port=8080)
