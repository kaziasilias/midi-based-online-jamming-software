# serverakosvolume2.py
import asyncio
import json
from aiohttp import web

# Room storage: room_name -> set of WebSocket clients
rooms = {}
clients = set()   # connected clients not in a room
usernames = {}  # maps ws -> username


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
    await ws.send_str(json.dumps({"type": "room_list", "rooms": list(rooms.keys())}))
    clients.add(ws)

    room = None
    username = None

    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            data = json.loads(msg.data)

            if data["type"] == "join":
                room = data["room"]
                username = data.get("user", "Unknown")
                usernames[ws] = username

                if ws in clients:
                    clients.remove(ws)

                if room not in rooms:
                    rooms[room] = set()
                rooms[room].add(ws)
                # Broadcast full user list to the room
                await broadcast_user_list(room)

                print(f"👤 {username} joined room '{room}'")
                await broadcast_room_list()
                continue

            if data["type"] == "leave":
                room = data["room"]
                username = usernames.get(ws, "Unknown")

                if room in rooms and ws in rooms[room]:
                    rooms[room].remove(ws)
                    usernames.pop(ws, None)  # ✅ remove from usernames
                    print(f"👋 {username} left room '{room}'")
                    if not rooms[room]:
                        del rooms[room]

                # Now broadcast updated user list
                if room in rooms:
                    await broadcast_user_list(room)

                await broadcast_room_list()
                continue

            # Handle WebRTC signaling messages
            if data["type"] in ["offer", "answer", "candidate"]:
                print(f"📤 Relaying {data['type'].upper()} from {usernames.get(ws, 'Unknown')} in room {data['room']}")
                target_room = data["room"]
                for peer in rooms.get(target_room, []):
                    if peer != ws:
                        await peer.send_str(json.dumps(data))
                continue

            # Relay any other data (like MIDI JSON) to peers in the room
            if room and room in rooms:
                for peer in list(rooms[room]):
                    if peer is not ws:
                        await peer.send_str(msg.data)

    # Cleanup on disconnect
    clients.discard(ws)
    if room and ws in rooms.get(room, set()):
        rooms[room].remove(ws)
        username = usernames.pop(ws, "Unknown")
        print(f"❌ {username} disconnected from room '{room}'")
        if rooms.get(room):
            user_list = [usernames.get(peer, "Unknown") for peer in rooms[room]]
            for peer in rooms[room]:
                await peer.send_str(json.dumps({
                    "type": "user_list",
                    "users": user_list
                }))
        else:
            del rooms[room]
        await broadcast_room_list()

    return ws

app = web.Application()
app.router.add_get("/ws", websocket_handler)

if __name__ == "__main__":
    web.run_app(app, port=8080)
