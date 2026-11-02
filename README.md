# JARVIS Robot Console

Version **0.1.0** — a native Windows desktop console for the JARVIS tabletop robotics project.

The current application provides two video monitoring pages: **Wrist Camera** and **Astra Global Camera**. It reads camera images only. It does not send robot motion, reset, firmware update, or robot configuration commands, and it does not require Kortex account credentials.

## Current features

- Switch between the two camera pages, or use `Ctrl+1` and `Ctrl+2`.
- Independent capture processes keep the interface responsive when one source is unavailable. Switching pages does not reopen a camera.
- Connect to a wrist RTSP stream. The default is `rtsp://192.168.1.10/color`.
- Select the USB camera by its device name. The default is `Astra Pro HD Camera`; if the named device is absent, the application does not silently substitute a built-in webcam. Refresh the device list to select another connected camera explicitly.
- Disconnect, reconnect, retry interrupted streams, and inspect frame rate and last-frame time.
- Save a snapshot of a current live frame. Stale frames are dimmed and marked as no longer updating.

## Run the Windows application

For a packaged build, extract the entire Windows artifact and run `JARVIS_Robot_Console.exe`. Keep the executable and its `_internal` folder together.

When the packaged build is placed in this repository's `Windows/` directory, double-click `launch.cmd` to open it. The launcher prefers `Windows/JARVIS_Robot_Console.exe`; otherwise, it tries the source application using `.venv`, the Windows Python 3.13 launcher, or a Python 3.13 interpreter on `PATH`.

The packaged application does not require a separate Python installation. Store it in a writable directory so that it can save settings, logs, and snapshots.

## Settings, logs, and snapshots

Runtime files are stored beside the running executable, or in the repository root when running from source:

| File or directory | Purpose |
| --- | --- |
| `data/settings.json` | Selected page, wrist stream URL, and USB camera name |
| `data/app.log` | Application diagnostics |
| `screenshots/` | Snapshots saved by the user |

For example, when launched as `Windows/JARVIS_Robot_Console.exe`, settings are in `Windows/data/settings.json` and snapshots are in `Windows/screenshots/`. These runtime files are excluded from version control. They may contain private stream addresses or images; review them before sharing.

## Run from source

Use **64-bit Python 3.13 on Windows**, with Tcl/Tk support from the official Python Windows installer.

From the repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

After the virtual environment is prepared, `launch.cmd` can also run the source application. The application does not install its own dependencies.

## Build a Windows executable

Build on Windows using the same 64-bit Python 3.13 environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath . JARVIS.spec
```

The result is `Windows/JARVIS_Robot_Console.exe` with its companion files in `Windows/_internal/`. Distribute the complete `Windows/` directory. `requirements-build.txt` pins PyInstaller to `6.22.3`.

The GitHub Actions Windows workflow builds the same folder and uploads it as an artifact. A successful build checks packaging; it does not validate physical camera availability or image output.

## Current validation and limits

The local Astra USB camera has been used for live capture. Interface checks have covered page switching, disconnection, snapshots, and wrist-source failure. Capture-service tests also verified reconnection and bounded cleanup of camera processes. The English publication copy has passed syntax and stream-address validation checks.

The wrist camera is currently offline in the project's hardware setup. Its page and failure handling are available, but **live images from the real wrist camera have not yet been validated**. This application does not repair an unavailable robot vision module.

Version 0.1.0 does not provide camera-to-robot calibration, object recognition, robot control, or VLA execution.

## Project layout and future pages

```text
main.py                         Desktop entry point and multiprocessing freeze support
jarvis_camera/
  app.py                        Window, navigation, pages, and display updates
  capture.py                    Capture processes, retries, and cleanup
  settings.py                   Local settings and stream URL validation
assets/                         Application icon and other UI assets
JARVIS.spec                     Windows PyInstaller build definition
launch.cmd                      Packaged-build or source launcher
.github/workflows/windows-build.yml
```

The Python package remains named `jarvis_camera`. Camera pages are described by `PAGE_DEFINITIONS`. Future calibration, robot status, or task pages can extend the navigation and reuse the capture service. Keep camera acquisition separate from Tkinter widgets, and keep any future robot command path separate from video monitoring.

## License

No license has been selected for this repository. This repository does not grant an open-source license. Third-party dependencies retain their respective licenses.
