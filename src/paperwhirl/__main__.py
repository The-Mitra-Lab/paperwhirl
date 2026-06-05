# Stage 6 E2: enables `python -m paperwhirl`. The bundled .app's
# bin/paperwhirl is a thin shell wrapper that does
# `python3.12 -m paperwhirl` — that way the entry point doesn't
# depend on the absolute shebang pip writes into console scripts,
# which would otherwise bake the build machine's path into the .app.
from paperwhirl.web.server import main

if __name__ == "__main__":
    main()
