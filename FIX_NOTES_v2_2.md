# UI Cleanup v2.2

Changes:

1. Removed the decorative custom window-control icons from the app header.
   - Minimize / maximize / close are handled by the native Windows title bar.
   - Building into an EXE would not automatically make the decorative icons work unless Electron IPC handlers were added.

2. Updated sidebar footer copyright text to:
   © 2026 Smit SHA Textile Machinery

3. Preserved the linked Python backend integration and 2FAST loom icon.
