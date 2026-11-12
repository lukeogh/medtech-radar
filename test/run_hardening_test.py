#!/usr/bin/env python
"""Acceptance checks for the dashboard's write guard and the anthropic pin.

No model calls and no network beyond loopback. RADAR_MOCK is forced on, so a
route that reached the scorer by mistake would stop with an error and spend
nothing. The server runs on a loopback port inside this process with every
side effect stubbed, so a request the guard lets through reaches a stub and
never a file, a subprocess or a database, except in the one section that
says it uses a throwaway database.

What it checks:

- every POST route the server defines is refused in enforce mode when it
  arrives the way a forged cross-site request does, and refused for the
  right reason before any route code runs,
- in warn mode the same requests go through and each one leaves a log line
  saying it would have been refused,
- a normal request from the dashboard's own page passes in both modes, on
  the loopback address and behind the proxy's host name,
- a rebound host name, a look-alike name, a null Origin, a repeated Host or
  Origin header and a missing or empty header are all refused, and a browser
  preflight is never answered with permission,
- no reply carries a CORS header, and in enforce mode every reply forbids
  framing,
- a refused POST with a slow body still gets its 403, so the browser shows
  the refusal and not a network error,
- an exception inside a route is a clear 500 the page can show, never a
  dropped connection,
- the handler has only the two verbs the guard covers, and each one starts
  by asking the guard,
- main() puts the mode and the names from radar.yaml into the handler,
- every page's script wraps fetch so that its POSTs carry the header. When
  node is on the path the scripts are also parsed and the wrapper is run,
- requirements.txt keeps anthropic below 1.0.0, where the temperature
  keyword that claude_call sends became a TypeError, the Dockerfile snippet
  in the docs carries the same range, and the interpreter running this
  suite takes the keyword.

Exits non zero on any failure, readable FAILs, never a traceback.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Forced on before anything else loads, so a stub that was forgotten can only
# ever reach the scorer's refusal to run without a mock.
os.environ["RADAR_MOCK"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_dashboard
import cv_store
import radar_common
import serve_dashboard

# The guard a fresh import starts with, before main() replaces it.
DEFAULT_GUARD = serve_dashboard.GUARD

failures: list[str] = []
skipped: list[str] = []

HEADER = serve_dashboard.WRITE_HEADER
PROXY_NAME = "radar.example.test"
# The name build_digest.py links the Monday digest to, which radar.yaml lists.
SHIPPED_PROXY = "medtech-radar.keogh.cloud"
EVIL = "evil.example"
LOOKALIKES = ("localhost.evil.example", "evil-localhost", "xlocalhost",
              "evilradar.example.test", "radar.example.test.evil.example",
              "127.0.0.1.evil.example", "127.0.0.10", "1127.0.0.1")


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def note(text: str, skip: bool = False) -> None:
    print("NOTE  " + text)
    if skip:
        skipped.append(text)


# ------------------------------------------------------------- the stubs

CALLS: list[str] = []

# (module, attribute, what the stub returns). Everything a POST route can
# change or spend goes through one of these, so a request that gets past the
# guard in this runner proves it reached the route and does nothing else.
STUBS = (
    (serve_dashboard, "run_watcher", {"ok": True, "summary": {}}),
    (serve_dashboard, "run_stale_rescore",
     {"ok": True, "stale_rescored": 0, "remaining_stale": 0}),
    (serve_dashboard, "set_acknowledged", {"ok": True, "id": 1}),
    (serve_dashboard, "set_lifecycle", {"ok": True, "id": 1}),
    (serve_dashboard, "set_applied", {"ok": True, "id": 1}),
    (serve_dashboard, "set_signal_state", {"ok": True, "id": 1}),
    (serve_dashboard, "set_company_state", {"ok": True, "id": 1}),
    (serve_dashboard, "set_touch_outcome", {"ok": True, "id": 1}),
    (radar_common, "update_pref_line", "stored"),
    (radar_common, "add_job_source", {"id": "stub-alert"}),
    (cv_store, "extract_markdown", "# stub"),
    (cv_store, "stage", "0123456789abcdef"),
    (cv_store, "confirm", {"active": "cv-stub.md"}),
    (cv_store, "discard", None),
)

# One row per POST route, the path, a body that satisfies the route's own
# validation, the extra headers it needs, and the stub that proves it ran.
ROUTES = (
    ("/watch", b"", {}, "run_watcher"),
    ("/ack", b'{"id": 1}', {}, "set_acknowledged"),
    ("/unack", b'{"id": 1}', {}, "set_acknowledged"),
    ("/close", b'{"id": 1}', {}, "set_lifecycle"),
    ("/unclose", b'{"id": 1}', {}, "set_lifecycle"),
    ("/dismiss", b'{"id": 1}', {}, "set_lifecycle"),
    ("/undismiss", b'{"id": 1}', {}, "set_lifecycle"),
    ("/apply", b'{"id": 1}', {}, "set_applied"),
    ("/unapply", b'{"id": 1}', {}, "set_applied"),
    ("/profile/setting", b'{"key": "keywords", "value": "x"}', {},
     "update_pref_line"),
    ("/touch/outcome", b'{"id": 1, "outcome": "none"}', {},
     "set_touch_outcome"),
    ("/company/state", b'{"id": 1, "state": "client"}', {},
     "set_company_state"),
    ("/jobs/source", b'{"name": "x", "sender": "y"}', {}, "add_job_source"),
    ("/sig/done", b'{"id": 1}', {}, "set_signal_state"),
    ("/sig/ack", b'{"id": 1}', {}, "set_signal_state"),
    ("/sig/unack", b'{"id": 1}', {}, "set_signal_state"),
    ("/cv/preview", b"A CV.", {"X-Filename": "cv.txt"}, "extract_markdown"),
    ("/cv/rescore", b'{"confirm": true}', {}, "run_stale_rescore"),
    ("/cv/confirm", b'{"token": "t"}', {}, "confirm"),
    ("/cv/discard", b'{"token": "t"}', {}, "discard"),
)


@contextlib.contextmanager
def stubbed(skip=()):
    """Replace every effect with a recorder, restore the originals after."""
    saved = []
    for module, name, result in STUBS:
        if name in skip:
            continue
        saved.append((module, name, getattr(module, name)))

        def fake(*_a, _name=name, _result=result, **_k):
            CALLS.append(_name)
            return _result
        setattr(module, name, fake)
    del CALLS[:]
    try:
        yield
    finally:
        for module, name, original in saved:
            setattr(module, name, original)


@contextlib.contextmanager
def serving(guard):
    """The real Handler on a loopback port with the given guard installed.

    The server's own access log is silenced, so what reaches stderr is the
    guard's lines and nothing else.
    """
    previous = serve_dashboard.GUARD
    log = serve_dashboard.Handler.log_message
    serve_dashboard.GUARD = guard
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    server = serve_dashboard.RadarServer(("127.0.0.1", 0),
                                         serve_dashboard.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        serve_dashboard.GUARD = previous
        serve_dashboard.Handler.log_message = log


@contextlib.contextmanager
def captured_stderr():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


def call(port, method, path, body=b"", headers=None):
    """One request. Returns (status, response headers dict, body bytes).

    A socket error comes back as status 0 with a NOTE, so the check that
    asked for the request fails by its own name and the run goes on.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body or None, headers=headers or {})
        resp = conn.getresponse()
        return (resp.status, {k.lower(): v for k, v in resp.getheaders()},
                resp.read())
    except (OSError, http.client.HTTPException) as err:
        note(f"{method} {path[:40]} failed with {type(err).__name__}")
        return 0, {}, b""
    finally:
        conn.close()


def raw_post(port, header_lines, body=b"", piece=0, pause=0.0):
    """POST /ack with hand-built header lines, the body optionally in pieces.

    Returns everything the server replied, or the name of the socket error
    that cut the exchange short.
    """
    head = ("POST /ack HTTP/1.1\r\n" + "\r\n".join(header_lines)
            + f"\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n")
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        sock.sendall(head.encode())
        if piece:
            for at in range(0, len(body), piece):
                sock.sendall(body[at:at + piece])
                time.sleep(pause)
        else:
            sock.sendall(body)
        reply = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
        return reply
    except OSError as err:
        return type(err).__name__.encode()
    finally:
        sock.close()


def guard_lines(buf) -> list[str]:
    return [ln for ln in buf.getvalue().splitlines()
            if ln.startswith("write guard")]


def forbids_framing(headers: dict) -> bool:
    return (headers.get("x-frame-options") == "DENY"
            and "frame-ancestors 'none'"
            in headers.get("content-security-policy", ""))


def has_cors(headers: dict) -> bool:
    return any(k.startswith("access-control-") for k in headers)


def scenarios(port):
    """(name, headers, passes). passes is what enforce mode must do."""
    own = f"127.0.0.1:{port}"
    return (
        ("a request from the dashboard's own page",
         {"Host": own, "Origin": f"http://{own}", HEADER: "1"}, True),
        ("a request from the page behind the proxy",
         {"Host": PROXY_NAME, "Origin": f"https://{PROXY_NAME}", HEADER: "1"},
         True),
        ("a tool that sends the header and no Origin",
         {"Host": own, HEADER: "1"}, True),
        ("a forged no-cors POST from another site",
         {"Host": own, "Origin": f"https://{EVIL}",
          "Content-Type": "text/plain;charset=UTF-8"}, False),
        ("a forged POST that guessed the header",
         {"Host": own, "Origin": f"https://{EVIL}", HEADER: "1"}, False),
        ("a DNS rebinding page, header and all",
         {"Host": f"{EVIL}:{port}", "Origin": f"http://{EVIL}:{port}",
          HEADER: "1"}, False),
        ("a foreign Host with an Origin and header that look right",
         {"Host": f"{EVIL}:{port}", "Origin": f"http://{own}", HEADER: "1"},
         False),
        ("the proxy's name with a foreign Origin",
         {"Host": PROXY_NAME, "Origin": f"https://{EVIL}", HEADER: "1"}, False),
        ("a sandboxed page whose Origin is null",
         {"Host": own, "Origin": "null", HEADER: "1"}, False),
        ("a POST with no header at all",
         {"Host": own}, False),
        ("a POST whose header is empty",
         {"Host": own, HEADER: ""}, False),
    )


def run_matrix(port, mode):
    """Every scenario against every route, one verdict line per scenario."""
    for name, extra, passes in scenarios(port):
        bad_status, bad_effect, bad_log, bad_cors = [], [], [], []
        for path, body, route_headers, stub in ROUTES:
            headers = {"Content-Type": "application/json"}
            headers.update(route_headers)
            headers.update(extra)
            del CALLS[:]
            with captured_stderr() as buf:
                status, got, data = call(port, "POST", path, body, headers)
            lines = guard_lines(buf)
            reached = stub in CALLS
            if has_cors(got):
                bad_cors.append(path)
            if mode == "enforce":
                if status != (200 if passes else 403):
                    bad_status.append(f"{path} {status}")
                if passes and not reached:
                    bad_effect.append(path)
                if not passes and CALLS:
                    bad_effect.append(f"{path} ran {CALLS}")
                if not passes:
                    try:
                        said = json.loads(data).get("note", "")
                    except ValueError:
                        said = ""
                    if "The write guard refused this request" not in said:
                        bad_status.append(f"{path} gave no clear note")
                    if not forbids_framing(got):
                        bad_status.append(f"{path} allowed framing")
                refused = any(ln.startswith("write guard refused")
                              for ln in lines)
                if passes == refused:
                    bad_log.append(path)
            else:
                if status != 200:
                    bad_status.append(f"{path} {status}")
                if not reached:
                    bad_effect.append(path)
                if forbids_framing(got) or "x-frame-options" in got:
                    bad_status.append(f"{path} forbade framing in warn mode")
                warned = any(ln.startswith("write guard would refuse")
                             and "warn only" in ln for ln in lines)
                if passes == warned:
                    bad_log.append(path)
        verdict = ("passes" if passes else
                   "is refused before the route runs" if mode == "enforce"
                   else "goes through and is logged")
        bad = bad_status or bad_effect or bad_log or bad_cors
        check(not bad,
              f"in {mode} mode {name} {verdict} on all {len(ROUTES)} routes"
              + (f" (bad status {len(bad_status)} {bad_status[:3]}, "
                 f"bad effect {len(bad_effect)} {bad_effect[:3]}, "
                 f"bad log {len(bad_log)} {bad_log[:3]}, "
                 f"CORS header {len(bad_cors)} {bad_cors[:3]})"
                 if bad else ""))


# ------------------------------------------------------------- the checks

def server_tree() -> ast.Module:
    return ast.parse((REPO_ROOT / "scripts" / "serve_dashboard.py")
                     .read_text(encoding="utf-8"))


def post_routes_in_source() -> set[str]:
    """Every path the server's POST router compares against, read off the AST.

    It sees path == "/x", path in ("/x", "/y") and path.startswith("/x"). A
    route written in any other shape is not seen here, and the guard does not
    depend on this list, because the verb check below pins that do_POST asks
    the guard before it does anything else.
    """
    found: set[str] = set()
    for node in ast.walk(server_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == "_route_post":
            for sub in ast.walk(node):
                values = []
                if (isinstance(sub, ast.Compare)
                        and isinstance(sub.left, ast.Name)
                        and sub.left.id == "path"):
                    for comp in sub.comparators:
                        values += (comp.elts if isinstance(
                            comp, (ast.Tuple, ast.List, ast.Set)) else [comp])
                elif (isinstance(sub, ast.Call)
                      and isinstance(sub.func, ast.Attribute)
                      and sub.func.attr == "startswith"
                      and isinstance(sub.func.value, ast.Name)
                      and sub.func.value.id == "path"):
                    values += sub.args
                for v in values:
                    if (isinstance(v, ast.Constant) and isinstance(v.value, str)
                            and v.value.startswith("/")):
                        found.add(v.value)
    return found


def handler_methods() -> dict:
    for node in server_tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "Handler":
            return {n.name: n for n in node.body
                    if isinstance(n, ast.FunctionDef)}
    return {}


def admits_first(fn: ast.FunctionDef, method: str) -> bool:
    """Is asking the guard the first thing this handler does that matters?"""
    body = [s for s in fn.body
            if not (isinstance(s, ast.Expr)
                    and isinstance(s.value, ast.Constant))
            and not (isinstance(s, ast.Assign)
                     and all(isinstance(t, ast.Attribute)
                             and t.attr == "_answered" for t in s.targets))]
    first = body[0] if body else None
    if isinstance(first, ast.Try) and first.body:
        first = first.body[0]
    return (isinstance(first, ast.If)
            and f"GUARD.admit(self, '{method}')" in ast.unparse(first.test))


def sends_temperature() -> bool:
    """Does radar_common pass a temperature keyword to a create() call."""
    tree = ast.parse((REPO_ROOT / "scripts" / "radar_common.py")
                     .read_text(encoding="utf-8"))
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "create"
               and any(k.arg == "temperature" for k in n.keywords)
               for n in ast.walk(tree))


def script_text(html: str) -> str:
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))


# What node is asked to do with the page scripts. It is a file of its own in
# the temporary directory, so no quoting of a long script reaches a shell.
NODE_CHECK = r"""
const vm = require('vm');
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let bad = 0;
const say = (ok, label) => {
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label);
  if (!ok) bad += 1;
};
(async () => {
  for (const [name, text] of Object.entries(data.pages)) {
    try {
      new vm.Script(text, { filename: name });
      say(true, `node parses the ${name} page script`);
    } catch (err) {
      say(false, `node parses the ${name} page script (${err.message})`);
    }
  }
  if (typeof Headers === 'undefined') {
    console.log('NOTE  this node has no Headers, so the wrapper was not run');
    process.exit(bad ? 1 : 0);
  }
  const calls = [];
  const sandbox = {
    Headers,
    document: {
      addEventListener() {}, getElementById() { return null; },
      querySelectorAll() { return []; },
      documentElement: { setAttribute() {} },
    },
    localStorage: { getItem() { return null; }, setItem() {} },
  };
  sandbox.window = sandbox;
  sandbox.fetch = (url, opts) => {
    calls.push({ url, opts });
    return Promise.resolve({ ok: true });
  };
  vm.createContext(sandbox);
  try {
    vm.runInContext(data.shared, sandbox);
  } catch (err) {
    say(false, `the shared page script runs (${err.message})`);
    process.exit(1);
  }
  const sent = async (opts) => {
    await sandbox.window.fetch('/ack', opts);
    return calls[calls.length - 1].opts;
  };
  const shapes = {
    'no headers': undefined,
    'a plain object': { 'Content-Type': 'application/json' },
    'a Headers object': new Headers({ 'Content-Type': 'application/json' }),
    'an array of pairs': [['Content-Type', 'application/json']],
  };
  for (const [label, headers] of Object.entries(shapes)) {
    const opts = { method: 'POST', body: '{}' };
    if (headers !== undefined) opts.headers = headers;
    const got = await sent(opts);
    const h = got.headers;
    const keeps = headers === undefined
      || h.get('content-type') === 'application/json';
    say(h instanceof Headers && h.get(data.header.toLowerCase()) === '1'
        && keeps && got.body === '{}' && got.method === 'POST',
        `a POST with ${label} reaches fetch with ${data.header} and its own `
        + 'headers, method and body');
  }
  const lower = await sent({ method: 'post', body: '{}' });
  say(lower.headers && lower.headers.get(data.header.toLowerCase()) === '1',
      'a POST whose method is written in lower case gets the header too');
  const plain = { method: 'GET' };
  const got = await sent(plain);
  say(got === plain && got.headers === undefined,
      'a GET goes through untouched');
  calls.length = 0;
  await sandbox.window.fetch('/x');
  say(calls.length === 1 && calls[0].opts === undefined,
      'a fetch with no options goes through untouched');
  const before = calls.length;
  let threw = false;
  let rejected = false;
  try {
    const pending = sandbox.window.fetch('/cv/preview', {
      method: 'POST', body: 'x', headers: { 'X-Filename': 'CV – final' },
    });
    await pending.then(() => {}, () => { rejected = true; });
  } catch (err) {
    threw = true;
  }
  say(!threw && rejected && calls.length === before,
      'a header value fetch will not take is a rejected promise, not a '
      + 'throw, and nothing is sent');
  process.exit(bad ? 1 : 0);
})();
"""


def node_checks(pages: dict, tmp: Path) -> None:
    """Parse every page script in node and run the shared fetch wrapper."""
    node = shutil.which("node")
    if not node:
        note("node is not on the path, so the page scripts were not parsed "
             "or run", skip=True)
        return
    (tmp / "check.js").write_text(NODE_CHECK, encoding="utf-8")
    (tmp / "scripts.json").write_text(json.dumps({
        "pages": {n: script_text(h) for n, h in pages.items()},
        "shared": build_dashboard.SCRIPT,
        "header": HEADER,
    }), encoding="utf-8")
    try:
        done = subprocess.run(
            [node, str(tmp / "check.js"), str(tmp / "scripts.json")],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as err:
        check(False, f"node ran the page script checks ({type(err).__name__})")
        return
    lines = done.stdout.splitlines()
    for ln in lines:
        if ln.startswith(("PASS  ", "FAIL  ")):
            check(ln.startswith("PASS"), ln[6:])
        elif ln.startswith("NOTE  "):
            note(ln[6:], skip=True)
    if done.returncode != 0 and not any(ln.startswith("FAIL  ")
                                        for ln in lines):
        check(False, "node ran the page script checks ("
              + (done.stderr.strip()[:200] or f"exit {done.returncode}") + ")")


def _triple(text: str) -> tuple[int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", text)][:3]
    return tuple(nums + [0] * (3 - len(nums)))


def satisfies(version: str, spec: str) -> bool:
    """Does one release meet a requirements.txt version specifier.

    Done without the packaging library. It handles the seven comparison
    operators pip accepts for a plain release, comma separated. Anything it
    cannot read is False, so a mangled pin fails loudly instead of passing.
    """
    have = _triple(version)
    if not spec.strip():
        return False
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(~=|==|!=|>=|<=|>|<)\s*([0-9][0-9.]*)\s*", part)
        if not m:
            return False
        op, wanted = m.groups()
        want = _triple(wanted)
        if op == "~=":
            pieces = [int(n) for n in wanted.split(".")]
            if len(pieces) < 2:
                return False
            ceiling = pieces[:-1]
            ceiling[-1] += 1
            ok = want <= have < _triple(".".join(str(n) for n in ceiling))
        else:
            ok = {"==": have == want, "!=": have != want, ">=": have >= want,
                  "<=": have <= want, ">": have > want, "<": have < want}[op]
        if not ok:
            return False
    return True


class Headers:
    """The two methods the guard calls on a message's headers."""

    def __init__(self, pairs=()):
        self.pairs = list(pairs.items() if isinstance(pairs, dict) else pairs)

    def get_all(self, name, default=None):
        values = [v for k, v in self.pairs if k.lower() == name.lower()]
        return values or default

    def get(self, name, default=None):
        values = self.get_all(name)
        return values[0] if values else default


class BrokenHeaders:
    """A message whose headers cannot be read at all."""

    def get_all(self, *_a):
        raise ValueError("unreadable")

    get = get_all


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted for suite symmetry, unused")
    parser.parse_args()

    # ----- what a fresh import starts with
    check(DEFAULT_GUARD.enforcing
          and DEFAULT_GUARD.names == frozenset(serve_dashboard.LOOPBACK_NAMES),
          "before main() runs the guard enforces, for the loopback names only")
    check(serve_dashboard.WriteGuard("bogus", ()).enforcing,
          "a guard built with a mode it does not know enforces")

    # ----- the names the guard compares
    hn = serve_dashboard.host_name
    check(hn("Radar.Example.TEST") == "radar.example.test"
          and hn("radar.example.test:443") == "radar.example.test"
          and hn("https://radar.example.test") == "radar.example.test"
          and hn("https://radar.example.test:8443/x") == "radar.example.test"
          and hn("radar.example.test.") == "radar.example.test",
          "a host name reads the same from a Host, an Origin or a config entry")
    check(hn("[::1]:8787") == "::1" and hn("::1") == "::1"
          and hn("http://[::1]:8787") == "::1",
          "an IPv6 loopback reads with or without brackets and port")
    check(all(hn(v) == "" for v in (
        "", None, "a b", "x:abc", "[::1", "[::1]x", "good.example@evil.example",
        "good.example/evil", "good.example\\evil",
        "evil.example:80@good.example")),
        "anything that is not a plain host name reads as empty and never "
        "matches")

    # ----- the guard's verdict on headers alone, no sockets
    guard = serve_dashboard.WriteGuard(
        "enforce", ("localhost", "127.0.0.1", PROXY_NAME))
    good = {"Host": "localhost:8787", "Origin": "http://localhost:8787",
            HEADER: "1"}
    check(guard.problems("POST", "/ack", Headers(good)) == [],
          "a POST naming this server with the header has no problems")
    check(guard.problems("POST", "/ack", Headers(
        {"Host": "localhost", HEADER: "1"})) == [],
        "a POST with no Origin and the header has no problems")
    check(len(guard.problems("POST", "/ack", Headers(
        {"Host": "localhost", "Origin": f"https://{EVIL}"}))) == 2,
        "a foreign Origin and a missing header are two separate problems")
    check(len(guard.problems("POST", "/ack", Headers(
        {**good, "Host": EVIL}))) == 1,
        "a foreign Host alone is one problem, so the Host check stands "
        "without the Origin check")
    check(len(guard.problems("POST", "/ack", Headers(
        {**good, "Origin": f"https://{EVIL}"}))) == 1,
        "a foreign Origin alone is one problem, so the Origin check stands "
        "without the Host check")
    check(len(guard.problems("POST", "/ack", Headers(
        {"Host": "localhost", "Origin": "http://localhost", HEADER: "   "})
    )) == 1,
        "a header that is only spaces counts as missing")
    check(len(guard.problems("POST", "/ack", Headers(
        [("Host", "localhost"), ("Origin", "http://localhost"),
         ("Origin", "http://localhost"), (HEADER, "1")]))) == 1,
        "two Origin headers are one problem even when both look right")
    nulled = guard.problems("POST", "/ack", Headers({**good, "Origin": "null"}))
    check(len(nulled) == 1 and "is null" in nulled[0],
          "a null Origin is refused with a reason that says it is null")
    check(guard.problems("POST", "/ack", BrokenHeaders()) != [],
          "headers the guard cannot read are refused, never waved through")
    check(guard.problems("GET", "/", Headers({"Host": "localhost"})) == [],
          "a GET needs a good Host and nothing else")
    check(guard.problems("GET", "/health", Headers({"Host": EVIL})) == [],
          "GET /health is exempt from the Host check")
    check(len(guard.problems("GET", "/jobs", Headers({"Host": EVIL}))) == 1,
          "any other GET with a foreign Host has a problem")
    check(all(len(guard.problems(m, p, Headers({"Host": EVIL, **{
        k: v for k, v in good.items() if k != "Host"}}))) == 1
        for m, p in (("POST", "/health"), ("GET", "/healthz"),
                     ("GET", "/health/x"), ("GET", "/Health"))),
        "the /health exemption covers GET /health and nothing near it")
    check(guard.problems("GET", "/", Headers()) == [],
          "a request with no Host header at all is not a browser, it passes")
    as_host = [n for n in LOOKALIKES if len(guard.problems(
        "POST", "/ack", Headers({**good, "Host": f"{n}:8787"}))) != 1]
    as_origin = [n for n in LOOKALIKES if len(guard.problems(
        "POST", "/ack", Headers({**good, "Origin": f"https://{n}:8443"}))) != 1]
    check(not as_host and not as_origin,
          "names that only contain or resemble an allowed name are refused "
          f"as a Host and as an Origin (missed {as_host + as_origin})")

    # ----- reading the mode and the names from radar.yaml
    cfg = radar_common.load_config()
    file_mode = str(cfg.get("dashboard_guard", "")).strip().lower()
    file_hosts = cfg.get("dashboard_allowed_hosts")
    if isinstance(file_hosts, str):
        file_hosts = file_hosts.replace(",", " ").split()
    check(file_mode in ("warn", "enforce") and isinstance(file_hosts, list),
          "radar.yaml carries a dashboard_guard mode and a list of allowed "
          "hosts")
    from_cfg = serve_dashboard.guard_from_config(cfg, "127.0.0.1")
    check(from_cfg.mode == file_mode
          and {"localhost", "127.0.0.1", "::1"} <= from_cfg.names
          and all(hn(h) in from_cfg.names for h in file_hosts or []),
          "the shipped config yields its mode, the loopback names and its "
          "hosts")
    check(SHIPPED_PROXY in from_cfg.names,
          "the shipped config allows the proxy name the Monday digest links "
          "to")
    # The LAN address of LXC 110 is listed, so a monitor on the LAN that
    # reaches the dashboard by IP passes the Host check on a GET and on a
    # POST, while a neighbouring address that is not listed is still refused.
    LAN_IP = "192.168.1.100"
    lan_post = {"Host": f"{LAN_IP}:8787", "Origin": f"http://{LAN_IP}:8787",
                HEADER: "1"}
    check(LAN_IP in (file_hosts or []) and LAN_IP in from_cfg.names
          and from_cfg.problems("GET", "/", Headers(
              {"Host": f"{LAN_IP}:8787"})) == []
          and from_cfg.problems("POST", "/ack", Headers(lan_post)) == [],
          f"the shipped config lists {LAN_IP}, so Host {LAN_IP}:8787 passes "
          "the Host check on a GET and on a POST")
    unlisted = "192.168.1.101"
    check(unlisted not in from_cfg.names
          and len(from_cfg.problems("GET", "/", Headers(
              {"Host": f"{unlisted}:8787"}))) == 1
          and len(from_cfg.problems("POST", "/ack", Headers(
              {**lan_post, "Host": f"{unlisted}:8787"}))) == 1,
          f"an unlisted address {unlisted}:8787 is still refused on a GET and "
          "on a POST")
    with captured_stderr() as buf:
        odd = serve_dashboard.guard_from_config({"dashboard_guard": "warning"},
                                                None)
        off = serve_dashboard.guard_from_config({"dashboard_guard": False},
                                                None)
        missing = serve_dashboard.guard_from_config({}, None)
    check(odd.enforcing and off.enforcing and missing.enforcing
          and "neither warn nor enforce" in buf.getvalue(),
          "a mode that is not warn or enforce, or absent, enforces and says so")
    check(serve_dashboard.guard_from_config({"dashboard_guard": " Warn "},
                                            None).mode == "warn",
          "the mode ignores case and spaces")
    bound = serve_dashboard.guard_from_config(
        {"dashboard_guard": "warn",
         "dashboard_allowed_hosts": "a.example, b.example"}, "192.168.1.5")
    check({"192.168.1.5", "a.example", "b.example"} <= bound.names,
          "the bind address and a comma separated host string are allowed")
    wild = serve_dashboard.guard_from_config({"dashboard_guard": "warn"},
                                             "0.0.0.0")
    check("0.0.0.0" not in wild.names and "" not in wild.names,
          "a wildcard bind address is never itself an allowed name")
    with captured_stderr() as buf:
        scalar = serve_dashboard.guard_from_config(
            {"dashboard_guard": "warn", "dashboard_allowed_hosts": 5}, None)
        mixed = serve_dashboard.guard_from_config(
            {"dashboard_guard": "warn",
             "dashboard_allowed_hosts": ["fine.example", "bad/entry", 7]},
            None)
    check(scalar.names == frozenset(serve_dashboard.LOOPBACK_NAMES)
          and "fine.example" in mixed.names and "bad/entry" not in mixed.names
          and buf.getvalue().count("so it is ignored") == 3,
          "a host list of the wrong type or with a bad entry is ignored with "
          "a message, and never stops the server starting")

    # ----- the route list the matrix covers is the one the server defines
    defined = post_routes_in_source()
    covered = {r[0] for r in ROUTES}
    check(defined == covered,
          "this runner covers exactly the POST routes the server defines "
          f"(only in server {sorted(defined - covered)}, "
          f"only here {sorted(covered - defined)})")
    methods = handler_methods()
    verbs = sorted(n for n in methods if n.startswith("do_"))
    check(verbs == ["do_GET", "do_POST"]
          and admits_first(methods["do_GET"], "GET")
          and admits_first(methods["do_POST"], "POST"),
          "the handler has only do_GET and do_POST, and each starts by "
          f"asking the guard (verbs {verbs})")

    names = ("localhost", "127.0.0.1", PROXY_NAME)

    # ----- enforce mode, every route
    with stubbed():
        with serving(serve_dashboard.WriteGuard("enforce", names)) as port:
            run_matrix(port, "enforce")
            own = f"127.0.0.1:{port}"

            del CALLS[:]
            with captured_stderr():
                status, _h, _d = call(port, "POST", "/no-such-route", b"{}",
                                      {"Host": own, "Origin": f"https://{EVIL}"})
            check(status == 403 and not CALLS,
                  "in enforce mode a forged POST to a route that does not "
                  "exist is refused too, so the guard comes before the router")
            # No body, because the router answers an unknown route without
            # reading one, and closing on unread data can reset the connection.
            status, _h, _d = call(port, "POST", "/no-such-route", b"",
                                  {"Host": own, HEADER: "1"})
            check(status == 404,
                  "in enforce mode a good POST to a route that does not exist "
                  "is a 404")

            with captured_stderr() as buf:
                status, _h, data = call(port, "GET", "/no-such-page", b"",
                                        {"Host": EVIL})
            check(status == 403 and b"write guard" in data
                  and any(ln.startswith("write guard refused GET '/no-such")
                          for ln in guard_lines(buf)),
                  "in enforce mode a GET with a foreign Host is refused and "
                  "logged")
            status, _h, _d = call(port, "GET", "/no-such-page", b"",
                                  {"Host": own})
            check(status == 404,
                  "in enforce mode a GET with this server's own name reaches "
                  "the router")
            status, _h, _d = call(port, "GET", "/no-such-page", b"",
                                  {"Host": PROXY_NAME})
            check(status == 404,
                  "in enforce mode a GET by the proxy's name reaches the "
                  "router")
            status, _h, data = call(port, "GET", "/health", b"", {"Host": EVIL})
            check(status in (200, 503) and b'"status"' in data,
                  "in enforce mode GET /health answers whatever name the "
                  "monitor uses")

            status, got, _d = call(
                port, "OPTIONS", "/ack", b"",
                {"Host": own, "Origin": f"https://{EVIL}",
                 "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": HEADER.lower()})
            check(not 200 <= status < 300 and not has_cors(got),
                  "a browser preflight is never answered with permission "
                  f"(got {status})")

            replies = {
                "a page": call(port, "GET", "/health", b"", {"Host": own}),
                "a missing page": call(port, "GET", "/no-such-page", b"",
                                       {"Host": own}),
                "a good POST": call(port, "POST", "/ack", b'{"id": 1}',
                                    {"Host": own, HEADER: "1"}),
                "the answer to OPTIONS": call(port, "OPTIONS", "/ack", b"",
                                              {"Host": own}),
            }
            with captured_stderr():
                replies["a refused GET"] = call(port, "GET", "/jobs", b"",
                                                {"Host": EVIL})
            check(not [k for k, r in replies.items() if has_cors(r[1])],
                  "no reply carries an Access-Control header, so no other "
                  "site can read what the dashboard sends")
            check(all(forbids_framing(r[1]) for r in replies.values()),
                  "in enforce mode every reply forbids framing, the error "
                  "pages included")

            del CALLS[:]
            with captured_stderr() as buf:
                reply = raw_post(port, [
                    f"Host: {own}", f"Host: {EVIL}", f"{HEADER}: 1",
                    "Content-Type: application/json"], b'{"id":1}')
            check(reply.startswith(b"HTTP/1.0 403") and not CALLS
                  and any("more than one Host header" in ln
                          for ln in guard_lines(buf)),
                  "in enforce mode two Host headers are refused, the second "
                  "cannot smuggle past the first")
            del CALLS[:]
            with captured_stderr() as buf:
                reply = raw_post(port, [
                    f"Host: {own}", f"Origin: http://{own}",
                    f"Origin: https://{EVIL}", f"{HEADER}: 1",
                    "Content-Type: application/json"], b'{"id":1}')
            check(reply.startswith(b"HTTP/1.0 403") and not CALLS
                  and any("more than one Origin header" in ln
                          for ln in guard_lines(buf)),
                  "in enforce mode two Origin headers are refused, the second "
                  "cannot smuggle past the first")

            with captured_stderr():
                reply = raw_post(port, [
                    f"Host: {own}", f"Origin: https://{EVIL}", f"{HEADER}: 1",
                    "Content-Type: application/json"],
                    b"x" * 120_000, piece=6000, pause=0.004)
            check(reply.startswith(b"HTTP/1.0 403"),
                  "a refused POST whose body arrives slowly still gets its "
                  f"403, so the page shows the refusal (got {reply[:40]!r})")

            with captured_stderr():
                _s, _h, data = call(port, "POST", "/ack", b'{"id": 1}',
                                    {"Host": own, "Origin": f"https://{EVIL}",
                                     HEADER: "1"})
                _s2, _h2, data2 = call(port, "POST", "/ack", b'{"id": 1}',
                                       {"Host": own})
            said = json.loads(data).get("note", "")
            said2 = json.loads(data2).get("note", "")
            check("dashboard_allowed_hosts" in said and "restart" in said
                  and "reload the page" not in said,
                  "a refused name tells Luke to list it and restart, not to "
                  "reload")
            check("reload the page" in said2
                  and "dashboard_allowed_hosts" not in said2,
                  "a missing header tells Luke to reload, not to edit the "
                  "config")
            with captured_stderr():
                _s3, _h3, data3 = call(port, "POST", "/ack", b'{"id": 1}',
                                       {"Host": own, "Origin": "null",
                                        HEADER: "1"})
            said3 = json.loads(data3).get("note", "")
            check("is null" in said3 and "dashboard_allowed_hosts" not in said3
                  and "reload the page" not in said3,
                  "a null Origin gets no advice, because no setting fixes it")

    # ----- warn mode, every route
    with stubbed():
        with serving(serve_dashboard.WriteGuard("warn", names)) as port:
            run_matrix(port, "warn")
            own = f"127.0.0.1:{port}"
            with captured_stderr() as buf:
                status, got, _d = call(port, "GET", "/no-such-page", b"",
                                       {"Host": EVIL})
                status2, _h, _d2 = call(
                    port, "POST", "/ack", b'{"id": 1}',
                    {"Host": own, "Origin": f"https://{EVIL}",
                     "Content-Type": "application/json"})
            lines = guard_lines(buf)
            check(status == 404 and "x-frame-options" not in got
                  and any(ln.startswith("write guard would refuse GET "
                                        "'/no-such-page'")
                          and "warn only" in ln and EVIL in ln
                          for ln in lines),
                  "in warn mode a GET with a foreign Host reaches the router "
                  "and is logged with its route and Host")
            check(status2 == 200
                  and any(ln.startswith("write guard would refuse POST '/ack'")
                          and f"Origin 'https://{EVIL}'" in ln
                          and f"The {HEADER} header is missing" in ln
                          and "from 127.0.0.1" in ln for ln in lines),
                  "in warn mode a forged POST is logged with its route, its "
                  "Origin, every reason and where it came from")

        # A fresh guard, because each guard remembers the sightings it logged.
        with serving(serve_dashboard.WriteGuard("warn", names)) as port:
            own = f"127.0.0.1:{port}"
            with captured_stderr() as buf:
                for _ in range(3):
                    call(port, "POST", "/ack", b'{"id": 1}',
                         {"Host": own, "Origin": f"http://{own}", HEADER: "1",
                          "Content-Type": "application/json"})
                    call(port, "GET", "/no-such-page", b"",
                         {"Host": own, "Origin": f"http://{EVIL}"})
                    call(port, "GET", "/no-such-page", b"", {"Host": own})
            sights = [ln for ln in guard_lines(buf) if " sees " in ln]
            check(len(sights) == 2
                  and any("sees POST" in ln and f"with {HEADER}" in ln
                          and f"Origin 'http://{own}'" in ln
                          and f"Host '{own}'" in ln for ln in sights)
                  and any("sees GET" in ln and f"Host '{own}'" in ln
                          for ln in sights),
                  "in warn mode each distinct way a good request arrives is "
                  "logged once, which is how Luke reads his own Host and "
                  f"Origin (got {len(sights)} lines)")
            with captured_stderr() as buf:
                call(port, "POST", "/ack", b'{"id": 1}',
                     {"Host": own, "Content-Type": "application/json",
                      HEADER: "1"})
            check(any("no Origin header" in ln for ln in guard_lines(buf)),
                  "a POST with no Origin says so, not that its Origin is None")

    # ----- an exception in a route is a clear 500, not a dropped connection
    with stubbed(skip=("set_acknowledged",)):
        saved_ack = serve_dashboard.set_acknowledged

        def boom(*_a, **_k):
            raise RuntimeError("secret internal detail")
        serve_dashboard.set_acknowledged = boom
        try:
            with serving(serve_dashboard.WriteGuard("enforce", names)) as port:
                own = f"127.0.0.1:{port}"
                with captured_stderr() as buf:
                    status, _h, data = call(
                        port, "POST", "/ack", b'{"id": 1}',
                        {"Host": own, HEADER: "1",
                         "Content-Type": "application/json"})
                body = json.loads(data) if data else {}
                logged = buf.getvalue()
                check(status == 500 and body.get("ok") is False
                      and "Server error (RuntimeError)" in body.get("note", ""),
                      "an exception in a route is a 500 with a clear note "
                      f"(got {status} {body})")
                check("secret internal detail" not in data.decode("utf-8")
                      and "secret internal detail" in logged
                      and "Traceback" in logged,
                      "the page never sees the exception text, the log has "
                      "the whole traceback")
                status, _h, _d = call(port, "GET", "/no-such-page", b"",
                                      {"Host": own})
                check(status == 404,
                      "the server is still answering after the exception")
                with captured_stderr():
                    status, _h, data = call(
                        port, "POST", "http://[::1/ack", b"",
                        {"Host": own, HEADER: "1"})
                check(status == 500 and json.loads(data or b"{}").get("ok")
                      is False,
                      "a request line the router cannot parse is a 500 with a "
                      f"note, not a dropped connection (got {status})")

                def reset(*_a, **_k):
                    raise ConnectionResetError("route code lost a peer")
                serve_dashboard.set_acknowledged = reset
                with captured_stderr() as buf:
                    raw_post(port, [f"Host: {own}", f"{HEADER}: 1",
                                    "Content-Type: application/json"],
                             b'{"id": 1}')
                check("ended early with ConnectionResetError" in buf.getvalue(),
                      "a connection error raised by route code leaves a line "
                      "in the log, not silence")
        finally:
            serve_dashboard.set_acknowledged = saved_ack

    # ----- the real route code behind the guard, on a throwaway database
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "guard.sqlite"
        conn = radar_common.get_db(db)
        now = radar_common.now_iso()
        for h, company in (("g1", "Wired Ltd"), ("g2", "Forged Ltd"),
                           ("g3", "Warned Ltd")):
            conn.execute(
                "INSERT INTO opportunities (url_hash, first_seen, company,"
                " title, thread_type, status) VALUES (?,?,?,?,?,?)",
                (h, now, company, "Quality Lead", "inbound", "new"))
        cid = radar_common.resolve_company(conn, "Wired Diagnostics")
        conn.commit()
        ids = {r["company"]: r["id"] for r in
               conn.execute("SELECT id, company FROM opportunities")}
        conn.close()

        previous_args = serve_dashboard.ARGS
        serve_dashboard.ARGS = argparse.Namespace(
            db=str(db), push=False, host="127.0.0.1", port=0)

        def acked(item_id):
            r = sqlite3.connect(db)
            try:
                return r.execute("SELECT acknowledged_at FROM opportunities"
                                 " WHERE id = ?", (item_id,)).fetchone()[0]
            finally:
                r.close()

        try:
            with serving(serve_dashboard.WriteGuard("enforce", names)) as port:
                own = {"Host": f"127.0.0.1:{port}",
                       "Content-Type": "application/json"}
                status, _h, _d = call(
                    port, "POST", "/ack",
                    json.dumps({"id": ids["Wired Ltd"]}).encode(),
                    {**own, "Origin": f"http://127.0.0.1:{port}", HEADER: "1"})
                check(status == 200 and acked(ids["Wired Ltd"]),
                      "in enforce mode the page's own POST really "
                      "acknowledges a row")
                with captured_stderr():
                    status, _h, _d = call(
                        port, "POST", "/ack",
                        json.dumps({"id": ids["Forged Ltd"]}).encode(),
                        {**own, "Origin": f"https://{EVIL}",
                         "Content-Type": "text/plain;charset=UTF-8"})
                check(status == 403 and not acked(ids["Forged Ltd"]),
                      "in enforce mode a forged POST changes no row")

            with serving(serve_dashboard.WriteGuard("warn", names)) as port:
                with captured_stderr() as buf:
                    status, _h, _d = call(
                        port, "POST", "/ack",
                        json.dumps({"id": ids["Warned Ltd"]}).encode(),
                        {"Host": f"127.0.0.1:{port}",
                         "Origin": f"https://{EVIL}",
                         "Content-Type": "text/plain;charset=UTF-8"})
                check(status == 200 and acked(ids["Warned Ltd"])
                      and any("would refuse" in ln for ln in guard_lines(buf)),
                      "in warn mode the same forged POST goes through, and is "
                      "logged")

            # ----- every page's script wraps fetch before it uses it
            saved_profile = cv_store.PROFILE_DIR
            cv_store.PROFILE_DIR = Path(tmp) / "profile"
            try:
                pages = {name: serve_dashboard.render(name).decode("utf-8")
                         for name in ("home", "jobs", "insights", "archive")}
                pages["company"] = serve_dashboard.render_company(
                    cid).decode("utf-8")
                pages["cv"] = serve_dashboard.render_cv_page().decode("utf-8")
            finally:
                cv_store.PROFILE_DIR = saved_profile
            for name, html in pages.items():
                # Comments are dropped first, so prose that mentions fetch
                # cannot stand in for a call.
                js = re.sub(r"(?m)^\s*//.*$", "", script_text(html))
                wrap = js.find("window.fetch = function")
                first = re.search(r"(?<![A-Za-z_.])fetch\(", js)
                check(wrap >= 0 and HEADER in js
                      and first is not None and wrap < first.start(),
                      f"the {name} page wraps fetch to add {HEADER} before "
                      "any of its own scripts use it")
                glued = re.findall(r"\w'\s*\+\s*'\w", js)
                check(not glued,
                      f"the {name} page script never runs two words together "
                      f"where a message is split over lines (found {glued[:2]})")
            node_checks(pages, Path(tmp))
        finally:
            serve_dashboard.ARGS = previous_args

    check("__WRITE_HEADER__" not in build_dashboard.SCRIPT
          and build_dashboard.WRITE_HEADER == HEADER,
          "the page script and the server share one header name")

    # ----- main() puts the config into the handler
    made = []

    class FakeServer:
        def __init__(self, address, handler):
            made.append((address, handler))

        def serve_forever(self):
            return None

    saved = (serve_dashboard.RadarServer, serve_dashboard.GUARD,
             serve_dashboard.ARGS)
    serve_dashboard.RadarServer = FakeServer
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = serve_dashboard.main(
                    ["--db", str(Path(tmp) / "wire.sqlite"),
                     "--host", "192.0.2.7", "--port", "8799"])
            installed = serve_dashboard.GUARD
    finally:
        serve_dashboard.RadarServer = saved[0]
        serve_dashboard.GUARD = saved[1]
        serve_dashboard.ARGS = saved[2]
    os.environ["RADAR_MOCK"] = "1"
    check(code == 0 and made == [(("192.0.2.7", 8799), serve_dashboard.Handler)]
          and installed.mode == file_mode and installed is not DEFAULT_GUARD
          and {"192.0.2.7", SHIPPED_PROXY, "localhost"} <= installed.names
          and f"Write guard is on {file_mode}" in out.getvalue(),
          "main() builds the guard from radar.yaml and the bind address, "
          "hands it to the handler and says which mode it is in")

    # ----- the anthropic pin
    # radar_common.claude_call sends temperature=0. The Python SDK removed
    # that keyword at 1.0.0, so on any 1.x or 2.x release the call raises
    # TypeError before a request is made. The premise was checked against the
    # 33 published wheels from 0.120.2 to 2.7.0. Messages.create takes the
    # keyword in every one of 0.120.2 to 0.125.0 and in none of 1.0.0 to
    # 2.7.0. The pin keeps every install below that, and these checks keep
    # the pin, the docs and the interpreter that runs this suite honest.
    check(satisfies("0.120.2", ">=0.120.2,<0.121")
          and not satisfies("0.121.0", ">=0.120.2,<0.121")
          and satisfies("0.120.9", "~=0.120.2")
          and not satisfies("0.121.0", "~=0.120.2")
          and satisfies("0.125.0", "<1") and not satisfies("1.0.0", "<1")
          and not satisfies("1.0.0", "") and not satisfies("1.0.0", ">=banana"),
          "the specifier reader these checks use agrees with pip on the "
          "cases that matter")
    reqs = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    found = re.search(r"^\s*anthropic\s*([^#\n]*)", reqs, re.M | re.I)
    spec = found.group(1).strip() if found else ""
    sends = sends_temperature()
    check(sends,
          "claude_call still sends a temperature keyword, which is why the "
          "pin exists (if it stopped, this check and the pin can go)")
    check(bool(spec) and satisfies("0.120.2", spec),
          "requirements.txt keeps anthropic at the release the dashboard "
          f"container runs, 0.120.2 (line is {spec!r})")
    check(not sends or not any(
        satisfies(v, spec) for v in ("1.0.0", "1.17.0", "2.0.0", "2.5.0",
                                     "2.7.0", "2.8.0")),
        "while claude_call sends temperature, requirements.txt excludes every "
        "release from 1.0.0, where the keyword is a TypeError")
    docs = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    check(f"anthropic{spec.replace(' ', '')}" in docs,
          "the Dockerfile snippet in docs/architecture.md carries the same "
          "anthropic range as requirements.txt")
    try:
        import inspect
        import anthropic
        from anthropic.resources.messages import Messages
        installed_sdk = anthropic.__version__
        takes = "temperature" in inspect.signature(Messages.create).parameters
    except ImportError as err:
        note(f"anthropic is not importable here ({err}), so the check of the "
             "installed SDK was skipped", skip=True)
    else:
        verdict = takes or not sends
        check(verdict,
              f"the anthropic installed in this interpreter ({installed_sdk}) "
              "takes the temperature keyword that claude_call sends"
              + ("" if verdict else
                 ". Run pip install -r requirements.txt in this environment"))
        if spec and not satisfies(installed_sdk, spec):
            note(f"anthropic {installed_sdk} is installed here, outside the "
                 f"pinned {spec}. It takes the keyword, so this passes, but "
                 "it is not what the box will run")

    print()
    extra = f" ({len(skipped)} skipped, see the NOTE lines)" if skipped else ""
    if failures:
        print(f"{len(failures)} HARDENING CHECK(S) FAILED")
        return 1
    print("ALL HARDENING CHECKS PASSED" + extra)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # noqa: BLE001  readable FAIL, never a traceback
        print(f"\nFAIL. Unexpected {type(err).__name__}. {err}")
        sys.exit(1)
