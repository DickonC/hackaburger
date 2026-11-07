"""Send the device count to the UNO Q microcontroller's LED matrix.

Talks MessagePack-RPC to the arduino-router over its Unix socket, calling the
"show_count" method that arduino/count_display/count_display.ino provides.
Failures are reported but never stop the scanner.
"""

import itertools
import socket

import msgpack

ROUTER_SOCKET = "/var/run/arduino-router.sock"
REQUEST, RESPONSE = 0, 1


class McuDisplay:
    def __init__(self, path: str = ROUTER_SOCKET, timeout: float = 3.0):
        self.path = path
        self.timeout = timeout
        self._ids = itertools.count(1)

    def show_count(self, n: int) -> bool:
        msgid = next(self._ids)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.path)
                s.sendall(msgpack.packb([REQUEST, msgid, "show_count", [int(n)]]))
                unpacker = msgpack.Unpacker(raw=False)
                while True:
                    data = s.recv(4096)
                    if not data:
                        raise ConnectionError("router closed the connection")
                    unpacker.feed(data)
                    for msg in unpacker:
                        if msg[0] == RESPONSE and msg[1] == msgid:
                            if msg[2]:
                                print(f"   [display] error: {msg[2]}")
                                return False
                            return True
        except (OSError, ConnectionError) as e:
            print(f"   [display] not updated: {e}")
            return False
