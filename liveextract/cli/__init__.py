"""
CLI interface package for LiveExtract.
"""

def main():
    from liveextract.cli.main import main as _main
    return _main()

__all__ = ["main"]
