"""Setuptools build hooks that freeze the Git version into every artifact."""

from functools import wraps
from pathlib import Path

from setuptools import build_meta
from versioning import write_version

ROOT = Path(__file__).resolve().parents[1]


def versioned(hook):
    @wraps(hook)
    def run(*args, **kwargs):
        write_version(ROOT)
        return hook(*args, **kwargs)

    return run


get_requires_for_build_wheel = versioned(build_meta.get_requires_for_build_wheel)
get_requires_for_build_sdist = versioned(build_meta.get_requires_for_build_sdist)
get_requires_for_build_editable = versioned(build_meta.get_requires_for_build_editable)
prepare_metadata_for_build_wheel = versioned(
    build_meta.prepare_metadata_for_build_wheel
)
prepare_metadata_for_build_editable = versioned(
    build_meta.prepare_metadata_for_build_editable
)
build_wheel = versioned(build_meta.build_wheel)
build_sdist = versioned(build_meta.build_sdist)
build_editable = versioned(build_meta.build_editable)
