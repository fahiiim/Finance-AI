"""
Client-demo chat UI for the Financial AI service.

Runs as a SEPARATE process on its own port and proxies to the real API. No
demo code, no token-minting endpoint, and no extra route ever ships inside
`app/` — the service under demonstration is exactly the service you deploy.

It mints a service token per request, the same way the Laravel backend will
in production (docs/handover-backend.md §3), so the recording shows the real
authentication path rather than a bypass.

    python -m uvicorn app.main:app --reload    # terminal 1: the real service
    python scripts/demo_ui.py                  # terminal 2: this UI
    # then open http://127.0.0.1:8500

Switching the user in the UI mints a token for a different `sub`, which is
what makes cross-user isolation visible on camera: the same question returns
entirely different finances, and neither user can see the other's history.
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt  # noqa: E402

from app.core.config import settings  # noqa: E402

API = "http://127.0.0.1:8000"

# Users offered in the picker when there is no real database to read -- the
# mock backend invents data for any id, so these are arbitrary.
MOCK_USERS = [("42", "User 42"), ("99", "User 99"), ("7", "User 7"), ("1001", "User 1001"), ("2048", "User 2048")]

# How many transactions a user needs before they are worth demonstrating. A
# user with four rows makes the assistant look like it has nothing to say.
MIN_TRANSACTIONS = 50

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

GENERIC_SAMPLES = [
    "What is my checking account balance?",
    "How much did I spend last month?",
    "How much did I spend on groceries last month?",
    "What subscriptions am I paying for?",
    "Compare my spending this month to last month",
    "Can I afford to spend $500 this week?",
    "What is my financial score?",
    "How is my overall financial health?",
]


def _user_profiles() -> dict[str, dict]:
    """
    What each user actually has, so the demo can adapt to it.

    A user whose transactions stop in June is not a broken user -- ask them
    about June and the assistant answers perfectly. What breaks a demo is
    asking every user about "last month" when only six of them have data for
    it. So the picker records each person's real data window and the page
    builds its suggested questions from that.
    """
    path = Path(settings.demo_db_path)
    if settings.backend_integration_mode.value != "sqlite" or not path.exists():
        return {}

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT t.user_id,
                   u.Name,
                   COUNT(*)                    AS txns,
                   MIN(t.transaction_date)     AS first_txn,
                   MAX(t.transaction_date)     AS last_txn,
                   (SELECT COUNT(*) FROM plaid_accounts a
                     WHERE a.user_id = t.user_id)                       AS accounts,
                   (SELECT COUNT(*) FROM plaid_recurring_streams s
                     WHERE s.user_id = t.user_id AND s.active = 1)      AS recurring,
                   (SELECT COUNT(*) FROM credit_scores k
                     WHERE k.user_id = t.user_id AND k.score IS NOT NULL) AS scores
            FROM plaid_transactions t
            LEFT JOIN application_users u ON u.id = t.user_id
            WHERE t.is_deleted = 0 AND t.is_review_transfer = 0
            GROUP BY t.user_id
            HAVING txns >= ?
            ORDER BY last_txn DESC, txns DESC
            """,
            (MIN_TRANSACTIONS,),
        ).fetchall()
    finally:
        conn.close()

    profiles: dict[str, dict] = {}
    for user_id, name, txns, first_txn, last_txn, accounts, recurring, scores in rows:
        profiles[str(user_id)] = {
            "name": (name or "").strip(),
            "txns": txns,
            "first": first_txn,
            "last": last_txn,
            "accounts": accounts,
            "recurring": recurring,
            "has_score": bool(scores),
        }
    return profiles


def _month_phrase(iso_date: str, offset: int = 0) -> str:
    """"2026-06-22" -> "June 2026"; offset=-1 gives the month before."""
    year, month = int(iso_date[:4]), int(iso_date[5:7])
    month += offset
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return f"{MONTH_NAMES[month - 1]} {year}"


def _samples_for(profile: dict | None) -> list[str]:
    """
    Questions this particular user can actually answer.

    The generic list asks everyone about "last month". For a user whose data
    ended in June that returns $0.00 -- correct, and it looks broken. Naming
    their real months turns the same user into a good demo.
    """
    if not profile:
        return GENERIC_SAMPLES

    last = profile["last"]
    recent = _month_phrase(last)
    previous = _month_phrase(last, -1)

    samples = [
        "What is my checking account balance?",
        f"How much did I spend in {recent}?",
        f"What were my top spending categories in {recent}?",
        f"Compare my spending in {recent} to {previous}",
        f"How much did I spend on food and drink in {recent}?",
    ]
    if profile["recurring"]:
        samples.append("What subscriptions am I paying for?")
    if profile["has_score"]:
        samples.append("What is my financial score?")
    samples.append("Can I afford to spend $500 this week?")
    samples.append("How is my overall financial health?")
    return samples


def _demo_users() -> list[tuple[str, str]]:
    """
    The people in the picker, best demo first.

    Ranked, not filtered. A user whose data stopped in June still demos well
    once the questions name June -- and hiding two thirds of the list to
    guarantee that would misrepresent how much data there is. The label says
    what each one has so nothing is a surprise on camera.
    """
    profiles = _user_profiles()
    if not profiles:
        return MOCK_USERS

    latest = max(p["last"] for p in profiles.values())
    current_month = latest[:7]

    def rank(item: tuple[str, dict]) -> tuple:
        _, p = item
        # Complete first: data in the newest month everyone is measured
        # against, then a credit score, then recurring streams, then volume.
        return (
            p["last"][:7] == current_month,
            p["has_score"],
            bool(p["recurring"]),
            p["txns"],
        )

    users: list[tuple[str, str]] = []
    for user_id, p in sorted(profiles.items(), key=rank, reverse=True):
        label = f"User {user_id}"
        if p["name"] and p["name"].lower() != "test":
            label += f" - {p['name']}"

        # Say what is there. "to 2026-06-22" alone did not stop anyone asking
        # that user about last month and getting $0.00.
        marks = []
        if p["last"][:7] == current_month:
            marks.append("current")
        if p["has_score"]:
            marks.append("score")
        extra = " +".join(marks)
        window = f"{_month_phrase(p['first'])} to {_month_phrase(p['last'])}"
        suffix = f"({p['txns']:,} txns, {window}"
        suffix += f", {extra})" if extra else ")"
        users.append((user_id, f"{label}  {suffix}"))
    return users or MOCK_USERS


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Financial AI - Demo</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0e141a;--panel:#151d26;--line:#26313d;--ink:#e6ecf2;--dim:#9fafc0;
--accent:#7bb0e4;--user:#1d4e89;--ok:#72c6ac;--warn:#e3b368}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 "Segoe UI",system-ui,sans-serif;height:100vh;display:flex;flex-direction:column}
header{padding:14px 22px;border-bottom:1px solid var(--line);background:var(--panel);display:flex;align-items:center;gap:22px;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600;letter-spacing:-.01em}
.who{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--dim)}
select,input,button{font:inherit;background:#0e141a;color:var(--ink);border:1px solid var(--line);border-radius:7px;padding:8px 11px}
button{background:var(--accent);color:#0e141a;font-weight:600;border:none;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
main{flex:1;display:grid;grid-template-columns:1fr 300px;overflow:hidden}
#chat{overflow-y:auto;padding:22px}
.msg{max-width:70ch;margin:0 0 16px;padding:11px 15px;border-radius:11px;white-space:pre-wrap}
.me{background:var(--user);margin-left:auto}
.ai{background:var(--panel);border:1px solid var(--line)}
.err{background:#331a18;border:1px solid #f09a92;color:#f09a92}
.meta{font-size:11.5px;color:var(--dim);margin:-10px 0 16px;font-family:ui-monospace,Consolas,monospace}
aside{border-left:1px solid var(--line);background:var(--panel);padding:18px;overflow-y:auto;font-size:13px}
aside h2{font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--dim);margin:0 0 10px;font-family:ui-monospace,monospace}
.chip{display:block;width:100%;text-align:left;background:#0e141a;color:var(--ink);border:1px solid var(--line);
border-radius:7px;padding:8px 10px;margin-bottom:7px;font-size:12.5px;cursor:pointer;font-weight:400}
.chip:hover{border-color:var(--accent)}
footer{padding:14px 22px;border-top:1px solid var(--line);background:var(--panel);display:flex;gap:10px}
#q{flex:1}
.kv{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid var(--line);font-family:ui-monospace,monospace;font-size:11.5px}
.kv span:last-child{color:var(--ok)}
</style></head><body>
<header>
  <h1>Financial AI Assistant</h1>
  <div class="who">Signed in as
    <select id="user">__USER_OPTIONS__</select>
  </div>
  <div class="who">Session <input id="sess" value="demo-1" size="10"></div>
  <div class="who" title="The API this page is talking to">API <span style="color:var(--ok)">__API_URL__</span></div>
  <button id="reset" style="background:none;border:1px solid var(--line);color:var(--dim);font-weight:400">New conversation</button>
</header>
<main>
  <div id="chat"></div>
  <aside>
    <h2>Try asking</h2>
    <div id="chips"></div>
    <h2 style="margin-top:22px">Last response</h2>
    <div id="detail" style="color:var(--dim)">-</div>
  </aside>
</main>
<footer>
  <input id="q" placeholder="Ask about your finances..." autocomplete="off">
  <button id="send">Send</button>
</footer>
<script>
// Suggested questions per user, keyed by id. Each user's data covers a
// different span, so the questions name months that person actually has.
const SAMPLES_BY_USER = __SAMPLES__;
const GENERIC_SAMPLES = __GENERIC_SAMPLES__;
const chat=document.getElementById('chat'),q=document.getElementById('q'),
send=document.getElementById('send'),detail=document.getElementById('detail'),
userSel=document.getElementById('user'),sess=document.getElementById('sess');

function renderChips(){
  const list = SAMPLES_BY_USER[userSel.value] || GENERIC_SAMPLES;
  document.getElementById('chips').innerHTML =
    list.map(s=>`<button class="chip">${s}</button>`).join('');
  document.querySelectorAll('.chip').forEach(b=>
    b.onclick=()=>{q.value=b.textContent;ask();});
}
renderChips();

document.getElementById('reset').onclick=()=>{
  chat.innerHTML='';detail.textContent='-';
  sess.value='demo-'+Math.floor(Math.random()*10000);
};
userSel.onchange=()=>{chat.innerHTML='';detail.textContent='-';renderChips();};

function bubble(cls,text){
  const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;
  chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d;
}
async function ask(){
  const text=q.value.trim();if(!text)return;
  q.value='';send.disabled=true;
  bubble('me',text);
  const thinking=bubble('ai','...');
  try{
    const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user:userSel.value,session:sess.value,message:text})});
    const j=await r.json();
    if(j.error){
      thinking.className='msg err';
      thinking.textContent=j.error+(j.hint?'\\n\\n'+j.hint:'');
      detail.textContent='-';
    }else{
      thinking.textContent=j.message.content;
      const m=j.metadata;
      const line=document.createElement('div');line.className='meta';
      line.textContent='intent: '+m.intent+'  |  tools: '+((m.tools_used||[]).join(', ')||'none');
      chat.appendChild(line);chat.scrollTop=chat.scrollHeight;
      detail.innerHTML=
        kv('intent',m.intent)+kv('data_used',m.data_used)+
        kv('calculation_used',m.calculation_used)+
        kv('needs_clarification',m.needs_clarification)+
        kv('tools',(m.tools_used||[]).length);
    }
  }catch(e){thinking.className='msg err';thinking.textContent=String(e);}
  send.disabled=false;q.focus();
}
function kv(k,v){return `<div class="kv"><span>${k}</span><span>${v}</span></div>`;}
send.onclick=ask;
q.addEventListener('keydown',e=>{if(e.key==='Enter')ask();});
q.focus();
</script></body></html>"""


def mint(user: str, session: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user,
            "session_id": session,
            "timezone": "America/New_York",
            "currency": "USD",
            "iat": now,
            "exp": now + timedelta(seconds=300),
        },
        settings.service_jwt_secret,
        algorithm="HS256",
    )


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """
    A server that refuses to share its port.

    Python sets SO_REUSEADDR by default, which on Windows lets a SECOND
    process bind a port that is already being listened on -- no error, both
    sockets live, and incoming connections go to one of them unpredictably.
    That is how two demo UIs ended up on port 8500 pointing at two different
    APIs, and why refreshing the page changed the numbers. Turning it off
    makes the second start fail loudly, which is the only useful outcome.
    """

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):  # keep the demo console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            # Name the API in the page itself. Two UI processes once ran on
            # this port at the same time -- Windows lets a second bind succeed
            # -- each proxying to a different API, so refreshing the browser
            # silently switched datasets and one question answered with two
            # different totals. Now the answer is readable off the screen.
            profiles = _user_profiles()
            options = "".join(
                f'<option value="{value}">{label}</option>' for value, label in _demo_users()
            )
            samples = {user: _samples_for(profile) for user, profile in profiles.items()}
            page = (
                PAGE.replace("__API_URL__", API)
                .replace("__USER_OPTIONS__", options)
                .replace("__SAMPLES__", json.dumps(samples))
                .replace("__GENERIC_SAMPLES__", json.dumps(GENERIC_SAMPLES))
            )
            self._send(200, page.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if self.path != "/ask":
            self._send(404, b"not found", "text/plain")
            return

        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = str(payload.get("user", "42"))
        session = str(payload.get("session", "demo-1"))
        token = mint(user, session)

        req = urllib.request.Request(
            f"{API}/api/v1/chat",
            data=json.dumps({"session_id": session, "message": payload["message"]}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-Session-Id", session)

        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                self._send(200, r.read(), "application/json")
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                err = json.loads(raw)
            except json.JSONDecodeError:
                err = {"message": raw}
            hint = ""
            if err.get("code") in ("llm_provider_error", "llm_timeout"):
                hint = "Set OPENAI_API_KEY in .env and restart the service."
            out = {"error": f"{e.code} {err.get('code', '')}: {err.get('message', '')}", "hint": hint}
            self._send(200, json.dumps(out).encode(), "application/json")
        except urllib.error.URLError as e:
            out = {
                "error": f"Cannot reach the API at {API} ({e.reason}).",
                "hint": "Start it with:  python -m uvicorn app.main:app --reload",
            }
            self._send(200, json.dumps(out).encode(), "application/json")


def _exclusive_server(port: int) -> _ExclusiveHTTPServer:
    return _ExclusiveHTTPServer(("127.0.0.1", port), Handler)


def main() -> int:
    global API  # must precede any read of API in this scope

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8500)
    ap.add_argument("--api", default=API)
    args = ap.parse_args()
    API = args.api

    if not settings.service_jwt_secret:
        print("SERVICE_JWT_SECRET is empty - configure .env first.", file=sys.stderr)
        return 1

    try:
        server = _exclusive_server(args.port)
    except OSError:
        print(
            f"Port {args.port} is already in use - another demo UI is running. "
            "Stop it first, or start this one on a different port with --port. "
            "Two UIs on one port is not a harmless duplicate: each may point at "
            "a different API, and the browser reaches whichever answers first.",
            file=sys.stderr,
        )
        return 1

    print(f"\n  Demo UI   http://127.0.0.1:{args.port}")
    print(f"  API       {API}")
    users = _demo_users()
    source = settings.backend_integration_mode.value
    detail = f" ({settings.demo_db_path})" if source == "sqlite" else ""
    print(f"  Data      {source}{detail}")
    print(f"  Users     {len(users)} in the picker")
    print("\n  Ctrl+C to stop.\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
