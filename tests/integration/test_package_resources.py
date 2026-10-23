from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import zipfile


def test_packaged_profiles_match_documented_profile_sources() -> None:
    repository = Path(__file__).resolve().parents[2]
    for source in sorted((repository / "resources" / "profiles").glob("*.json")):
        packaged = repository / "src" / "silverloupe" / "resources" / "profiles" / source.name
        assert packaged.read_bytes() == source.read_bytes()


def test_built_wheel_loads_genre_profiles_without_source_checkout(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(wheel_dir)],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("silverloupe-*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)

    script = (
        "import sys; "
        f"sys.path.insert(0, {str(installed)!r}); "
        "from silverloupe.contracts import Brief; "
        "from silverloupe.curation import _load_profiles; "
        "profiles = _load_profiles(Brief(genres=['wedding'])); "
        "assert profiles[0].genre == 'wedding'"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
