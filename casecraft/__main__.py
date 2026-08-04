"""Entry points.

    python -m casecraft            → run the MCP server on stdio (what Claude launches)
    python -m casecraft --room     → run just the interview room, for development
    python -m casecraft --check    → validate the case library and exit
"""

from __future__ import annotations

import sys


def main() -> int:
    args = sys.argv[1:]

    if "--check" in args:
        from .library import Library

        lib = Library()
        cat = lib.catalog()
        print(f"cases:     {len(cat['cases'])}")
        print(f"questions: {cat['total_questions']}")
        print(f"types:     {cat['drill_types']}")
        if cat["problems"]:
            print("\nPROBLEMS:")
            for p in cat["problems"]:
                print(f"  - {p}")
            return 1
        print("\nlibrary OK")
        return 0

    if "--room" in args:
        import time

        from .room import RoomServer
        from .session import Room

        room = Room()
        server = RoomServer(room)
        url = server.start()
        print(f"interview room: {url}")
        room.update(status="Development mode — no session attached.")
        server.open_browser()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    from .server import main as run_server

    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
