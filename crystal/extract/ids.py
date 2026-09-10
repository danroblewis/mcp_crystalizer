"""Typed regex catalog for ID-shaped values. Each entry: name -> (regex, description).
Order matters for typing a raw string: earlier, more specific shapes win."""
from __future__ import annotations

import re

ID_PATTERNS: dict[str, tuple[str, str]] = {
    "jira_key":     (r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b", "Jira issue key, e.g. PAY-101"),
    "sha40":        (r"\b[0-9a-f]{40}\b", "full git commit SHA"),
    "trace_id":     (r"\b[0-9a-f]{32}\b", "32-hex trace id"),
    "uuid":         (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "UUID"),
    "trace_id16":   (r"\b[0-9a-f]{16}\b", "16-hex span/trace id"),
    # A digit is required in both: without it, ordinary words match. `defaced` is 7 hex letters, and
    # `what-requires-being` is three hyphenated words of exactly pod-name lengths (both seen in real projects).
    "sha_short":    (r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,12}\b", "abbreviated git SHA"),
    # A pod from a Deployment is `<deployment>-<replicaset hash>-<5>`. Kubernetes generates both suffixes from a
    # vowel-free alphabet (bcdfghjklmnpqrstvwxz2456789) precisely so they never spell words; we also accept hex,
    # since plenty of tooling fakes them that way, but require a digit and forbid the whole thing exceeding the
    # 63-character DNS label limit. Without those guards `what-requires-being` matched, and it is a place name.
    "k8s_pod":      (r"(?<![\w.-])(?=[a-z0-9-]{1,63}(?![\w.-]))[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
                     r"-(?=[a-z0-9]{8,10}-)(?:[bcdfghjklmnpqrstvwxz2456789]{8,10}|(?=[a-f0-9]*\d)[a-f0-9]{8,10})"
                     r"-(?:[bcdfghjklmnpqrstvwxz2456789]{5}|(?=[a-f0-9]*\d)[a-f0-9]{5})(?![\w.-])",
                     "kubernetes pod name (deployment-replicaset-pod)"),
    "iso_ts":       (r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b", "ISO-8601 timestamp"),
    "date":         (r"\b\d{4}-\d{2}-\d{2}\b", "calendar date"),
    "slack_ts":     (r"\b1[6-9]\d{8}\.\d{6}\b", "Slack message ts"),
    "slack_channel_id": (r"\bC[0-9A-F]{8,10}\b", "Slack channel id"),
    "slack_dm_id":      (r"\bD[0-9A-F]{8,10}\b", "Slack direct-message (im) channel id"),
    "slack_user_id":    (r"\bU[0-9A-F]{8,10}\b", "Slack user id"),
    "pd_incident_id":   (r"\bQ[0-9A-Z]{13}\b", "PagerDuty incident id"),
    "pd_service_id":    (r"\bP[0-9A-Z]{6}\b", "PagerDuty service id"),
    "http_status":  (r"(?<![\w.])[1-5]\d{2}(?![\w.])", "HTTP status code"),
    "ipv4":         (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IPv4 address"),
    "email":        (r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "email"),
    "url":          (r"https?://[^\s<>\"')]+", "URL"),
    "slack_channel":(r"(?<![\w/])#(?![0-9a-fA-F]{6}(?![\w-]))[a-z0-9][a-z0-9_-]{1,40}\b", "Slack channel name"),
    "error_class":  (r"\b[A-Z][A-Za-z0-9]+(?:Error|Exception|Timeout|Mismatch|Drift|Expired|NotFound|Limited|Snapshot)\b", "exception-like class name"),

    # ---- code and version control
    "semver":       (r"(?<![\w.-])v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?(?![\w.])", "semantic version"),
    "git_remote":   (r"\bgit@[\w.-]+:[\w.-]+/[\w.-]+?(?:\.git)?(?![\w.-])", "git ssh remote"),
    "github_repo":  (r"(?<![\w/])(?:github\.com[/:])([\w.-]+/[\w.-]+?)(?:\.git)?(?![\w.-])", "GitHub owner/repo"),
    "github_ref":   (r"\b[\w.-]+/[\w.-]+#\d+\b", "GitHub issue or PR reference (owner/repo#123)"),
    "file_line":    (r"(?<![\w/])(?:/|\./|[\w.-]+/)[\w./-]*\.[A-Za-z][\w]{0,7}:\d+(?::\d+)?(?![\w])", "file path with a line number"),
    "abs_path":     (r"(?<![\w:])/(?:[\w.-]+/){1,}[\w.-]+(?![\w/])", "absolute file path"),
    "java_class":   (r"\b(?:[a-z][a-z0-9_]*\.){2,}[A-Z][A-Za-z0-9_]*\b", "fully qualified Java-style class"),
    "python_module":(r"\b(?:[a-z][a-z0-9_]*\.){2,}[a-z][a-z0-9_]*\b(?!\()", "dotted Python module path"),
    "package_spec": (r"\b[a-z][\w.-]*(?:==|@)\d+\.\d+(?:\.\d+)?(?![\w.])", "pinned package (name==1.2.3)"),

    # ---- containers and cloud
    "docker_image": (r"\b(?:[\w.-]+(?::\d+)?/)?[\w.-]+/[\w.-]+:[\w.-]+\b", "container image reference"),
    "image_digest": (r"\bsha256:[a-f0-9]{64}\b", "container image digest"),
    "aws_arn":      (r"\barn:aws[\w-]*:[\w-]+:[\w-]*:\d*:[\w:/.-]+", "AWS ARN"),
    "ec2_instance": (r"\bi-(?:[0-9a-f]{17}|[0-9a-f]{8})\b", "EC2 instance id"),
    "s3_uri":       (r"\bs3://[\w.-]+(?:/[\w./-]*)?", "S3 URI"),
    "gcs_uri":      (r"\bgs://[\w.-]+(?:/[\w./-]*)?", "Google Cloud Storage URI"),
    "k8s_namespace_ref": (r"\bns/[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\b", "kubernetes namespace reference (ns/name)"),
    "k8s_resource": (r"\b(?:pod|deployment|deploy|svc|service|statefulset|daemonset|job|cronjob|configmap|secret|ingress)/[a-z0-9]([-a-z0-9.]{0,61}[a-z0-9])?\b",
                     "kubernetes resource reference (kind/name)"),

    # ---- network
    "mac_address":  (r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", "MAC address"),
    "ipv6":         (r"(?<![\w:.])(?:[0-9a-fA-F]{1,4}:){1,7}:(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4})*)?(?![\w:.])"
                     r"|(?<![\w:.])(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}(?![\w:.])", "IPv6 address"),
    "cidr":         (r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b", "CIDR block"),
    "host_port":    (r"\b(?:[\w-]+\.)+[a-z]{2,24}:\d{2,5}\b", "host:port"),
    "fqdn":         (r"\b(?:[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.){2,}(?:com|org|net|io|dev|ai|co|internal|local|cluster|svc)\b",
                     "fully qualified domain name"),

    # ---- identifiers from other systems
    "ulid":         (r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b", "ULID"),
    "object_id":    (r"\b[0-9a-f]{24}\b", "MongoDB ObjectId"),
    "snowflake_id": (r"(?<![\w.])\d{17,19}(?![\w.])", "Snowflake id (Discord/Twitter style)"),
    "epoch_ms":     (r"(?<![\w.])1[5-9]\d{11}(?![\w.])", "epoch milliseconds"),
    "epoch_s":      (r"(?<![\w.])1[5-9]\d{8}(?![\w.])", "epoch seconds"),
    "traceparent":  (r"\b00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}\b", "W3C traceparent"),
    "gdrive_id":    (r"\b1[A-Za-z0-9_-]{32,43}\b", "Google Drive file id"),

    # ---- observability and scheduling
    "prom_metric":  (r"\b[a-z_][a-z0-9_]*_(?:total|seconds|bytes|count|sum|bucket|ratio|errors|requests)\b", "Prometheus metric name"),
    "prom_selector":(r'\{[a-z_][a-z0-9_]*\s*(?:=~|!~|!=|=)\s*"[^"]*"(?:\s*,\s*[a-z_][a-z0-9_]*\s*(?:=~|!~|!=|=)\s*"[^"]*")*\}',
                     "Prometheus label selector"),
    "log_level":    (r"\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b", "log level"),
    "iso_duration": (r"\bP(?:\d+[YMWD])*(?:T(?:\d+[HMS])+)?\b(?<!\bP)", "ISO-8601 duration"),
    "relative_time":(r"(?<![\w-])now-\d+[smhdw](?![\w])", "relative time (now-15m)"),
    "cron_expr":    (r"(?<![\w*/-])(?=[\d*/,\s-]*[*/])(?:[\d*/,-]+[ \t]+){4}[\d*/,-]+(?![\w*/-])", "cron expression"),
    "hex_color":    (r"#[0-9a-fA-F]{6}\b", "hex colour"),
}

# Values that must never be stored as an example or passed on: a flow that carries a credential is a leak. These
# are detected so they can be redacted, and are excluded from anything that proposes flow inputs.
SECRET_PATTERNS: dict[str, tuple[str, str]] = {
    "github_token": (r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", "GitHub token"),
    "slack_token":  (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    "aws_key_id":   (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "AWS access key id"),
    "openai_key":   (r"\bsk-[A-Za-z0-9_-]{20,}\b", "API key (sk-...)"),
    "google_key":   (r"\bAIza[0-9A-Za-z_-]{35}\b", "Google API key"),
    "jwt":          (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "JSON Web Token"),
    "private_key":  (r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "private key block"),
    "bearer":       (r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}", "bearer credential"),
}
ID_PATTERNS.update(SECRET_PATTERNS)
SECRET_TYPES = frozenset(SECRET_PATTERNS)
_COMPILED = {k: re.compile(v[0]) for k, v in ID_PATTERNS.items()}
_SECRET_RX = [re.compile(v[0]) for v in SECRET_PATTERNS.values()]


def is_secret(kind: str) -> bool:
    """A credential: detected so it can be redacted, never offered as a parameter or stored as an example."""
    return kind in SECRET_TYPES


def redact(text: str, replacement: str = "[redacted]") -> str:
    """Every credential-shaped value replaced. Use before storing recorded text anywhere a person will read it."""
    for rx in _SECRET_RX:
        text = rx.sub(replacement, text or "")
    return text


def find_all(text: str, kind: str) -> list[str]:
    """All matches of one typed pattern, in order, deduplicated. A match that sits inside a span of a
    different, more specific type is skipped (a replica-set hash inside a pod name is not a commit SHA)."""
    seen, out = set(), []
    others = [(k, rx) for k, rx in _COMPILED.items() if k != kind and list(_COMPILED).index(k) < list(_COMPILED).index(kind) or k == "k8s_pod"]
    covering = [(m.start(), m.end()) for k, rx in others if k != kind for m in rx.finditer(text or "")]
    for m in _COMPILED[kind].finditer(text or ""):
        v = m.group(0)
        if any(a <= m.start() and m.end() <= b and (b - a) > (m.end() - m.start()) for a, b in covering):
            continue
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def type_of(value: str) -> str | None:
    """Best-guess type of a whole string, or None."""
    for k, rx in _COMPILED.items():
        if rx.fullmatch(value or ""):
            return k
    return None


def typed_mentions(text: str) -> list[dict]:
    """Every typed ID-shaped span in a text: [{type, value, start, end}], longest/most specific first per span."""
    spans = []
    for k, rx in _COMPILED.items():
        for m in rx.finditer(text or ""):
            spans.append({"type": k, "value": m.group(0), "start": m.start(), "end": m.end()})
    # drop spans fully contained in an earlier-typed span
    spans.sort(key=lambda s: (s["start"], -(s["end"] - s["start"])))
    out = []
    for s in spans:
        if any(o["start"] <= s["start"] and s["end"] <= o["end"] and o is not s for o in out):
            continue
        out.append(s)
    return out
