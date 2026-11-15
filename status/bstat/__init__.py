"""bstat — a read-only status monitor for a Bambu Lab printer on the local network (P2S first).

It listens to the printer's status channel (MQTT over TLS, port 8883, user 'bblp', the printer's LAN
access code) and shows print progress, temperatures, fans, the AMS and errors in three front-ends:
a local web page, a terminal dashboard and a desktop window. It never sends print or control commands:
the only message it publishes is the status request 'pushall' (ask the printer to send its full state),
and that can be turned off. A camera snapshot is read from the printer's camera port when the printer
allows it.

It lives in print3d_translator/status/ but is a separate tool: p3dt does not import it, and p3dt itself
never talks to a printer.
Version: 2026.11.14.1
"""
__version__ = "2026.11.14.1"
