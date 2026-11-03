# -*- mode: python -*-
# Crea l'app per Mac (.app) e per Windows (.exe):  pyinstaller TransferSending.spec
import os
import sys

sys.path.insert(0, SPECPATH)

from transfersending import __version__

a = Analysis(
    ["run.py"],
    datas=[("transfersending/web", "transfersending/web")],
    hiddenimports=["qrcode.image.svg"],
    excludes=["tkinter", "PIL", "numpy"],
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TransferSending",
              console=False, argv_emulation=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="TransferSending")
    app = BUNDLE(
        coll,
        name="TransferSending.app",
        bundle_identifier="io.github.transfersending",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "TransferSending",
            "CFBundleShortVersionString": __version__,
            "NSLocalNetworkUsageDescription":
                "Serve per ricevere foto e file dai telefoni collegati alla tua Wi-Fi.",
            "LSMinimumSystemVersion": "11.0",
        },
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="TransferSending",
              console=False, upx=False)
