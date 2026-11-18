"""Read-only access to a running HotSpot JVM from a separate process.

This package only opens processes with query and read rights. It must never gain write,
injection, debugger or thread suspension calls.
"""
