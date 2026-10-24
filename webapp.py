"""Local web interface for the triage agent.

Read-only by default. Proposals are an explicit checkbox. There is no approval here:
a human approves in a separate terminal.
"""
import argparse
import asyncio
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

import actions
import agent
import agent_core as core
import agent_ui as ui
import tool_bridge as tb

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
MAX_HISTORY = 3

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DevOps Triage</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header><h1>DevOps Triage</h1><p id="meta">Connecting...</p></header>
<main>
  <div>
    <section id="ask">
      <label for="repo">Repository (owner/name)</label>
      <input id="repo" type="text" autocomplete="off" spellcheck="false">
      <label for="request">Your request</label>
      <textarea id="request" rows="4" maxlength="2000"
        placeholder="Which 3 issues should we fix first? Check the evidence before ranking."></textarea>
      <label class="check"><input id="allow" type="checkbox">
        Allow proposals (the agent may draft a comment; a human still approves in a terminal)</label>
      <div class="buttons">
        <button id="send">Ask</button>
        <button id="clear" class="secondary">Clear conversation</button>
      </div>
      <p id="status" role="status"></p>
    </section>
    <section id="result" hidden>
      <h2>Answer</h2>
      <pre id="answer"></pre>
      <div id="warnings"></div>
      <div id="proposalbox"></div>
      <h2>Run summary</h2>
      <div id="summary"></div>
    </section>
  </div>
  <aside>
    <h2>Proposals</h2>
    <p class="hint">Approval happens in a terminal, never here.</p>
    <div id="proposals"></div>
    <div class="buttons"><button id="refresh" class="secondary">Refresh</button></div>
  </aside>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

STYLE_CSS = r""":root { color-scheme: light dark; --bg:#fff; --fg:#1b1b1b; --muted:#666; --line:#d0d0d0; --accent:#0b5fff; --warn:#a15c00; }
@media (prefers-color-scheme: dark) { :root { --bg:#111; --fg:#eee; --muted:#9a9a9a; --line:#333; --accent:#6aa0ff; --warn:#e5a14a; } }
body { margin:0; font:15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
header { padding:16px 24px; border-bottom:1px solid var(--line); }
h1 { margin:0; font-size:20px; }
h2 { font-size:16px; margin:20px 0 8px; }
h3 { font-size:14px; }
#meta, .hint, .label { color:var(--muted); margin:2px 0; }
main { display:grid; grid-template-columns:minmax(0,2fr) minmax(0,1fr); gap:24px; padding:24px; max-width:1100px; margin:0 auto; }
@media (max-width:800px) { main { grid-template-columns:1fr; } }
label { display:block; margin-top:12px; font-weight:600; }
.check { font-weight:400; }
input[type=text], textarea { width:100%; box-sizing:border-box; padding:8px; border:1px solid var(--line); border-radius:6px; background:transparent; color:inherit; font:inherit; }
button { padding:8px 16px; border:0; border-radius:6px; background:var(--accent); color:#fff; font:inherit; cursor:pointer; }
button.secondary { background:transparent; color:var(--accent); border:1px solid var(--accent); }
button:disabled { opacity:.5; cursor:wait; }
.buttons { display:flex; gap:8px; margin-top:12px; }
pre { white-space:pre-wrap; word-break:break-word; padding:12px; border:1px solid var(--line); border-radius:6px; }
code { display:block; padding:6px 8px; margin:4px 0; border:1px solid var(--line); border-radius:6px; overflow-x:auto; }
.warning { color:var(--warn); margin:6px 0; }
.row { display:flex; gap:12px; }
.row .label { min-width:90px; }
.item { padding:8px 0; border-bottom:1px solid var(--line); }
"""

APP_JS = r""""use strict";
(function () {
  var params = new URLSearchParams(location.hash.slice(1));
  var token = params.get("token") || "";
  history.replaceState(null, "", location.pathname);

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    if (cls) { e.className = cls; }
    return e;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  async function api(path, options) {
    options = options || {};
    var headers = Object.assign({"X-UI-Token": token}, options.headers || {});
    var response = await fetch(path, Object.assign({}, options, {headers: headers}));
    var data = null;
    try { data = await response.json(); } catch (e) { data = null; }
    if (!response.ok) {
      var message = data && (data.error || data.detail);
      throw new Error(typeof message === "string" ? message : "Request failed (" + response.status + ")");
    }
    return data;
  }

  function addRow(table, label, value) {
    var row = el("div", null, "row");
    row.appendChild(el("span", label, "label"));
    row.appendChild(el("span", value, "value"));
    table.appendChild(row);
  }

  function renderResult(r) {
    $("result").hidden = false;
    $("answer").textContent = r.answer || "(no answer)";

    var warnings = $("warnings");
    clear(warnings);
    if (r.injection_flags.length) {
      warnings.appendChild(el("p", "Injection-like text appeared in tool results: " + r.injection_flags.join(", "), "warning"));
    }
    r.unverified_claims.forEach(function (c) {
      warnings.appendChild(el("p", "The answer claims an action was done, but this agent cannot do that: " + c, "warning"));
    });
    if (r.ungrounded_refs.length) {
      warnings.appendChild(el("p", "The answer mentions issue(s) no tool showed: " +
        r.ungrounded_refs.map(function (n) { return "#" + n; }).join(", "), "warning"));
    }

    var box = $("proposalbox");
    clear(box);
    if (r.proposals.length) {
      box.appendChild(el("h3", "Waiting for your approval (nothing has been posted)"));
      r.proposals.forEach(function (p) { box.appendChild(el("code", p.command)); });
    }

    var summary = $("summary");
    clear(summary);
    addRow(summary, "Result", r.stop_text);
    if (r.error) { addRow(summary, "Error", r.error); }
    addRow(summary, "Steps", r.steps);
    addRow(summary, "Tokens", r.tokens);
    addRow(summary, "Time", r.seconds + "s (" + r.waited + "s waiting for rate limits)");
    r.tool_calls.forEach(function (t, i) {
      var note = t.flags.length ? "  flags: " + t.flags.join(",") : "";
      if (t.reason) { note += "  " + t.reason; }
      addRow(summary, i === 0 ? "Tools" : "", t.name + " " + t.status + " " + t.chars + " chars" + note);
    });
  }

  async function loadProposals() {
    var box = $("proposals");
    try {
      var data = await api("/api/proposals");
      clear(box);
      if (!data.proposals.length) { box.appendChild(el("p", "No proposals yet.", "hint")); return; }
      data.proposals.forEach(function (p) {
        var item = el("div", null, "item");
        item.appendChild(el("strong", p.id + "  " + p.status));
        item.appendChild(el("div", p.tool + " on " + p.repo + "#" + p.number, "hint"));
        if (p.status === "pending") { item.appendChild(el("div", "expires in " + p.expires_in + "s", "hint")); }
        if (p.has_warnings) { item.appendChild(el("div", "Has warnings. Read it carefully.", "warning")); }
        if (p.status === "pending") { item.appendChild(el("code", p.command)); }
        box.appendChild(item);
      });
    } catch (e) {
      clear(box);
      box.appendChild(el("p", e.message, "warning"));
    }
  }

  async function send() {
    var text = $("request").value.trim();
    if (!text) { $("status").textContent = "Type a request first."; return; }
    $("send").disabled = true;
    $("status").textContent = "Working... free-tier rate limits can add a minute or two.";
    try {
      var data = await api("/api/ask", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({request: text, repo: $("repo").value.trim() || null,
                              allow_proposals: $("allow").checked})
      });
      renderResult(data);
      $("status").textContent = "";
      loadProposals();
    } catch (e) {
      $("status").textContent = e.message;
    } finally {
      $("send").disabled = false;
    }
  }

  async function clearConversation() {
    try {
      await api("/api/clear", {method: "POST"});
      $("result").hidden = true;
      $("status").textContent = "Conversation cleared.";
    } catch (e) {
      $("status").textContent = e.message;
    }
  }

  async function init() {
    try {
      var cfg = await api("/api/config");
      $("meta").textContent = "Model: " + cfg.model + "   Read-only unless you tick the box";
      $("repo").value = cfg.default_repo;
    } catch (e) {
      $("meta").textContent = "Not connected. Open the link printed by webapp.py (it contains the token).";
    }
    loadProposals();
  }

  $("send").addEventListener("click", send);
  $("clear").addEventListener("click", clearConversation);
  $("refresh").addEventListener("click", loadProposals);
  init();
})();
"""


class AskBody(BaseModel):
    request: str = Field(min_length=1, max_length=2000)
    repo: str | None = Field(default=None, max_length=200)
    allow_proposals: bool = False

    @field_validator("request")
    @classmethod
    def not_blank(cls, value):
        value = ui.safe_text(value).strip()
        if not value:
            raise ValueError("request is empty")
        return value

    @field_validator("repo")
    @classmethod
    def valid_repo(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not actions.REPO_RE.fullmatch(value) or any(p in (".", "..") for p in value.split("/")):
            raise ValueError("repo must look like owner/name")
        return value


def _clean(value, limit: int) -> str:
    return ui.safe_text(value)[:limit]


def result_payload(res) -> dict:
    calls = []
    for t in res.tool_calls[:12]:
        calls.append({
            "name": _clean(t.get("name", ""), 40),
            "status": _clean(t.get("status", ""), 12),
            "chars": int(t.get("chars") or 0),
            "truncated": bool(t.get("truncated")),
            "flags": [_clean(f, 40) for f in t.get("flags") or []],
            "reason": _clean(t.get("reason") or "", 120),
        })
    return {
        "answer": _clean(res.answer, 8000),
        "stop_reason": _clean(res.stop_reason, 40),
        "stop_text": ui.STOP_TEXT.get(res.stop_reason) or "unknown",
        "error": _clean(res.error, 200),
        "steps": res.steps,
        "tokens": res.total_tokens,
        "seconds": round(res.elapsed, 1),
        "waited": round(res.seconds_waited),
        "tool_calls": calls,
        "refused_calls": res.refused_calls,
        "injection_flags": [_clean(f, 40) for f in res.injection_flags_seen],
        "unverified_claims": [_clean(c, 160) for c in res.unverified_claims],
        "ungrounded_refs": [int(n) for n in res.ungrounded_refs],
        "proposals": [{"id": pid, "command": f"uv run python approve.py show {pid}"}
                      for pid in res.proposals],
    }


@asynccontextmanager
async def real_runner():
    """One long-lived MCP session and model client for the life of the app."""
    llm = agent.make_llm()
    async with agent.open_session() as session:
        specs = await agent.load_specs(session)
        call_tool = agent.make_call_tool(session)
        limits = core.Limits()

        async def run(request, allow, repo, history):
            mode = tb.MODE_PROPOSE if allow else tb.MODE_READ
            return await core.run_agent(
                request, llm=llm, call_tool=call_tool,
                exposed=tb.select_tools(specs, mode),
                system_prompt=ui.build_system_prompt(repo, allow),
                limits=limits, prior_turns=history)

        yield run, llm.model


def create_app(*, runner_factory=None, token=None, default_repo=None,
               allowed_hosts=LOOPBACK_HOSTS) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    factory = runner_factory or real_runner

    @asynccontextmanager
    async def lifespan(app):
        async with factory() as (run, model):
            app.state.run = run
            app.state.model = model
            yield

    app = FastAPI(title="DevOps Triage", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    app.state.default_repo = default_repo
    app.state.history = []
    app.state.busy = False
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers[key] = value
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_token(x_ui_token: str | None = Header(default=None)):
        if not secrets.compare_digest((x_ui_token or "").encode(), token.encode()):
            raise HTTPException(status_code=401, detail="Missing or wrong UI token")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return INDEX_HTML

    @app.get("/app.js")
    async def app_js():
        return Response(APP_JS, media_type="text/javascript")

    @app.get("/style.css")
    async def style_css():
        return Response(STYLE_CSS, media_type="text/css")

    @app.get("/api/config", dependencies=[Depends(require_token)])
    async def config():
        return {"model": _clean(app.state.model, 80),
                "default_repo": _clean(app.state.default_repo or "", 200)}

    @app.post("/api/ask", dependencies=[Depends(require_token)])
    async def ask(body: AskBody):
        if app.state.busy:
            return JSONResponse({"error": "A run is already in progress. Wait for it to finish."},
                                status_code=409)
        repo = body.repo or app.state.default_repo
        try:
            ui.build_system_prompt(repo, body.allow_proposals)      # validates the repo name
        except ValueError:
            return JSONResponse({"error": "The default repo is invalid. Enter owner/name."},
                                status_code=422)
        app.state.busy = True
        try:
            res = await app.state.run(body.request, body.allow_proposals, repo,
                                      list(app.state.history))
        except Exception as exc:                  # never show exception text to the page
            return JSONResponse({"error": f"The run failed ({type(exc).__name__})."},
                                status_code=500)
        finally:
            app.state.busy = False
        if res.stop_reason == "answered" and res.answer:
            app.state.history.append((body.request, res.answer))
            del app.state.history[:-MAX_HISTORY]
        return result_payload(res)

    @app.post("/api/clear", dependencies=[Depends(require_token)])
    async def clear_history():
        if app.state.busy:
            return JSONResponse({"error": "A run is in progress."}, status_code=409)
        app.state.history.clear()
        return {"ok": True}

    @app.get("/api/proposals", dependencies=[Depends(require_token)])
    async def proposals():
        now = time.time()
        rows = []
        for a in await asyncio.to_thread(actions.list_actions, None, 20):
            left = max(0, int(a["expires_at"] - now))
            status = a["status"]
            if status == "pending" and left == 0:
                status = "expired"
            rows.append({
                "id": _clean(a["id"], 16), "status": _clean(status, 12),
                "tool": _clean(a["tool"], 30), "repo": _clean(a["repo"], 120),
                "number": int(a["number"]),
                "expires_in": left if status == "pending" else 0,
                "has_warnings": bool(a["risk_flags"]),
                "command": f"uv run python approve.py show {_clean(a['id'], 16)}",
            })
        return {"proposals": rows}

    return app


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Local web interface for the triage agent.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(agent.HERE / ".env.agent")
    app = create_app(token=(os.getenv("WEB_UI_TOKEN") or "").strip() or None,
                     default_repo=(os.getenv("DEFAULT_REPO") or "").strip() or None)

    if args.host not in LOOPBACK_HOSTS and args.host != "0.0.0.0":
        print("WARNING: this host is not loopback. Anyone who can reach it and has the token can use it.")
    print("\nOpen this link. It contains your session token, so treat it like a password:")
    print(f"  http://127.0.0.1:{args.port}/#token={app.state.token}\n")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())