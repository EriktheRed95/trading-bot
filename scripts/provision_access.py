"""Put a Cloudflare Access login in front of the dashboard, before it serves anything.

WHY THIS RUNS BEFORE THE DEPLOY, NOT AFTER. A Cloudflare Pages project is live
the instant it first deploys. If Access were configured afterwards there would be
a window, however short, where the dashboard was readable by anyone who guessed
the hostname. Erik asked for a login on this thing, so the login has to exist
first. This script is therefore ordered ahead of `wrangler pages deploy` in the
workflow, and the deploy is skipped if it fails.

WHY CLOUDFLARE AND NOT GITHUB PAGES. Access control for GitHub Pages requires
GitHub Enterprise Cloud and an organization-owned repo. On a personal account a
Pages site is publicly readable whether the source repo is public or private,
because site visibility is a separate setting from repository visibility. There
is no login option at any price on a personal plan. Cloudflare Access provides a
real one, by email one-time code or Google sign-in, on the Zero Trust free tier
(50 users). Erik already runs this exact pattern on the escrow portal, so it is
proven on his account rather than theoretical.

IDEMPOTENT. Safe to run on every deploy. It looks for an existing application on
the hostname and updates its policy rather than stacking duplicates, because a
workflow step that only works the first time is a trap for whoever runs it next.

Needs a Cloudflare API token with BOTH:
  Account > Cloudflare Pages > Edit          (for the deploy step)
  Account > Access: Apps and Policies > Edit (for this script)

Env: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, ACCESS_ALLOWED_EMAILS
     (comma separated), ACCESS_HOSTNAME
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"


def call(method, path, token, body=None):
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:600]
        raise SystemExit(f"Cloudflare API {method} {path} failed: {e.code}\n{detail}")


def main():
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    host = os.environ.get("ACCESS_HOSTNAME", "").strip()
    emails = [e.strip() for e in
              os.environ.get("ACCESS_ALLOWED_EMAILS", "").split(",") if e.strip()]

    if not (token and account and host and emails):
        raise SystemExit("Missing one of CLOUDFLARE_API_TOKEN, "
                         "CLOUDFLARE_ACCOUNT_ID, ACCESS_HOSTNAME, "
                         "ACCESS_ALLOWED_EMAILS. Refusing to continue, because "
                         "deploying without a login is the one outcome to avoid.")

    existing = call("GET", f"/accounts/{account}/access/apps", token)
    app = next((a for a in existing.get("result", [])
                if a.get("domain", "").rstrip("/") == host.rstrip("/")), None)

    policy = {
        "name": "Erik only",
        "decision": "allow",
        "include": [{"email": {"email": e}} for e in emails],
    }
    payload = {
        "name": "Trading dashboard",
        "domain": host,
        "type": "self_hosted",
        "session_duration": "168h",   # a week, so it is not a login every glance
        "app_launcher_visible": True,
        "policies": [policy],
    }

    if app:
        app_id = app["id"]
        call("PUT", f"/accounts/{account}/access/apps/{app_id}", token, payload)
        print(f"Updated the existing Access app for {host} (id {app_id})")
    else:
        created = call("POST", f"/accounts/{account}/access/apps", token, payload)
        app_id = created["result"]["id"]
        print(f"Created an Access app for {host} (id {app_id})")

    # Read back rather than trusting the write. A policy that silently failed to
    # attach would leave the dashboard open, which is the whole thing being
    # guarded against.
    back = call("GET", f"/accounts/{account}/access/apps/{app_id}/policies", token)
    pols = back.get("result", [])
    allowed = [inc.get("email", {}).get("email")
               for p in pols for inc in p.get("include", [])
               if p.get("decision") == "allow"]
    if not pols or not allowed:
        raise SystemExit("Access app exists but has NO allow policy attached. "
                         "Refusing to let the deploy proceed.")
    print(f"Verified {len(pols)} policy/policies. Allowed: {', '.join(a for a in allowed if a)}")
    print(f"{host} now requires a login before it serves anything.")


if __name__ == "__main__":
    sys.exit(main())
