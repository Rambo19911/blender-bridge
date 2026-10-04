# blender-bridge

Lets your coding agent drive a **running Blender** and build **game-ready 3D assets**. The agent writes `bpy` code, **renders previews so it can see** what it built, checks the model against a triangle budget and common mesh problems, and exports `.glb` for three.js, Babylon.js, Godot, Unity or Unreal.

```
Agent ── MCP (stdio) ── blender_mcp.mjs ── socket 127.0.0.1 + token ── Agent Bridge add-on (inside Blender)
```

## Tools

| Tool | What it does |
|---|---|
| `blender_status` | Connection check: Blender version, open file, scene, selection |
| `blender_run_python` | Run `bpy` code on Blender's main thread; variables persist between calls |
| `blender_scene_info` | Objects with transforms, dimensions, triangle counts, materials, modifiers |
| `blender_render_preview` | Auto-framed preview renders (iso, front, back, left, right, top, scene camera), returned as images |
| `blender_check_asset` | Game-readiness: tri budget, unapplied scale, UVs, materials, non-manifold and loose geometry, zero-area faces, origin |
| `blender_export_glb` | Export objects (with children) to `.glb`, applying modifiers |
| `blender_install_addon` | Builds the add-on from its source in this repo and installs it through Blender's own CLI |

Skills:

- **blender-game-assets** is a workflow for modelling with visual checks after every step. It covers triangle budgets per asset type, glTF-safe materials, and a fix-up cookbook (apply transforms, origin to base, clean geometry, UV unwrap).
- **blender-setup** covers installing the add-on, connecting, and troubleshooting.

## Requirements

- Blender **4.2 or newer** (tested on 5.2)
- **Node.js 18+** on `PATH`. The MCP server uses only Node's standard library; nothing is downloaded.

## Setup

1. Install the plugin in Claude Code:
   ```
   /plugin marketplace add Rambo19911/blender-bridge
   /plugin install blender-bridge@blender-bridge
   ```
2. Install the Blender add-on. Either ask the agent to *"set up Blender"* (it runs `blender_install_addon`), or build the package yourself with `blender --command extension build --source-dir blender_addon/agent_bridge --output-dir dist`, then in Blender go to **Edit → Preferences → Get Extensions → ⌄ → Install from Disk** and pick the zip from `dist/`.
3. In Blender, open **3D Viewport → N → Agent → Start Bridge**. Tick **Start automatically** to skip this step next time.
4. Ask the agent: *"Model a low-poly watchtower, about 8 m tall, under 2000 tris, and export it to assets/tower.glb."*

## Security

- The add-on listens on **127.0.0.1 only** and requires a random token, regenerated on every start. The token is stored in `~/.blender-bridge/session.json`, created with owner-only permissions where the OS supports them.
- `blender_run_python` runs **arbitrary Python** with your user's rights, the same as Blender's own Python console. Use it only with agents and prompts you trust, and stop the bridge when you are not using it.
- Nothing is sent over the network.

## Headless use

```
blender -b scene.blend --python blender_addon/agent_bridge/__init__.py
```

The bridge serves until it is stopped. Set `BLENDER_BRIDGE_PORT` to use another port.

## Development

```
# rebuild the add-on package after editing blender_addon/agent_bridge
blender --command extension build --source-dir blender_addon/agent_bridge --output-dir dist
```

## License

The plugin is MIT ([LICENSE](LICENSE)). The Blender add-on in `blender_addon/` is GPL-3.0-or-later, as Blender requires for add-ons.
