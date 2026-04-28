# UI Fix Notes v2.1

This update fixes the two UI issues reported from the instrument panel screenshot.

## Fixed

1. Sidebar module switching
   - Dashboard, Loom Status, Administrative Declaration, IP Management, History, and Settings now switch to their corresponding module views instead of only changing the highlighted menu item.

2. Header overlap
   - The top header layout was changed so the panel title, Beijing Time, backend status badge, QT5 badge, and window-control icons no longer overlap.

## Preserved

- Python backend auto-start
- Connection to the frozen loom_host_master.py backend
- /health, /api/looms, /api/events, /action/read-all, and /action/admin-declaration integration
- 2FAST attachment icon in the loom overview panel
