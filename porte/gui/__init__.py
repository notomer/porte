"""Native-UI integration layer for Porte.

`porte.gui.bridge` is the only supported programmatic surface for a GUI. It
speaks newline-delimited JSON over stdin/stdout (protocol 1) and calls the same
engine modules the CLI does. `porte.gui.runner` executes a portable Windows
image in its own process so a UI can cancel it without corrupting the bridge.

The protocol is documented in `docs/09-macos-gui.md`. Human-readable output is
never sent on the protocol stream: anything the engine prints goes to stderr.
"""
