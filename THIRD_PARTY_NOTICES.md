# Lumveil third-party notices

Lumveil 2.0.0 is Copyright (c) 2026 ふぁん. All rights reserved.

Lumveil includes or uses the components below. The corresponding license texts
are installed in the `licenses` directory. This file is informational and does
not replace those license texts.

## mpv / libmpv

- License: GNU Lesser General Public License, version 2.1 or later
- Build: `mpv-dev-lgpl-x86_64-20260801-git-1d15686142.7z`
- Upstream: https://github.com/mpv-player/mpv
- Windows build source: https://github.com/zhongfly/mpv-winbuild
- Binary release: https://github.com/zhongfly/mpv-winbuild/releases/tag/2026-08-01-1d15686142
- Archive SHA-256: `f82125c58012586b515e2243f6ecfae665941ff6210d592d81ede43bb7f62e65`

The distributed `libmpv-2.dll` comes from the explicitly named LGPL build.
Lumveil dynamically loads this library through python-mpv. A user may replace
the DLL with a compatible modified LGPL build.

## FFmpeg

- License of the distributed executable: GNU General Public License, version 3
  or later
- Build: `ffmpeg 8.1.1-full_build-www.gyan.dev`
- Upstream: https://ffmpeg.org/
- Windows build and source information: https://www.gyan.dev/ffmpeg/builds/

Lumveil starts `ffmpeg.exe` as a separate process for thumbnail and frame
analysis. The executable reports `--enable-gpl --enable-version3`; therefore it
must not be described as an LGPL-only build.

## Anime4K shaders

- Copyright: Anime4K contributors / bloc97
- License: MIT License
- Source: https://github.com/bloc97/Anime4K

## Pillow

- Copyright: Pillow contributors
- License: MIT-CMU License
- Source: https://github.com/python-pillow/Pillow

## tkinterdnd2

- Copyright: Philippe Gagne and contributors
- License: MIT License
- Source: https://github.com/pmgagne/tkinterdnd2

## Python

- Copyright: Python Software Foundation and contributors
- License: Python Software Foundation License
- Source: https://www.python.org/

## PyInstaller

- License: GPL-2.0-or-later with the PyInstaller bootloader exception; selected
  files may also be available under the Apache License 2.0
- Source: https://github.com/pyinstaller/pyinstaller

The PyInstaller exception permits distributing the generated Lumveil executable
under Lumveil's own terms, subject to every bundled dependency's license.
