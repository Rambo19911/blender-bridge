#!/usr/bin/env node
// MCP server (stdio, Node standard library only) that drives Blender through the
// Agent Bridge add-on. Each tool sends a small Python snippet to the bridge, which
// runs it on Blender's main thread. Connection details come from
// ~/.blender-bridge/session.json, written by the add-on when it starts.

import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const SERVER_NAME = "blender-bridge";
const SERVER_VERSION = "0.1.1";
const PLUGIN_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const ADDON_SRC = path.join(PLUGIN_ROOT, "blender_addon", "agent_bridge");
const SESSION_FILE = path.join(os.homedir(), ".blender-bridge", "session.json");
const RESULT_MARK = "@@BRIDGE_RESULT@@";

const NOT_RUNNING =
  "Blender bridge is not running. Ask the user to open Blender and click " +
  "3D Viewport > Sidebar (N) > Agent > Start Bridge. If the 'Agent' tab is missing, " +
  "the add-on is not installed yet: call blender_install_addon.";

class BridgeError extends Error {}

// --- bridge client -------------------------------------------------------------

function readSession() {
  let s;
  try {
    s = JSON.parse(fs.readFileSync(SESSION_FILE, "utf8"));
  } catch {
    throw new BridgeError(NOT_RUNNING);
  }
  const port = Number(process.env.BLENDER_BRIDGE_PORT || s.port || 9876);
  return { host: s.host || "127.0.0.1", port, token: s.token || "" };
}

function sendCode(code, timeoutS = 300) {
  const { host, port, token } = readSession();
  const payload = Buffer.from(JSON.stringify({ token, code }), "utf8");
  return new Promise((resolve, reject) => {
    const chunks = [];
    const sock = net.createConnection({ host, port });
    const timer = setTimeout(() => {
      sock.destroy();
      reject(new BridgeError(`Blender did not answer within ${timeoutS}s.`));
    }, timeoutS * 1000);
    sock.on("connect", () => sock.end(payload));
    sock.on("data", (c) => chunks.push(c));
    sock.on("error", (e) => {
      clearTimeout(timer);
      reject(new BridgeError(`${NOT_RUNNING} (${e.code || e.message})`));
    });
    sock.on("close", () => {
      clearTimeout(timer);
      const data = Buffer.concat(chunks).toString("utf8");
      if (!data) return reject(new BridgeError("Blender closed the connection without a reply."));
      try {
        resolve(JSON.parse(data));
      } catch {
        reject(new BridgeError("Unreadable reply from Blender: " + data.slice(0, 500)));
      }
    });
  });
}

// A JSON string literal is also a valid Python string literal.
const prelude = (args) => String.raw`
import bpy, json
ARGS = json.loads(${JSON.stringify(JSON.stringify(args))})
def _emit(obj):
    print("${RESULT_MARK}" + json.dumps(obj))
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
`;

async function runSnippet(snippet, args, timeoutS = 300) {
  const res = await sendCode(prelude(args) + snippet, timeoutS);
  let result = null;
  const log = [];
  for (const line of (res.output || "").split(/\r?\n/)) {
    if (line.startsWith(RESULT_MARK)) result = JSON.parse(line.slice(RESULT_MARK.length));
    else if (line.trim()) log.push(line);
  }
  if (!res.ok) {
    throw new BridgeError("Blender raised an error:\n" + (res.error || "") + (log.length ? "\n" + log.join("\n") : ""));
  }
  return { result, log: log.join("\n") };
}

// --- snippets (Python, run inside Blender) --------------------------------------

const STATUS = String.raw`
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
`;

const SCENE_INFO = String.raw`
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
`;

const CHECK_ASSET = String.raw`
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
`;

const RENDER_PREVIEW = String.raw`
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
`;

const EXPORT_GLB = String.raw`
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
`;

// --- tools -----------------------------------------------------------------------

const text = (s) => ({ type: "text", text: typeof s === "string" ? s : JSON.stringify(s) });
const withLog = ({ result, log }) => (log ? [text(result), text("Blender output:\n" + log)] : [text(result)]);

function versionKey(dir) {
  return (path.basename(dir).match(/[\d.]+$/)?.[0] || "0").split(".").map(Number);
}

function findBlender(explicit) {
  const cands = [explicit, process.env.BLENDER_PATH];
  if (process.platform === "win32") {
    const found = [];
    for (const root of [process.env.ProgramFiles, process.env["ProgramFiles(x86)"]].filter(Boolean)) {
      const base = path.join(root, "Blender Foundation");
      let dirs = [];
      try { dirs = fs.readdirSync(base).map((d) => path.join(base, d)); } catch {}
      for (const d of dirs) found.push(d);
    }
    found.sort((a, b) => {
      const va = versionKey(a), vb = versionKey(b);
      for (let i = 0; i < Math.max(va.length, vb.length); i++) {
        if ((vb[i] || 0) !== (va[i] || 0)) return (vb[i] || 0) - (va[i] || 0);
      }
      return 0;
    });
    cands.push(...found.map((d) => path.join(d, "blender.exe")));
    if (process.env["ProgramFiles(x86)"]) {
      cands.push(path.join(process.env["ProgramFiles(x86)"], "Steam", "steamapps", "common", "Blender", "blender.exe"));
    }
  } else if (process.platform === "darwin") {
    cands.push("/Applications/Blender.app/Contents/MacOS/Blender");
  }
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (dir) cands.push(path.join(dir, process.platform === "win32" ? "blender.exe" : "blender"));
  }
  return cands.find((c) => c && fs.existsSync(c) && fs.statSync(c).isFile()) || null;
}

function blenderCli(blender, args) {
  const p = spawnSync(blender, ["--command", "extension", ...args], { encoding: "utf8", timeout: 300000 });
  const out = `${p.stdout || ""}${p.stderr || ""}`.trim();
  if (p.error || p.status !== 0) {
    throw new BridgeError(`'blender --command extension ${args[0]}' failed (${p.error?.message || "exit " + p.status}):\n${out}`);
  }
  return out;
}

async function installAddon(a) {
  const blender = findBlender(a.blender_path);
  if (!blender) {
    throw new BridgeError(
      "Could not find Blender. Call again with blender_path, or set the BLENDER_PATH environment variable.");
  }
  // Package the readable add-on source with Blender's own builder, then install it.
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "agent-bridge-"));
  const buildLog = blenderCli(blender, ["build", "--source-dir", ADDON_SRC, "--output-dir", outDir]);
  const zip = fs.readdirSync(outDir).find((f) => f.endsWith(".zip"));
  if (!zip) throw new BridgeError("Blender built no package:\n" + buildLog);
  const installLog = blenderCli(blender, ["install-file", "-r", "user_default", "-e", path.join(outDir, zip)]);
  fs.rmSync(outDir, { recursive: true, force: true });
  return [text(
    `Installed and enabled 'Agent Bridge' in ${blender}.\n${installLog}\n\n` +
    "If Blender is open, restart it. Then: 3D Viewport > press N > 'Agent' tab > Start Bridge " +
    "(tick 'Start automatically' to skip this next time).")];
}

const OBJ_LIST = {
  type: "array", items: { type: "string" },
  description: "Object names. Default: selected objects, or all visible objects if none selected.",
};

const TOOLS = [
  {
    name: "blender_status", readOnly: true,
    description: "Check the Blender connection: version, open file, scene, object counts, selection.",
    inputSchema: { type: "object", properties: {} },
    run: async (a) => withLog(await runSnippet(STATUS, a, 30)),
  },
  {
    name: "blender_run_python", readOnly: false,
    description: "Run Python (bpy) inside the running Blender on its main thread and return stdout. Variables persist " +
      "between calls. Use for modelling, materials, modifiers, anything bpy can do. Print what you need to see.",
    inputSchema: {
      type: "object", required: ["code"], properties: {
        code: { type: "string", description: "Python source. `bpy` is already imported." },
        timeout_s: { type: "number", description: "Seconds to wait (default 300)." },
      },
    },
    run: async (a) => {
      const res = await sendCode(a.code, Number(a.timeout_s || 300));
      const out = res.output || "(no output)";
      if (!res.ok) throw new BridgeError(out + "\n" + (res.error || ""));
      return [text(out)];
    },
  },
  {
    name: "blender_scene_info", readOnly: true,
    description: "List scene objects with transforms, dimensions, triangle counts, materials and modifiers.",
    inputSchema: { type: "object", properties: { limit: { type: "integer", description: "Max objects (default 200)." } } },
    run: async (a) => withLog(await runSnippet(SCENE_INFO, a, 120)),
  },
  {
    name: "blender_render_preview", readOnly: false,
    description: "Render quick preview images so you can SEE the result. Frames the target objects automatically with " +
      "a temporary camera; scene settings are restored afterwards. Use after every meaningful modelling step.",
    inputSchema: {
      type: "object", properties: {
        views: {
          type: "array",
          items: { type: "string", enum: ["iso", "iso_back", "front", "back", "left", "right", "top", "camera"] },
          description: "Angles to render (default ['iso']). 'camera' uses the scene camera.",
        },
        objects: OBJ_LIST,
        resolution: { type: "integer", description: "Square size in px, 64-2048 (default 768)." },
        engine: {
          type: "string", enum: ["workbench", "eevee"],
          description: "workbench = fast solid shading (default); eevee = materials and lights.",
        },
      },
    },
    run: async (a) => {
      const { result, log } = await runSnippet(RENDER_PREVIEW, a, 600);
      const content = [];
      for (const img of result.images) {
        content.push(text(`View: ${img.view}`));
        content.push({ type: "image", data: img.png_b64, mimeType: "image/png" });
      }
      content.push(text({ framed_objects: result.framed_objects, engine: result.engine }));
      if (log) content.push(text("Blender output:\n" + log));
      return content;
    },
  },
  {
    name: "blender_check_asset", readOnly: true,
    description: "Game-readiness check for mesh objects: triangle budget, unapplied scale, UVs, materials, " +
      "non-manifold and loose geometry, zero-area faces, origin placement. Returns warnings per object.",
    inputSchema: {
      type: "object", properties: {
        objects: OBJ_LIST,
        max_tris: { type: "integer", description: "Per-object triangle budget." },
        max_tris_total: { type: "integer", description: "Budget for all checked objects together." },
      },
    },
    run: async (a) => withLog(await runSnippet(CHECK_ASSET, a, 120)),
  },
  {
    name: "blender_export_glb", readOnly: false,
    description: "Export objects to a binary glTF (.glb) for web/game engines (three.js, Babylon.js, Godot, Unity).",
    inputSchema: {
      type: "object", required: ["path"], properties: {
        path: { type: "string", description: "Output .glb path; relative paths resolve next to the .blend file." },
        objects: OBJ_LIST,
        include_children: { type: "boolean", description: "Also export children (default true)." },
        apply_modifiers: { type: "boolean", description: "Apply modifiers on export (default true)." },
      },
    },
    run: async (a) => withLog(await runSnippet(EXPORT_GLB, a, 600)),
  },
  {
    name: "blender_install_addon", readOnly: false,
    description: "Install and enable the 'Agent Bridge' Blender add-on that this plugin needs, built from the plugin's " +
      "own source with Blender's CLI. Ask the user before running.",
    inputSchema: {
      type: "object", properties: {
        blender_path: { type: "string", description: "Path to the blender executable if auto-detect fails." },
      },
    },
    run: installAddon,
  },
];
const TOOL_MAP = new Map(TOOLS.map((t) => [t.name, t]));

// --- MCP stdio loop ------------------------------------------------------------------

const send = (msg) => process.stdout.write(JSON.stringify(msg) + "\n");

async function handle(req) {
  const { method, params = {} } = req;
  if (method === "initialize") {
    return {
      protocolVersion: params.protocolVersion || "2025-06-18",
      capabilities: { tools: {} },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
    };
  }
  if (method === "ping") return {};
  if (method === "tools/list") {
    return {
      tools: TOOLS.map((t) => ({
        name: t.name, description: t.description, inputSchema: t.inputSchema,
        annotations: { readOnlyHint: t.readOnly },
      })),
    };
  }
  if (method === "tools/call") {
    const tool = TOOL_MAP.get(params.name);
    if (!tool) throw Object.assign(new Error(`unknown tool ${params.name}`), { code: -32602 });
    try {
      return { content: await tool.run(params.arguments || {}) };
    } catch (e) {
      return { content: [text(e instanceof BridgeError ? e.message : String(e.stack || e))], isError: true };
    }
  }
  throw Object.assign(new Error(`method not found: ${method}`), { code: -32601 });
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", async (line) => {
  if (!line.trim()) return;
  let req;
  try {
    req = JSON.parse(line);
  } catch {
    return send({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } });
  }
  const isNotification = req.id === undefined || req.id === null;
  try {
    const result = await handle(req);
    if (!isNotification) send({ jsonrpc: "2.0", id: req.id, result });
  } catch (e) {
    if (!isNotification) send({ jsonrpc: "2.0", id: req.id, error: { code: e.code || -32603, message: e.message } });
  }
});
