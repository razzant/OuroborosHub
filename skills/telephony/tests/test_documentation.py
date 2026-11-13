"""What README, SKILL.md and the quick start promise is what the code and the manifest do."""
import json
import pathlib
import re
import xml.etree.ElementTree as ET

import pytest

from conftest import SKILL, import_plugin, run
from hubfake import ScriptedProvider
from phone import config as cfg
from phone import manager as mgr
from phone import sip_ua
from phone.sip_ua import SipConfig


DOCS = SKILL / "README.md", SKILL / "QUICKSTART.ru.md"


def test_twiml_bins_answer_on_bridge_and_bound_the_ringing():
    readme = (SKILL / "README.md").read_text(encoding="utf-8")
    xml_blocks = re.findall(r"```xml\s*(.*?)\s*```", readme, re.S)
    assert len(xml_blocks) >= 2
    roots = [ET.fromstring(b.replace("{{#e164}}{{To}}{{/e164}}", "+15550001001")) for b in xml_blocks]
    inbound = [r for r in roots if r.tag == "Response" and r.find("./Dial/Sip") is not None]
    outbound = [r for r in roots if r.tag == "Response" and r.find("./Dial") is not None
                and r.find("./Dial/Sip") is None]
    assert inbound and outbound
    in_dial = inbound[0].find("./Dial")
    assert in_dial.attrib.get("answerOnBridge") == "true"
    assert "@" in (in_dial.findtext("Sip") or "")
    out_dial = outbound[0].find("./Dial")
    assert out_dial.attrib.get("answerOnBridge") == "true"
    assert int(out_dial.attrib["timeout"]) >= 45
    assert (out_dial.attrib.get("callerId") or "").startswith("+")
    assert (out_dial.text or "").strip() == "+15550001001"

    quickstart = (SKILL / "QUICKSTART.ru.md").read_text(encoding="utf-8")
    dial_snippets = re.findall(r"`(<Dial\b[^`]*</Dial>)`", quickstart)
    assert len(dial_snippets) >= 2
    parsed_dials = [ET.fromstring(s.replace("{{#e164}}{{To}}{{/e164}}", "+15550001001")) for s in dial_snippets]
    assert any(d.attrib.get("answerOnBridge") == "true" and d.find("Sip") is not None for d in parsed_dials)
    assert any(d.attrib.get("answerOnBridge") == "true" and int(d.attrib.get("timeout", "0")) >= 45
               and (d.text or "").strip() == "+15550001001" for d in parsed_dials)


# -- What the documents promise is what the code does ------------------------------------------------------

def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_documents_state_what_the_code_enforces(tmp_path):
    from phone import policy, rtp
    from phone.policy import Policy, PolicyRefusal, normalize_number
    from test_plugin_manifest import manifest_data

    readme = _flat((SKILL / "README.md").read_text(encoding="utf-8"))
    skill_md = _flat((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    quick = _flat((SKILL / "QUICKSTART.ru.md").read_text(encoding="utf-8"))
    plugin = import_plugin()
    label = next(f["label"] for f in plugin.SETTINGS_FIELDS if f["name"] == "trusted_sources")

    # what a line administrator needs: ringing, ports for a firewall, keys
    ring = policy.DEFAULT_LIMITS["ring_timeout_seconds"]
    assert f"| Ringing | {ring} s both ways" in readme and f"Гудки длятся не дольше {ring} с" in quick
    low, high = SipConfig(registrar="pbx.test", username="u").rtp_port_range
    assert f"one even UDP port per call in {low}-{high}" in readme and f"{low}–{high}" in quick
    assert f"a keepalive every {int(sip_ua.KEEPALIVE_SECONDS)} s while registered" in readme
    assert f"{rtp.DTMF_DIGIT_MS} ms each with {rtp.DTMF_GAP_MS} ms between" in readme and "1-32 keys" in readme
    m = mgr.CallManager(tmp_path, cfg.DEFAULTS, "", None, provider=ScriptedProvider())
    assert run(m.dtmf("1" * 32))["code"] == "PHONE_NOT_CONNECTED"  # 32 keys are a valid request
    assert run(m.dtmf("1" * 33))["code"] == "PHONE_DTMF_INVALID"

    # trusted sources: IPv4 only, nothing wider than the validator's /8
    wide = f"/{cfg.MIN_TRUSTED_PREFIX}"
    for text in (readme, skill_md, quick, label):
        assert wide in text and "IPv6" in text, text[:80]
    with pytest.raises(ValueError):
        cfg.from_form(cfg.load(tmp_path), {"trusted_sources": "2001:db8::/32"})

    # numbers: international form only; the Russian premium ranges as the policy blocks them
    for prefix in ("+7809", "+7803"):
        assert prefix in policy.BLOCKED_PREFIXES and f"`{prefix}`" in quick
        with pytest.raises(PolicyRefusal) as refused:
            Policy(cfg._merge(cfg.DEFAULTS, {"outbound": {"allowed_prefixes": ["+7"]}}), tmp_path).check_outbound(
                prefix + "5550000", 0.0)
        assert refused.value.code == "PHONE_NUMBER_BLOCKED"
    with pytest.raises(PolicyRefusal) as national:
        normalize_number("8-809-555-00-00")
    assert national.value.code == "PHONE_NUMBER_INVALID" and "запись вида `8…` линия не примет" in quick

    # forking is handled (first answer kept, the others ACKed and hung up), the API and keys are named
    assert "call forking" not in readme and "keeps the first answer" in readme
    assert "`refresh_companion_process` (2.4)" in readme and "(`OPENAI_API_KEY`)" in readme

    # the catalog entry says what the manifest says
    entry_path = SKILL.parent.parent / "entries" / "telephony.json"
    if entry_path.exists():
        entry, manifest = json.loads(entry_path.read_text(encoding="utf-8")), manifest_data()
        for key in ("description", "when_to_use", "version"):
            assert entry[key] == str(manifest[key]), key


def test_the_readme_states_the_stop_deadline_in_use():
    readme = (pathlib.Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert f"within {mgr.STOP_SECONDS:g} s of the supervisor's SIGTERM (it kills after 5 s)" in readme
