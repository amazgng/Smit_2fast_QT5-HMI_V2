# Backend Integration Notes

This Electron build is linked to the uploaded frozen Python backend files:

```text
backend/loom_host_master.py
backend/loom_host_v2.py
backend/loom_host_master_config_v23.example.json
```

## Integration model

Electron does not directly implement the loom protocol. Instead:

1. Electron starts the Python host backend.
2. The Python backend owns TCP/UDP/TS loom communication.
3. The React UI calls Electron IPC.
4. Electron IPC calls the backend HTTP endpoints at `127.0.0.1:18080`.
5. The UI renders normalized backend data.

## Main linked IPC calls

```text
config:load              -> loads runtime backend config and loom list
config:save              -> saves runtime backend config
backend:status           -> checks /health
loom:readAllStatus       -> POST /action/read-all
loom:sendDeclaration     -> POST /auth/login + POST /action/admin-declaration
```

## Backend endpoints used

```text
GET  /health
GET  /api/looms
GET  /api/events?limit=12
POST /action/read-all
POST /auth/login
POST /action/admin-declaration
```

## Data mapping

The UI maps backend command results approximately as follows:

```text
full_status.decoded.event.speed_rpm       -> Current Speed
speed.speed_rpm                           -> Current Speed fallback
full_status.decoded.event.total_picks     -> Picks
total_picks.total_picks                   -> Picks fallback
density.selected_density + unit_text      -> Density
pattern_current.name                      -> Current Pattern
full_status.decoded.event.shift           -> Shift
full_status/status decoded interpreted    -> Loom State
```

When the backend returns only partial data, the UI keeps placeholders instead of crashing.

## Important limitation

The uploaded backend's `/action/admin-declaration` endpoint configures declaration template/code `565`; it does not directly force the loom HMI to submit completed values. Completed values are captured when the loom terminal sends the declaration completion back to the host backend.
