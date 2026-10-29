#!/usr/bin/env python3
"""Write the MSIX fixtures.

A valid package needs the container, a manifest, the content-type map and a
block map that matches the payload byte for byte. The tampered variant is built
from the same inputs with one payload byte flipped after the block map has been
computed, so Porte's validation has something real to reject.
"""
from __future__ import annotations

import base64
import hashlib
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "tree" / "PorteFixture.exe"
BLOCK = 64 * 1024
MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10">
  <Identity Name="Porte.MsiFixture" Publisher="CN=Porte" Version="1.0.0.0"
            ProcessorArchitecture="x64"/>
  <Properties>
    <DisplayName>Porte MSIX Fixture</DisplayName>
    <PublisherDisplayName>Porte</PublisherDisplayName>
    <Logo>Assets\\logo.png</Logo>
  </Properties>
  <Applications>
    <Application Id="Fixture" Executable="PorteFixture.exe">
      <uap:VisualElements DisplayName="Porte MSIX Fixture"
                          Description="Fixture package"
                          BackgroundColor="transparent" Square150x150Logo="Assets\\logo.png"
                          Square44x44Logo="Assets\\logo.png"/>
    </Application>
  </Applications>
  <Capabilities>
    <Capability Name="runFullTrust"/>
  </Capabilities>
</Package>
"""
CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/vnd.ms-appx.manifest+xml"/>
  <Default Extension="exe" ContentType="application/octet-stream"/>
  <Default Extension="txt" ContentType="text/plain"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""


def block_map(files: dict[str, bytes]) -> bytes:
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<BlockMap xmlns="http://schemas.microsoft.com/appx/2010/blockmap"',
             '          HashMethod="http://www.w3.org/2001/04/xmlenc#sha256">']
    for name, data in sorted(files.items()):
        lines.append(f'  <File Name="{name}" Size="{len(data)}" '
                     f'LfhSize="{len(name) + 30}">')
        for offset in range(0, max(len(data), 1), BLOCK):
            chunk = data[offset:offset + BLOCK]
            digest = base64.b64encode(hashlib.sha256(chunk).digest()).decode()
            lines.append(f'    <Block Hash="{digest}"/>')
        lines.append("  </File>")
    lines.append("</BlockMap>")
    return ("\n".join(lines) + "\n").encode()


def build(target: Path, tamper: bool) -> None:
    if not PAYLOAD.exists():
        raise SystemExit("run build.sh; tree/PorteFixture.exe is missing")
    payload = PAYLOAD.read_bytes()
    files = {
        "PorteFixture.exe": payload,
        "readme.txt": b"Porte installer fixture\n",
        "Assets/logo.png": b"\x89PNG\r\n\x1a\n",
    }
    mapping = block_map(files)
    if tamper:
        flipped = bytearray(payload)
        flipped[len(flipped) // 2] ^= 0xFF
        files["PorteFixture.exe"] = bytes(flipped)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("AppxManifest.xml", MANIFEST)
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("AppxBlockMap.xml", mapping)
    print(f"{target}  {'tampered' if tamper else 'valid'}")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: make_msix.py VALID.msix TAMPERED.msix", file=sys.stderr)
        return 2
    build(Path(argv[1]), tamper=False)
    build(Path(argv[2]), tamper=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
