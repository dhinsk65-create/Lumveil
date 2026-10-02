# Windows stability regression checks

Run the isolated logic checks with Python 3.12 or newer:

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
```

The checks extract the relevant code with AST. They do not import the player,
open a window, or read/write the user's Lumveil settings.
They also inject JSON serialization, disk flush, and file replacement failures
to confirm that the previous settings file stays intact.

For actual Tk/libmpv/GPU verification, use the Python environment containing
`python-mpv`, `Pillow`, and `tkinterdnd2`, with the player's FFmpeg and libmpv DLL:

```powershell
python -B tests/runtime_smoke.py --work-dir build/runtime-smoke
```

This test creates synthetic media and a separate settings profile in the given
work directory. It checks interpolation ON/OFF, GPU shader/frame retrieval,
baseline cache reuse, AUTO restart, stale resume/playlist results, UI callbacks,
current-video repeat (twice), switching back to stop/next, repeat persistence,
stale/late end-of-file notifications, paused AUTO sampling and refresh on seek
or adjustment, atomic settings saves, and worker shutdown. The player window
is withdrawn. Results are saved to
`results.json` in that directory.

To verify the embedded player code and resource layout in a PyInstaller EXE:

```powershell
python -B tests/runtime_smoke.py --work-dir build/bundle-smoke --bundle path/to/Lumveil.exe
```

This also requires PyInstaller. It loads the EXE's embedded code with the frozen
resource paths, using the development Python environment; it does not exercise
the EXE bootloader. Long playback, 4K/HDR materials, and visual acceptance remain
separate checks.

For the full review's actual settings/navigation/UI checks:

```powershell
python -B tests/full_ui_smoke.py --work-dir build/full-ui-smoke
python -B tests/full_ui_smoke.py --work-dir build/full-ui-bundle --bundle path/to/Lumveil.exe
```

This checks scale clicks against native values, settings scrolling at minimum
size, popup input isolation, fullscreen menu access, invalid GPU settings,
visible playback errors, time/chapter navigation, editing/saving/loading lists,
large bookmark scrolling, state indicators, help, actual decoder reporting,
and manual setting restoration in fresh processes with restoration ON/OFF.
The profile and media are synthetic and isolated. Brief test windows may appear.

To check the same UI and CPU playback paths without GPU video output/decoding:

```powershell
python -B tests/full_ui_smoke.py --work-dir build/full-ui-software --software-only
python -B tests/full_ui_smoke.py --work-dir build/full-ui-software-bundle --bundle path/to/Lumveil.exe --software-only
```

This test-only backend forces `vo=null`, `hwdec=no`, one decoder thread, and
disables mpv configuration files. It blocks requests to enable hardware decoding.
The flag also reaches the child processes that check setting restoration.
It exercises real Tk/libmpv and the EXE's extracted code, but does not establish
video rendering quality, GPU stability, or the frozen EXE bootloader's startup.

Distribution tests use an in-memory Windows registry and injected filesystem
failures. They never alter real associations or execute the Lumveil installer.
The filesystem transaction test models the rollback; passing it and compiling
NSIS does not establish actual installed-environment update/rollback behavior.

The GPU runtime smoke also reads the rendered window image, requires nonblank
output with the D3D11 backend on Windows, and verifies that adjustment changes
the rendered pixels. It writes `gpu-rendered-frame.png` as evidence.

To exercise the actual EXE bootloader with an isolated profile:

```powershell
./tests/exe_smoke.ps1 -Bundle path/to/Lumveil.exe -Video path/to/synthetic.mp4 -WorkDir build/exe-smoke
```

This refuses to run alongside an existing Lumveil process. It disables hardware
decoding, interpolation and correction, requests a normal window close, and
checks the exit code and isolated settings. GPU counters are reported separately;
zero activity in these counters does not count as successful GPU rendering.
