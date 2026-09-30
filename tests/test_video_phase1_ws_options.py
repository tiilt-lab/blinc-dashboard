"""Phase 1 / audit B.6: the video websocket factory sets keepalive and a
payload cap. Source-level contract (server.py loads GPU models at import).
"""
import os
import re

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _server():
    with open(os.path.join(SRC, "video_processing", "server.py")) as f:
        return f.read()


def test_factory_sets_keepalive_and_payload_cap():
    s = _server()
    m = re.search(r"factory\.setProtocolOptions\((.*?)\)\n", s, re.S)
    assert m, "WebSocketServerFactory must get setProtocolOptions"
    opts = m.group(1)
    assert "autoPingInterval=10" in opts
    assert "autoPingTimeout=20" in opts
    assert "maxMessagePayloadSize=MAX_WS_MESSAGE_BYTES" in opts
    # options are applied before the protocol/listen wiring in __main__
    assert s.index("factory.setProtocolOptions(") < s.index("reactor.listenTCP(")


def test_payload_cap_is_twice_the_largest_chunk():
    s = _server()
    ns = {}
    for name in ("CHUNK_SECONDS", "MAX_VIDEO_CHUNK_BYTES", "MAX_WS_MESSAGE_BYTES"):
        m = re.search(r"^%s = (.+?)(\s+#.*)?$" % name, s, re.M)
        assert m, name
        ns[name] = eval(m.group(1), {}, dict(ns))
    # 10 s chunk at the client's 5 Mbps video + 128 kbps audio ceiling
    assert ns["CHUNK_SECONDS"] == 10
    assert ns["MAX_VIDEO_CHUNK_BYTES"] == (5_000_000 + 128_000) * 10 // 8
    assert ns["MAX_WS_MESSAGE_BYTES"] == 2 * ns["MAX_VIDEO_CHUNK_BYTES"]
    assert 12_000_000 < ns["MAX_WS_MESSAGE_BYTES"] < 14_000_000
