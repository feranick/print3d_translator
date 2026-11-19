"""mcp_handshake.py — times an MCP server's stdio handshake (initialize, then tools/list), as Gemini's and Claude's
apps do. Usage: python tools/mcp_handshake.py /full/path/p3dt-mcp   (Gemini gives up after about 2 s)
Version: 2026.11.18.1
"""
import json, subprocess, sys, time, os
cmd = sys.argv[1:]
t0 = time.time()
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ)
def send(o): p.stdin.write((json.dumps(o) + "\n").encode()); p.stdin.flush()
def recv():
    line = p.stdout.readline()
    return json.loads(line) if line else None
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
      "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}}})
r = recv(); t1 = time.time()
print(f"initialize answered in {t1 - t0:.2f} s:", (r or {}).get("result", {}).get("serverInfo"))
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
r = recv(); print(f"tools/list after {time.time() - t0:.2f} s:", [t["name"] for t in (r or {}).get("result", {}).get("tools", [])])
p.kill()
err = p.stderr.read().decode()[-500:]
if err.strip(): print("stderr:", err)
