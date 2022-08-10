# version.py

"""Provide BLPAPI SDK versions"""

from . import versionhelper

__version__ = "3.18.3"


def print_version() -> None:
    """Print version information of BLPAPI python module and blpapi C++ SDK"""
    print("Python BLPAPI SDK version: ", version())
    print("C++ BLPAPI SDK version:    ", cpp_sdk_version())


def version() -> str:
    """
    Returns:
        str: BLPAPI Python module version
    """
    return __version__


def cpp_sdk_version() -> str:
    """
    Returns:
        str: BLPAPI C++ SDK dependency version
    """
    version_string = ".".join(map(str, versionhelper.blpapi_getVersionInfo()))

    commit_id = versionhelper.blpapi_getVersionIdentifier()
    if commit_id != "Unknown":
        version_string += " (" + commit_id + ")"
    return version_string
