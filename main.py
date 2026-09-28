import sys

from chunk_cleaner.gui import CleanerApp, main

if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        # Packaged-app startup check for Windows CI. No scan, logs, or deletion.
        try:
            import tkinter as tk
            import lz4.block
            import xxhash

            root = tk.Tk()
            app = CleanerApp(root)
            root.after(300, app.close)
            root.mainloop()
        except Exception:
            sys.exit(1)
    else:
        main()
