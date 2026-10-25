#!/usr/bin/env python3
"""A stand-in for `codex app-server`: JSON-RPC over stdio, with methods that misbehave on request."""

import json
import os
import sys
import time


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        time.sleep(float(os.environ.get("FAKE_INIT_DELAY", "0")))
        send({"id": rid, "result": {"generation": os.getpid()}})
    elif method == "echo":
        send({"id": rid, "result": msg["params"]})
    elif method == "pid":
        send({"id": rid, "result": {"pid": os.getpid()}})
    elif method == "silent":
        pass
    elif method == "garbage":
        sys.stdout.write('["not", "an", "object"]\n')
        sys.stdout.flush()
        send({"id": rid, "result": {"after": "garbage"}})
    elif method == "die":
        sys.exit(3)
    elif method == "tool":
        # Ask the client to run a tool, then answer this request once the tool result arrives.
        send({"id": "srv-1", "method": "item/tool/call", "params": msg["params"]})
        reply = json.loads(sys.stdin.readline())
        send({"id": rid, "result": {"toolReply": reply}})
    elif rid is not None:
        send({"id": rid, "error": {"message": f"unknown method {method}"}})
