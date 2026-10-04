---
name: blender-game-assets
description: Build, inspect and export game-ready 3D models in a running Blender through the blender-bridge MCP tools. Use when the user asks to model, edit, texture, check or export something in Blender, or to make a 3D asset (building, prop, unit, vehicle) for a web or game engine.
---

# Building game assets in Blender

You drive the user's open Blender through the `blender_*` MCP tools. You cannot see Blender's window, so `blender_render_preview` is your eyes. Never say a model is done without having looked at a preview of the final state.

## 0. Connect

Call `blender_status`. If it fails, follow the `blender-setup` skill, then continue.

## 1. Agree on the target before modelling

Settle these first (ask only for what the request and project files do not already say):

- **Engine / destination**: three.js, Babylon.js, Godot, Unity, Unreal, or render only. Default: glTF (`.glb`).
- **Scale**: 1 Blender unit = 1 metre. Write down the real size, e.g. "barracks, about 12 × 8 m, 6 m tall".
- **Triangle budget**: see the table below.
- **Camera distance**: how big the asset appears on screen decides how much detail matters.
- **Style**: low-poly flat, stylised, or realistic.

| Asset (web / mobile game) | Tris |
|---|---|
| Small prop, rock, crate | 50 – 500 |
| Unit, vehicle, character (strategy camera) | 500 – 3 000 |
| Building | 1 000 – 6 000 |
| Hero asset, close-up | 5 000 – 20 000 |

Desktop engines can take 2–5 times more. When the project states its own budget, use that.

## 2. Model in small, visible steps

- Use `blender_run_python` for each step. Keep each call focused: one part, one change. Print names and counts you need to know.
- Give objects meaningful names (`Barracks_Roof`, not `Cube.003`), parent the parts under one root empty or object, and keep each asset in its own collection.
- Block out the big shapes first, check proportions with a preview, and only then add detail.
- Prefer non-destructive modifiers (Mirror, Array, Bevel, Solidify, Boolean); they are applied on export.
- **Silhouette first**: a strategy-game asset is recognised by its outline at 64–128 px. Detail that vanishes at that size is wasted triangles.
- After every meaningful step, call `blender_render_preview` with `views: ["iso"]`. Add `front`, `top` or `iso_back` when shape or alignment matters. Look for:
  - floating or intersecting parts, gaps, and z-fighting surfaces
  - wrong proportions compared with the stated real size
  - parts hidden inside other parts (wasted tris)
- To judge readability from far away, preview once at `resolution: 128`.

Variables persist between `blender_run_python` calls, but Blender data is the source of truth: look objects up by name (`bpy.data.objects["Barracks_Roof"]`) rather than relying on old Python references.

## 3. Materials that survive export

glTF exports only the **Principled BSDF** node (base colour, metallic, roughness, emission, alpha, normal map) plus image textures. Procedural textures, noise and colour ramps do **not** export, so bake them to images or avoid them.

For low-poly work, a few flat-colour materials or one small palette texture is usually best. Keep material counts low because each material is usually a separate draw call.

## 4. Check before export

Call `blender_check_asset` with the budget, for example `{"objects": ["Barracks"], "max_tris_total": 4000}`, and fix every **warning**. Each **info** item is a judgement call.

Common fixes, run in `blender_run_python`:

```python
import bpy, bmesh
from mathutils import Vector, Matrix

o = bpy.data.objects["Barracks_Walls"]

# apply rotation + scale without operators (mesh objects without children)
loc = o.matrix_basis.to_translation()
o.data.transform(Matrix.LocRotScale(None, o.matrix_basis.to_quaternion(), o.matrix_basis.to_scale()))
o.matrix_basis = Matrix.Translation(loc)

# origin to bottom centre of the mesh (so the asset sits on the ground)
vs = o.data.vertices
mn = Vector((min(v.co.x for v in vs), min(v.co.y for v in vs), min(v.co.z for v in vs)))
mx = Vector((max(v.co.x for v in vs), max(v.co.y for v in vs), max(v.co.z for v in vs)))
pivot = Vector(((mn.x + mx.x) / 2, (mn.y + mx.y) / 2, mn.z))
o.data.transform(Matrix.Translation(-pivot))
o.location += o.matrix_world.to_3x3() @ pivot

# clean geometry: merge doubles, delete loose, recalc normals
bm = bmesh.new(); bm.from_mesh(o.data)
bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-4)
bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_edges], context="VERTS")
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
bm.to_mesh(o.data); bm.free(); o.data.update()

# UVs if missing: automatic smart-project unwrap
bpy.context.view_layer.objects.active = o
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode="OBJECT")
```

For heavy reduction, add a Decimate modifier (`ratio` 0.3–0.7), preview it, and keep it only if the silhouette still reads.

## 5. Export

Call `blender_export_glb` with a path inside the user's project, for example `assets/models/barracks.glb`. Use the root object as `objects`; children are included automatically. Report the path, file size and final triangle count.

## Rules

- Do not save, overwrite or close the user's `.blend` file unless they ask. Do not delete objects you did not create without asking.
- `blender_run_python` has full Python access on the user's machine. Use it for Blender work only: no network calls, and no file writes outside the project or export paths.
- If a step fails, read the traceback, fix the cause and retry. Do not repeat the same call unchanged.
- Long operations (heavy booleans, high-res EEVEE renders) block Blender's UI while they run. Warn the user first.
