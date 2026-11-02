import hashlib
import hmac
import json
import time
import urllib.parse

import yaml

from asf.adapters.base import AdapterContext
from asf.adapters.delivery.github import GitHubDelivery
from asf.adapters.intake.github_issues import GitHubIssuesIntake
from asf.adapters.intake.jira import JiraIntake, adf_text
from asf.adapters.intake.linear import LinearIntake
from asf.adapters.intake.pagerduty import PagerDutyIntake
from asf.adapters.intake.sentry import SentryIntake
from asf.adapters.intake.slack import SlackIntake
from asf.adapters.sandbox.docker import docker_command
from asf.ci import github_actions_workflow
from asf.models import WorkItem
from asf.util import ExecResult
from conftest import invoice_config, load_cfg

SECRETS = {"GITHUB_WEBHOOK_SECRET": "gh", "LINEAR_WEBHOOK_SECRET": "ln", "SLACK_SIGNING_SECRET": "sl",
           "SENTRY_CLIENT_SECRET": "se", "PAGERDUTY_WEBHOOK_SECRET": "pd", "JIRA_WEBHOOK_SECRET": "ji"}


def ctx(tmp_path, runner=None):
    return AdapterContext(repo=tmp_path, state_dir=tmp_path, config=load_cfg(invoice_config()),
                          secrets=SECRETS.get, runner=runner)


def sig(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_github_webhook(tmp_path):
    a = GitHubIssuesIntake({"label": "factory"}, ctx(tmp_path))
    body = json.dumps({"action": "labeled", "label": {"name": "factory"},
                       "issue": {"number": 7, "title": "Crash on save", "body": "steps",
                                 "labels": [{"name": "bug"}, {"name": "factory"}],
                                 "html_url": "https://github.com/o/r/issues/7", "user": {"login": "amy"}}}).encode()
    good = {"X-Hub-Signature-256": "sha256=" + sig("gh", body), "X-GitHub-Event": "issues"}
    assert a.verify_webhook(good, body)
    assert not a.verify_webhook({**good, "X-Hub-Signature-256": "sha256=bad"}, body)
    [item] = a.normalize_webhook(good, body)
    assert item.external_id == "7" and item.kind == "bug" and item.reporter == "amy"


def test_linear_webhook(tmp_path):
    a = LinearIntake({"trigger_state": "Ready for agent"}, ctx(tmp_path))
    body = json.dumps({"type": "Issue", "action": "update", "data": {
        "id": "u1", "identifier": "ENG-12", "title": "Fix export", "description": "d", "priority": 1,
        "state": {"name": "Ready for agent"}, "labels": [{"name": "Bug"}], "url": "https://linear.app/x"}}).encode()
    assert a.verify_webhook({"Linear-Signature": sig("ln", body)}, body)
    [item] = a.normalize_webhook({}, body)
    assert item.external_id == "ENG-12" and item.risk_hint == "high" and item.kind == "bug"


def test_slack_slash_command(tmp_path):
    a = SlackIntake({}, ctx(tmp_path))
    body = urllib.parse.urlencode({"text": "fix the date picker off-by-one", "user_name": "sam",
                                   "channel_id": "C1", "trigger_id": "T9"}).encode()
    ts = str(int(time.time()))
    headers = {"X-Slack-Request-Timestamp": ts, "Content-Type": "application/x-www-form-urlencoded",
               "X-Slack-Signature": "v0=" + sig("sl", b"v0:" + ts.encode() + b":" + body)}
    assert a.verify_webhook(headers, body)
    stale = {**headers, "X-Slack-Request-Timestamp": str(int(time.time()) - 900)}
    assert not a.verify_webhook(stale, body)
    [item] = a.normalize_webhook(headers, body)
    assert item.title.startswith("the date picker") and item.reporter == "sam"


def test_sentry_and_pagerduty(tmp_path):
    s = SentryIntake({}, ctx(tmp_path))
    body = json.dumps({"action": "created", "data": {"issue": {
        "id": "99", "title": "TypeError: x is undefined", "culprit": "app/save.ts", "count": "40",
        "permalink": "https://sentry.io/i/99", "metadata": {"type": "TypeError", "value": "x"}}}}).encode()
    headers = {"Sentry-Hook-Resource": "issue", "Sentry-Hook-Signature": sig("se", body)}
    assert s.verify_webhook(headers, body)
    [item] = s.normalize_webhook(headers, body)
    assert item.kind == "bug" and "app/save.ts" in item.body and item.acceptance

    p = PagerDutyIntake({}, ctx(tmp_path))
    body = json.dumps({"event": {"event_type": "incident.triggered", "data": {
        "id": "P1", "title": "API 5xx", "urgency": "high", "service": {"summary": "api"}}}}).encode()
    assert p.verify_webhook({"X-PagerDuty-Signature": "v1=" + sig("pd", body)}, body)
    [item] = p.normalize_webhook({}, body)
    assert item.kind == "incident" and item.risk_hint == "high"


def test_jira(tmp_path):
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
                                       {"type": "paragraph", "content": [{"type": "text", "text": "World"}]}]}
    assert "Hello" in adf_text(doc) and "World" in adf_text(doc)
    a = JiraIntake({"site": "https://acme.atlassian.net"}, ctx(tmp_path))
    body = json.dumps({"issue": {"key": "WEB-3", "fields": {
        "summary": "Broken link", "description": doc, "labels": ["factory"],
        "issuetype": {"name": "Bug"}, "priority": {"name": "High"}}}}).encode()
    assert a.verify_webhook({"X-Hub-Signature": "sha256=" + sig("ji", body)}, body)
    [item] = a.normalize_webhook({}, body)
    assert item.url.endswith("/browse/WEB-3") and item.risk_hint == "high"


def test_github_delivery_commands(tmp_path):
    calls = []

    def fake(args, **kw):
        calls.append(args)
        return ExecResult(0, "https://github.com/o/r/pull/5\n" if args[:3] == ["gh", "pr", "create"] else "{}",
                          "", 0.1)

    class SB:
        branch = "factory/x"

    d = GitHubDelivery({}, ctx(tmp_path, runner=fake))
    out = d.deliver({"id": "J", "data": {}}, WorkItem("local", "1", "Fix it"), SB(), "report", draft=True)
    assert out["url"].endswith("/pull/5")
    assert calls[0][:2] == ["git", "push"] and "--draft" in calls[1]


def test_docker_command_has_no_network_or_secrets(tmp_path):
    cmd = docker_command(tmp_path, "pkg", "npm test", {"docker": {"image": "node:22"}})
    assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    assert not any("API_KEY" in c for c in cmd)


def test_ci_workflow_is_valid_yaml():
    cfg = load_cfg(invoice_config(**{"project.stacks": ["python"]}))
    wf = yaml.safe_load(github_actions_workflow(cfg, "pip install asf"))
    steps = wf["jobs"]["factory"]["steps"]
    assert any("asf --repo ." in (s.get("run") or "") for s in steps)
    assert wf["permissions"]["contents"] == "write"
