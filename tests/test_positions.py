"""Position-program synthesis (FlashExtract-lite): learn a span extractor from (text, span) pairs, run it as the
`position` extractor kind, and let the inducer fall back on it for bindings nothing else explains."""
import yaml

from crystal.extract import positions as P
from crystal.extract.extractors import run_extractor
from crystal.induce.inducer import induce
from crystal.trace.store import Session

TEXTS = [
    "Alert fired on payments-api at 2026-08-06T11:37:00Z.\n\nError: PaymentGatewayTimeout: gateway did not respond\nSample trace: trace_id=858ef2d9f6008916df0ebcfbc4ee0b2b\nAffected pod: payments-api-7d9f8b6c4-x2k9q",
    "Alert fired on checkout-web at 2026-08-10T09:12:00Z.\n\nError: CartSnapshotMismatch: stale snapshot\nSample trace: trace_id=0ac2fb6a1a0e5b0d3e1f6f0b7c8d9e0f\nAffected pod: checkout-web-5c6d7e8f9-abcde",
    "Alert fired on ledger at 2026-08-11T01:02:00Z.\n\nError: LedgerDrift: sum mismatch\nSample trace: trace_id=ffffffffffffffffffffffffffffffff\nAffected pod: ledger-1a2b3c4d5-zzzzz",
]
NEW = "Alert fired on notifier at 2026-09-01T00:00:00Z.\n\nError: TemplateNotFound: missing\nSample trace: trace_id=0123456789abcdef0123456789abcdef\nAffected pod: notifier-9f8e7d6c5-qqqqq"


def _examples(values):
    return [(t, P.example_from_value(t, v)) for t, v in zip(TEXTS, values)]


def test_service_name_between_literal_anchors():
    progs = P.learn(_examples(["payments-api", "checkout-web", "ledger"]))
    assert progs, "no consistent program"
    prog = progs[0]
    assert P.apply(prog, NEW) == "notifier"
    assert all(P.apply(prog, t) == v for t, v in zip(TEXTS, ["payments-api", "checkout-web", "ledger"]))
    toks = prog["start"]["left"] + prog["start"]["right"] + prog["end"]["left"] + prog["end"]["right"]
    assert any(t.startswith("lit:") for t in toks), toks           # anchored on text around the span ...
    assert not any(t == "lit:-" for t in toks), toks                # ... never on a literal inside it


def test_value_after_literal_anchor():
    progs = P.learn(_examples(["858ef2d9f6008916df0ebcfbc4ee0b2b", "0ac2fb6a1a0e5b0d3e1f6f0b7c8d9e0f", "ffffffffffffffffffffffffffffffff"]))
    prog = progs[0]
    assert P.apply(prog, NEW) == "0123456789abcdef0123456789abcdef"
    assert any("=" in t for t in prog["start"]["left"]), prog       # the `trace_id=` anchor
    assert P.apply(prog, "trace_id=deadbeefdeadbeefdeadbeefdeadbeef pod=x") == "deadbeefdeadbeefdeadbeefdeadbeef"


def test_error_line_and_apply_all():
    progs = P.learn(_examples(["PaymentGatewayTimeout: gateway did not respond", "CartSnapshotMismatch: stale snapshot", "LedgerDrift: sum mismatch"]))
    prog = progs[0]
    assert P.apply(prog, NEW) == "TemplateNotFound: missing"
    assert P.apply_all(prog, TEXTS[0]) == ["PaymentGatewayTimeout: gateway did not respond"]


def test_negatives_prefer_anchor_over_ordinal():
    # the chosen hex value sits at a different ordinal position in each text: counting cannot explain it, an anchor can
    texts = ["pods a1b2c3d4 e5f6a7b8\nsuspect=deadbeef\nother=cafebabe",
             "pods 11112222\nsuspect=feedface\nother=0badf00d 22223333",
             "pods 33334444 55556666 77778888\nsuspect=abad1dea\nother=00000000"]
    progs = P.learn([(t, P.example_from_value(t, v)) for t, v in zip(texts, ["deadbeef", "feedface", "abad1dea"])])
    prog = progs[0]
    assert P.apply(prog, "pods 1 2 3 4 5\nsuspect=c0ffee00\nother=1") == "c0ffee00"
    assert any("suspect" in t or t == "lit:=" for t in prog["start"]["left"]), prog


def test_position_extractor_kind_and_yaml_roundtrip():
    prog = P.serializable(P.learn(_examples(["payments-api", "checkout-web", "ledger"]))[0])
    spec = yaml.safe_load(yaml.safe_dump({"from": "fields.description", "using": "position", "program": prog}))
    assert run_extractor(spec, {"fields": {"description": NEW}}) == "notifier"
    spec_all = {"from": "items[*].text", "using": "position", "program": prog, "all": True}
    assert run_extractor(spec_all, {"items": [{"text": TEXTS[0]}, {"text": NEW}]}) == ["payments-api", "notifier"]
    assert run_extractor(spec, {"fields": {"description": "nothing here"}}) is None


def test_unexplainable_span_yields_no_program():
    assert P.learn([]) == []
    # first word of one text, last word of the other, at different offsets: no context or ordinal agrees
    assert P.learn([("a b", (0, 1)), ("c d", (2, 3))]) == []
    # '|' and '-' are both PUNCT: a class program fits and generalises
    prog = P.learn([("ab|cd", (3, 5)), ("xy-zw", (3, 5))])[0]
    assert "PUNCT" in prog["start"]["left"] and P.apply(prog, "12+34") == "34"
    # a span starting inside a word shares no context with one after a separator: absolute positions are the fallback
    prog = P.learn([("ab|cd", (3, 5)), ("abcde", (3, 5))])[0]
    assert prog["start"] == {"abs": 3} and P.apply(prog, "12345") == "45"


# ---------------------------------------------------------------- inducer fallback
def _session(sid, key, svc, tid):
    text = f"Alert fired on {svc} at 2026-08-27T13:35:00Z.\n\nSample trace: trace_id={tid}\nAffected pod: {svc}-7d9f8b6c4-x2k9q"
    issue = {"server": "jira", "tool": "jira_get_issue", "input": {"issue_key": key}, "is_error": False,
             "output": {"key": key, "fields": {"summary": "elevated 5xx", "description": text, "created": "2026-08-27T13:47:00Z"}}}
    metrics = {"server": "chronosphere", "tool": "query_prometheus_range", "input": {"service": svc, "step_seconds": 300},
               "is_error": False, "output": {"status": "success", "data": {"result": [{"metric": {"service": svc}}]}}}
    return Session(session_id=sid, source="scripted", meta={"trigger": "t", "inputs": {"key": key}}, calls=[issue, metrics])


def test_inducer_resolves_binding_with_position_program():
    sessions = [_session("s1", "X-1", "ledger", "a" * 32), _session("s2", "X-2", "billing-core", "b" * 32),
                _session("s3", "X-3", "search-index", "c" * 32)]
    # no catalog: the gazetteer cannot explain the service name; it is not an id, a leaf, or a 'Label: value' line
    flow, report = induce(sessions, "t", catalog={})
    metrics = next(s for s in flow["steps"] if s["id"] == "metrics")
    assert metrics["args"]["service"] == "{{ issue.service }}", metrics
    assert report["unresolved"] == {}
    assert list(report["position_programs"]) == ["metrics.service"]
    assert report["position_programs"]["metrics.service"]["sessions"] == 3
    issue = next(s for s in flow["steps"] if s["id"] == "issue")
    spec = issue["extract"]["service"]
    assert spec["using"] == "position" and spec["from"] == "fields.description"
    assert run_extractor(spec, _session("x", "X-9", "notifier", "d" * 32).calls[0]["output"]) == "notifier"
    assert report["kinds"]["metrics"]["service"] == {"position": 3}
