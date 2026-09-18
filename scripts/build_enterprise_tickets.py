#!/usr/bin/env python3
"""
Rebuilds "Enterprise Customers Tickets.html" from live Jira data.

Pulls every issue in the Jira "ECS" project (Enterprise Customers - Sivan),
splits Epics (= customer boards) from regular tickets, groups tickets by
their "Project" custom field (the customer/business each ticket belongs
to), and links each business to a matching Epic where possible. Also
pulls in engineering/product tickets from the RD and PM projects that are
linked to a customer's Epic via a "Polaris datapoint work item link", so
those show up grouped under the same business. Renders the result into
the HTML template.

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
FIELDS = ["summary", "status", "issuetype", BUSINESS_FIELD, "priority", "created", "updated", "issuelinks"]

# Engineering/product work for a customer request lives in these two other
# projects, and gets connected to the customer's ECS Epic via a Jira
# "Polaris datapoint work item link" (the epic acts as the "idea", the
# RD/PM ticket is the delivery work "added to" it).
DEV_LINK_TYPE = "Polaris datapoint work item link"
DEV_PROJECT_KEYS = {"RD", "PM"}

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


def fetch_issues_by_keys(keys, fields):
    """Bulk-fetch a fixed set of issues by key, batched to keep the JQL short."""
    out = {}
    keys = list(keys)
    batch_size = 50
    for i in range(0, len(keys), batch_size):
        batch_keys = keys[i:i + batch_size]
        next_page_token = None
        while True:
            body = {
                "jql": "key in ({})".format(", ".join(batch_keys)),
                "fields": fields,
                "maxResults": 100,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            data = jira_post("/rest/api/3/search/jql", body)
            for it in data.get("issues", []):
                out[it["key"]] = it
            next_page_token = data.get("nextPageToken")
            if data.get("isLast", not next_page_token) or not data.get("issues"):
                break
    return out


def extract_dev_links(raw_epics):
    """Pull RD/PM tickets linked to each ECS Epic via the Polaris
    datapoint work item link (inward side: the epic is the "idea")."""
    links = []
    for it in raw_epics:
        epic_key = it["key"]
        for link in it["fields"].get("issuelinks") or []:
            if link.get("type", {}).get("name") != DEV_LINK_TYPE:
                continue
            inward = link.get("inwardIssue")
            if not inward:
                continue
            ticket_key = inward["key"]
            project_key = ticket_key.split("-")[0]
            if project_key not in DEV_PROJECT_KEYS:
                continue
            lf = inward["fields"]
            links.append({
                "epicKey": epic_key,
                "key": ticket_key,
                "summary": lf.get("summary") or "",
                "status": lf["status"]["name"],
                "statusCategory": lf["status"]["statusCategory"]["name"],
                "issuetype": lf["issuetype"]["name"],
                "priority": (lf.get("priority") or {}).get("name"),
                "source": project_key,
            })
    return links


def fetch_dev_links(raw_epics):
    links = extract_dev_links(raw_epics)
    if not links:
        return []
    unique_keys = sorted({l["key"] for l in links})
    extra = fetch_issues_by_keys(unique_keys, ["created", "updated"])
    for l in links:
        it = extra.get(l["key"])
        l["created"] = (it["fields"].get("created") or "")[:10] if it else ""
        l["updated"] = (it["fields"].get("updated") or "")[:10] if it else ""
    return links


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


def build_dataset(raw_issues, dev_links):
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
                "source": "ECS",
            })

    epic_norms = [(norm(e["summary"]), e) for e in epics]
    epic_by_key = {e["key"]: e for e in epics}

    # Group native ECS tickets by their "Project" (business) field, and
    # match each business to a customer Epic - same logic as before.
    groups = {}
    for t in tickets:
        key = norm(t["business"]) if t["business"] else "__none__"
        g = groups.setdefault(key, {"raw_labels": Counter(), "issues": [], "epicKey": None})
        if t["business"]:
            g["raw_labels"][t["business"]] += 1
        g["issues"].append(t)

    biz_norm_to_label = {}
    for key, g in groups.items():
        label = g["raw_labels"].most_common(1)[0][0] if g["raw_labels"] else "ללא עסק משויך"
        biz_norm_to_label[key] = label
        g["label"] = label
        if key == "__none__":
            epic = None
        elif label in MANUAL_EPIC_OVERRIDE:
            epic = epic_by_key.get(MANUAL_EPIC_OVERRIDE[label])
        else:
            epic, _method = find_epic(key, epic_norms)
        g["epicKey"] = epic["key"] if epic else None

    # Primary business per Epic (highest native-ticket count wins) - used
    # to route RD/PM tickets linked to that Epic to the right business.
    epic_key_to_biz_norm = {}
    for key, g in sorted(groups.items(), key=lambda kv: -len(kv[1]["issues"])):
        if g["epicKey"] and g["epicKey"] not in epic_key_to_biz_norm:
            epic_key_to_biz_norm[g["epicKey"]] = key

    # Merge in RD/PM tickets linked via the customer's Epic. A ticket
    # linked to several customers' Epics is duplicated once per business.
    for link in dev_links:
        biz_norm = epic_key_to_biz_norm.get(link["epicKey"])
        if biz_norm is None:
            # No native business claims this Epic yet - create one from
            # the Epic's own name so the linked ticket isn't dropped.
            epic = epic_by_key.get(link["epicKey"])
            label = epic["summary"] if epic else link["epicKey"]
            biz_norm = norm(label) or link["epicKey"].lower()
            if biz_norm not in groups:
                groups[biz_norm] = {"raw_labels": Counter(), "issues": [], "label": label, "epicKey": link["epicKey"]}
                biz_norm_to_label[biz_norm] = label
            epic_key_to_biz_norm[link["epicKey"]] = biz_norm
        groups[biz_norm]["issues"].append({
            "key": link["key"],
            "summary": link["summary"],
            "status": link["status"],
            "statusCategory": link["statusCategory"],
            "business": biz_norm_to_label[biz_norm],
            "issuetype": link["issuetype"],
            "priority": link["priority"],
            "created": link["created"],
            "updated": link["updated"],
            "source": link["source"],
        })

    business_list = []
    for key, g in groups.items():
        statuses = Counter(t["statusCategory"] for t in g["issues"])
        epic = epic_by_key.get(g["epicKey"]) if g["epicKey"] else None
        business_list.append({
            "norm": key,
            "label": g["label"],
            "count": len(g["issues"]),
            "statusCounts": dict(statuses),
            "epicKey": g["epicKey"],
            "epicSummary": epic["summary"] if epic else (g["label"] if g["epicKey"] else None),
        })
    business_list.sort(key=lambda b: -b["count"])

    tickets_out = []
    for key, g in groups.items():
        for t in g["issues"]:
            tickets_out.append({
                "key": t["key"],
                "summary": t["summary"],
                "status": t["status"],
                "statusCategory": t["statusCategory"],
                "business": g["label"],
                "businessNorm": key,
                "issuetype": t["issuetype"],
                "priority": t["priority"],
                "created": t["created"],
                "updated": t["updated"],
                "source": t["source"],
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

    raw_epics = [it for it in raw_issues if it["fields"]["issuetype"]["name"] == "Epic"]
    dev_links = fetch_dev_links(raw_epics)

    dataset = build_dataset(raw_issues, dev_links)
    html = render(dataset)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {len(dataset['tickets'])} tickets across "
          f"{len([b for b in dataset['businesses'] if b['norm'] != '__none__'])} businesses "
          f"to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
