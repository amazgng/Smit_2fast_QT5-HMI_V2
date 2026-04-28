# Backend Runtime Logic Notes

This release adds host-side runtime tracking to the Python backend.

## What the backend now returns

The `/action/read-all` response now includes:

- `runtime`: detailed runtime object
- `runtimeMinutes`: current running session length, in minutes
- `runtimeSeconds`: current running session length, in seconds
- `speedHistory`: per-minute speed history objects
- `speedHistoryValues`: per-minute speed values for charting

## How the runtime is calculated

The backend watches decoded loom status events from:

- `status`
- `complete_status`
- `full_status`
- explicit `read-all` polling

When the decoded/interpreted status reports the loom as running, the backend starts a runtime session for that loom IP address.

While the loom remains running, the backend records speed samples by elapsed minute. If multiple speed readings arrive in the same minute bucket, the latest value replaces the previous value for that minute.

When the loom status changes to a stop/idle/halt condition, the current runtime session and speed history are reset.

## Current limitation

This is host-side runtime history. It starts when the backend is running and receiving loom status data. If the backend process is restarted, the runtime history resets unless persistent storage is added later.
