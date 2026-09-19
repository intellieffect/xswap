"""Allow `python -m xswap` to run the same entry point as the `xswap` script."""

from xswap.cli import main

if __name__ == "__main__":
    main()
