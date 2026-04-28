# Speed Display and Chart Fix Notes

This version fixes the incorrect speed display and invisible/incorrect speed chart.

## Changes

1. Speed normalization added in Python backend, Electron main process, and React UI.
   - Some loom replies encode speed as `rpm × 100`.
   - Example: raw `50432` is now displayed as `504.3 rpm`, not `50432 rpm`.

2. Backend real-time speed history now records one in-memory sample per second while the loom is running.
   - This supports real-time chart plotting instead of one point per minute only.
   - The data is not persisted to disk. Restarting the backend clears the in-memory chart history.

3. The chart now plots normalized speed values and dynamically scales the Y-axis if needed.

4. The current speed card now formats speed as rpm with a maximum of one decimal place.

## Remaining dependency

The displayed speed still depends on the actual protocol reply from the loom. If the captured frame shows a different scaling rule on another machine, adjust the normalization function in `backend/loom_host_master.py` and `electron/main.js`.
