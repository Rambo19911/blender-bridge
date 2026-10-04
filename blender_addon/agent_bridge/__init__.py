# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent Bridge: lets a local AI coding agent run Python inside Blender.

The bridge listens on 127.0.0.1 only. Every request must carry a random token
that is written to ~/.blender-bridge/session.json when the bridge starts, so
only processes running as the same OS user can talk to it.

Protocol: the client sends one JSON object {"token": str, "code": str} and
closes its write side. The bridge runs the code on Blender's main thread and
answers with {"ok": bool, "output": str, "error"?: str}.

Usage:
  * GUI: 3D Viewport > Sidebar (N) > "Agent" tab > Start.
  * Headless: blender -b [file.blend] --python agent_bridge/__init__.py
"""

bl_info = {
    "name": "Agent Bridge",
    "author": "Rihards",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Agent",
    "description": "Let a local AI coding agent drive Blender over a socket",
    "category": "Development",
}

import contextlib
import io
import json
import os
import secrets
import socket
import sys
import time
import traceback

import bpy

HOST = "127.0.0.1"
DEFAULT_PORT = 9876
MAX_REQUEST_BYTES = 32 * 1024 * 1024
SESSION_DIR = os.path.join(os.path.expanduser("~"), ".blender-bridge")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")

# State lives in driver_namespace so it survives add-on reloads.
_KEY = "_agent_bridge_state"


def _state():
    return bpy.app.driver_namespace.setdefault(_KEY, {})


def is_running():
    sock = _state().get("sock")
    return sock is not None and sock.fileno() != -1


# --- request handling ------------------------------------------------------

def _run(code):
    ns = _state().setdefault("ns", {"bpy": bpy, "__name__": "__agent_bridge__"})
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            exec(compile(code, "<agent>", "exec"), ns)
        return {"ok": True, "output": out.getvalue()}
    except Exception:
        return {"ok": False, "output": out.getvalue(), "error": traceback.format_exc()}


def _read_request(conn):
    conn.settimeout(10)
    chunks, size = [], 0
    while True:
        b = conn.recv(65536)
        if not b:
            break
        size += len(b)
        if size > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        chunks.append(b)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _handle(conn):
    try:
        try:
            req = _read_request(conn)
        except Exception as e:
            res = {"ok": False, "output": "", "error": f"bad request: {e}"}
        else:
            token = _state().get("token") or ""
            if not secrets.compare_digest(str(req.get("token", "")), token):
                res = {"ok": False, "output": "", "error": "invalid token"}
            else:
                _state()["requests"] = _state().get("requests", 0) + 1
                res = _run(str(req.get("code", "")))
        conn.sendall(json.dumps(res).encode("utf-8"))
    finally:
        conn.close()


def _accept_pending():
    """Serve every queued connection. Returns False once the socket is closed."""
    sock = _state().get("sock")
    if sock is None or sock.fileno() == -1:
        return False
    while True:
        try:
            conn, _ = sock.accept()
        except (BlockingIOError, socket.timeout):
            return True
        except OSError:
            if sock.fileno() == -1:
                return False
            traceback.print_exc()
            return True
        try:
            _handle(conn)
        except Exception:
            traceback.print_exc()


def _poll():
    return 0.1 if _accept_pending() else None


# --- start / stop ----------------------------------------------------------

def _write_session(port, token):
    os.makedirs(SESSION_DIR, exist_ok=True)
    data = {
        "host": HOST,
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "blender_version": bpy.app.version_string,
        "started": time.time(),
    }
    tmp = SESSION_FILE + ".tmp"
    # Create with owner-only permissions where the OS supports it.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, SESSION_FILE)


def _remove_session():
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            if json.load(f).get("pid") != os.getpid():
                return  # another Blender owns the session file now
        os.remove(SESSION_FILE)
    except (OSError, ValueError):
        pass


def start(port=DEFAULT_PORT):
    stop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        raise
    sock.listen(8)
    token = secrets.token_urlsafe(32)
    st = _state()
    st.update(sock=sock, token=token, port=port, requests=0)
    _write_session(port, token)
    if not bpy.app.background:
        sock.setblocking(False)
        st["timer"] = _poll
        bpy.app.timers.register(_poll, persistent=True)
    print(f"Agent Bridge listening on {HOST}:{port}")


def stop():
    st = _state()
    fn = st.pop("timer", None)
    if fn and bpy.app.timers.is_registered(fn):
        bpy.app.timers.unregister(fn)
    sock = st.pop("sock", None)
    if sock:
        with contextlib.suppress(OSError):
            sock.close()
        _remove_session()
    st.pop("token", None)


def serve_blocking(port=DEFAULT_PORT):
    """Headless mode: serve until a request sets bpy.app.driver_namespace flag or Ctrl+C."""
    start(port)
    sock = _state()["sock"]
    sock.settimeout(0.5)
    try:
        while not _state().get("stop_requested"):
            if not _accept_pending():
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop()


# --- UI --------------------------------------------------------------------

def _prefs():
    if not __package__:
        return None
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _port():
    p = _prefs()
    return p.port if p else DEFAULT_PORT


class AGENTBRIDGE_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or __name__

    port: bpy.props.IntProperty(name="Port", default=DEFAULT_PORT, min=1024, max=65535)
    autostart: bpy.props.BoolProperty(
        name="Start automatically",
        description="Start the bridge when Blender starts",
        default=False,
    )

    def draw(self, context):
        row = self.layout.row()
        row.prop(self, "port")
        row.prop(self, "autostart")


class AGENTBRIDGE_OT_start(bpy.types.Operator):
    bl_idname = "agent_bridge.start"
    bl_label = "Start Bridge"
    bl_description = "Listen on 127.0.0.1 for a local AI agent"

    def execute(self, context):
        try:
            start(_port())
        except OSError as e:
            self.report({"ERROR"}, f"Port {_port()} is busy: {e}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Agent Bridge listening on port {_port()}")
        return {"FINISHED"}


class AGENTBRIDGE_OT_stop(bpy.types.Operator):
    bl_idname = "agent_bridge.stop"
    bl_label = "Stop Bridge"

    def execute(self, context):
        stop()
        return {"FINISHED"}


class AGENTBRIDGE_PT_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Agent"
    bl_label = "Agent Bridge"

    def draw(self, context):
        col = self.layout.column()
        st = _state()
        if is_running():
            col.label(text=f"Listening on port {st.get('port')}", icon="LINKED")
            col.label(text=f"Requests served: {st.get('requests', 0)}")
            col.operator(AGENTBRIDGE_OT_stop.bl_idname, icon="CANCEL")
        else:
            col.label(text="Stopped", icon="UNLINKED")
            col.operator(AGENTBRIDGE_OT_start.bl_idname, icon="PLAY")
        p = _prefs()
        if p:
            col.prop(p, "port")
            col.prop(p, "autostart")


_classes = (AGENTBRIDGE_Preferences, AGENTBRIDGE_OT_start, AGENTBRIDGE_OT_stop, AGENTBRIDGE_PT_panel)


def _autostart():
    p = _prefs()
    if p and p.autostart and not is_running():
        try:
            start(p.port)
        except OSError:
            traceback.print_exc()
    return None


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    if not bpy.app.background:
        bpy.app.timers.register(_autostart, first_interval=1.0)


def unregister():
    stop()
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    # Run as a script: `blender -b --python __init__.py` serves until Ctrl+C;
    # in the GUI (Scripting tab > Run Script) it just starts the bridge.
    port = int(os.environ.get("BLENDER_BRIDGE_PORT", DEFAULT_PORT))
    if bpy.app.background:
        serve_blocking(port)
    else:
        start(port)
