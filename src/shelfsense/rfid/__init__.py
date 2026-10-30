"""RFID middleware: device-neutral commands/events, device adapters (mock + future vendor drivers).

Nothing outside this package imports a vendor SDK: application code talks to the interfaces in
`devices.py`. A real reader is supported by writing one driver class and registering it (see registry.py).
"""
