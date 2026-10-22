"""QgisMCPServer's read loop against a stubbed qgis, driven over a socketpair.

Covers the paths that never reach a handler: the auth lockout, batch shape checks,
and a handler result json cannot encode. Each used to drop the connection or leak
a traceback instead of answering.
"""

import json
import socket

import pytest
from conftest import FakeQVariant

from qgis_mcp.protocol import HEADER_STRUCT


@pytest.fixture
def server_module(plugin_handlers):
    import qgis_mcp_plugin.server as mod

    return mod


@pytest.fixture
def server(server_module):
    srv = server_module.QgisMCPServer()
    srv.running = True
    return srv


@pytest.fixture
def peer(server):
    """The client end of a connected pair; the server end is registered as a client."""
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    theirs.settimeout(1)
    server.clients[ours] = bytearray()
    yield theirs
    ours.close()
    theirs.close()


def _frame(obj):
    data = json.dumps(obj).encode("utf-8")
    return HEADER_STRUCT.pack(len(data)) + data


def _read_frame(sock):
    (length,) = HEADER_STRUCT.unpack(sock.recv(4))
    return json.loads(sock.recv(length))


def test_repeated_bad_tokens_disconnect_the_client(server, server_module, peer, monkeypatch):
    monkeypatch.setenv("QGIS_MCP_TOKEN", "right")
    peer.sendall(_frame({"type": "ping", "token": "wrong"}) * server.MAX_AUTH_FAILURES)

    server.process_server()

    assert server.clients == {} and server._auth_failures == {}
    for _ in range(server.MAX_AUTH_FAILURES - 1):
        assert _read_frame(peer)["message"] == server_module.AUTH_FAILED_MESSAGE
    assert peer.recv(4) == b"", "the server closed its end"


def test_accepted_command_resets_the_failure_count(server, peer, monkeypatch):
    monkeypatch.setenv("QGIS_MCP_TOKEN", "right")
    bad = _frame({"type": "ping", "token": "wrong"})
    good = _frame({"type": "ping", "token": "right"})
    peer.sendall(bad * (server.MAX_AUTH_FAILURES - 1) + good + bad)

    server.process_server()

    assert len(server.clients) == 1
    assert list(server._auth_failures.values()) == [1]


def test_batch_rejects_malformed_shapes(server, server_module):
    with pytest.raises(server_module.CommandError):
        server.batch("ping")

    results = server.batch([42, {"type": "batch", "params": {"commands": []}}])

    assert [r["status"] for r in results] == ["error", "error"]
    assert "object" in results[0]["message"]
    assert "'batch'" in results[1]["message"]


def test_unserializable_result_is_answered_not_dropped(server, peer):
    (ours,) = server.clients

    server._send_response(ours, {"status": "success", "result": {1, 2}})

    reply = _read_frame(peer)
    assert reply["status"] == "error" and reply["internal"] is True
    assert "serializable" in reply["message"]
    assert ours in server.clients


def test_misspelled_parameter_is_rejected_not_silently_ignored(server):
    """Nearly every handler ends in **kwargs, which swallows a typo otherwise."""
    response = server._dispatch({"type": "get_layers", "params": {"limits": 5}})

    assert response["status"] == "error"
    assert "limits" in response["message"]
    assert "limit, offset" in response["message"], "the accepted names are the hint"
    assert "internal" not in response, "a caller mistake, not a plugin defect"


def test_known_parameters_still_reach_the_handler(server):
    assert server._dispatch({"type": "ping", "params": {}}) == {
        "status": "success",
        "result": {"pong": True},
    }
    assert server._dispatch({"type": "ping"})["status"] == "success"


def test_frames_split_across_recv_chunks_are_reassembled(server, peer):
    """One request arriving in pieces must be answered once, not dropped."""
    (ours,) = server.clients
    payload = _frame({"type": "ping"})
    peer.sendall(payload[:3])

    server.process_server()
    assert bytes(server.clients[ours]) == payload[:3], "held until the frame completes"

    peer.sendall(payload[3:] + _frame({"type": "ping"}))
    server.process_server()

    assert _read_frame(peer)["result"] == {"pong": True}
    assert _read_frame(peer)["result"] == {"pong": True}
    assert bytes(server.clients[ours]) == b"", "consumed bytes are dropped"


def test_convert_attribute_turns_qt_dates_into_iso_strings(server):
    import datetime

    class FakeQDate:
        def toPyDate(self):
            return datetime.date(2020, 1, 2)

    class FakeQDateTime:
        def toPyDateTime(self):
            return datetime.datetime(2021, 3, 4, 5, 6, 7)

    class NullVariant(FakeQVariant):
        def isNull(self):
            return True

    assert server._convert_attribute(NullVariant()) is None
    assert server._convert_attribute(FakeQDate()) == "2020-01-02"
    assert server._convert_attribute(FakeQDateTime()) == "2021-03-04T05:06:07"
    assert server._convert_attribute(datetime.date(2020, 1, 2)) == "2020-01-02"
    assert server._convert_attribute(3.5) == 3.5


# --- execute_code output caps ---


def _run_code(server, **params):
    return server._dispatch({"type": "execute_code", "params": params})


def test_execute_code_clips_a_huge_stdout(server):
    """A runaway print must not put a megabyte into the conversation."""
    response = _run_code(server, code="print('x' * 1000000)")

    result = response["result"]
    assert result["executed"] is True
    assert result["stdout_truncated"] is True
    assert result["stdout_total_chars"] == 1000001
    assert len(result["stdout"]) < 8300, "head + marker + tail, not the whole stream"
    assert "x" * 100 in result["stdout"]


def test_execute_code_keeps_the_end_of_a_long_stream(server):
    """The last line of a long loop is the state it ended in."""
    stdout = _run_code(server, code="print('START'); print('x' * 20000); print('END')")["result"][
        "stdout"
    ]

    assert stdout.startswith("START")
    assert stdout.rstrip().endswith("END")


def test_execute_code_keeps_the_last_line_of_a_long_traceback(server):
    """A deep traceback is clipped from the tail: the exception is last.

    A chain of distinct functions, not one recursive one: Python collapses
    repeated frames into "[Previous line repeated N more times]", which stays
    short enough that nothing would be clipped.
    """
    code = "def f0():\n    raise ValueError('deep boom')\n" + "".join(
        f"def f{i}():\n    return f{i - 1}()\n" for i in range(1, 200)
    )
    code += "f199()\n"
    result = _run_code(server, code=code)["result"]

    assert result["executed"] is False
    assert result["traceback_truncated"] is True
    assert result["traceback"].rstrip().endswith("ValueError: deep boom")


def test_execute_code_refuses_a_max_output_over_the_cap(server):
    """Refused, not clamped: a shortened output reads as the whole output."""
    response = _run_code(server, code="print(1)", max_output=60000)

    assert response["status"] == "error"
    assert "exceeds the maximum of 50000" in response["message"]


def test_execute_code_max_output_raises_the_cap(server):
    result = _run_code(server, code="print('x' * 19990)", max_output=20000)["result"]

    assert "stdout_truncated" not in result
    assert len(result["stdout"]) == 19991


def test_execute_code_small_output_is_untouched(server):
    result = _run_code(server, code="print('hello')")["result"]

    assert result["stdout"] == "hello\n"
    assert "stdout_truncated" not in result
    assert "stdout_total_chars" not in result
