#!/usr/bin/env python3
"""
Rebuilds "Enterprise Customers Tickets.html" from live Jira data.

Pulls every issue in the Jira "ECS" project (Enterprise Customers - Sivan),
splits Epics (= customer boards) from regular tickets, groups tickets by
their "Project" custom field (the customer/business each ticket belongs
to), links each business to a matching Epic where possible, and renders
the result into the HTML template.

Required environment variables:
  JIRA_EMAIL      - Atlassian account email used to authenticate
  JIRA_API_TOKEN  - API token for that account (id.atlassian.com/manage-profile/security/api-tokens)

Optional:
  JIRA_BASE_URL   - defaults to https://arbox.atlassian.net
"""
import base64
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://arbox.atlassian.net").rstrip("/")
PROJECT_KEY = "ECS"
BUSINESS_FIELD = "customfield_10301"
FIELDS = ["summary", "status", "issuetype", BUSINESS_FIELD, "priority", "created", "updated"]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(REPO_ROOT, "templates", "enterprise_tickets_template.html")
OUTPUT_PATH = os.path.join(REPO_ROOT, "Enterprise Customers Tickets.html")

PREFIXES = ["מועצה אזורית", "מועצה אוזרית", "עמותת", "ארגון"]

# Same-entity aliases confirmed by hand (Hebrew/English variants of one
# customer, or wording too different for the automatic matcher below).
MANUAL_EPIC_OVERRIDE = {
    "מרכז הספורט הלאומי ת״א- יפו (ולודרום)": "ECS-10",
    "נעים": "ECS-27",
    "מרכז הספורט באוניברסיטת ת״א": "ECS-14",
}


def jira_post(path, body):
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    url = f"{JIRA_BASE_URL}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "ignore")
            raise RuntimeError(f"Jira API error {e.code} for {path}: {body_text}") from e
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Jira API request failed after retries: {last_err}")


def fetch_all_issues():
    """Fetch every issue in the ECS project via the enhanced JQL search
    endpoint (cursor-paginated - the classic startAt-based /search
    endpoint is deprecated on Jira Cloud)."""
    issues = []
    next_page_token = None
    while True:
        body = {
            "jql": f"project = {PROJECT_KEY} ORDER BY key",
            "fields": FIELDS,
            "maxResults": 100,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        data = jira_post("/rest/api/3/search/jql", body)
        batch = data.get("issues", [])
        issues.extend(batch)
        next_page_token = data.get("nextPageToken")
        if data.get("isLast", not next_page_token) or not batch:
            break
    return issues


def norm(s):
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def strip_prefix(s):
    for p in PREFIXES:
        if s.startswith(p.lower()):
            return s[len(p):].strip()
    return s


def find_epic(biz_norm, epic_norms):
    for en, e in epic_norms:
        if en == biz_norm:
            return e, "exact"
    b2 = strip_prefix(biz_norm)
    for en, e in epic_norms:
        if strip_prefix(en) == b2:
            return e, "prefix-exact"
    for en, e in epic_norms:
        en2 = strip_prefix(en)
        if len(b2) >= 3 and len(en2) >= 3 and (b2 in en2 or en2 in b2):
            return e, "containment"
    best, best_ratio = None, 0
    for en, e in epic_norms:
        r = difflib.SequenceMatcher(None, b2, strip_prefix(en)).ratio()
        if r > best_ratio:
            best_ratio, best = r, e
    if best_ratio >= 0.86:
        return best, "fuzzy"
    return None, None


def build_dataset(raw_issues):
    epics = []
    tickets = []
    for it in raw_issues:
        f = it["fields"]
        issuetype = f["issuetype"]["name"]
        if issuetype == "Epic":
            epics.append({
                "key": it["key"],
                "summary": (f.get("summary") or "").strip(),
                "status": f["status"]["name"],
            })
        else:
            biz_field = f.get(BUSINESS_FIELD)
            business = biz_field["value"].strip() if biz_field else None
            tickets.append({
                "key": it["key"],
                "summary": f.get("summary") or "",
                "status": f["status"]["name"],
                "statusCategory": f["status"]["statusCategory"]["name"],
                "business": business,
                "issuetype": issuetype,
                "priority": (f.get("priority") or {}).get("name"),
                "created": (f.get("created") or "")[:10],
                "updated": (f.get("updated") or "")[:10],
            })

    epic_norms = [(norm(e["summary"]), e) for e in epics]
    epic_by_key = {e["key"]: e for e in epics}

    groups = {}
    for t in tickets:
        key = norm(t["business"]) if t["business"] else "__none__"
        g = groups.setdefault(key, {"raw_labels": Counter(), "issues": []})
        if t["business"]:
            g["raw_labels"][t["business"]] += 1
        g["issues"].append(t)

    business_list = []
    biz_norm_to_label = {}
    for key, g in groups.items():
        label = g["raw_labels"].most_common(1)[0][0] if g["raw_labels"] else "ללא עסק משויך"
        biz_norm_to_label[key] = label
        if key == "__none__":
            epic, method = None, None
        elif label in MANUAL_EPIC_OVERRIDE:
            epic, method = epic_by_key.get(MANUAL_EPIC_OVERRIDE[label]), "manual"
        else:
            epic, method = find_epic(key, epic_norms)
        statuses = Counter(t["statusCategory"] for t in g["issues"])
        business_list.append({
            "norm": key,
            "label": label,
            "count": len(g["issues"]),
            "statusCounts": dict(statuses),
            "epicKey": epic["key"] if epic else None,
            "epicSummary": epic["summary"] if epic else None,
        })
    business_list.sort(key=lambda b: -b["count"])

    tickets_out = []
    for t in tickets:
        key = norm(t["business"]) if t["business"] else "__none__"
        tickets_out.append({
            "key": t["key"],
            "summary": t["summary"],
            "status": t["status"],
            "statusCategory": t["statusCategory"],
            "business": biz_norm_to_label[key],
            "businessNorm": key,
            "issuetype": t["issuetype"],
            "priority": t["priority"],
            "created": t["created"],
            "updated": t["updated"],
        })

    return {"tickets": tickets_out, "businesses": business_list, "epics": epics}


def render(dataset):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()

    data_json = json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))
    biz_count = len([b for b in dataset["businesses"] if b["norm"] != "__none__"])
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = template
    html = html.replace("__DATA_JSON__", data_json)
    html = html.replace("__TOTAL__", str(len(dataset["tickets"])))
    html = html.replace("__BIZCOUNT__", str(biz_count))
    html = html.replace("__UPDATED_AT__", updated_at)
    return html


def main():
    raw_issues = fetch_all_issues()
    if not raw_issues:
        print("No issues fetched from Jira - aborting without touching the output file.", file=sys.stderr)
        sys.exit(1)

    dataset = build_dataset(raw_issues)
    html = render(dataset)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {len(dataset['tickets'])} tickets across "
          f"{len([b for b in dataset['businesses'] if b['norm'] != '__none__'])} businesses "
          f"to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
