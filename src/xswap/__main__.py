"""Allow `python -m xswap` to run the same entry point as the `xswap` script."""

from xswap.manager import main

if __name__ == "__main__":
    main()
