# Build on Windows: python -m PyInstaller --noconfirm --distpath . JARVIS.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
camera_data, camera_binaries, camera_hidden = collect_all('cv2_enumerate_cameras')

analysis = Analysis(
    [str(root / 'main.py')],
    pathex=[str(root)],
    binaries=camera_binaries,
    datas=camera_data + [(str(root / 'assets'), 'assets')],
    hiddenimports=camera_hidden,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name='JARVIS_Robot_Console',
    console=False,
    upx=False,
    icon=str(root / 'assets' / 'jarvis.ico'),
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    name='Windows',
    upx=False,
)
