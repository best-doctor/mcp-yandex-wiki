from .__about__ import __version__

__all__ = ["__version__", "main", "main_readonly"]


def __getattr__(name: str):
    if name in ("main", "main_readonly"):
        from .server import main, main_readonly  # noqa: F811

        globals()["main"] = main
        globals()["main_readonly"] = main_readonly
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
