# IP Management Editing Update

Version base: `loom-electron-host-ui-linked-v2-ui-brandlogo-runtimebackend.zip`

## What changed

1. The IP Management table now supports inline editing.
2. The pencil button now opens editable fields instead of only toggling a mock status value.
3. Editable fields:
   - Enabled
   - Loom Name
   - IP Address
   - Host Port
   - Loom Port
4. The save button writes the edited values to the Electron runtime backend configuration.
5. The cancel button exits edit mode without applying the draft values.
6. IPv4 and port validation were added before saving.
7. The Electron config conversion now preserves stable loom IDs, edited IP values, loom ports, and host-port values.
8. When the Electron process owns the Python backend process, the backend is restarted automatically after a config save so the new IP/port configuration can take effect.

## Runtime time/history note

No persistent runtime-time storage was added. Runtime data remains host-side live runtime data and is not saved as a separate long-term runtime history file.
