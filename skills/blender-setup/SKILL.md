---
name: blender-setup
description: Connect the agent to Blender for the blender-bridge plugin - install the Agent Bridge add-on, start it, and fix connection errors. Use when blender_* tools report that the bridge is not running, or when the user asks to set up or connect Blender.
---

# Connecting to Blender

The plugin talks to the **Agent Bridge** add-on running inside Blender (4.2 or newer). The add-on listens on `127.0.0.1` only and writes a random access token to `~/.blender-bridge/session.json`. The MCP server reads that file, so no ports or keys need configuring.

## Steps

1. Call `blender_status`. If it succeeds, setup is done.
2. Ask the user whether Blender is open and whether its 3D Viewport sidebar (press **N**) has an **Agent** tab.
   - **The tab is there**: ask them to click **Start Bridge**, then go back to step 1. Mention the **Start automatically** checkbox for next time.
   - **The tab is missing**: with the user's OK, call `blender_install_addon`. It finds Blender and runs `blender --command extension install-file -r user_default -e <zip>`. Then ask them to restart Blender and click **Start Bridge**.
   - **Auto-detect fails**: call `blender_install_addon` with `blender_path`, or have the user install manually via **Edit > Preferences > Get Extensions > ⌄ > Install from Disk** and pick `dist/agent_bridge-0.1.0.zip` from this plugin's folder.
3. Call `blender_status` again to confirm.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Port 9876 is busy" in Blender | Another Blender, or an old bridge script, holds the port. Close it or change **Port** in the Agent panel. The session file tells the MCP server the new port. |
| `invalid token` | Two Blenders were started and the session file belongs to the newer one. Click **Start Bridge** again in the Blender you want. |
| MCP server does not start (`python` not found) | Install Python 3.9+ or set the env var `BLENDER_BRIDGE_PYTHON` to its path (on macOS/Linux usually `python3`), then restart the agent. |
| Calls time out | Blender is busy (rendering, a modal dialog, or a heavy operation). Wait, or ask the user to close the dialog. |

## Headless / CI

The bridge also runs without a UI. It serves until it is stopped:

```
blender -b scene.blend --python <plugin>/blender_addon/agent_bridge/__init__.py
```

Set `BLENDER_BRIDGE_PORT` to use another port. To stop it, call `blender_run_python` with:

```python
bpy.app.driver_namespace["_agent_bridge_state"]["stop_requested"] = True
```
