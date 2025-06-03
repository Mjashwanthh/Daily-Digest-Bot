from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import requests
import os
import ollama
import datetime
import schedule
import time
import threading
import json
import csv
import tempfile
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
from slack_sdk import WebClient

load_dotenv()

with open("useridtoemail.json", "r") as f:
    SLACK_TO_JIRA_EMAIL = json.load(f)

with open("useridtobitbucket.json", "r") as f:
    SLACK_TO_BITBUCKET_USERNAME = json.load(f)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
BITBUCKET_APP_PASSWORD = os.getenv("BITBUCKET_APP_PASSWORD")
BITBUCKET_WORKSPACE = os.getenv("BITBUCKET_WORKSPACE")
BITBUCKET_REPOS = os.getenv("BITBUCKET_REPOS", "").split(",")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",")]
DIGEST_CHANNEL_ID = os.getenv("DIGEST_CHANNEL_ID")


slack_client = WebClient(token=SLACK_BOT_TOKEN)
app = App(token=SLACK_BOT_TOKEN)

def get_reviewer_review_time(pr, reviewer_username):
    pr_id = pr['id']
    repo_slug = pr['destination']['repository']['slug']
    url = f"https://api.bitbucket.org/2.0/repositories/{BITBUCKET_WORKSPACE}/{repo_slug}/pullrequests/{pr_id}/activity"
    response = requests.get(url, auth=(reviewer_username, BITBUCKET_APP_PASSWORD))
    if response.status_code != 200:
        return None

    activities = response.json().get("values", [])
    added_time = None
    reviewed_time = None

    for activity in activities:
        ts = activity.get("created_on")
        user = activity.get("user", {}).get("username")
        action_type = activity.get("approval") or activity.get("comment") or {}

        # Check if reviewer was added
        update = activity.get("update", {})
        reviewers = update.get("reviewers", [])
        for r in reviewers:
            if r.get("username") == reviewer_username and not added_time:
                added_time = ts

        # Check if reviewer took any action
        if user == reviewer_username:
            if activity.get("approval") or activity.get("comment"):
                reviewed_time = ts

    if added_time and reviewed_time:
        start = datetime.strptime(added_time, "%Y-%m-%dT%H:%M:%S.%f%z")
        end = datetime.strptime(reviewed_time, "%Y-%m-%dT%H:%M:%S.%f%z")
        return round((end - start).total_seconds() / (60 * 60 * 24), 2)

    return None

def safe_trim(text, limit=2900):
    """Safely trim long text at the last paragraph boundary."""
    if len(text) <= limit:
        return text
    cutoff = text[:limit].rfind("\n\n")
    cutoff = cutoff if cutoff != -1 else limit
    return text[:cutoff].rstrip() + "\n\n_⚠️ Truncated: Digest was too long_"

def get_jira_issues(user_email):
    url = f"{JIRA_BASE_URL}/rest/api/3/search"
    jql = f"assignee = '{user_email}' AND statusCategory != Done ORDER BY priority DESC"
    headers = {
        "Authorization": f"Basic {JIRA_API_TOKEN}",
        "Content-Type": "application/json"
    }
    params = {
        "jql": jql,
        "fields": "summary,status,priority,duedate"
    }
    response = requests.get(url, headers=headers, params=params)
    return response.json().get("issues", [])

@app.command("/priority")
def classify_priorities(ack, body, say):
    ack()
    channel_id = body['channel_id']

    summary_lines = []
    for user_id in ALLOWED_USERS:
        email = SLACK_TO_JIRA_EMAIL.get(user_id)
        if not email:
            continue

        issues = get_jira_issues(email)
        for issue in issues:
            key = issue['key']
            summary = issue['fields']['summary']
            status = issue['fields']['status']['name']
            priority_obj = issue['fields'].get('priority')
            priority = priority_obj.get('name', '').lower() if priority_obj else ''

            if priority in ["high", "major", "immediate"]:
                jira_url = f"{JIRA_BASE_URL}/browse/{key}"
                summary_lines.append(f"*<{jira_url}|{key}>*: {summary} _(Status: {status}, Priority: {priority.title()}, Owner: <@{user_id}>)_")

    if not summary_lines:
        say(channel=channel_id, text="✅ No critical Jira issues found.")
        return

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🔥 Team-Wide Critical Jira Issues"}},
        {"type": "divider"}
    ]

    for line in summary_lines:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":red_circle: {line}"}
        })

    say(channel=channel_id, text="Critical Jira Issues", blocks=blocks)

def get_user_created_prs(username):
    all_prs = []
    for repo in BITBUCKET_REPOS:
        url = f"https://api.bitbucket.org/2.0/repositories/{BITBUCKET_WORKSPACE}/{repo}/pullrequests"
        params = {"q": f"author.username=\"{username}\" AND state=\"OPEN\""}
        response = requests.get(url, auth=(username, BITBUCKET_APP_PASSWORD), params=params)
        if response.status_code == 200:
            prs = response.json().get("values", [])
            # Sort by created_on descending (newest first)
            prs.sort(key=lambda pr: pr['created_on'], reverse=True)
            for pr in prs:
                # Fetch unresolved comments
                comments_url = pr['links']['comments']['href']
                comment_resp = requests.get(comments_url, auth=(username, BITBUCKET_APP_PASSWORD))
                unresolved_count = 0
                if comment_resp.status_code == 200:
                    comments = comment_resp.json().get("values", [])
                    unresolved_count = sum(1 for c in comments if not c.get("deleted", False) and not c.get("resolved", True))
                pr['unresolved_comments'] = unresolved_count
                all_prs.append(pr)
    return all_prs


def get_user_review_prs(username):
    all_prs = []
    for repo in BITBUCKET_REPOS:
        url = f"https://api.bitbucket.org/2.0/repositories/{BITBUCKET_WORKSPACE}/{repo}/pullrequests"
        params = {"q": f"reviewers.username=\"{username}\" AND state=\"OPEN\""}
        response = requests.get(url, auth=(username, BITBUCKET_APP_PASSWORD), params=params)
        if response.status_code == 200:
            all_prs.extend(response.json().get("values", []))
    return all_prs

def generate_digest(jira_issues, user_prs, review_prs, slack_user_id=None):
    if not jira_issues and not user_prs and not review_prs:
        return f"*✅ No Jira tickets or PRs for <@{slack_user_id}> today. 🎉*"

    user_label = f"<@{slack_user_id}>" if slack_user_id else "You"

    jira_summary = ""
    for i in jira_issues:
        key = i['key']
        summary = i['fields']['summary']
        status = i['fields']['status']['name']
        priority_raw = i['fields']['priority']['name'] if i['fields'].get('priority') else "None"
        priority_emoji = {
            "High": "🔴",
            "Medium": "🟡",
            "Low": "🔵"
        }.get(priority_raw, "⚪")
        priority = f"{priority_emoji} {priority_raw}"
        jira_link = f"{JIRA_BASE_URL}/browse/{key}"
        jira_summary += f"• *<{jira_link}|{key}>*: {summary} _({status}, Priority: {priority})_\n\n"

    user_pr_summary = ""
    for pr in user_prs:
        title = pr['title']
        pr_id = pr['id']
        pr_link = pr['links']['html']['href']
        reviewers = ", ".join([r['display_name'] for r in pr.get("reviewers", [])])
        unresolved = pr.get('unresolved_comments', 0)
        comment_info = f"{unresolved} unresolved comment(s)" if unresolved > 0 else "No unresolved comments"
        user_pr_summary += f"• *<{pr_link}|PR #{pr_id}>*: {title} _(Reviewers: {reviewers}, {comment_info})_\n\n"

    review_pr_summary = ""
    for pr in review_prs:
        title = pr['title']
        author = pr['author']['display_name']
        pr_id = pr['id']
        pr_link = pr['links']['html']['href']
        review_pr_summary += f"• *<{pr_link}|PR #{pr_id}>*: {title} _(by {author})_\n\n"

    prompt = f"""
You are a task management assistant. From the following list of *open or in-progress* Jira issues and Bitbucket PRs, return the top 3 tasks for today, ranked by urgency or importance.

Jira Tasks:
{jira_summary}

PRs:
{user_pr_summary + review_pr_summary}
"""
    response = ollama.chat(model='llama3', messages=[{"role": "user", "content": prompt}])
    ranked_tasks = response['message']['content']

    summary = f"*🔥 Top 3 Priorities for {user_label}:*\n{ranked_tasks}\n\n" \
              f"*📝 Jira Issues (Open/In Progress):*\n{jira_summary or '_None_'}\n\n" \
              f"*📦 Open PRs by {user_label}:*\n{user_pr_summary or '_None_'}\n\n" \
              f"*👀 PRs Awaiting Review by {user_label}:*\n{review_pr_summary or '_None_'}\n\n" \
              f"👉 Click on the issue IDs above to open them directly in Jira or Bitbucket."

    return summary


@app.command("/myday")
def daily_digest(ack, body, say):
    ack()
    user_id = body['user_id']
    jira_email = SLACK_TO_JIRA_EMAIL.get(user_id)
    bitbucket_username = SLACK_TO_BITBUCKET_USERNAME.get(user_id)

    jira_issues = get_jira_issues(jira_email)
    user_prs = get_user_created_prs(bitbucket_username)
    review_prs = get_user_review_prs(bitbucket_username)
    full_summary = generate_digest(jira_issues, user_prs, review_prs)

    MAX_CHUNK = 2800
    chunks = []
    while full_summary:
        chunk = full_summary[:MAX_CHUNK]
        cutoff = chunk.rfind("\n\n")
        if cutoff == -1 or len(full_summary) <= MAX_CHUNK:
            cutoff = len(chunk)
        chunk = full_summary[:cutoff]
        chunks.append(chunk)
        full_summary = full_summary[cutoff:].lstrip()

    say(
        channel=user_id,
        text="*🎯 Your Daily Digest*",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": "*🎯 Your Daily Digest*"}},
            {"type": "divider"},
        ]
    )

    for chunk in chunks:
        say(
            channel=user_id,
            text=chunk,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": chunk}},
            ]
        )

    say(
        channel=user_id,
        blocks=[
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_Updated at: {datetime.now().strftime('%I:%M %p')}_"}]}
        ]
    )


def send_scheduled_digests():
    print("🔁 Running scheduled digest...")
    for user_id in ALLOWED_USERS:
        try:
            jira_email = SLACK_TO_JIRA_EMAIL[user_id]
            bitbucket_username = SLACK_TO_BITBUCKET_USERNAME[user_id]

            jira_issues = get_jira_issues(jira_email)
            user_prs = get_user_created_prs(bitbucket_username)
            review_prs = get_user_review_prs(bitbucket_username)
            full_summary = generate_digest(jira_issues, user_prs, review_prs)

            trimmed_summary = safe_trim(full_summary)

            slack_client.chat_postMessage(
                channel=DIGEST_CHANNEL_ID,
                text="Daily Digest",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": "*🎯 Your Daily Digest*"}},
                    {"type": "divider"},
                    {"type": "section", "text": {"type": "mrkdwn", "text": trimmed_summary}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_Updated at: {datetime.now().strftime('%I:%M %p')}_"}]}
                ]
            )
        except Exception as e:
            print(f"❌ Failed to send digest to {user_id}: {e}")

def run_scheduler():
    schedule.every().day.at("09:00").do(send_scheduled_digests)
    while True:
        schedule.run_pending()
        time.sleep(60)

@app.command("/teamday")
def team_digest(ack, body, say):
    ack()
    requester_id = body['user_id']
    channel_id = body['channel_id']

    if requester_id not in ALLOWED_USERS:
        say("🚫 You are not authorized to use this command.")
        return

    summary = generate_team_digest()

    # Slack block text must be <= 3000 characters
    MAX_BLOCK_TEXT = 2900
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*🧑‍💻 Team Daily Digest*"}},
        {"type": "divider"}
    ]

    # Split large text into chunks of ≤ MAX_BLOCK_TEXT
    while summary:
        chunk = summary[:MAX_BLOCK_TEXT]
        cutoff = chunk.rfind("\n\n")
        if cutoff == -1 or len(summary) <= MAX_BLOCK_TEXT:
            cutoff = len(chunk)
        chunk = summary[:cutoff]
        summary = summary[cutoff:].lstrip()

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": chunk}
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"_Updated at: {datetime.now().strftime('%I:%M %p')}_"}]
    })

    say(channel=channel_id, text="Team Daily Digest", blocks=blocks)



def generate_team_digest():
    digest = ""
    for user_id in ALLOWED_USERS:
        try:
            jira_email = SLACK_TO_JIRA_EMAIL.get(user_id)
            bitbucket_username = SLACK_TO_BITBUCKET_USERNAME.get(user_id)
            if not jira_email or not bitbucket_username:
                continue

            jira_issues = get_jira_issues(jira_email)
            user_prs = get_user_created_prs(bitbucket_username)
            review_prs = get_user_review_prs(bitbucket_username)

            # Personal header
            header = f"\n*👤 <@{user_id}>*\n"

            section = generate_digest(jira_issues, user_prs, review_prs)
            short_section = section.split("*🔥 Top 3 Priorities:*")[-1].strip()  # remove LLM summary
            digest += f"{header}\n{short_section}\n{'-'*40}\n"
        except Exception as e:
            digest += f"\n<@{user_id}>: ❌ Error fetching data.\n"

    if not digest:
        return "_No active issues or PRs for the team today._"

    return digest

@app.command("/team-metrics")
def send_metrics_report(ack, body, say):
    ack()
    user_id = body['user_id']
    channel_id = body['channel_id']

    if user_id not in ALLOWED_USERS:
        say("🚫 You are not authorized to run this command.")
        return

    metrics_text, csv_data = generate_metrics_report()

    with tempfile.NamedTemporaryFile(mode='w', newline='', suffix='.csv', delete=False) as tmpfile:
        writer = csv.writer(tmpfile)
        writer.writerow(["Email", "Open Jira Issues", "Avg PR Review Time (days)", ">3d Unreviewed PRs"])
        writer.writerows(csv_data)
        tmpfile_path = tmpfile.name

    try:
        with open(tmpfile_path, "rb") as f:
            from time import time
            slack_client.files_upload_v2(
                file=f,
                filename=os.path.basename(tmpfile_path),
                title="Team Metrics CSV",
                initial_comment="📎 Here's the CSV report:",
                channels=[channel_id],
                expiration_ts=int(time()) + 86400  # auto-expire in 24 hours
            )
    finally:
        os.remove(tmpfile_path)

    say(
        channel=channel_id,
        text="*📊 Team Metrics Report*",
        blocks=[]  # No extra button or duplicate message
    )

@app.action("export_metrics_csv")
def handle_export_button(ack, body, client):
    ack()
    channel_id = body['channel']['id']
    user_id = body['user']['id']
    if user_id not in ALLOWED_USERS:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text="🚫 You are not authorized.")
        return

    _, csv_data = generate_metrics_report()
    with tempfile.NamedTemporaryFile(mode='w', newline='', suffix='.csv', delete=False) as tmpfile:
        writer = csv.writer(tmpfile)
        writer.writerow(["Email", "Open Jira Issues", "Avg PR Review Time (days)", ">3d Unreviewed PRs"])
        writer.writerows(csv_data)
        tmpfile_path = tmpfile.name

    try:
        from time import time
        with open(tmpfile_path, "rb") as f:
            slack_client.files_upload_v2(
                file=f,
                filename=os.path.basename(tmpfile_path),
                title="Team Metrics CSV",
                initial_comment="📎 Here's the CSV report:",
                channels=[channel_id],
                expiration_ts=int(time()) + 86400
            )
    finally:
        os.remove(tmpfile_path)


def generate_metrics_report():
    csv_rows = []
    lines = []

    for user_id in ALLOWED_USERS:
        jira_email = SLACK_TO_JIRA_EMAIL.get(user_id)
        bitbucket_username = SLACK_TO_BITBUCKET_USERNAME.get(user_id)
        if not jira_email or not bitbucket_username:
            continue

        jira_issues = get_jira_issues(jira_email)
        open_issues_count = len(jira_issues)
        user_prs = get_user_review_prs(bitbucket_username)

        review_times = []
        old_unreviewed_count = 0

        for pr in user_prs:
            pr_id = pr['id']
            pr_url = pr['links']['self']['href']
            match = re.search(r"/repositories/[^/]+/([^/]+)/", pr_url)
            repo_slug = match.group(1) if match else None
            if not repo_slug:
                continue

            url = f"https://api.bitbucket.org/2.0/repositories/{BITBUCKET_WORKSPACE}/{repo_slug}/pullrequests/{pr_id}/activity"
            response = requests.get(url, auth=(bitbucket_username, BITBUCKET_APP_PASSWORD))
            if response.status_code != 200:
                continue

            activities = response.json().get("values", [])
            reviewer_added_at = None
            reviewer_reviewed_at = None

            for activity in activities:
                user = activity.get("user", {}).get("username")
                ts = activity.get("created_on")

                if not user or not ts:
                    continue

                if user == bitbucket_username:
                    if not reviewer_added_at and activity.get("update"):
                        reviewers = activity["update"].get("reviewers", [])
                        for r in reviewers:
                            if r.get("username") == bitbucket_username:
                                reviewer_added_at = ts

                    if not reviewer_reviewed_at:
                        if activity.get("approval"):
                            reviewer_reviewed_at = ts
                        elif activity.get("comment"):
                            reviewer_reviewed_at = ts

            if reviewer_added_at and reviewer_reviewed_at:
                added = datetime.strptime(reviewer_added_at, "%Y-%m-%dT%H:%M:%S.%f%z")
                reviewed = datetime.strptime(reviewer_reviewed_at, "%Y-%m-%dT%H:%M:%S.%f%z")
                delta = (reviewed - added).total_seconds() / (60 * 60 * 24)
                review_times.append(delta)

            created_at = datetime.strptime(pr['created_on'], "%Y-%m-%dT%H:%M:%S.%f%z")
            if (datetime.now(datetime.utcnow().astimezone().tzinfo) - created_at).days > 3:
                old_unreviewed_count += 1

        avg_review_time = round(sum(review_times) / len(review_times), 1) if review_times else 0.0

        lines.append(f"• 👤 <@{user_id}> ({jira_email}):\n   - 📝 {open_issues_count} open Jira issues\n   - ⏱ Avg PR review time: {avg_review_time} days\n   - 🚨 {old_unreviewed_count} PRs >3 days unreviewed\n")
        csv_rows.append([jira_email, open_issues_count, avg_review_time, old_unreviewed_count])

    return "\n".join(lines), csv_rows

@app.command("/priority")
def classify_priorities(ack, body, say):
    ack()
    user_id = body['user_id']
    email = SLACK_TO_JIRA_EMAIL.get(user_id)
    if not email:
        say("🚫 No email mapping found for this user.")
        return

    issues = get_jira_issues(email)
    if not issues:
        say("✅ No active Jira issues found.")
        return

    summary_text = ""
    for issue in issues:
        key = issue['key']
        summary = issue['fields']['summary']
        status = issue['fields']['status']['name']
        summary_text += f"- [{key}] {summary} ({status})\n"

    prompt = f"""
From the following Jira issues, classify each as Critical, Moderate, or Minor based on summary and status:

{summary_text}
"""

    response = ollama.chat(model="llama3", messages=[{"role": "user", "content": prompt}])
    categories = response['message']['content']

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📌 AI-based Priority Classification*"}},
        {"type": "divider"},
    ]

    for line in categories.splitlines():
        if line.strip():
            if "Critical" in line:
                color = "#FF4D4D"
            elif "Moderate" in line:
                color = "#FFD700"
            elif "Minor" in line:
                color = "#1E90FF"
            else:
                color = "#CCCCCC"

            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"```{line.strip()}```"
                    },
                    {
                        "type": "image",
                        "image_url": f"https://singlecolorimage.com/get/{color}/16x16",
                        "alt_text": "priority"
                    }
                ]
            })

    say(blocks=blocks)

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
