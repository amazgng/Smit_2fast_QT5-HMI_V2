# Loom Real-Time Monitoring Panel — Electron Host UI Linked Version

This version is no longer only a static interface. It includes the uploaded frozen Python host/backend program and connects the Electron UI to the backend HTTP API.

## What is included

```text
loom-electron-host-ui/
├── backend/
│   ├── loom_host_master.py
│   ├── loom_host_v2.py
│   └── loom_host_master_config_v23.example.json
├── electron/
│   ├── main.js
│   └── preload.js
├── public/
│   └── 2FAST_100px.png
├── src/
│   ├── App.jsx
│   ├── main.jsx
│   └── styles.css
├── package.json
└── README.md
```

## Required software

Install these on the Windows host computer:

1. Node.js LTS
2. Python 3.x
3. npm, which is included with Node.js

Check them in PowerShell:

```powershell
node -v
npm -v
python --version
```

If `python --version` does not work but `py -3 --version` works, the Electron app will still try to use `py -3` first on Windows.

## How to run

Assume the ZIP file is placed here:

```text
C:\Users\Kemen\Downloads\loom-electron-host-ui-linked.zip
```

Open PowerShell and run:

```powershell
cd C:\Users\Kemen\Downloads
Expand-Archive -Path .\loom-electron-host-ui-linked.zip -DestinationPath .\loom-electron-host-ui-linked-run -Force

$project = Get-ChildItem -Path .\loom-electron-host-ui-linked-run -Recurse -Filter package.json | Select-Object -First 1
cd $project.DirectoryName

dir
npm install
npm run dev
```

The Electron window should open. The top bar will show one of these backend states:

```text
Backend Starting
Backend Connected :18080
Backend Offline
```

For normal use, it should become:

```text
Backend Connected :18080
```

## What happens when the UI starts

The Electron main process automatically starts:

```powershell
python backend\loom_host_master.py --config <runtime-config> --log-level INFO run
```

The Python backend provides:

```text
TCP/UDP host listener: 0.0.0.0:13001
Backend HTTP API:      127.0.0.1:18080
Default loom TS port:  13000
```

The UI calls these backend endpoints:

```text
GET  /health
GET  /api/looms
GET  /api/events?limit=12
POST /action/read-all
POST /auth/login
POST /action/admin-declaration
```

## Runtime configuration location

The first time the app starts, it copies the bundled backend config into the Electron user-data directory.

In the UI, the config is based on:

```text
backend\loom_host_master_config_v23.example.json
```

At runtime it is copied to a path similar to:

```text
C:\Users\Kemen\AppData\Roaming\Loom Real-Time Monitoring Panel\backend-runtime\loom_host_master_config_v23.runtime.json
```

This runtime config is what the Electron app actually uses.

## Important IP settings

The uploaded backend config currently contains this loom entry:

```json
{
  "name": "2fast-loom-02",
  "ip": "169.254.4.102",
  "ts_port": 13000,
  "supports_qt5_full_status": true,
  "enabled": true
}
```

If your actual loom IP is different, change it in the IP Management table or edit the runtime JSON config.

For the real loom network, the host computer must be on the same network segment as the loom terminal or gateway. Example:

```text
Host PC IP:  169.254.4.xxx
Loom IP:     169.254.4.102
Loom port:   13000
Host port:   13001
```

## Buttons linked to backend

### Read All Status

The button calls:

```text
POST /action/read-all
```

The UI then displays speed, total picks, density, current pattern, shift, and loom state from the backend result when the loom replies successfully.

### Send Declaration

The button calls:

```text
POST /auth/login
POST /action/admin-declaration
```

The uploaded backend uses daily write authentication. The Electron bridge logs in with the backend's built-in credentials and then configures declaration code `565` using the values entered in the UI.

Note: in the uploaded backend, `/action/admin-declaration` configures the declaration template for the loom-side terminal response workflow. The final completed reply is received when the loom terminal sends the declaration completion back to the host.

## Build a Windows portable EXE

After testing with `npm run dev`, build a portable Windows app:

```powershell
npm run dist
```

The output will be generated in:

```text
release\
```

## Troubleshooting

### 1. npm cannot find package.json

You are in the wrong folder. Run:

```powershell
Get-ChildItem -Recurse -Filter package.json
```

Then `cd` into the folder shown by PowerShell.

### 2. Backend Offline

Check Python:

```powershell
python --version
py -3 --version
```

Also check whether port `13001` or `18080` is already occupied.

```powershell
netstat -ano | findstr :13001
netstat -ano | findstr :18080
```

### 3. Read All Status fails

Check that the loom IP and port are reachable from the host PC:

```powershell
ping 169.254.4.102
Test-NetConnection 169.254.4.102 -Port 13000
```

### 4. The interface opens but no real data appears

This means the Electron UI is working but the host cannot communicate with the loom. Confirm:

```text
1. Correct loom IP address
2. Correct loom port, usually 13000
3. Host PC network adapter is on the same network segment
4. Firewall allows Python/Electron to listen on host port 13001
5. The loom terminal/gateway is powered and reachable
```
