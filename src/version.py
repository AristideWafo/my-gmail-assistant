import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
UNKNOWN = "unknown"


def app_version(pyproject: Path = PYPROJECT) -> str:
    """The version semantic-release writes in pyproject.toml, which ships with the sources."""
    try:
        with open(pyproject, "rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return UNKNOWN
