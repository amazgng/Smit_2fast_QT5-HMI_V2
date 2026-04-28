# Real-Time Speed and Loom-Side Declaration Reply Update

This version changes the dashboard behavior in three areas.

## 1. Administrative Declaration reply handling

The dashboard no longer fills the Loom Reply boxes using only the values typed on the host computer.

After the operator sends an Administrative Declaration from the host dashboard, the dashboard clears the reply boxes and waits for the loom-side terminal to send the actual declaration-completion reply back to the backend.

When the backend receives a declaration-completion event from the loom, it exposes the parsed values through the real-time endpoint. The Electron UI polls this endpoint and updates these three reply boxes:

- Employee ID
- Production Plan ID
- Yarn Package Number

If the loom does not send a declaration-completion frame, or if the returned text uses a different field format, the host dashboard will continue waiting or may require parser adjustment.

## 2. Density unit

The density display unit is now shown as:

```text
weft/dm
```

The Electron normalization layer preserves weft/dm as the dashboard display unit.

## 3. Real-time speed update

The dashboard now calls the backend real-time endpoint every second:

```text
POST /action/realtime-status
```

The backend reads the loom speed through the protocol speed request and returns:

- currentSpeed
- runtimeMinutes
- runtimeSeconds
- speedHistory
- adminCompletion

The Current Speed card and speed trend data are therefore updated continuously while the dashboard is open.

## Important limitation

This is still dependent on the loom and the protocol response:

- real-time speed needs the loom to answer the speed request quickly;
- administrative reply update needs the loom terminal to send a declaration-completion message back to the host;
- if the loom-side reply format differs from the supported parser, the parser may need to be adjusted after capturing the real reply frame.
