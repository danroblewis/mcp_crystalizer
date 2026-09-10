

def test_detector_catalog_covers_common_parameter_shapes():
    """The hand-written catalog is the weak point, so it is worth breadth AND precision: every entry below must
    type correctly, and ordinary prose must type as nothing at all."""
    from crystal.extract import ids

    cases = {
        "v1.2.3": "semver", "git@github.com:acme/app.git": "git_remote", "acme/app#42": "github_ref",
        "crystal/app/dossier.py:42": "file_line", "com.acme.billing.Ledger": "java_class",
        "sha256:" + "a" * 64: "image_digest", "arn:aws:s3:::bucket/key": "aws_arn",
        "i-0abc1234def56789a": "ec2_instance", "s3://logs/2026/": "s3_uri", "gs://bucket/x": "gcs_uri",
        "deploy/payments-api": "k8s_resource", "nginx-7d8f4c9b5d-x2k4m": "k8s_pod",
        "10.0.0.0/8": "cidr", "00:1b:44:11:3a:b7": "mac_address", "fe80::1": "ipv6",
        "http_requests_total": "prom_metric", "now-15m": "relative_time", "PT15M": "iso_duration",
        "*/15 * * * *": "cron_expr", "#a1b2c3": "hex_color",
    }
    for value, kind in cases.items():
        assert ids.type_of(value) == kind, (value, ids.type_of(value))

    for word in ("the quick brown fox", "what-requires-being", "a sentence about being here",
                 "1 2 3 4 5", "12:30:45", "defaced"):
        assert ids.type_of(word) is None, word


def test_credentials_are_detected_so_they_can_be_redacted():
    """A flow that carries a token is a leak: credentials are recognised, excluded from parameters, and redacted."""
    from crystal.extract import ids

    token = "ghp_" + "a" * 36
    assert ids.type_of(token) == "github_token" and ids.is_secret("github_token")
    assert ids.redact(f"call with {token} please") == "call with [redacted] please"
    assert ids.redact("Authorization: Bearer " + "x" * 30).endswith("[redacted]")
    assert not ids.is_secret("jira_key")


def test_a_credential_never_becomes_a_flow_input():
    from crystal.trace.transcripts import prompt_inputs

    class C:
        def __init__(self, inp):
            self.input = inp

    token = "ghp_" + "b" * 36
    text = f"Use the token {token} against repo acme/app."
    got = prompt_inputs(text, [C({"token": token, "repo": "acme/app"})])
    assert token not in got.values()
    assert got.get("repo") == "acme/app"
