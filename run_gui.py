"""Start the control window. Usage: python run_gui.py [--start]   (--start: start the bot right away)"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from pgbot.gui.app import main  # noqa: E402

if __name__ == "__main__":
    main(autostart="--start" in sys.argv[1:])
