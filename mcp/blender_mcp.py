#!/usr/bin/env python3
"""MCP server (stdio, standard library only) that drives Blender through the Agent Bridge add-on.

Each tool sends a small Python snippet to the bridge, which runs it on Blender's
main thread. Connection details come from ~/.blender-bridge/session.json, which the
add-on writes when it starts.
"""

import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import traceback

SERVER_NAME = "blender-bridge"
SERVER_VERSION = "0.1.0"
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_ZIP = os.path.join(PLUGIN_ROOT, "dist", "agent_bridge-0.1.0.zip")
SESSION_FILE = os.path.join(os.path.expanduser("~"), ".blender-bridge", "session.json")
RESULT_MARK = "@@BRIDGE_RESULT@@"

NOT_RUNNING = (
    "Blender bridge is not running. Ask the user to open Blender and click "
    "3D Viewport > Sidebar (N) > Agent > Start Bridge. If the 'Agent' tab is missing, "
    "the add-on is not installed yet: call blender_install_addon."
)


class BridgeError(Exception):
    pass


# --- bridge client ---------------------------------------------------------

def _session():
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        raise BridgeError(NOT_RUNNING)
    port = int(os.environ.get("BLENDER_BRIDGE_PORT") or s.get("port", 9876))
    return s.get("host", "127.0.0.1"), port, s.get("token", "")


def send_code(code, timeout=300):
    host, port, token = _session()
    payload = json.dumps({"token": token, "code": code}).encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                data += chunk
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        if isinstance(e, socket.timeout):
            raise BridgeError(f"Blender did not answer within {timeout}s.")
        raise BridgeError(NOT_RUNNING + f" ({e})")
    if not data:
        raise BridgeError("Blender closed the connection without a reply.")
    return json.loads(data.decode("utf-8"))


PRELUDE = r'''
import bpy, json
ARGS = json.loads(%r)
def _emit(obj):
    print("%s" + json.dumps(obj))
def _targets(names, meshes_only=True):
    sc = bpy.context.scene
    if names:
        objs, missing = [], []
        for n in names:
            o = sc.objects.get(n)
            (objs.append(o) if o else missing.append(n))
        if missing:
            raise ValueError("Objects not found in scene: " + ", ".join(missing))
    else:
        objs = [o for o in sc.objects if o.select_get()] or [o for o in sc.objects if o.visible_get()]
    if meshes_only:
        objs = [o for o in objs if o.type == "MESH"]
    return objs
'''


def run_tool_snippet(snippet, args, timeout=300):
    code = PRELUDE % (json.dumps(args), RESULT_MARK) + snippet
    res = send_code(code, timeout=timeout)
    output = res.get("output", "")
    result, log = None, []
    for line in output.splitlines():
        if line.startswith(RESULT_MARK):
            result = json.loads(line[len(RESULT_MARK):])
        else:
            log.append(line)
    if not res.get("ok"):
        raise BridgeError("Blender raised an error:\n" + res.get("error", "") + ("\n" + "\n".join(log) if log else ""))
    return result, "\n".join(log).strip()


# --- snippets ----------------------------------------------------------------

STATUS = r'''
sc = bpy.context.scene
_emit({
    "blender_version": bpy.app.version_string,
    "file": bpy.data.filepath or "(unsaved)",
    "is_dirty": bpy.data.is_dirty,
    "scene": sc.name,
    "objects": len(sc.objects),
    "mesh_objects": sum(1 for o in sc.objects if o.type == "MESH"),
    "selected": [o.name for o in sc.objects if o.select_get()][:50],
    "render_engine": sc.render.engine,
    "unit_system": sc.unit_settings.system,
    "unit_scale": sc.unit_settings.scale_length,
    "frame": sc.frame_current,
    "background_mode": bpy.app.background,
})
'''

SCENE_INFO = r'''
sc = bpy.context.scene
deps = bpy.context.evaluated_depsgraph_get()
limit = int(ARGS.get("limit", 200))
items, total_tris = [], 0
for o in sc.objects:
    d = {
        "name": o.name, "type": o.type,
        "location": [round(v, 4) for v in o.location],
        "rotation_deg": [round(v * 57.29578, 2) for v in o.rotation_euler],
        "scale": [round(v, 4) for v in o.scale],
        "dimensions": [round(v, 4) for v in o.dimensions],
        "visible": o.visible_get(),
        "parent": o.parent.name if o.parent else None,
        "collections": [c.name for c in o.users_collection],
    }
    if o.type == "MESH":
        ev = o.evaluated_get(deps)
        me = ev.to_mesh()
        me.calc_loop_triangles()
        d["tris"] = len(me.loop_triangles)
        d["verts"] = len(me.vertices)
        total_tris += d["tris"] if o.visible_get() else 0
        ev.to_mesh_clear()
        d["materials"] = [s.material.name if s.material else None for s in o.material_slots]
        d["modifiers"] = [m.type for m in o.modifiers]
    items.append(d)
_emit({"scene": sc.name, "object_count": len(items), "visible_tris": total_tris,
       "objects": items[:limit], "truncated": len(items) > limit})
'''

CHECK_ASSET = r'''
import bmesh
from mathutils import Vector
deps = bpy.context.evaluated_depsgraph_get()
objs = _targets(ARGS.get("objects"))
max_tris = ARGS.get("max_tris")
max_total = ARGS.get("max_tris_total")
report, total = [], 0
for o in objs:
    w, info = [], []
    ev = o.evaluated_get(deps)
    me = ev.to_mesh()
    bm = bmesh.new()
    bm.from_mesh(me)
    tris = sum(len(f.verts) - 2 for f in bm.faces)
    total += tris
    nonmanifold = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    boundary = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    loose_edges = sum(1 for e in bm.edges if not e.link_faces)
    loose_verts = sum(1 for v in bm.verts if not v.link_edges)
    degenerate = sum(1 for f in bm.faces if f.calc_area() < 1e-10)
    ngons = sum(1 for f in bm.faces if len(f.verts) > 4)
    has_uv = len(me.uv_layers) > 0
    bm.free()
    ev.to_mesh_clear()
    corners = [o.matrix_world @ Vector(c) for c in o.bound_box]
    mn = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    mx = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    origin = o.matrix_world.translation
    base = Vector(((mn.x + mx.x) / 2, (mn.y + mx.y) / 2, mn.z))
    size = max((mx - mn).length, 1e-6)
    if max_tris and tris > max_tris:
        w.append(f"{tris} tris exceeds budget {max_tris}")
    if any(abs(s - 1) > 1e-4 for s in o.scale):
        w.append(f"unapplied scale {tuple(round(s, 4) for s in o.scale)} (Ctrl+A > Scale)")
    if any(abs(r) > 1e-4 for r in o.rotation_euler):
        info.append("unapplied rotation")
    if not has_uv:
        w.append("no UV map (textures will not map)")
    if not o.material_slots or any(s.material is None for s in o.material_slots):
        w.append("missing material or empty material slot")
    if nonmanifold:
        w.append(f"{nonmanifold} non-manifold edges (shared by 3+ faces)")
    if loose_verts or loose_edges:
        w.append(f"loose geometry: {loose_verts} verts, {loose_edges} edges")
    if degenerate:
        w.append(f"{degenerate} zero-area faces")
    if boundary:
        info.append(f"{boundary} open boundary edges (fine if the hole is never seen)")
    if ngons:
        info.append(f"{ngons} n-gons (exporters triangulate them)")
    if (origin - base).length > 0.05 * size:
        info.append("origin is not at the bottom centre of the bounds")
    report.append({
        "name": o.name, "tris": tris,
        "dimensions_m": [round(v, 4) for v in (mx - mn)],
        "materials": [s.material.name if s.material else None for s in o.material_slots],
        "has_uv": has_uv, "warnings": w, "info": info,
    })
summary = []
if max_total and total > max_total:
    summary.append(f"total {total} tris exceeds budget {max_total}")
_emit({"objects_checked": len(report), "total_tris": total,
       "ok": not summary and not any(r["warnings"] for r in report),
       "summary_warnings": summary, "objects": report})
'''

RENDER_PREVIEW = r'''
import math, os, tempfile, base64
from mathutils import Vector
sc = bpy.context.scene
r = sc.render
views = ARGS.get("views") or ["iso"]
res = max(64, min(int(ARGS.get("resolution", 768)), 2048))
engine = ARGS.get("engine", "workbench")
objs = _targets(ARGS.get("objects"), meshes_only=False)
geo = [o for o in objs if o.type not in {"CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE"}] or objs
pts = [o.matrix_world @ Vector(c) for o in geo for c in o.bound_box] or [Vector((0, 0, 0))]
mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
center, radius = (mn + mx) / 2, max((mx - mn).length / 2, 0.01)
DIRS = {"iso": (1, -1, 0.8), "iso_back": (-1, 1, 0.8), "front": (0, -1, 0.0001), "back": (0, 1, 0.0001),
        "left": (-1, 0, 0.0001), "right": (1, 0, 0.0001), "top": (0, -0.0001, 1)}
ENGINES = {"workbench": ["BLENDER_WORKBENCH"], "eevee": ["BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"]}
shading = sc.display.shading
saved = dict(engine=r.engine, x=r.resolution_x, y=r.resolution_y, pct=r.resolution_percentage,
             path=r.filepath, fmt=r.image_settings.file_format, cam=sc.camera,
             light=shading.light, color=shading.color_type)
cam_data = bpy.data.cameras.new("_bridge_preview_cam")
cam = bpy.data.objects.new("_bridge_preview_cam", cam_data)
sc.collection.objects.link(cam)
images = []
try:
    for e in ENGINES.get(engine, ENGINES["workbench"]):
        try:
            r.engine = e
            break
        except TypeError:
            pass
    if r.engine == "BLENDER_WORKBENCH":
        shading.light = "STUDIO"
        shading.color_type = "MATERIAL"
    r.resolution_x = r.resolution_y = res
    r.resolution_percentage = 100
    r.image_settings.file_format = "PNG"
    cam_data.clip_start = radius * 0.001
    cam_data.clip_end = radius * 50 + 100
    for view in views:
        if view == "camera":
            if saved["cam"] is None:
                raise ValueError("view 'camera' requested but the scene has no camera")
            sc.camera = saved["cam"]
        else:
            d = Vector(DIRS[view]).normalized()
            dist = radius / math.sin(cam_data.angle / 2) * 1.05
            cam.location = center + d * dist
            cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
            sc.camera = cam
        path = os.path.join(tempfile.gettempdir(), f"bridge_preview_{view}.png")
        r.filepath = path
        bpy.ops.render.render(write_still=True)
        with open(path, "rb") as f:
            images.append({"view": view, "png_b64": base64.b64encode(f.read()).decode("ascii")})
finally:
    r.engine = saved["engine"]
    r.resolution_x, r.resolution_y, r.resolution_percentage = saved["x"], saved["y"], saved["pct"]
    r.filepath, r.image_settings.file_format = saved["path"], saved["fmt"]
    sc.camera = saved["cam"]
    shading.light, shading.color_type = saved["light"], saved["color"]
    bpy.data.objects.remove(cam)
    bpy.data.cameras.remove(cam_data)
_emit({"images": images, "framed_objects": [o.name for o in geo][:50], "engine": engine})
'''

EXPORT_GLB = r'''
import os
sc = bpy.context.scene
vl = bpy.context.view_layer
path = ARGS["path"]
if not os.path.isabs(path):
    base = bpy.path.abspath("//") if bpy.data.filepath else os.getcwd()
    path = os.path.join(base, path)
if not path.lower().endswith(".glb"):
    path += ".glb"
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
objs = _targets(ARGS.get("objects"), meshes_only=False)
if ARGS.get("include_children", True):
    seen = {o.name for o in objs}
    for o in list(objs):
        for c in o.children_recursive:
            if c.name not in seen:
                objs.append(c)
                seen.add(c.name)
prev_sel = [o for o in sc.objects if o.select_get()]
prev_active = vl.objects.active
for o in sc.objects:
    try:
        o.select_set(False)
    except RuntimeError:
        pass
for o in objs:
    o.select_set(True)
try:
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True,
                              export_apply=bool(ARGS.get("apply_modifiers", True)))
finally:
    for o in sc.objects:
        try:
            o.select_set(o in prev_sel)
        except RuntimeError:
            pass
    vl.objects.active = prev_active
_emit({"path": path, "bytes": os.path.getsize(path), "objects": [o.name for o in objs]})
'''


# --- tools -----------------------------------------------------------------------

def _text(s):
    return {"type": "text", "text": s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)}


def _with_log(result, log):
    out = [_text(result)]
    if log:
        out.append(_text("Blender output:\n" + log))
    return out


def tool_status(a):
    return _with_log(*run_tool_snippet(STATUS, a, timeout=30))


def tool_run_python(a):
    res = send_code(a["code"], timeout=float(a.get("timeout_s", 300)))
    text = res.get("output", "") or "(no output)"
    if not res.get("ok"):
        raise BridgeError(text + "\n" + res.get("error", ""))
    return [_text(text)]


def tool_scene_info(a):
    return _with_log(*run_tool_snippet(SCENE_INFO, a, timeout=120))


def tool_check_asset(a):
    return _with_log(*run_tool_snippet(CHECK_ASSET, a, timeout=120))


def tool_render_preview(a):
    result, log = run_tool_snippet(RENDER_PREVIEW, a, timeout=600)
    content = []
    for img in result["images"]:
        content.append(_text(f"View: {img['view']}"))
        content.append({"type": "image", "data": img["png_b64"], "mimeType": "image/png"})
    content.append(_text({"framed_objects": result["framed_objects"], "engine": result["engine"]}))
    if log:
        content.append(_text("Blender output:\n" + log))
    return content


def tool_export_glb(a):
    return _with_log(*run_tool_snippet(EXPORT_GLB, a, timeout=600))


def find_blender(explicit=None):
    cands = [explicit, os.environ.get("BLENDER_PATH"), shutil.which("blender")]
    if sys.platform == "win32":
        pf = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")]
        found = []
        for root in filter(None, pf):
            found += glob.glob(os.path.join(root, "Blender Foundation", "Blender *", "blender.exe"))
        found.sort(key=lambda p: [int(x) if x.isdigit() else 0 for x in
                                  os.path.basename(os.path.dirname(p)).split()[-1].split(".")], reverse=True)
        cands += found
        steam = os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Steam", "steamapps", "common", "Blender", "blender.exe")
        cands.append(steam)
    elif sys.platform == "darwin":
        cands.append("/Applications/Blender.app/Contents/MacOS/Blender")
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def tool_install_addon(a):
    if not os.path.isfile(ADDON_ZIP):
        raise BridgeError(f"Add-on package not found at {ADDON_ZIP}")
    blender = find_blender(a.get("blender_path"))
    if not blender:
        raise BridgeError(
            "Could not find Blender. Pass blender_path, or install manually: Blender > Edit > "
            f"Preferences > Get Extensions > (v menu) Install from Disk > {ADDON_ZIP}")
    p = subprocess.run([blender, "--command", "extension", "install-file", "-r", "user_default", "-e", ADDON_ZIP],
                       capture_output=True, text=True, timeout=300)
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        raise BridgeError(f"Install failed (exit {p.returncode}) using {blender}:\n{out}")
    return [_text(
        f"Installed and enabled 'Agent Bridge' in {blender}.\n{out}\n\n"
        "If Blender is open, restart it. Then: 3D Viewport > press N > 'Agent' tab > Start Bridge "
        "(tick 'Start automatically' to skip this next time).")]


OBJ_LIST = {"type": "array", "items": {"type": "string"},
            "description": "Object names. Default: selected objects, or all visible objects if none selected."}

TOOLS = [
    ("blender_status", tool_status, "Check the Blender connection: version, open file, scene, object counts, selection.",
     {"type": "object", "properties": {}}, True),
    ("blender_run_python", tool_run_python,
     "Run Python (bpy) inside the running Blender on its main thread and return stdout. Variables persist between "
     "calls. Use for modelling, materials, modifiers, anything bpy can do. Print what you need to see.",
     {"type": "object", "required": ["code"], "properties": {
         "code": {"type": "string", "description": "Python source. `bpy` is already imported."},
         "timeout_s": {"type": "number", "description": "Seconds to wait (default 300)."}}}, False),
    ("blender_scene_info", tool_scene_info,
     "List scene objects with transforms, dimensions, triangle counts, materials and modifiers.",
     {"type": "object", "properties": {"limit": {"type": "integer", "description": "Max objects (default 200)."}}}, True),
    ("blender_render_preview", tool_render_preview,
     "Render quick preview images so you can SEE the result. Frames the target objects automatically with a "
     "temporary camera; scene settings are restored afterwards. Use after every meaningful modelling step.",
     {"type": "object", "properties": {
         "views": {"type": "array", "items": {"type": "string", "enum": [
             "iso", "iso_back", "front", "back", "left", "right", "top", "camera"]},
             "description": "Angles to render (default ['iso']). 'camera' uses the scene camera."},
         "objects": OBJ_LIST,
         "resolution": {"type": "integer", "description": "Square size in px, 64-2048 (default 768)."},
         "engine": {"type": "string", "enum": ["workbench", "eevee"],
                    "description": "workbench = fast solid shading (default); eevee = materials and lights."}}}, False),
    ("blender_check_asset", tool_check_asset,
     "Game-readiness check for mesh objects: triangle budget, unapplied scale, UVs, materials, non-manifold and "
     "loose geometry, zero-area faces, origin placement. Returns warnings per object.",
     {"type": "object", "properties": {
         "objects": OBJ_LIST,
         "max_tris": {"type": "integer", "description": "Per-object triangle budget."},
         "max_tris_total": {"type": "integer", "description": "Budget for all checked objects together."}}}, True),
    ("blender_export_glb", tool_export_glb,
     "Export objects to a binary glTF (.glb) for web/game engines (three.js, Babylon.js, Godot, Unity).",
     {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "Output .glb path; relative paths resolve next to the .blend file."},
         "objects": OBJ_LIST,
         "include_children": {"type": "boolean", "description": "Also export children (default true)."},
         "apply_modifiers": {"type": "boolean", "description": "Apply modifiers on export (default true)."}}}, False),
    ("blender_install_addon", tool_install_addon,
     "Install and enable the 'Agent Bridge' Blender add-on that this plugin needs. Ask the user before running.",
     {"type": "object", "properties": {
         "blender_path": {"type": "string", "description": "Path to blender executable if auto-detect fails."}}}, False),
]
TOOL_MAP = {t[0]: t[1] for t in TOOLS}


# --- MCP stdio loop ----------------------------------------------------------------

def _send(msg):
    sys.stdout.buffer.write((json.dumps(msg) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _handle(req):
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        return {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s,
                           "annotations": {"readOnlyHint": ro}} for n, _, d, s, ro in TOOLS]}
    if method == "tools/call":
        p = req.get("params", {})
        fn = TOOL_MAP.get(p.get("name"))
        if not fn:
            raise LookupError(f"unknown tool {p.get('name')}")
        try:
            return {"content": fn(p.get("arguments") or {})}
        except BridgeError as e:
            return {"content": [_text(str(e))], "isError": True}
        except Exception:
            return {"content": [_text(traceback.format_exc())], "isError": True}
    if rid is None:
        return None  # notification
    raise LookupError(f"method not found: {method}")


def main():
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        rid = req.get("id")
        try:
            result = _handle(req)
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "result": result})
        except LookupError as e:
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": str(e)}})
        except Exception as e:
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})


if __name__ == "__main__":
    main()
