"""One-off: build the static OFL fonts in clippie/assets/fonts.

Downloads the upstream files from the google/fonts repository and instantiates static
weights from the variable fonts with fontTools (run via `uvx --from fonttools`, which is
not a project dependency). Usage:

    uvx --from fonttools python scripts/prepare_fonts.py
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
OUT = Path(__file__).resolve().parents[1] / "clippie" / "assets" / "fonts"

VARIABLE = {
    "Montserrat": ("montserrat/Montserrat%5Bwght%5D.ttf", {"ExtraBold": {"wght": 800}, "Bold": {"wght": 700}}),
    "Inter": ("inter/Inter%5Bopsz,wght%5D.ttf",
              {"Medium": {"wght": 500, "opsz": 14}, "SemiBold": {"wght": 600, "opsz": 14},
               "Bold": {"wght": 700, "opsz": 14}}),
}
STATIC = {"Poppins": ["Medium", "SemiBold", "Bold"], "Anton": ["Regular"]}
LICENSES = {"Montserrat": "montserrat", "Inter": "inter", "Poppins": "poppins", "Anton": "anton"}


def fetch(path: str) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=120) as response:
        return response.read()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for family, folder in LICENSES.items():
        (OUT / f"OFL-{family}.txt").write_bytes(fetch(f"{folder}/OFL.txt"))
    for family, (path, weights) in VARIABLE.items():
        raw = OUT / f".{family}-VF.ttf"
        raw.write_bytes(fetch(path))
        for name, location in weights.items():
            font = instancer.instantiateVariableFont(TTFont(raw), location, updateFontNames=True)
            font.save(OUT / f"{family}-{name}.ttf")
        raw.unlink()
    for family, names in STATIC.items():
        for name in names:
            (OUT / f"{family}-{name}.ttf").write_bytes(fetch(f"{family.lower()}/{family}-{name}.ttf"))


if __name__ == "__main__":
    main()
