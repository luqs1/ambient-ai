# PyInstaller entry for the ambient-engine sidecar. Don't import engine.addon here:
# mitmdump loads it from disk via -s, and a second copy would split its state.
import sys

from engine.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
