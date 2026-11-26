# Whale Desktop Operational Rules

- Always start and operate the virtual desktop through the `whale-desktop` MCP tools (`desktop_start`, `desktop_inspect_ui`, `desktop_screenshot`, `desktop_click_text`, `desktop_click`, `desktop_act`, `desktop_type`, `desktop_key`, `desktop_key_hold`, `desktop_stop`, `desktop_save_image`).
- Prefer `desktop_inspect_ui` or `desktop_screenshot` before interacting when UI state could have changed, and verify the result afterward.
- Keep host workspace mounts disabled or read-only unless the user explicitly asks to share a writable folder.
- Never bake passwords, tokens, or secrets into the Docker image (`Dockerfile` or `desktop_save_image`).
- Whenever `desktop_save_image` is run, the plugin automatically saves an offline compressed image snapshot under `images/whale-desktop-v1.tar.gz` so the desktop survives Docker resets.
