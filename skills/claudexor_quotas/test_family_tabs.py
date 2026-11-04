"""The mark tab of the kit (kit: mark tab) and the head's family tabs that wear it. The count is red, with a small red dot
between the name and the count; the picked tab has no grey outline: the model's mark just grows and turns red; the word does not
grow and the count does not grow. The icons, the logic, the number and the dots all live in the widget's kit, everything in capsules.

The look is the kit's, written once under /* kit: mark tab */ with a usage contract that opens the section (what it is, when, how,
states, motion, tokens, accessibility, what not to do); the head's family tabs are its first use and add only their wiring (the label,
the title, the focus key, the click) and their place (.harness-seg). markTab({key, mark, name, count, pressed}), markRow(tabs),
markPreviewed(tab), markStart(row, fromKey, toKey, lit) and settleMarks() stand together in widget.js and are the only builders of a
tab, its mark, its dot, its number and its row.
A tab is no plate: no border, no ground and no shadow in any state (rest, hover, focus, press, picked), and its text keeps the regular
weight in every one: a pick changes no word. Inside it stand the model's mark (.tab-mark-icon: the drawing, or the initial where the
widget has none), the name, and, with a number, a dot (.tab-mark-dot, the accent, out of the reading order) and the number
(.tab-mark-count, the accent). The pick is told by the mark alone: picked (aria-pressed=true) it turns the accent and grows to
--mark-on; under the pointer or the keyboard a tab not picked shows the red without the growth, a preview of the pick; the colour and
the size fade (--dur-5 on --ease), and under reduced motion they do not.
The row is redrawn at a pick, so a new mark has no look to fade from. On the reader's own pick of a family the two marks that change
are drawn in the look they had a moment ago (the start state: .tab-mark-was-off on the mark of the tab picked, the tab's ink at its
size; .tab-mark-was-lit instead of it when that tab was showing its preview at the click - the pointer on it or the keyboard on it,
its mark was red then - so it starts from the red at its size and only the size moves, no flicker through the ink; .tab-mark-was-on
on the mark of the tab left, the picked look) and the next animation frame takes the class off - after a read of the look, so the
browser holds it as the look the change starts from (Chromium makes no transition without that read) - and the mark's own transition
carries colour AND size. There are no keyframes: the canon lets a keyframe move transform and opacity alone (test_style_canons.py),
so a keyframe could not fade the red. A redraw inside the pick's span draws the start state again with a negative transition-delay (as
far on as the span has gone); any other drawing, the pick of a window, reduced motion and a page with no frames to take the class off
with draw none.
The rules are read from the STYLE array of widget.js; the markup and the start state run in the widget's own fake-DOM run (the frame
is a stub the test calls), and the helpers on their own over a row of any number of tabs, which the head (three families) cannot
show; what a browser really does (computed colours, the transitions, the finger's height, reduced motion, the pointer and the
keyboard on a tab) is checked in Chromium and WebKit where Playwright has them. The colour of the number is the accent at 12px: 3:1 on
the head's ground in both themes, as the lit capital of the text action (the one red gives 3.81 on the dark ground and 4.76 on the
light one); the number is also said by the tab's label, so the colour tells nothing alone.

The kit's second kind of tab is the number tab (.tab-mark-num: no mark of a model, its number is it; the window row of the top tile
wears it): a tab of a tablist (role=tab, aria-selected), told by the colour of its dot and number alone, as the design sheet
("Footnote on the number") draws it: the name is the ink in EVERY state (--text-primary, never a quieter grey); the dot and the
number are --text-meta at rest and --accent when the tab is picked; under the pointer or the keyboard a tab not picked turns its dot and
number to the ink, never to the red; the star after the number (.tab-mark-note) is the ink in the semibold weight at rest and under the
pointer, and the red with the number when the tab is picked. Nothing grows and no word changes its weight: a pick, a preview and a
focus move colour alone (--dur-5 on --ease, none under reduced motion). The rules are pinned here (the colour roles, the specificity
of the start states, no growth, no red on a preview, the reduced pair), the markup by the helpers alone, and the colours a browser
really computes in Chromium and WebKit over the real window row, in both themes."""
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_quotas
from test_order_focus import REAL_PAGE_STUB
from test_reserve import _node, with_i18n
from test_style_canons import _compounds, _declarations, _rules, _sections, _selectors, _style, _tokens
from test_text_actions import _contrast, _css_rgb, _rgb, _specificity, _themes
from test_window_tabs import fixture_file  # noqa: F401  (the fixture: single-family views of the real backend)

WIDGET = Path(__file__).with_name("widget.js")
KIT = "kit: mark tab"
TOUCH = "kit: touch"
HEAD = "capsule: head"
MOTION = "kit: motion"
ROW = ".tab-mark-row"
CLASSES = ["tab-mark", "tab-mark-count", "tab-mark-dot", "tab-mark-icon", "tab-mark-name", "tab-mark-note",
           "tab-mark-num", "tab-mark-rail", "tab-mark-ride", "tab-mark-ring", "tab-mark-row", "tab-mark-slide", "tab-mark-still",
           "tab-mark-unit", "tab-mark-was-lit", "tab-mark-was-off", "tab-mark-was-on", "tab-mark-was-press", "tab-mark-yield"]


def _all_rules():
    """[(section, media, selector, {prop: value})]: every rule of the style outside the tokens, one entry per selector."""
    out = []
    for name, text in _sections(_style()).items():
        if name == "tokens":
            continue
        for media, listing, body in _rules(text):
            decls = {p.strip(): v.strip() for p, v in _declarations(body)}
            out.extend((name, media, selector, decls) for selector in _selectors(listing))
    return out


def _one(selector, media="", section=KIT):
    found = [d for n, m, s, d in _all_rules() if s == selector and m == media and n == section]
    assert len(found) == 1, "%s under %r in %s: %d rules" % (selector, media, section, len(found))
    return found[0]


def _tab_rules():
    """[(media, selector, decls)] of every rule that names the tab itself (.tab-mark, not .tab-mark-icon ...), in any state, in any
    section."""
    return [(m, s, d) for _, m, s, d in _all_rules() if re.search(r"\.tab-mark(?![\w-])", s)]


def _mark_rules():
    """[(section, media, selector, decls)] of every rule that names a class of the mark tab (.tab-mark and .tab-mark-...)."""
    return [(n, m, s, d) for n, m, s, d in _all_rules() if ".tab-mark" in s]


def _contract():
    """The comment that opens the kit section: its // lines from the marker to the first rule, the slashes off, joined by a space."""
    lines = _style().splitlines()
    first = next(i for i, line in enumerate(lines) if line.strip() == "/* %s */" % KIT) + 1
    out = []
    for line in lines[first:]:
        if not line.strip().startswith("//"):
            break
        out.append(line.strip()[2:].strip())
    return " ".join(out)


def _function(source, name):
    """The text of `function name(...) {...}` as widget.js writes it: four spaces in, closed by a brace in the same column."""
    found = re.search(r"\n    function %s\(.*?\n    }\n" % name, source, re.S)
    assert found, "function %s is not in widget.js" % name
    return found.group(0)


# ---------------------------------------------------------------------------
# The section: where it stands, what is the kit's and what the head keeps, the contract and the tokens it names.

def test_the_section_stands_after_the_text_action_and_the_kit_holds_every_rule_of_the_tab_but_the_touch_line_and_the_ghost_ink():
    css = _style()
    sections = _sections(css)
    order = list(sections)
    # The rail's tab is the number tab with a ring: its rules stand in the kit's ring and rail (after the mark tab), which the tab's kit includes.
    assert order.index(KIT) == order.index("kit: text action") + 1 and order.index(KIT) + 1 == order.index("kit: ring")
    assert order.index("kit: ring") + 1 == order.index("kit: rail") and order.index("kit: rail") + 1 == order.index("kit: segmented control")
    # Every rule that names a class of the tab is in the kit section, but the kit's touch line (one list for all the kit's parts) and the
    # ink of the head's ghosts, which is the head's own; nothing of the tab is left in the head under its old names.
    outside = sorted((n, m, s) for n, m, s, _ in _mark_rules() if n not in (KIT, "kit: rail"))
    assert outside == [(TOUCH, "@media(pointer:coarse)", ".tab-mark")], outside
    assert not re.search(r"harness-(btn|mark|dot|count|name)|(?<!tab-)mark-was-", css), "no old name of the tab is left in the style"
    code = "\n".join(line for line in WIDGET.read_text(encoding="utf-8").replace(css, "").splitlines() if not line.strip().startswith("//"))
    assert not re.search(r"harness-(btn|mark|dot|count|name)|(?<!tab-)mark-was-", code), "nor in the code"
    # What the head keeps of the tabs: the place of the row and the initial that stands as a mark (the ghosts wear the base's disabled ink).
    head = {s: d for n, m, s, d in _all_rules() if n == HEAD and m == "" and re.search(r"harness|tab-mark", s)}
    assert head == {".harness-seg": {"flex": "1 1 280px"},
                    ".harness-initial": {"display": "inline-flex", "align-items": "center", "justify-content": "center",
                                         "width": "var(--mark-px)", "height": "var(--mark-px)", "font-size": "var(--type-meta)", "font-weight": "var(--weight-semibold)"}}, head
    # The row is the kit's: a flex line, the tabs centred on each other, the gap they had; the head gives it only its place.
    assert _one(ROW) == {"display": "flex", "align-items": "center", "flex-wrap": "wrap", "gap": "var(--space-1)"}
    assert [name for name, section in sections.items() if ROW in section] == [KIT, "kit: rail"]   # the rail turns the row into a column
    assert not [n for n, _, s, _ in _all_rules() if n != HEAD and "harness-seg" in s]
    # The markup: the kit row first, then the head's place, as the text action's row wears .action-seg.
    source = WIDGET.read_text(encoding="utf-8")
    assert "seg.className += ' harness-seg';" in source and "el('div', 'harness-seg')" not in source


def test_the_page_button_and_the_option_are_as_they_were_and_the_tab_wears_neither_but_is_lifted_under_a_finger_by_the_kit():
    source, css = WIDGET.read_text(encoding="utf-8"), _style()
    sections = _sections(css)
    # Nothing names .btn: no rule of the style, no class in the markup (.btn-word, .btn-compact ... are other classes).
    assert [(n, s) for n, _, s, _ in _all_rules() if re.search(r"\.btn(?![\w-])", s)] == []
    code = source.replace(css, "")
    drawing = _function(code, "familyTab") + _function(code, "renderHarnessSeg")
    assert not re.search(r"""['"]btn[\s'"]|btn harness|' active'""", drawing), "no .btn on a tab, no class of the pick: it is aria-pressed"
    # The page button and the option are the same look as before the tab left it: the same declarations, word for word.
    kit = sections["kit: buttons and pills"]
    assert (".pill-btn,.seg-opt{display:inline-flex;align-items:center;justify-content:center;gap:var(--space-1h);min-height:var(--row-h);"
            "padding:var(--space-1) var(--space-2h);border:var(--border-w) solid transparent;border-radius:var(--radius-sm);background:transparent;"
            "color:var(--text-primary);font-size:var(--type-meta);line-height:var(--lh-ui);position:relative}") in kit
    assert ".pill-btn:hover,.seg-opt:hover{background:var(--inset)}" in kit
    assert ".pill-btn.on,.seg-opt.active{background:var(--inset);border-color:var(--edge);font-weight:var(--weight-semibold)}" in kit
    assert ".pill-btn{border-color:var(--edge);background:var(--surface)}" in kit
    # Under a finger the kit lifts the same parts it did, and the tab with them: one line for the kit, none of the head's.
    assert ("@media(pointer:coarse){.pill-btn,.seg-opt,.btn-compact,.btn-compact-wide,.btn-word,.tab-mark,.cover-link,"
            ".rows-menu summary,.rows-menu label{min-height:var(--tap-h)}.btn-word{min-width:var(--tap-h)}") in sections[TOUCH]
    # ...and, in the same rule of the kit, the row of bars is --tap-h high with no gap of its own, and each bar of it wears half the gap as a
    # transparent border at either side (the ground clipped to the padding box, the least and the most of its width grown by the gap): a touch
    # between two bars lands on one of them.
    assert (".bars{min-height:var(--tap-h);gap:0}.bars>.bar{border-inline:calc(var(--bar-gap) / 2) solid transparent;background-clip:padding-box;"
            "min-width:calc(var(--bar-min) + var(--bar-gap));max-width:calc(var(--bar-max) + var(--bar-gap))}}") in sections[TOUCH]
    assert ".bar:before" not in sections[TOUCH]
    # The old tab of the graph's head (.tl-tab) is gone from the list, the style and the markup: the number tab is the kit's tab.
    assert ".tl-tab" not in css and "tl-tab" not in source.replace(css, "")


def test_the_section_opens_with_the_usage_contract_and_names_every_token_its_rules_read():
    contract = _contract()
    # The eight parts of the contract, once each and in this order.
    parts = ["What it is:", "When to use it:", "How to use it:", "States:", "Motion:", "Tokens it reads:", "Accessibility:", "Not to do:"]
    at = [contract.find(part) for part in parts]
    assert all(i >= 0 for i in at) and at == sorted(at), dict(zip(parts, at))
    assert all(contract.count(part) == 1 for part in parts)
    for phrase in (
            "a button that is one choice of a row, told by its mark", "no plate: no border, no ground and no shadow in any state",
            "its text keeps the meta size and the regular weight in every one, so a pick changes no word",
            "a row of choices that each have a mark and of which one is picked", "must move no word", "Never for a command",
            "markTab({key, mark, name, count, pressed})", "markRow(tabs)", "markStart(row, fromKey, toKey, lit)", "markPreviewed(tab)",
            "settleMarks()", "nothing else builds a tab, a mark, a dot, a number or a row", "its wiring", "its place to a row",
            "at rest the mark is in the tab's ink (--text-primary)", ":hover, :focus-visible; not when it is disabled",
            "shows the same mark in --accent, not grown", "Picked (aria-pressed=true) the mark is in --accent and grown to --mark-on",
            "The dot and the number are in --accent in every state",
            "fade together by the mark's own transition (--dur-5 on --ease)", "a new mark has no look to fade from",
            "so that only the size moves", "the next frame takes the class off after a read of the look",
            "a start state and a transition, never keyframes", "the canon lets a keyframe move transform and opacity alone",
            "Under reduced motion nothing fades and nothing moves (the picked mark is simply drawn at its size) and no start state is drawn",
            "aria-pressed", "the row is a group", "an aria-label that says what it counts", "the dot is aria-hidden",
            "44px under a finger", "its ring is the base ring", "a tab that is disabled previews nothing",
            "no plate, no border, no shadow and no ground in any state", "no change of the text's weight or size",
            "no colour of its own", "the accent is the mark's, the dot's and the number's, nothing else", "no keyframes",
            # the number tab (the window row): the ink for the name in every state, the dot and number tell the pick, the star follows
            "a second kind, the number tab: no mark of a model, its number (with the dot before it) is its mark",
            "its name is in --text-primary, the ink, in every state", "at rest they are in --text-meta",
            "they go to --text-primary, not red", "picked (aria-selected=true) they are in --accent",
            "Its note (the star) is in the ink and the semibold weight at rest and previewed, and in --accent when the tab is picked, with the number",
            "Nothing grows", "Its text never wraps (white-space: nowrap)", "a tab of a tablist (role=tab, aria-selected)",
            "the number tab changes colour only, never its size or its weight, and shows the red on a pick alone, never on a preview"):
        assert phrase in contract, phrase
    assert "--text-secondary" not in contract, "the name of the number tab is the ink in every state: no quieter grey is read"
    # Its list of tokens is the rules' own: every token the rules of the tab read (the touch line included) and no other, each declared.
    # The contract names a token, it does not read one: the accent is read by five rules.
    named = set(re.findall(r"--[a-z0-9-]+", contract.split("Tokens it reads:")[1].split("Accessibility:")[0]))
    read = {token for n, m, s, decls in _mark_rules() if n in (KIT, TOUCH) for value in decls.values()
            for token in re.findall(r"var\((--[a-z0-9-]+)", value)}
    assert named == read, "the contract lists %s that the rules do not read; the rules read %s that it does not list" % (
        sorted(named - read), sorted(read - named))
    assert named <= set(_tokens(_sections(_style())))
    assert "var(" not in contract
    # The token of the scale says whose it is: the kit's (its comment in the token block names the mark tab and the class it grows).
    assert "(the kit's mark tab: .tab-mark-icon, the picked one and the one just left)" in _style()


# ---------------------------------------------------------------------------
# The rules: no .btn, no plate, no weight of its own in any state, the accent and the mark, the start state and its pair.

def test_no_rule_of_a_tab_draws_a_plate_a_border_a_shadow_a_ring_or_a_weight_in_any_state():
    tabs = _tab_rules()
    base = [d for m, s, d in tabs if (m, s) == ("", ".tab-mark")]
    assert len(base) == 1
    base = base[0]
    # The tab: an inline flex line of its parts, 5px between them, the row's height, the padding it had; no border and no
    # ground (the kit's transparent border and plate are not worn, so no state can fill them); the ink, the size and the weight of
    # its text; the ring's corner (the ring is the base's).
    assert base == {
        "display": "inline-flex", "align-items": "center", "gap": "5px", "min-height": "var(--row-h)",
        "padding": "var(--space-1) var(--space-1h)", "border": "0", "border-radius": "var(--radius-sm)", "background": "none",
        "appearance": "none", "color": "var(--text-primary)", "font-size": "var(--type-meta)",
        "font-weight": "var(--weight-regular)", "line-height": "var(--lh-ui)"}, base
    # The tab itself has no state: no :hover, :focus, :active, .active, [aria-pressed] on it. What it has: the ghost's ink, and
    # the finger's height.
    states = sorted((m, s) for m, s, _ in tabs if re.fullmatch(r"\.tab-mark(\.[\w-]+)?", _compounds(s)[-1]))
    assert states == [("", ".tab-mark"), ("@media(pointer:coarse)", ".tab-mark")], states
    # Every other rule that names the tab (the ghost, the finger, the mark inside it) draws none of: a plate, a line, a shadow, a
    # ring, a text weight, a size, a box. Only the ink and the transform of the mark, the ghost's ink, the finger's height.
    forbidden = re.compile(r"^(background.*|border(?!-radius).*|box-shadow|outline.*|font.*|padding.*|margin.*|width|height|"
                           r"letter-spacing|text-decoration.*|opacity|filter)$")
    for media, selector, decls in tabs:
        if (media, selector) != ("", ".tab-mark"):
            assert not [p for p in decls if forbidden.match(p)], (selector, decls)
    # The weight of the text never changes: it is written for the tab (regular), the number (regular), the dot (semibold, the dot's
    # own) and the star after the number (semibold, a sign's own, the number tab's), each once and in no state; the model's initial is a
    # mark, semibold in every state. Nothing of the number tab sets a weight in a state (a pick or a preview), and none sets a size.
    weights = {s: d["font-weight"] for _, _, s, d in _all_rules() if "font-weight" in d and re.search(r"harness|tab-mark", s)}
    assert weights == {".tab-mark": "var(--weight-regular)", ".harness-initial": "var(--weight-semibold)",
                       ".tab-mark-dot": "var(--weight-semibold)", ".tab-mark-count": "var(--weight-regular)",
                       ".tab-mark-note": "var(--weight-semibold)",
                       # the rail's tab (kit: rail): its share semibold, its narrow word medium (the picked row takes the ink, not a weight)
                       ".tab-mark-rail .tab-mark-count": "var(--weight-semibold)",
                       ".tab-mark-rail .tab-mark-name": "var(--weight-medium)",
                       # the wide rail's short name is the badge in the gap of the ring: semibold in every state, never a weight of its own per state
                       ".tab-mark-ring .tab-mark-name": "var(--weight-semibold)"}, weights
    # And no rule at all reads a state of the tab for a plate: nothing of the tab is :hover-, :active- or .active-painted.
    assert not [s for _, s, d in tabs if re.search(r":hover|:active|\.active|:focus(?!-visible)", s)
                and any(p.startswith(("background", "border", "box-shadow", "outline")) for p in d)]


def test_the_accent_and_the_mark_picked_it_grows_previewed_it_only_turns_and_the_token_is_declared_and_read():
    css, sections = _style(), _sections(_style())
    dark, light = _themes()
    assert dark["--mark-on"] == "1.2" and light["--mark-on"] == "1.2" and _tokens(sections)["--mark-on"] == "1.2"
    assert "--mark-on" not in sections["tokens"].split(":root[data-theme=light]{", 1)[1], "a scale, the same in both themes"
    # Read where it is used: the picked mark, in one rule with the mark just left in its start state (no keyframes read it): no other rule reads it.
    # The picked bar of a plate does not grow (the row of the bars scrolls, and a scroll box would cut the growth): it is the accent in its fill and its
    # number and a ring drawn inside it, so no rule of a bar reads the scale and none gives the picked bar a transform.
    readers = {s for _, _, s, d in _all_rules() if any("var(--mark-on)" in v for v in d.values())}
    assert readers == {".tab-mark[aria-pressed=true] .tab-mark-icon", ".tab-mark-icon.tab-mark-was-on",
                       ".tab-mark-rail[aria-selected=true] .tab-mark-ring", ".tab-mark-ring.tab-mark-was-on",
                       ".tab-mark-rail[aria-selected=true] .ring", ".tab-mark-ring.tab-mark-was-on .ring",
                       ".tab-mark-rail[aria-selected=true]:active .tab-mark-ring", ".tab-mark-rail[aria-selected=true]:active .ring",
                       }, readers   # (--lead-top, the thread from the picked family's mark down to the first ring, reads it too: in the tokens)
    assert css.count("var(--mark-on)") == 10, ("the press of the picked row's ring (its size less --press: wide, narrow, and the same size again in the pair of reduced motion, wide and narrow) four times; the tab's mark once; the rail's ring once (and once more for the ring itself on the narrow shell, where the box of the ring is no box); "
                                              "(the picked bar does not grow: its two readings, wide and under 640px, are gone); "
                                              "the thread under the grown mark once (--lead-top), how far a ring that is picked has grown once (--ring-grow, which the thread "
                                              "over it and the end of the thread from the head read) and how far its badge has gone down once (--badge-grow, which the thread "
                                              "under it reads); nothing else reads it")
    assert not [s for _, _, s, d in _all_rules() if re.search(r"\.bar\.sel", s) and "transform" in d], "the picked bar keeps its size: no rule gives it a transform"
    assert "calc(1 / var(--mark-on))" not in css, "no counter-scale of a '?'"
    picked = ".tab-mark[aria-pressed=true] .tab-mark-icon"
    assert _one(picked) == {"color": "var(--accent)", "transform": "scale(var(--mark-on))"}
    # The preview: a tab not picked, under the pointer or the keyboard: the red alone, no growth. A tab that is disabled (the ghosts)
    # previews nothing. The keyboard's preview is always on; the pointer's is off in a row that is still (.tab-mark-still: a pick has
    # moved the tabs under a pointer that stands still, and the first move of the pointer takes the class off).
    keyboard = ".tab-mark:focus-visible:not([aria-pressed=true]):not(:disabled) .tab-mark-icon"
    pointer = ".tab-mark-row:not(.tab-mark-still) .tab-mark:hover:not([aria-pressed=true]):not(:disabled) .tab-mark-icon"
    assert _one(keyboard) == _one(pointer) == {"color": "var(--accent)"}
    assert [listing for _, listing, _ in _rules(_sections(css)[KIT]) if keyboard in listing] == [keyboard + "," + pointer], (
        "one rule for the two ways of a preview: the red cannot differ")
    assert ":hover" not in keyboard and ":hover" in pointer and ".tab-mark-still" not in keyboard, "the keyboard is never held"
    # Nothing else of the tab lights: no rule that names the tab and a state reaches anything but the mark (the picked, the
    # previewed, and the picked tab's mark in its start states).
    lit = [s for m, s, d in _tab_rules() if re.search(r":hover|:focus|:active|aria-pressed", s)]
    assert sorted(lit) == sorted([picked, keyboard, pointer, picked + ".tab-mark-was-off", picked + ".tab-mark-was-lit"]), lit
    assert all(re.search(r" \.tab-mark-icon(\.tab-mark-was-(off|lit))?$", s) for s in lit), lit
    # The fade: colour and size, one duration and one curve, on the mark alone; the tab's own words do not move.
    assert _one(".tab-mark-icon") == {"transition": "color var(--dur-5) var(--ease),transform var(--dur-5) var(--ease)"}
    assert _tokens(sections)["--dur-5"] == "220ms"
    # The dot and the number are the accent, the dot semibold and the number regular on tabular digits.
    assert _one(".tab-mark-dot") == {"color": "var(--accent)", "font-weight": "var(--weight-semibold)"}
    assert _one(".tab-mark-count") == {"color": "var(--accent)", "font-weight": "var(--weight-regular)",
                                       "font-variant-numeric": "tabular-nums"}
    # The mark, the dot and the number keep 3:1 on the head's ground in both themes (the one red of the shell).
    for theme in (dark, light):
        ground = _rgb(theme["--shell-bg"], (0, 0, 0))
        assert _contrast(_rgb(theme["--accent"], ground), ground) >= 3.0, theme["--accent"]


NUM = ".tab-mark-num"
FIGURE = ".tab-mark-num .tab-mark-count"
FIGURE_PICKED = ".tab-mark-num[aria-selected=true] .tab-mark-count"
FIGURE_WAS_ON = ".tab-mark-num .tab-mark-count.tab-mark-was-on"
FIGURE_WAS_OFF = FIGURE_PICKED + ".tab-mark-was-off"
FIGURE_WAS_LIT = FIGURE_PICKED + ".tab-mark-was-lit"
FIGURE_PREVIEW = ".tab-mark-num:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .tab-mark-count"
STAR = ".tab-mark-note"
STAR_PICKED = ".tab-mark-num[aria-selected=true] .tab-mark-note"
STAR_WAS_ON = ".tab-mark-num .tab-mark-note.tab-mark-was-on"
STAR_START = ".tab-mark-num[aria-selected=true] .tab-mark-note:is(.tab-mark-was-off,.tab-mark-was-lit)"


def _spec(selector):
    """The specificity of a selector with :is() and :not() counted as their heaviest argument, as CSS counts them."""
    ids = classes = elements = 0
    rest = selector
    while True:
        found = re.search(r":(?:is|not)\(", rest)
        if not found:
            break
        depth, i = 1, found.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(rest[i], 0)
            i += 1
        args, depth, current = [], 0, ""
        for ch in rest[found.end():i - 1]:
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if ch == "," and depth == 0:
                args.append(current)
                current = ""
            else:
                current += ch
        args.append(current)
        heaviest = max(_spec(arg.strip()) for arg in args)
        ids, classes, elements = ids + heaviest[0], classes + heaviest[1], elements + heaviest[2]
        rest = rest[:found.start()] + rest[i:]
    base = _specificity(rest)
    return (ids + base[0], classes + base[1], elements + base[2])


def _number_rules():
    """[(media, selector, decls)] of every rule of the kit section that names the number tab or one of its parts."""
    return [(m, s, d) for n, m, s, d in _all_rules() if n == KIT and re.search(r"\.tab-mark-(num|note)(?![\w-])|\.tab-mark-(dot|count)", s)]


def test_the_number_tab_names_in_the_ink_tells_the_pick_by_its_dot_and_number_and_star_alone_and_never_reddens_on_a_preview_or_grows():
    sections, tokens = _sections(_style()), _tokens(_sections(_style()))
    # The name is the ink in every state: the tab's own (.tab-mark, --text-primary), and nothing on the number tab sets another colour
    # for the name - no quieter grey at rest, no second ink when picked. The rule of the tab itself keeps the text on one line.
    assert _one(".tab-mark")["color"] == "var(--text-primary)"
    assert _one(NUM) == {"white-space": "nowrap"}
    on_the_tab = [(m, s) for m, s, d in _number_rules() if re.search(r"\.tab-mark-num(?![\w-])(?:\[[^\]]*\]|:[\w-]+(?:\([^)]*\))*)*$", s)]
    assert on_the_tab == [("", NUM)], "no rule of a state on the tab itself: only the dot, the number and the star change: %s" % on_the_tab
    assert "text-secondary" not in "".join(v for _, _, d in _number_rules() for v in d.values())
    # The dot and the number: --text-meta at rest (as the sheet: .tl-tab-dot and .wt-num), the dot semibold, the number regular on
    # tabular digits (the kit's own rules), the red when the tab is picked (and on the mark of the tab just left, in its start state).
    assert _one(FIGURE) == {"color": "var(--text-meta)", "transition": "color var(--dur-5) var(--ease)"}
    assert _one(FIGURE_PICKED) == _one(FIGURE_WAS_ON) == {"color": "var(--accent)"}
    assert [l for _, l, _ in _rules(sections[KIT]) if "tab-mark-was-on" in l and ".tab-mark-count" in l and ".tab-mark-note" not in l] == [FIGURE_PICKED + "," + FIGURE_WAS_ON]
    # A preview (pointer or keyboard on a tab not picked, not aria-disabled): the ink, never the red; the picked tab previews nothing.
    assert _one(FIGURE_PREVIEW) == {"color": "var(--text-primary)"}
    assert ":hover" in FIGURE_PREVIEW and ":focus-visible" in FIGURE_PREVIEW
    previews = [(s, d) for m, s, d in _number_rules() if re.search(r":hover|:focus", s)]
    assert [s for s, _ in previews] == [FIGURE_PREVIEW], previews
    assert all("var(--accent)" not in v for _, d in previews for v in d.values()), "no red on a preview"
    # The star: the ink and the semibold weight, a hair of air before it; the red with the number when the tab is picked.
    assert _one(STAR) == {"margin-left": "1px", "font-weight": "var(--weight-semibold)", "color": "var(--text-primary)",
                          "transition": "color var(--dur-5) var(--ease)"}
    assert _one(STAR_PICKED) == _one(STAR_WAS_ON) == {"color": "var(--accent)"}
    assert [l for _, l, _ in _rules(sections[KIT]) if "tab-mark-note" in l and "tab-mark-was-on" in l] == [STAR_PICKED + "," + STAR_WAS_ON]
    assert _one(STAR_START) == {"color": "var(--text-primary)"}
    # The start states beat the rules they start from by weight, not by place: the picked tab's dot, number and star start from the
    # quiet look (was-off) or from the ink of the preview (was-lit); the tab left starts from the red (was-on), and the star with them.
    assert _one(FIGURE_WAS_OFF) == {"color": "var(--text-meta)"} and _one(FIGURE_WAS_LIT) == {"color": "var(--text-primary)"}
    assert _spec(FIGURE_WAS_OFF) == _spec(FIGURE_WAS_LIT) > _spec(FIGURE_PICKED) > _spec(FIGURE)
    assert _spec(FIGURE_WAS_ON) > _spec(FIGURE)
    assert _spec(STAR_START) > _spec(STAR_PICKED) > _spec(STAR) and _spec(STAR_WAS_ON) > _spec(STAR)
    # Nothing grows, and no word changes its weight: no rule of the number tab, in any state, sets a size, a scale, a box, a spacing or
    # a weight but the three signs' own (dot, number, star), each once and with no state in its selector.
    forbidden = re.compile(r"^(transform|scale|zoom|font-size|font-stretch|letter-spacing|padding.*|width|height|min-.*|max-.*|border.*|outline.*|"
                           r"background.*|box-shadow|filter|opacity)$")
    # The one box the number is given: the room of two digits, centred, so a tab keeps its width from 1 to 99 accounts (the rail's width and axis are read from the tabs).
    room = ".tab-mark-dot+.tab-mark-count"
    assert _one(room) == {"min-width": "2ch", "text-align": "center"}
    for media, selector, decls in _number_rules():
        assert not [p for p in decls if forbidden.match(p) and not (selector == room and p == "min-width")], (selector, decls)
        if "font-weight" in decls:
            assert selector in (".tab-mark-dot", ".tab-mark-count", STAR), selector
    assert "var(--mark-on)" not in "".join(v for _, _, d in _number_rules() for v in d.values()), "the growth is the model's mark's alone"
    # The number tab is the rail's alone (it has no dot: the number tab without a ring was never drawn, and its rules are gone).
    assert not [s for n, m, s, d in _all_rules() if n == KIT and ".tab-mark-num" in s and ".tab-mark-dot" in s], "no rule of the number tab names the dot"
    # The roles hold their contrast on the head's ground in both themes: the ink and the quiet meta ink are body text (4.5:1); the red
    # keeps its 3:1 (the one red of the shell, tested with the mark above).
    dark, light = _themes()
    for theme in (dark, light):
        ground = _rgb(theme["--shell-bg"], (0, 0, 0))
        for token in ("--text-primary", "--text-meta"):
            assert _contrast(_rgb(theme[token], ground), ground) >= 4.5, (token, theme[token])
        assert _contrast(_rgb(theme["--text-primary"], ground), ground) > _contrast(_rgb(theme["--text-meta"], ground), ground), (
            "the ink is stronger than the meta ink: the dot and the number rise to it on a preview")
    assert tokens["--text-meta"] != tokens["--text-primary"] and tokens["--accent"] == "#c93545"


def test_the_swap_is_a_start_state_and_the_marks_own_transition_no_keyframes_and_the_motion_of_the_tab_has_its_reduced_pair():
    css, sections = _style(), _sections(_style())
    source = WIDGET.read_text(encoding="utf-8")
    # No keyframes of the swap: nothing of mark-in / mark-out is left, and the kit's motion keeps the ones it had (the rail's five: the slide of
    # the head's tabs, rail-slide, and the dimming of the tabs that give way, rail-yield, are the pick of a family's, not the swap of a mark).
    # The wait of the chart's plate while the line of a pick is drawn adds two, both by opacity alone, in kit: motion, each with its reduced
    # pair: skel-pulse (the stand-ins pulse) and tl-reveal (the content comes in when the line has met). Nothing else is added.
    assert "mark-in" not in source and "mark-out" not in source
    kept = ["blink", "ring-sweep-arc", "ring-sweep-dot", "wt-rise", "wt-grow", "skel-pulse", "tl-reveal", "rail-draw", "rail-drop", "rail-fade", "ladder-drop", "ladder-leave", "rail-slide", "rail-yield"]
    assert re.findall(r"@keyframes ([\w-]+)", css) == kept
    assert re.findall(r"@keyframes ([\w-]+)", sections[MOTION]) == kept
    # The start state is three classes that hold a look and nothing that moves. The mark just left wears the picked look (in one rule
    # with the picked mark: the two cannot drift apart); the mark picked wears the tab's ink at its size, or, when its tab was showing
    # its preview at the click, the preview's red at its size; both beat the picked rule by weight.
    picked = ".tab-mark[aria-pressed=true] .tab-mark-icon"
    was_on, was_off, was_lit = ".tab-mark-icon.tab-mark-was-on", picked + ".tab-mark-was-off", picked + ".tab-mark-was-lit"
    assert _one(was_on) == _one(picked) == {"color": "var(--accent)", "transform": "scale(var(--mark-on))"}
    assert [listing for _, listing, _ in _rules(sections[KIT]) if "tab-mark-was-on" in listing and ".tab-mark-icon" in listing] == [
        picked + "," + was_on], "the mark's look is written once, in one rule with the picked mark"
    assert _one(was_off) == {"color": "var(--text-primary)", "transform": "none"}
    assert _one(was_lit) == {"color": "var(--accent)", "transform": "none"}
    assert _specificity(was_off) == _specificity(was_lit) > _specificity(picked), "they win over the picked look by weight, not by place"
    assert _specificity(was_on) > _specificity(".tab-mark-icon")
    # The one thing that moves a mark is its transition: colour and size together, on the mark alone.
    moving = sorted((p, s) for n, m, s, d in _all_rules() if n == KIT and "reduced-motion" not in m
                    for p in ("transition", "animation", "animation-name", "animation-delay", "transition-delay") if p in d)
    # (the model's mark fades colour and size; the number tab's dot and number, and its star, fade colour alone: nothing else moves)
    assert moving == [("transition", ".tab-mark-icon"), ("transition", ".tab-mark-note"),
                      ("transition", ".tab-mark-num .tab-mark-count")], moving
    assert _one(".tab-mark-note")["transition"] == _one(".tab-mark-num .tab-mark-count")["transition"] == "color var(--dur-5) var(--ease)"
    # Reduced motion: no fade; the state itself (red, grown) still holds, only its way there is cut.
    media = "@media(prefers-reduced-motion:reduce)"
    assert _one(".tab-mark-icon", media) == {"transition": "none"}
    assert sorted(s for n, m, s, _ in _all_rules() if n == KIT and m == media) == [
        ".tab-mark-icon", ".tab-mark-note", ".tab-mark-num .tab-mark-count"]
    assert all(_one(selector, media) == {"transition": "none"} for _, selector in moving)
    # The kit draws a start state only for a drawing that moves (never under reduced motion) and only where a frame can take it off.
    start = _function(source, "markStart")
    assert "if (!drawMotion || !fromKey || typeof window.requestAnimationFrame !== 'function') return;" in start
    settle = _function(source, "settleMarks")
    assert settle.index("getComputedStyle") < settle.index("classList.remove"), "the look is read before the class goes"
    assert all(("'%s'" % c) in settle for c in ("tab-mark-was-off", "tab-mark-was-lit", "tab-mark-was-on"))


def test_a_finger_gets_the_tap_height_and_a_mouse_the_rows():
    tokens = _tokens(_sections(_style()))
    assert tokens["--row-h"] == "32px" and tokens["--tap-h"] == "44px"
    assert _one(".tab-mark")["min-height"] == "var(--row-h)"
    # The finger's line is the kit's one (kit: touch), after the tab's own rule, and no rule of the head lifts it.
    assert _one(".tab-mark", "@media(pointer:coarse)", section=TOUCH) == {"min-height": "var(--tap-h)"}
    assert not [1 for n, m, s, _ in _all_rules() if n == HEAD and "pointer:coarse" in m and ".tab-mark" in s]
    assert list(_sections(_style())).index(KIT) < list(_sections(_style())).index(TOUCH)


# ---------------------------------------------------------------------------
# The markup and the swap, over the widget's fake DOM.

NODE_TABS = r"""
const classOf = (n) => String((n && n.className) || '').split(/\s+/).filter(Boolean);
const heard = (n) => (n.getAttribute('aria-hidden') === 'true' ? '' : (n._text || '') + n.childNodes.map(heard).join(''));
const click = (env, key) => byFocus(env.root, key).listeners.click[0]({ stopPropagation() {} });
const tab = (env, id) => byFocus(env.root, 'harness:' + id);
const markOf = (env, id) => tab(env, id).childNodes[0];
const swaps = (env) => classes(env.root, 'tab-mark-was-off').length + classes(env.root, 'tab-mark-was-on').length
  + classes(env.root, 'tab-mark-was-lit').length;
const pressed = (env) => ['claude', 'codex', 'openrouter'].map((id) => tab(env, id).getAttribute('aria-pressed'));
// The page's animation frames, by hand: the widget asks for one to take a start state off; a test runs it when it chooses.
const frames = [];
const hold = (env) => { env.window.requestAnimationFrame = (callback) => { frames.push(callback); return frames.length; }; };
const frame = () => frames.splice(0).forEach((callback) => callback(0));
const family = (id, label, accounts) => ({ harness_id: id, family_label: label, harness_status: 'ok', harness_enabled: true,
  provider_family: '', catalog_known: true, accounts, accounts_signed_in: accounts.length, accounts_unavailable: false });
// Three families: one with two accounts, one with three of which one is switched off, one with none (and no drawing: an initial).
function families() {
  const v = view([account('p1', quota()), account('p2', quota())]);
  v.groups.push(family('codex', 'Codex', [account('c1', quota(), { key: 'codex:c1' }), account('c2', quota(), { key: 'codex:c2' }),
    account('c3', quota(), { key: 'codex:c3', enabled: false })]));
  v.groups.push(family('openrouter', 'OpenRouter', []));
  return v;
}
"""

NODE_MARKUP = NODE_TABS + r"""
(async () => {
  // 1. The tabs at rest: a button each, no .btn and no class of the pick; the pick is aria-pressed; the label, the title and the
  // focus key are what they were.
  let env = await boot(families());
  const ids = ['claude', 'codex', 'openrouter'];
  const tabs = ids.map((id) => tab(env, id));
  assert.equal(classes(env.root, 'tab-mark').length, 3, 'one tab for each family');
  tabs.forEach((b, i) => assert.equal(classes(env.root, 'tab-mark')[i], b, 'in the order of the families'));
  for (const b of tabs) {
    assert.equal(b.tagName, 'BUTTON');
    assert.equal(b.getAttribute('type'), 'button');
    assert.ok(!classOf(b).includes('btn') && !classOf(b).includes('active'), 'no kit .btn, no class of the pick: ' + b.className);
  }
  assert.deepEqual(classOf(tabs[0]), ['tab-mark']);
  assert.deepEqual(classOf(tabs[2]), ['tab-mark', 'empty'], 'a family with no account keeps its mark of that');
  assert.deepEqual(tabs.map((b) => b.getAttribute('aria-pressed')), ['true', 'false', 'false']);
  assert.deepEqual(tabs.map((b) => b.getAttribute('data-mark-key')), ids, 'the kit finds a tab by its key');
  // The row is the kit's (markRow), in the place the head gives it; a group with a name; the tabs stand in it and nothing else does.
  const seg = tabs[0].parentNode;
  assert.deepEqual(classOf(seg), ['tab-mark-row', 'harness-seg'], 'the kit row, then the head\'s own place: nothing else');
  assert.equal(seg.tagName, 'DIV');
  assert.equal(seg.getAttribute('role'), 'group');
  assert.equal(seg.getAttribute('aria-label'), 'Agent family');
  assert.ok(seg.childNodes.length === tabs.length && tabs.every((node, i) => seg.childNodes[i] === node), 'the tabs, in the order of the families');
  const says = ['Claude — 2 accounts', 'Codex — 3 accounts', 'OpenRouter — no accounts'];
  assert.deepEqual(tabs.map((b) => b.getAttribute('aria-label')), says);
  assert.deepEqual(tabs.map((b) => b.title), says);
  assert.deepEqual(tabs.map((b) => b.getAttribute('data-focus')), ids.map((id) => 'harness:' + id));

  // 2. Inside a tab: the mark, the name, the dot, the number - in this order. The mark (a drawing, or the initial where there is
  // none) wears .tab-mark-icon; the dot is there only with a number, out of the reading order, with no attribute but that one.
  const shape = (b) => b.childNodes.map((n) => n.tagName + '.' + classOf(n).join('.'));
  const full = ['SVG.tab-mark-icon', 'SPAN.tab-mark-name', 'SPAN.tab-mark-dot', 'SPAN.tab-mark-count'];
  assert.deepEqual(shape(tabs[0]), full);
  assert.deepEqual(shape(tabs[1]), full);
  assert.deepEqual(shape(tabs[2]), ['SPAN.harness-initial.tab-mark-icon', 'SPAN.tab-mark-name'], 'no number, no dot');
  assert.equal(classes(tabs[2], 'tab-mark-dot').length + classes(tabs[2], 'tab-mark-count').length, 0);
  for (const b of [tabs[0], tabs[1]]) {
    const dot = b.childNodes[2];
    assert.equal(dot.textContent, '·', 'the middle dot, U+00B7');
    assert.deepEqual(dot.attributes, { 'aria-hidden': 'true' });
    assert.deepEqual(dot.listeners, {});
    assert.equal(dot.childNodes.length, 0);
  }
  assert.deepEqual([tabs[0], tabs[1]].map((b) => b.childNodes[3].textContent), ['2', '2'], 'the accounts switched on');
  assert.equal(tabs[2].childNodes[0].textContent, 'O', 'the initial stands where the widget has no drawing');
  assert.equal(heard(tabs[0]), 'Claude2', 'a reader hears the name and the number: no dot, no drawing');
  assert.equal(classes(env.root, 'tab-mark-icon').length, 3, 'one mark in each tab, and nowhere else');

  // 3. Before the first answer: three ghosts, tabs of the kit as well (markTab), faint and dead: disabled, no choice (no aria-pressed and
  // no key, so nothing is picked and the kit's preview, which leaves a disabled tab out, never lights them), no dot and no number.
  env = await boot({ ok: true, transport_error: '', facets: {}, groups: [] });
  const ghosts = classes(env.root, 'tab-mark');
  assert.equal(ghosts.length, 3);
  for (const g of ghosts) {
    assert.deepEqual(classOf(g), ['tab-mark']);
    assert.equal(g.disabled, true);
    assert.equal(g.getAttribute('type'), 'button');
    assert.deepEqual(shape(g), ['SVG.tab-mark-icon', 'SPAN.tab-mark-name'], 'a drawing and a name: no dot, no number');
    assert.equal(g.getAttribute('aria-pressed'), null);
    assert.equal(g.getAttribute('data-mark-key'), null);
    assert.equal(g.getAttribute('data-focus'), null);
  }
  assert.deepEqual(ghosts.map((g) => g.childNodes[1].textContent), ['Codex', 'Claude', 'Cursor']);
  assert.equal(classes(env.root, 'tab-mark-dot').length + classes(env.root, 'tab-mark-count').length, 0);
  const row = ghosts[0].parentNode;
  assert.deepEqual(classOf(row), ['tab-mark-row', 'harness-seg']);
  assert.equal(row.getAttribute('role'), 'group');
  assert.equal(row.getAttribute('aria-label'), 'Reading agent families');
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""

NODE_SWAP = NODE_TABS + r"""
(async () => {
  // 0. A page with no animation frames draws no start state at all: nothing could take the class off, and a mark must never stay in
  // a look that is not its own. The pick is made all the same.
  let env = await boot(families());
  click(env, 'harness:codex');
  assert.deepEqual(pressed(env), ['false', 'true', 'false']);
  assert.equal(swaps(env), 0, 'no frames, no start state');

  // 1. A plain drawing draws none and asks for no frame: the first one, the poll's, About's, a click on the tab that is already picked.
  env = await boot(families());
  hold(env);
  assert.equal(swaps(env), 0, 'the first drawing');
  env.interval()(); await settle();
  assert.equal(swaps(env), 0, 'the poll');
  click(env, 'about'); click(env, 'about');
  assert.equal(swaps(env), 0, 'About opened and shut');
  click(env, 'harness:claude');
  assert.equal(swaps(env), 0, 'a click on the picked tab picks nothing');
  assert.equal(tab(env, 'claude').getAttribute('aria-pressed'), 'true');
  assert.equal(frames.length, 0, 'none of them asked for a frame');

  // 2. The reader's own pick of a family: the mark of the new tab is drawn in its old look (the tab's ink, its size), the mark of the
  // tab left in the picked look, the third tab's in neither; the drawing that answers the pick starts the motion at its start (no
  // delay); the pick itself is made; one frame is asked for, for the two marks.
  click(env, 'harness:codex');
  assert.deepEqual(pressed(env), ['false', 'true', 'false']);
  assert.deepEqual(classOf(markOf(env, 'codex')), ['tab-mark-icon', 'tab-mark-was-off']);
  assert.deepEqual(classOf(markOf(env, 'claude')), ['tab-mark-icon', 'tab-mark-was-on']);
  assert.deepEqual(classOf(markOf(env, 'openrouter')), ['harness-initial', 'tab-mark-icon']);
  assert.equal(swaps(env), 2);
  assert.equal(markOf(env, 'codex').style.transitionDelay, undefined);
  assert.equal(markOf(env, 'claude').style.transitionDelay, undefined);
  assert.equal(frames.length, 1);
  // The tabs' words are what they were: the same label, the same number, the same focus key.
  assert.equal(tab(env, 'codex').getAttribute('aria-label'), 'Codex — 3 accounts');
  assert.equal(classes(tab(env, 'codex'), 'tab-mark-count')[0].textContent, '2');

  // 3. The next frame: the look of each of the two marks is read while it still wears its class (the browser holds it as the look the
  // change starts from), then the class goes; nothing else changes.
  const reads = [];
  env.window.getComputedStyle = (node) => { reads.push(classOf(node).join(' ')); return { transform: 'none' }; };
  frame();
  assert.deepEqual(reads.sort(), ['tab-mark-icon tab-mark-was-off', 'tab-mark-icon tab-mark-was-on']);
  assert.equal(swaps(env), 0);
  assert.deepEqual(classOf(markOf(env, 'codex')), ['tab-mark-icon']);
  assert.deepEqual(classOf(markOf(env, 'claude')), ['tab-mark-icon']);
  assert.deepEqual(pressed(env), ['false', 'true', 'false'], 'the frame changes no pick');
  assert.equal(frames.length, 0);

  // 4. The mark that comes in is the new pick's, whichever family is left: from the picked one to the empty one, and back. A frame
  // whose look cannot be read here (no getComputedStyle that answers) takes the class off all the same.
  env.window.getComputedStyle = () => { throw new Error('not here'); };
  click(env, 'harness:openrouter');
  assert.deepEqual(classOf(markOf(env, 'openrouter')), ['harness-initial', 'tab-mark-icon', 'tab-mark-was-off']);
  assert.deepEqual(classOf(markOf(env, 'codex')), ['tab-mark-icon', 'tab-mark-was-on']);
  assert.deepEqual(classOf(markOf(env, 'claude')), ['tab-mark-icon']);
  frame();
  assert.equal(swaps(env), 0);
  delete env.window.getComputedStyle;
  click(env, 'harness:claude');
  assert.deepEqual(classOf(markOf(env, 'claude')), ['tab-mark-icon', 'tab-mark-was-off']);
  assert.deepEqual(classOf(markOf(env, 'openrouter')), ['harness-initial', 'tab-mark-icon', 'tab-mark-was-on']);
  assert.equal(swaps(env), 2);
  frame();
  assert.equal(swaps(env), 0, 'a window without getComputedStyle: the class goes');

  // 5. Two drawings before the frame (the pick's own read answering at once): both ask for a frame, both start states stand, and
  // the one frame the page gives runs both; the marks that are on the page lose their class.
  click(env, 'harness:codex');
  env.interval()(); await settle();
  assert.equal(swaps(env), 2, 'the drawing that answers the pick draws the start state again');
  assert.ok(frames.length >= 2, 'every drawing asked for its frame (the pick\'s chart read may redraw too)');
  frame();
  assert.equal(swaps(env), 0);
  assert.deepEqual(pressed(env), ['false', 'true', 'false']);
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""

NODE_CARRY = NODE_TABS + r"""
(async () => {
  // A redraw inside the pick's span (the pick's own read, the poll) carries the motion on from where it was: the same two marks are
  // drawn in the start state again, the transition to start as far on as the time gone (a negative delay, as the window row's
  // underline does); once the span is over a redraw draws none, with no delay and no frame asked for.
  const real = Date.now;
  let clock = real.call(Date);
  Date.now = () => clock;
  let env = await boot(families());
  hold(env);
  clock += 1000;
  click(env, 'harness:codex');
  frame();
  assert.equal(swaps(env), 0, 'the pick\'s own frame took its start state off');
  clock += 90;
  env.interval()(); await settle();
  assert.deepEqual(classOf(markOf(env, 'codex')), ['tab-mark-icon', 'tab-mark-was-off']);
  assert.deepEqual(classOf(markOf(env, 'claude')), ['tab-mark-icon', 'tab-mark-was-on']);
  assert.equal(markOf(env, 'codex').style.transitionDelay, '-90ms');
  assert.equal(markOf(env, 'claude').style.transitionDelay, '-90ms');
  assert.equal(markOf(env, 'openrouter').style.transitionDelay, undefined, 'the tab that moved nothing is not delayed');
  assert.equal(swaps(env), 2);
  assert.ok(frames.length >= 1, 'every drawing asks for its frame (the pick\'s chart read may redraw too)');
  frame();
  assert.equal(swaps(env), 0, 'the frame takes it off');
  assert.equal(markOf(env, 'codex').style.transitionDelay, '-90ms', 'the transition has begun with its delay, which stays through this frame');
  assert.ok(frames.length >= 1, 'and the delay goes a frame later');
  frame();
  assert.equal(markOf(env, 'codex').style.transitionDelay, '');
  assert.equal(markOf(env, 'claude').style.transitionDelay, '');
  assert.equal(frames.length, 0);
  clock += 300;
  env.interval()(); await settle();
  assert.equal(swaps(env), 0, 'the span is over: a redraw draws no start state');
  assert.equal(frames.length, 0, 'and asks for no frame');
  assert.equal(markOf(env, 'codex').style.transitionDelay, undefined);
  assert.equal(tab(env, 'codex').getAttribute('aria-pressed'), 'true', 'and the pick stays');
  Date.now = real;
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""

NODE_REDUCED = NODE_TABS + r"""
(async () => {
  // Under reduced motion the pick is made, and nothing moves: no start state at all, and no frame asked for (the page does have frames).
  const env = await boot(families());
  hold(env);
  env.window.matchMedia = (query) => ({ matches: /prefers-reduced-motion: reduce/.test(query), media: query });
  click(env, 'harness:codex');
  assert.equal(tab(env, 'codex').getAttribute('aria-pressed'), 'true');
  assert.equal(tab(env, 'claude').getAttribute('aria-pressed'), 'false');
  assert.equal(swaps(env), 0);
  assert.deepEqual(classOf(markOf(env, 'codex')), ['tab-mark-icon']);
  assert.deepEqual(classOf(markOf(env, 'claude')), ['tab-mark-icon']);
  assert.equal(frames.length, 0);
  env.interval()(); await settle();
  assert.equal(swaps(env), 0);
  assert.equal(frames.length, 0);
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""

# A tab that shows its preview at the click (the pointer on it, or the keyboard on it) has a mark that is red a moment before the
# pick; the redraw makes a new mark, which must start from that red (tab-mark-was-lit), not from the ink (tab-mark-was-off), or the mark would
# flicker red - grey - red. The widget asks the tab itself (matches(':hover'), matches(':focus-visible')) at the click; a page that
# cannot answer one of the two says no to it.
NODE_LIT = NODE_TABS + r"""
(async () => {
  const asked = [];
  // A click on a tab that answers matches() the way a browser would for the pointer or the keyboard on it.
  const press = (env, id, ways, broken = []) => {
    const b = tab(env, id);
    // The pointer moves onto the tab before the click: a pick holds the head's previews by the pointer until it moves.
    (env.document.listeners.pointermove || []).forEach((handler) => handler({ clientX: 5, clientY: 5 }));
    b.matches = (state) => {
      asked.push(state);
      if (broken.includes(state)) throw new SyntaxError('not a selector here: ' + state);
      return ways.includes(state);
    };
    b.listeners.click[0]({ stopPropagation() {} });
  };
  // The start state each of the three marks wears now ('' for none), in the order claude, codex, openrouter.
  const starts = (env) => ['claude', 'codex', 'openrouter'].map((id) => classOf(markOf(env, id)).filter((c) => c.startsWith('tab-mark-was')).join('+'));
  let env = await boot(families());
  hold(env);

  // 1. The pointer is on the tab: its mark starts from the red at its size; the mark of the tab left is as it was (the picked look).
  press(env, 'codex', [':hover']);
  assert.deepEqual(pressed(env), ['false', 'true', 'false']);
  assert.deepEqual(asked.splice(0), [':hover'], 'the pointer is enough: the keyboard is not asked');
  assert.deepEqual(classOf(markOf(env, 'codex')), ['tab-mark-icon', 'tab-mark-was-lit']);
  assert.deepEqual(starts(env), ['tab-mark-was-on', 'tab-mark-was-lit', '']);
  assert.equal(swaps(env), 2);
  assert.equal(frames.length, 1, 'one frame, for the two marks');
  frame();
  assert.equal(swaps(env), 0, 'the frame takes the lit start off as it takes the others');
  assert.deepEqual(classOf(markOf(env, 'codex')), ['tab-mark-icon']);

  // 2. The keyboard is on the tab (no pointer): the same start. Both ways are asked, the pointer first.
  press(env, 'claude', [':focus-visible']);
  assert.deepEqual(asked.splice(0), [':hover', ':focus-visible']);
  assert.deepEqual(starts(env), ['tab-mark-was-lit', 'tab-mark-was-on', '']);
  frame();
  assert.equal(swaps(env), 0);

  // 3. Neither (a click from a script, a touch with no preview): the start is the ink, as it always was. The tab is asked twice, once
  // for each way, and says no to both.
  press(env, 'openrouter', []);
  assert.deepEqual(asked.splice(0), [':hover', ':focus-visible']);
  assert.deepEqual(starts(env), ['tab-mark-was-on', '', 'tab-mark-was-off']);
  frame();

  // 4. A page that does not know :focus-visible (the selector throws) is not trusted with it, and :hover alone still counts; a page
  // that throws for both starts from the ink. A tab with no matches() at all is the same (the other tests of this file click so).
  press(env, 'claude', [':hover'], [':focus-visible']);
  assert.deepEqual(starts(env), ['tab-mark-was-lit', '', 'tab-mark-was-on']);
  frame();
  press(env, 'codex', [':hover', ':focus-visible'], [':hover', ':focus-visible']);
  assert.deepEqual(starts(env), ['tab-mark-was-on', 'tab-mark-was-off', ''], 'a throw is a no, not a crash');
  frame();
  press(env, 'openrouter', [':focus-visible'], [':hover']);
  assert.deepEqual(starts(env), ['', 'tab-mark-was-on', 'tab-mark-was-lit'], 'the throw of one way leaves the other its say');
  frame();
  assert.deepEqual(pressed(env), ['false', 'false', 'true']);

  // 5. A click on the tab that is already picked picks nothing: no start state, no frame, and the tab is not asked.
  env = await boot(families());
  hold(env);
  asked.length = 0;
  press(env, 'claude', [':hover']);
  assert.deepEqual(pressed(env), ['true', 'false', 'false']);
  assert.deepEqual(asked, [], 'a click on the picked tab asks nothing');
  assert.equal(swaps(env), 0);
  assert.equal(frames.length, 0);

  // 6. A redraw inside the pick's span draws the same start state again (the lit one, not the ink), as far on as the span has gone.
  const real = Date.now;
  let clock = real.call(Date);
  Date.now = () => clock;
  env = await boot(families());
  hold(env);
  clock += 1000;
  press(env, 'codex', [':hover']);
  frame();
  clock += 90;
  env.interval()(); await settle();
  assert.deepEqual(classOf(markOf(env, 'codex')), ['tab-mark-icon', 'tab-mark-was-lit']);
  assert.deepEqual(classOf(markOf(env, 'claude')), ['tab-mark-icon', 'tab-mark-was-on']);
  assert.equal(markOf(env, 'codex').style.transitionDelay, '-90ms');
  frame(); frame();
  assert.equal(swaps(env), 0);
  assert.equal(markOf(env, 'codex').style.transitionDelay, '');
  clock += 300;
  env.interval()(); await settle();
  assert.equal(swaps(env), 0, 'the span is over: no start state, lit or not');
  Date.now = real;

  // 7. Under reduced motion no start state is drawn at all, however the tab was lit.
  env = await boot(families());
  hold(env);
  env.window.matchMedia = (query) => ({ matches: /prefers-reduced-motion: reduce/.test(query), media: query });
  press(env, 'codex', [':hover', ':focus-visible']);
  assert.equal(tab(env, 'codex').getAttribute('aria-pressed'), 'true');
  assert.equal(swaps(env), 0);
  assert.equal(frames.length, 0);
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""

# The pick of a window is not the pick of a family: it moves the plate (.wt-in) and draws no start state on the marks of the families
# (their drawings), though the family left is still in the widget's memory; it draws one on the dots and numbers of the two window
# tabs it changes (the number tab's mark is its dot and number), and the pick of a family draws none on them. Over a real view of the
# backend (claude) with an empty family beside it.
NODE_WINDOW = NODE_TABS + r"""
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const wearing = (env, part) => classes(env.root, part).filter((n) => classOf(n).some((c) => c.startsWith('tab-mark-was-'))).length;
const familyMarks = (env) => wearing(env, 'tab-mark-icon');
const windowFigures = (env) => wearing(env, 'tab-mark-dot') + wearing(env, 'tab-mark-count');
(async () => {
  const views = JSON.parse(fs.readFileSync(process.env.TABS_FIXTURE_FILE, 'utf8'));
  const v = views.claude;
  v.groups.push(family('openrouter', 'OpenRouter', []));
  const env = await boot(v);
  hold(env);
  assert.equal(swaps(env), 0);
  assert.ok(classes(env.root, 'wt-tab').length >= 2, 'the window row is there');
  click(env, 'harness:openrouter');
  assert.equal(familyMarks(env), 2, 'a family picked: its marks get a start state');
  assert.equal(windowFigures(env), 0, 'and the numbers of the windows get none');
  frame();
  assert.equal(swaps(env), 0);
  await wait(320);
  click(env, 'harness:claude');
  assert.equal(familyMarks(env), 2, 'and back');
  assert.equal(windowFigures(env), 0, 'the window row of the family picked is drawn at rest');
  frame();
  await wait(320);
  env.interval()(); await settle();
  assert.equal(swaps(env), 0);
  assert.equal(frames.length, 0);
  const other = classes(env.root, 'wt-tab').find((t) => t.getAttribute('aria-disabled') !== 'true' && t.getAttribute('aria-selected') !== 'true');
  assert.ok(other, 'a window to pick');
  const left = classes(env.root, 'wt-tab').find((t) => t.getAttribute('aria-selected') === 'true');
  const otherKey = other.getAttribute('data-focus');
  const leftKey = left.getAttribute('data-focus');
  click(env, otherKey);
  assert.ok(classes(env.root, 'wt-in').length >= 1, 'a window picked moves the plate');
  assert.equal(familyMarks(env), 0, 'but gives no start state to the marks of the families');
  // The number of the window picked (from its quiet look) and of the window left (from the picked red): two marks (the number stands in the
  // box of the ring and the rail's tab has no dot), the star none of its own (it follows its number's class in the stylesheet).
  const now = classes(env.root, 'wt-tab');
  const wornBy = (tab) => ['tab-mark-dot', 'tab-mark-count'].flatMap((part) => classes(tab, part).map((n) => classOf(n).filter((c) => c.startsWith('tab-mark-was-')).join('+')));
  assert.equal(windowFigures(env), 2);
  assert.deepEqual(wornBy(now.find((t) => t.getAttribute('data-focus') === otherKey)), ['tab-mark-was-off']);
  assert.deepEqual(wornBy(now.find((t) => t.getAttribute('data-focus') === leftKey)), ['tab-mark-was-on']);
  assert.equal(classes(env.root, 'tab-mark-note').filter((n) => classOf(n).length !== 1).length, 0, 'the star wears no start class');
  assert.equal(frames.length, 1, 'one frame is asked for, to take them off');
  frame();
  assert.equal(swaps(env), 0, 'the next frame takes the start state off');
  await wait(320);
  // (the read the pick asked for redraws inside the span and carries the start state on, with a delay a second frame takes off: run them)
  for (let i = 0; i < 4 && frames.length; i++) frame();
  env.interval()(); await settle();
  assert.equal(swaps(env), 0);
  assert.equal(frames.length, 0, 'the poll asks for none');
  assert.equal(classes(env.root, 'wt-in').length, 0);
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""


# The helpers on their own text: el, markTab, markRow, markPreviewed, markStart and settleMarks are cut out of widget.js and built over the
# fake DOM, with a state of their own (the drawing's motion and elapsed time, the marks that wear a start state) and a window of their own
# (frames the test runs by hand), so a row of any number of tabs, keys and states is seen, which the head (three families) cannot show.
NODE_KIT = r"""
const src = fs.readFileSync(process.env.WIDGET_PATH, 'utf8');
const grab = (name) => {
  const found = src.match(new RegExp('\\n    function ' + name + '\\([\\s\\S]*?\\n    }\\n'));
  assert.ok(found, name + ' is in the widget');
  return found[0];
};
const { document } = makeDocument();
const classOf = (n) => String((n && n.className) || '').split(/\s+/).filter(Boolean);
const heard = (n) => (n.getAttribute('aria-hidden') === 'true' ? '' : (n._text || '') + n.childNodes.map(heard).join(''));
function build(win) {
  return new Function('document', 'window',
    'var drawMotion = true, drawElapsed = 0, markStarts = [];' +
    grab('el') + grab('markTab') + grab('markRow') + grab('markPreviewed') + grab('markPressed') + grab('markStart') + grab('settleMarks') +
    'return { markTab: markTab, markRow: markRow, markPreviewed: markPreviewed, markPressed: markPressed, markStart: markStart, settleMarks: settleMarks,' +
    ' motion: function (on, elapsed) { drawMotion = on; drawElapsed = elapsed || 0; }, held: function () { return markStarts.length; } };')(document, win);
}
const queue = [];
const win = { requestAnimationFrame: (callback) => { queue.push(callback); return queue.length; }, getComputedStyle: null };
const frame = () => queue.splice(0).forEach((callback) => callback(0));
const kit = build(win);
const drawing = () => document.createElementNS('http://www.w3.org/2000/svg', 'svg');
const initial = (text) => { const n = document.createElement('span'); n.className = 'own-initial'; n.textContent = text; return n; };

// 1. A tab: a button, the kit's class, and nothing but what the kit owns (type, the key, aria-pressed); the mark first with the kit's
// class after its own, then the name, then, with a number, the dot (out of the reading order) and the number.
let t = kit.markTab({ key: 'a', mark: drawing(), name: 'Alpha', count: 3, pressed: true });
assert.equal(t.tagName, 'BUTTON');
assert.equal(t.className, 'tab-mark');
assert.deepEqual(t.attributes, { type: 'button', 'data-mark-key': 'a', 'aria-pressed': 'true' }, 'the kit sets its own and the part sets the rest');
assert.deepEqual(t.listeners, {}, 'no click: the part wires it');
assert.deepEqual(t.childNodes.map((n) => n.tagName + '.' + n.className), ['SVG.tab-mark-icon', 'SPAN.tab-mark-name', 'SPAN.tab-mark-dot', 'SPAN.tab-mark-count']);
assert.equal(t.childNodes[1].textContent, 'Alpha');
assert.equal(t.childNodes[2].textContent, '·');
assert.deepEqual(t.childNodes[2].attributes, { 'aria-hidden': 'true' });
assert.equal(t.childNodes[3].textContent, '3');
assert.equal(heard(t), 'Alpha3', 'a reader hears the name and the number: no dot');
// The caller's own class on its mark stays, and the kit's comes after it.
t = kit.markTab({ key: 'b', mark: initial('B'), name: 'Beta', count: 0, pressed: false });
assert.equal(t.childNodes[0].className, 'own-initial tab-mark-icon');
assert.equal(t.childNodes[0].textContent, 'B');
assert.equal(t.getAttribute('aria-pressed'), 'false');
assert.equal(t.childNodes[3].textContent, '0', 'a count of zero is a number: it is drawn');
// A count that is neither a number nor a text draws neither the dot nor the number.
for (const count of [undefined, null, NaN, Infinity, {}, true, '']) {
  const bare = kit.markTab({ key: 'c', mark: drawing(), name: 'Gamma', count });
  assert.deepEqual(bare.childNodes.map((n) => n.className), ['tab-mark-icon', 'tab-mark-name'], 'count ' + String(count));
}
// A short text is a figure too (the number tab's 68%, a dash): it draws the dot and the number.
for (const count of ['3', '68%', '—']) {
  const shown = kit.markTab({ key: 'c', mark: drawing(), name: 'Gamma', count });
  assert.deepEqual(shown.childNodes.map((n) => n.className), ['tab-mark-icon', 'tab-mark-name', 'tab-mark-dot', 'tab-mark-count'], 'count ' + count);
  assert.equal(shown.childNodes[3].textContent, count);
}
// A tab that is no choice has no aria-pressed (pressed left out, or not a boolean); one with no key has no key.
for (const pressed of [undefined, null, 1, 'true']) {
  assert.equal(kit.markTab({ key: 'd', mark: drawing(), name: 'Delta', pressed }).getAttribute('aria-pressed'), null, 'pressed ' + String(pressed));
}
const keyless = kit.markTab({ mark: drawing(), name: 'Keyless' });
assert.deepEqual(keyless.attributes, { type: 'button' });
assert.equal(kit.markTab({ key: 0, mark: drawing(), name: 'Zero' }).getAttribute('data-mark-key'), '0', 'a key of 0 is a key');
assert.equal(kit.markTab({ key: 'e', mark: drawing(), name: '' }).childNodes[1].textContent, '', 'an empty name is an empty span');

// 2. A row: a group of the tabs given, in their order, with the kit's class and nothing else (its name and place are the part's).
const keys = ['a', 'b', 'c', 'd'];
const make = () => keys.map((key) => kit.markTab({ key, mark: drawing(), name: key.toUpperCase(), count: 1, pressed: key === 'a' }));
let tabs = make();
let row = kit.markRow(tabs);
assert.equal(row.tagName, 'DIV');
assert.equal(row.className, 'tab-mark-row');
assert.deepEqual(row.attributes, { role: 'group' });
assert.ok(row.childNodes.length === tabs.length && tabs.every((node, i) => row.childNodes[i] === node));
assert.equal(kit.markRow([]).childNodes.length, 0);
assert.equal(kit.markRow([kit.markTab({ mark: drawing(), name: 'Solo' })]).childNodes.length, 1);
const iconOf = (tab) => tab.childNodes[0];
const starts = (r) => r.childNodes.map((tab) => classOf(iconOf(tab)).filter((c) => c.startsWith('tab-mark-was')).join('+'));

// 3. The start state of a pick: the tab left and the tab picked, the others none; a lit pick holds the red.
kit.motion(true, 0);
kit.markStart(row, 'a', 'c', false);
assert.deepEqual(starts(row), ['tab-mark-was-on', '', 'tab-mark-was-off', '']);
assert.equal(kit.held(), 2);
assert.equal(iconOf(tabs[0]).style.transitionDelay, undefined, 'no delay for a drawing that begins the motion');
frame(); // (nothing asked for a frame yet: settleMarks is the caller's, once per drawing)
assert.deepEqual(starts(row), ['tab-mark-was-on', '', 'tab-mark-was-off', ''], 'a frame nobody asked for takes nothing off');
tabs = make(); row = kit.markRow(tabs);
kit.markStart(row, 'd', 'b', true);
assert.deepEqual(starts(row), ['', 'tab-mark-was-lit', '', 'tab-mark-was-on']);
assert.equal(kit.held(), 4, 'the marks of both drawings are held until a frame takes them off');
// The frame takes them off: the look is read first (while the class is on), then the class goes; then the next frame the delays.
const reads = [];
win.getComputedStyle = (node) => { reads.push(classOf(node).filter((c) => c.startsWith('tab-mark-was')).join('+')); return { transform: 'none' }; };
kit.settleMarks();
assert.equal(queue.length, 1, 'one frame, for all the marks held');
assert.equal(kit.held(), 0);
frame();
assert.deepEqual(reads.slice().sort(), ['tab-mark-was-lit', 'tab-mark-was-off', 'tab-mark-was-on', 'tab-mark-was-on'], 'read while it wears the class');
assert.deepEqual(starts(row), ['', '', '', '']);
assert.equal(queue.length, 0, 'no delay, no second frame');
kit.settleMarks();
assert.equal(queue.length, 0, 'nothing held, no frame asked for');

// 4. Inside the pick's span the transition starts as far on as the span has gone, and the delay goes a frame after the class.
tabs = make(); row = kit.markRow(tabs);
kit.motion(true, 90);
kit.markStart(row, 'a', 'b', true);
assert.deepEqual(starts(row), ['tab-mark-was-on', 'tab-mark-was-lit', '', '']);
assert.equal(iconOf(tabs[0]).style.transitionDelay, '-90ms');
assert.equal(iconOf(tabs[1]).style.transitionDelay, '-90ms');
assert.equal(iconOf(tabs[2]).style.transitionDelay, undefined, 'a tab that moves nothing is not delayed');
win.getComputedStyle = () => { throw new Error('not here'); };
kit.settleMarks();
frame();
assert.deepEqual(starts(row), ['', '', '', ''], 'a page that cannot be read still loses the class');
assert.equal(iconOf(tabs[0]).style.transitionDelay, '-90ms', 'the delay stays through the frame that took the class off');
assert.equal(queue.length, 1);
frame();
assert.equal(iconOf(tabs[0]).style.transitionDelay, '');
assert.equal(iconOf(tabs[1]).style.transitionDelay, '');

// 5. Nothing is drawn when nothing moves: no motion in this drawing, no tab left, no frames in the page, no key to find a tab by, a
// tab that is not in the row. A tab with no mark of the kit is passed over.
tabs = make(); row = kit.markRow(tabs);
kit.motion(false, 0);
kit.markStart(row, 'a', 'b', false);
assert.deepEqual(starts(row), ['', '', '', ''], 'a drawing that does not move (reduced motion, the poll)');
kit.motion(true, 0);
for (const from of ['', undefined, null]) kit.markStart(row, from, 'b', false);
assert.deepEqual(starts(row), ['', '', '', ''], 'no tab left: a window\'s pick, the first drawing');
const bare = build({});
bare.motion(true, 0);
bare.markStart(row, 'a', 'b', false);
assert.deepEqual(starts(row), ['', '', '', ''], 'a page with no frames to take the class off with');
assert.equal(bare.held(), 0);
kit.markStart(row, 'x', 'y', false);
assert.deepEqual(starts(row), ['', '', '', ''], 'keys that are in no tab of the row');
const ghosts = kit.markRow([kit.markTab({ mark: drawing(), name: 'G1' }), kit.markTab({ mark: drawing(), name: 'G2' })]);
kit.markStart(ghosts, 'a', 'b', true);
assert.deepEqual(starts(ghosts), ['', ''], 'tabs with no key are never found');
const odd = document.createElement('button');
odd.setAttribute('data-mark-key', 'a');
odd.appendChild(document.createElement('span'));
kit.markStart(kit.markRow([odd]), 'a', 'b', false);
assert.equal(kit.held(), 0, 'a tab with no mark of the kit has nothing to start');
kit.markStart({ childNodes: [{ childNodes: [] }, document.createTextNode('a stray text')] }, 'a', 'b', false);
assert.equal(kit.held(), 0, 'a node of the row that is no tab (a text, a node with no attributes) is passed over');
// The tab left and the tab picked may be the same key only by mistake: the picked one wins, as in the head.
kit.markStart(row, 'a', 'a', false);
assert.deepEqual(starts(row), ['tab-mark-was-off', '', '', '']);

// 6. markPreviewed: the pointer or the keyboard on the tab, each asked on its own; a thrown question is a no; no matches() is a no.
const answers = (ways, broken = []) => ({ matches: (state) => { if (broken.includes(state)) throw new SyntaxError(state); return ways.includes(state); } });
assert.equal(kit.markPreviewed(answers([':hover'])), true);
assert.equal(kit.markPreviewed(answers([':focus-visible'])), true);
assert.equal(kit.markPreviewed(answers([':hover', ':focus-visible'])), true);
assert.equal(kit.markPreviewed(answers([])), false);
assert.equal(kit.markPreviewed(answers([':hover'], [':hover'])), false, 'a throw is a no');
assert.equal(kit.markPreviewed(answers([':focus-visible'], [':hover'])), true, 'and leaves the other way its say');
assert.equal(kit.markPreviewed(answers([':hover'], [':focus-visible'])), true);
assert.equal(kit.markPreviewed({}), false);
assert.equal(kit.markPreviewed(null), false);
assert.equal(kit.markPreviewed(document.createElement('button')), false, 'the fake DOM has no matches(): no');

// 7. The number tab is the rail's: no mark, a ring (a tab with neither is none of the kit's: the number tab without a ring was never drawn, and is gone).
// It wears .tab-mark-num and .tab-mark-rail besides .tab-mark; role=tab and aria-selected (never aria-pressed); one box (.tab-mark-ring) holds the ring,
// the number, the name and the star (aria-hidden), and there is no dot. A figure is a number or a short text (68%, a dash).
const numberAttrs = (selected) => ({ type: 'button', 'data-mark-key': 'w', role: 'tab', 'aria-selected': selected });
const inBox = (tab) => tab.childNodes[0].childNodes;
const partsOf = (tab) => inBox(tab).slice(1).map((n) => n.tagName + '.' + n.className);
let nt = kit.markTab({ key: 'w', name: '5H', count: '68%', note: '*', selected: true, ring: drawing() });
assert.equal(nt.className, 'tab-mark tab-mark-num tab-mark-rail');
assert.deepEqual(nt.attributes, numberAttrs('true'));
assert.equal(nt.getAttribute('aria-pressed'), null, 'a tab of a tablist is picked by aria-selected, not aria-pressed');
assert.equal(nt.childNodes.length, 1, 'the row holds the box of the ring alone: no dot, no word after it');
assert.equal(nt.childNodes[0].className, 'tab-mark-ring');
assert.deepEqual(partsOf(nt), ['SPAN.tab-mark-count', 'SPAN.tab-mark-name', 'SPAN.tab-mark-note']);
assert.equal(inBox(nt)[3].textContent, '*');
assert.deepEqual(inBox(nt)[3].attributes, { 'aria-hidden': 'true' }, 'the star is for the eye: the label of the tab says it in words');
nt = kit.markTab({ key: 'w', name: '7D', count: 41, selected: false, ring: drawing() });
assert.deepEqual(nt.attributes, numberAttrs('false'));
assert.deepEqual(partsOf(nt), ['SPAN.tab-mark-count', 'SPAN.tab-mark-name'], 'no note asked for: no star');
assert.deepEqual(partsOf(kit.markTab({ key: 'w', name: '7D', count: 0, note: '', selected: false, ring: drawing() })), ['SPAN.tab-mark-count', 'SPAN.tab-mark-name'], 'an empty note is none');
assert.equal(kit.markTab({ key: 'w', name: '7D', count: '—', selected: false, ring: drawing() }).childNodes[0].childNodes[1].textContent, '—', 'a dash is a figure');
// A place with nothing to tell has no figure; its name and its star stay.
nt = kit.markTab({ key: 'w', name: '30D', count: null, note: '*', selected: false, ring: drawing() });
assert.deepEqual(partsOf(nt), ['SPAN.tab-mark-name', 'SPAN.tab-mark-note']);
// No mark and no ring: no number tab (a plain tab of the kit, no tablist wiring: selected is the part's to tell).
nt = kit.markTab({ key: 'w', name: 'Plain', count: 3 });
assert.equal(nt.className, 'tab-mark');
assert.deepEqual(nt.attributes, { type: 'button', 'data-mark-key': 'w' });
// A tab with a mark of the caller's is the other kind.
assert.equal(kit.markTab({ key: 'w', mark: drawing(), name: 'Marked', count: 3, pressed: false }).className, 'tab-mark');
// The row of number tabs is a tablist when told so, else a group.
const windowTabs = () => ['w1', 'w2', 'w3'].map((key, i) => kit.markTab({ key, name: key, count: (50 + i) + '%', note: i === 1 ? '*' : '', selected: i === 0, ring: drawing() }));
const wtabs = windowTabs();
const wrow = kit.markRow(wtabs, true);
assert.equal(wrow.className, 'tab-mark-row');
assert.deepEqual(wrow.attributes, { role: 'tablist' });
assert.ok(wrow.childNodes.length === wtabs.length && wtabs.every((node, i) => wrow.childNodes[i] === node));
assert.deepEqual(kit.markRow(windowTabs()).attributes, { role: 'group' });
// The start state of a pick sits on the number and on the star (the number tab's mark, which the star follows by a class of its own), not on the name.
const wearOf = (n) => classOf(n).filter((c) => c.startsWith('tab-mark-was')).join('+');
const figs = (r) => r.childNodes.map((tab) => inBox(tab).filter((n) => classOf(n)[0] === 'tab-mark-count').map(wearOf).join(','));
const stars = (r) => r.childNodes.map((tab) => inBox(tab).filter((n) => classOf(n)[0] === 'tab-mark-note').map(wearOf).join(','));
const names = (r) => r.childNodes.map((tab) => inBox(tab).filter((n) => classOf(n)[0] === 'tab-mark-name').map(wearOf).join(','));
kit.motion(true, 0);
let before = kit.held();
kit.markStart(wrow, 'w1', 'w2', false);
assert.deepEqual(figs(wrow), ['tab-mark-was-on', 'tab-mark-was-off', '']);
assert.deepEqual(stars(wrow), ['', 'tab-mark-was-off', ''], 'the star of the tab picked fades with its number');
assert.equal(kit.held() - before, 5, 'the ring and the number of the tab left, the ring, the number and the star of the tab picked');
assert.deepEqual(names(wrow), ['', '', ''], 'the name wears no start class');
const lit = kit.markRow(windowTabs(), true);
kit.markStart(lit, 'w3', 'w1', true);
assert.deepEqual(figs(lit), ['tab-mark-was-lit', '', 'tab-mark-was-on']);
before = kit.held();
for (const from of ['', undefined, null]) kit.markStart(kit.markRow(windowTabs(), true), from, 'w2', false);
assert.equal(kit.held(), before, 'no tab left (a first drawing, the poll): nothing is started');
win.getComputedStyle = () => ({ transform: 'none', color: 'rgb(0, 0, 0)' });
kit.settleMarks();
frame();
assert.deepEqual(figs(wrow), ['', '', ''], 'the next frame takes them off');
assert.deepEqual(figs(lit), ['', '', ''], 'both rows');
assert.deepEqual(stars(wrow), ['', '', ''], 'and the star');
assert.equal(kit.held(), 0);

// 8. A press of the pointer (markPressed): a click that has a count (the mouse button, a finger) is one; the keyboard's click has none.
assert.ok(kit.markPressed({ detail: 1 }) === true && kit.markPressed({ detail: 2 }) === true);
assert.ok(kit.markPressed({ detail: 0 }) === false && kit.markPressed({}) === false && kit.markPressed(null) === false && kit.markPressed(undefined) === false);
// A rail row (the number tab with a ring) picked by a press starts its RING from the press (was-press: the ring at --press, as it was when the
// button came up) and its number and its star from the ink (was-lit) or the quiet look (was-off) as every number tab; the row left, all in the picked
// look (was-on). A pick that is no press starts the ring from the preview's size (was-lit) or the rest (was-off). The name wears none.
const railTabs = () => ['w1', 'w2'].map((key, i) => kit.markTab({ key, name: key, count: '5' + i + '%', note: i === 1 ? '*' : '', selected: i === 0, ring: drawing() }));
const wornBy = (tab) => {
  const box = tab.childNodes[0];
  const of = (cls) => box.childNodes.filter((n) => classOf(n).includes(cls)).map((n) => classOf(n).filter((c) => c.startsWith('tab-mark-was')).join('+')).join(',');
  return { box: classOf(box).filter((c) => c.startsWith('tab-mark-was')).join('+'), count: of('tab-mark-count'), star: of('tab-mark-note'), name: of('tab-mark-name') };
};
for (const [lit, pressed, ring, number] of [[true, true, 'tab-mark-was-press', 'tab-mark-was-lit'], [false, true, 'tab-mark-was-press', 'tab-mark-was-off'],
                                           [true, false, 'tab-mark-was-lit', 'tab-mark-was-lit'], [false, false, 'tab-mark-was-off', 'tab-mark-was-off']]) {
  const row = kit.markRow(railTabs(), true);
  before = kit.held();
  kit.markStart(row, 'w1', 'w2', lit, pressed);
  assert.deepEqual(wornBy(row.childNodes[1]), { box: ring, count: number, star: number, name: '' }, 'lit ' + lit + ' pressed ' + pressed);
  assert.deepEqual(wornBy(row.childNodes[0]), { box: 'tab-mark-was-on', count: 'tab-mark-was-on', star: '', name: '' }, 'the row left');
  assert.equal(kit.held() - before, 5, 'the ring, the number and the star of the row picked, the ring and the number of the row left');
  win.getComputedStyle = () => ({ transform: 'none', color: 'rgb(0, 0, 0)' });
  kit.settleMarks();
  frame();
  assert.deepEqual(wornBy(row.childNodes[1]), { box: '', count: '', star: '', name: '' }, 'the next frame takes it off, was-press too');
  assert.equal(kit.held(), 0);
}
console.log('ok');
"""


def _run(script, **extra):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    harness = harness.replace("    root: made.root,", "    window,\n    root: made.root,", 1)
    env = {**os.environ, "WIDGET_PATH": str(WIDGET.resolve()), **extra}
    result = subprocess.run([str(node), "-e", harness + script], cwd=Path(__file__).parent, env=env,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok"), result.stdout


def test_a_tab_is_a_button_with_its_mark_its_name_and_with_a_number_a_dot_and_the_number_and_the_ghosts_have_none():
    _run(NODE_MARKUP)


def test_the_reader_s_pick_of_a_family_draws_the_start_state_the_next_frame_takes_it_off_and_no_plain_drawing_draws_one():
    _run(NODE_SWAP)


def test_a_redraw_inside_the_span_draws_the_start_state_again_a_step_on_and_one_after_it_draws_none():
    _run(NODE_CARRY)


def test_under_reduced_motion_the_pick_is_made_and_the_marks_get_no_start_state_and_no_frame():
    _run(NODE_REDUCED)


def test_a_tab_that_shows_its_preview_at_the_click_starts_its_mark_from_the_red_and_the_other_ways_start_as_before():
    _run(NODE_LIT)
    # The memory of a pick (the family left and whether the tab picked was lit) is kept by the head and dropped by a pick of a window.
    source = WIDGET.read_text(encoding="utf-8")
    window = _function(source, "pickWindow")
    assert "markFrom = '';" in window and "markLit = false;" in window
    # It is told at the click, by the tab itself, before the redraw makes a new one, and handed to the kit with the family left.
    tab = _function(source.replace(_style(), ""), "familyTab")
    assert tab.index("markLit = markPreviewed(btn);") < tab.index("rerender();")
    assert "markStart(seg, markFrom, selected ? selected.harness_id : '', markLit);" in _function(source, "renderHarnessSeg")
    assert "lit ? 'tab-mark-was-lit' : 'tab-mark-was-off'" in _function(source, "markStart")
    asking = _function(source, "markPreviewed")
    assert "':hover'" in asking and "':focus-visible'" in asking and "try {" in asking and "catch (err)" in asking


def test_the_pick_of_a_window_draws_no_start_state_on_the_marks_of_the_families(fixture_file):  # noqa: F811
    _run(NODE_WINDOW, TABS_FIXTURE_FILE=fixture_file)
    assert "markFrom = '';" in re.search(r"function pickWindow\(.*?\n    }\n", WIDGET.read_text(encoding="utf-8"), re.S).group(0)


def test_the_order_of_the_family_tabs_lives_in_the_memory_of_the_widget_and_nowhere_else():
    # The family picked last stands first, the others keep the order they were seen in (orderedFamilies): the order is one variable of the
    # script, written by that function alone, and it is kept in no storage, no cookie and no request.
    source = WIDGET.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.replace(_style(), "").splitlines() if not line.strip().startswith("//"))
    order = _function(code, "orderedFamilies")
    assert not re.search(r"localStorage|sessionStorage|indexedDB|document\.cookie|fetch\(|postMessage", order), order
    assert code.count("var familyOrder = [];") == 1
    assert code.replace(order, "").count("familyOrder") == 1, "the variable is declared once and written in orderedFamilies alone"
    # The row is built in that order, in the markup (the order to read is the order to see): no CSS order moves a tab.
    assert "markRow(orderedFamilies(groups, selected).map(" in _function(code, "renderHarnessSeg")
    assert not re.search(r"\.tab-mark[^{]*\{[^}]*\border:", _style()), "no CSS `order` on a tab"


def test_the_helpers_alone_build_a_tab_a_row_and_a_start_state_for_any_number_of_tabs_keys_and_states():
    _run(NODE_KIT)


def test_markTab_markRow_markStart_markPreviewed_and_settleMarks_are_the_only_builders_of_a_tab_a_mark_a_dot_a_number_and_a_row():
    source, css = WIDGET.read_text(encoding="utf-8"), _style()
    for name in ("markTab", "markRow", "markPreviewed", "markStart", "settleMarks"):
        assert source.count("function %s(" % name) == 1, name
    # The code of the widget, the style array and every comment line taken out: every class of the mark tab that the code names stands
    # in the five helpers, which stand together, and nowhere else. The head names none of them.
    code = "\n".join(line for line in source.replace(css, "").splitlines() if not line.strip().startswith("//"))
    assert sorted(set(re.findall(r"tab-mark[\w-]*", code))) == CLASSES
    settle = _function(code, "settleMarks")
    helpers = code[code.index("    function markTab("):code.index(settle) + len(settle)]
    assert code.replace(helpers, "").count("tab-mark") == 0 and code.replace(helpers, "").count("data-mark-key") == 0
    for name in ("markTab", "markRow", "markPreviewed", "markStart", "settleMarks"):
        assert helpers.count("function %s(" % name) == 1
    assert code.count("el('button', 'tab-mark')") == 1 and code.count("el('div', 'tab-mark-row')") == 1
    # The parts call them: a tab for each family and for each ghost (the head) and for each window (the window row), one row for each of
    # the three, the pick told to a row once by the head and once by the window row, the preview asked once by each, settled once.
    calls = lambda name: len(re.findall(r"(?<!function )\b%s\(" % name, code.replace(helpers, "")))
    assert (calls("markTab"), calls("markRow"), calls("markStart"), calls("markPreviewed"), calls("settleMarks")) == (3, 3, 2, 2, 1)
    # The window row's tabs are the number tab (no mark: its number is it), a tab of a tablist; its wiring is all the row adds - no
    # kit class, no role, no pick (role and aria-selected are the kit's), its place (.wt-tab) and the row's place and name.
    windows = _function(code, "windowRow")
    # ... and it wears a ring (the rail's tab, kit: rail): the one ring builder, ringGauge, makes it from the share the tab says.
    # The rail says a window short (shortLength: "5H", "7D", "30D"); the full name (tab.name) stays in the label and the title of the tab.
    assert "markTab({ key: tab.seconds, name: shortLength(tab.seconds), count: g ? avg : '—', note: incomplete ? '*' : '', selected: on," in windows
    assert "var ring = ringGauge(parseFloat(avg), g && avg !== '—' ? 'ok' : 'unknown', 'lg', true, sweeps);" in windows and "ring: ring });" in windows
    assert "tab.name" in windows and "btn.setAttribute('aria-label'" in windows and "btn.title = hint || spoken;" in windows
    assert "var row = markRow(items, true);" in windows and "markStart(row, winFrom, active ? String(active.seconds) : '', winLit, winPressed);" in windows
    assert "markPreviewed(btn)" in windows and "btn.className += ' wt-tab';" in windows and "row.className += ' wt-row';" in windows
    assert not re.search(r"tab-mark|'role'|aria-selected|aria-pressed|'type'", windows), "the kit makes the tab, the row only wires it"
    for wiring in ("setAttribute('aria-controls', 'quotas-win-panel')", "setAttribute('aria-describedby', 'quotas-foot')",
                   "setAttribute('data-focus', 'win:' + hid + ':' + tab.seconds)", "setAttribute('aria-label'", "btn.title = ", "addEventListener('click'"):
        assert wiring in windows, wiring
    # What the head adds to a tab is its wiring: the label, the title, the focus key, the click (and its pip and its 'empty' hook). It
    # sets no pick (aria-pressed is the kit's), no type, no class of the kit, no dot, no number.
    tab = _function(code, "familyTab")
    for wiring in ("setAttribute('aria-label', say)", "setAttribute('data-focus', 'harness:' + group.harness_id)", "btn.title = say",
                   "addEventListener('click'", "btn.className += ' empty'"):
        assert wiring in tab, wiring
    assert "aria-pressed" not in tab and "'type'" not in tab and "tab-mark" not in tab and "appendChild(el('span', 'pip warn seg-pip'))" in tab
    assert "markTab({ key: group.harness_id, mark: familyMark(group)" in tab and "pressed: isOn })" in tab
    seg = _function(code, "renderHarnessSeg")
    assert "tab-mark" not in seg and "aria-pressed" not in seg
    assert "' loading'" not in seg and "ghost.disabled = true;" in seg and "seg.className += ' harness-seg';" in seg
    # The mark is the head's node, with no class of the kit on it: the kit puts its own on it.
    assert "mark.classList.add" not in _function(code, "familyMark")


# ---------------------------------------------------------------------------
# In a real browser (Chromium and WebKit where Playwright has them): the colours and the sizes a browser really gives, the
# animations a pick really starts, the finger's height, reduced motion.

def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _account(family, subject, enabled=True):
    now = datetime.now(timezone.utc)
    return {"key": "%s:%s" % (family, subject), "kind": "profile", "subject_id": subject, "label": "Same label", "caption": "oauth",
            "email": "named@example.com", "plan": "Pro", "enabled": enabled, "signed_in": True, "next_up": False,
            "last_verified_at": _iso(now - timedelta(hours=1)), "verification_state": "passed", "verification_source": "vendor",
            "verified_live": True, "verification": {"tone": "ok", "label": "Verified live"}, "detail": "",
            "quota": {"state": "ok", "label": "30% used", "resets_at": "", "note": "",
                      "constraints": [{"id": "weekly", "label": "Weekly", "used_pct": 30, "resets_at": "", "cooldown_until": "",
                                       "scoped_models": [], "window_seconds": 604800}],
                      "stale": [], "availability": "available", "observed_at": _iso(now - timedelta(minutes=2)), "absence": None}}


def _family(harness, label, accounts):
    return {"harness_id": harness, "family_label": label, "harness_status": "ok", "harness_enabled": True, "provider_family": "",
            "catalog_known": True, "accounts": accounts, "accounts_signed_in": len(accounts), "accounts_unavailable": False}


@pytest.fixture(scope="module")
def tabs_view():
    return {"ok": True, "transport_error": "", "facets": {"catalog": "ok", "accounts": "ok", "quota": "ok"}, "facet_note": "",
            "daemon": {"state": "running", "engine_version": "3.9.4"},
            "groups": [_family("claude", "Claude", [_account("claude", "p1"), _account("claude", "p2")]),
                       _family("codex", "Codex", [_account("codex", "c1"), _account("codex", "c2"), _account("codex", "c3", False)]),
                       _family("openrouter", "OpenRouter", [])]}


def _open(playwright, engine, view, **context):
    try:
        browser = getattr(playwright, engine).launch()
    except Exception as exc:  # no browser build for this Playwright here
        pytest.skip("no %s for Playwright on this machine (%s)" % (engine, type(exc).__name__))
    page = browser.new_context(**context).new_page()
    page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
    page.evaluate(with_i18n(REAL_PAGE_STUB), view)
    page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
    page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    return browser, page


# What each tab looks like, as a browser computes it: the plate (ground, line, shadow), the weight of its text, its box, its ink,
# and its parts: the mark (ink, transform), the name, the dot and the number (ink, weight, width).
SNAP = """() => Object.fromEntries([...document.querySelectorAll('.tab-mark')].map((b) => {
  const cs = getComputedStyle(b);
  const part = (selector) => {
    const n = b.querySelector(selector);
    if (!n) return null;
    const s = getComputedStyle(n);
    return { color: s.color, weight: s.fontWeight, width: n.getBoundingClientRect().width, left: n.getBoundingClientRect().left,
             right: n.getBoundingClientRect().right };
  };
  const mark = getComputedStyle(b.querySelector('.tab-mark-icon'));
  const box = b.getBoundingClientRect();
  return [b.getAttribute('data-focus').split(':')[1], {
    pressed: b.getAttribute('aria-pressed'), background: cs.backgroundColor, image: cs.backgroundImage, shadow: cs.boxShadow,
    border: ['Top', 'Right', 'Bottom', 'Left'].map((side) => cs['border' + side + 'Width'] + ' ' + cs['border' + side + 'Style']),
    weight: cs.fontWeight, color: cs.color, display: cs.display, width: box.width, height: box.height,
    outline: cs.outlineStyle, outlineWidth: cs.outlineWidth, outlineOffset: cs.outlineOffset,
    mark: { color: mark.color, transform: mark.transform, property: mark.transitionProperty, duration: mark.transitionDuration },
    name: part('.tab-mark-name'), dot: part('.tab-mark-dot'), count: part('.tab-mark-count') }];
}))"""
MARK_IS = """([id, colour, transform]) => {
  const s = getComputedStyle(document.querySelector('[data-focus="harness:' + id + '"] .tab-mark-icon'));
  return s.color === colour && s.transform === transform;
}"""
# The tabs of the head stand at their places: none is on its way to its own (the slide of a pick, the keyframes of .tab-mark-slide, puts the
# family picked first and lasts --dur-flip, and the tabs that give way dim for as long), so their boxes are read at rest.
SLID = """() => {
  const tabs = [...document.querySelectorAll('.harness-seg [data-mark-key]')];
  return tabs.length > 0 && tabs.every((t) => t.getAnimations().length === 0);
}"""
PICK_AND_LOOK = """(id) => {
  document.querySelector('[data-focus="harness:' + id + '"]').click();
  return Object.fromEntries([...document.querySelectorAll('.tab-mark')].map((b) => {
    const mark = b.querySelector('.tab-mark-icon');
    return [b.getAttribute('data-focus').split(':')[1], { pressed: b.getAttribute('aria-pressed'), cls: mark.getAttribute('class'),
      animations: mark.getAnimations().map((a) => [a.animationName || null, a.transitionProperty || null, a.effect.getTiming().duration]),
      transition: getComputedStyle(mark).transitionProperty }];
  }));
}"""
# The marks' transitions as a browser really runs them. A click, then NO read of any look from the page until the widget's frame has
# run and the transitions have begun (two frames of the page's own): what the widget does alone, a read of the page could hide. Then
# every transition of every mark is paused and set to a time, and the marks are read there: colour and size.
PICK_AND_RUN = """async ([id, times]) => {
  const ids = ['claude', 'codex', 'openrouter'];
  const mark = (i) => document.querySelector('[data-focus="harness:' + i + '"] .tab-mark-icon');
  const frames = (n) => new Promise((resolve) => { const next = () => (n-- > 0 ? requestAnimationFrame(next) : resolve()); next(); });
  document.querySelector('[data-focus="harness:' + id + '"]').click();
  await frames(2);
  const running = Object.fromEntries(ids.map((i) => [i, mark(i).getAnimations().map((a) => ({
    kind: a.constructor.name, property: a.transitionProperty || a.animationName || null, duration: a.effect.getTiming().duration}))]));
  const classes = Object.fromEntries(ids.map((i) => [i, mark(i).getAttribute('class')]));
  const left = document.querySelectorAll('.tab-mark-was-on, .tab-mark-was-off').length;
  // (the swap of the marks is transitions alone; the only keyframes a pick runs are the kit's slide of the tabs and the dimming of the tabs that give way)
  const keyframed = document.getAnimations().filter((a) => a.constructor.name !== 'CSSTransition' && !['rail-slide', 'rail-yield'].includes(a.animationName)).length;
  const at = {};
  for (const ms of times) {
    at[ms] = {};
    for (const i of ids) {
      for (const a of mark(i).getAnimations()) { a.pause(); a.currentTime = ms; }
      const s = getComputedStyle(mark(i));
      at[ms][i] = { color: s.color, transform: s.transform };
    }
  }
  for (const i of ids) for (const a of mark(i).getAnimations()) a.finish();
  return { running, classes, left, keyframed, at };
}"""
# The start state itself: the look in force at the click (the page reads it at once, which the widget's own read would do a frame
# later), and the classes the marks wear.
PICK_AND_START = """(id) => {
  document.querySelector('[data-focus="harness:' + id + '"]').click();
  return Object.fromEntries([...document.querySelectorAll('.tab-mark')].map((b) => {
    const mark = b.querySelector('.tab-mark-icon');
    const s = getComputedStyle(mark);
    return [b.getAttribute('data-focus').split(':')[1], { cls: mark.getAttribute('class'), color: s.color, transform: s.transform }];
  }));
}"""


# The mark of one tab as a browser gives it, noted by a script of the page across a pick: at every change of the root's tree (the
# microtask after the click's redraw, so the look the new mark has before any frame) and at every frame after. The mark is looked up
# anew each time, as the redraw replaces it: one that stood there before the pick is `old`, one that came with the redraw `fresh`.
# Each note keeps the colour and the size the mark has and the properties that are in transition on it.
WATCH_MARK = """(id) => {
  const pick = () => document.querySelector('[data-focus="harness:' + id + '"] .tab-mark-icon');
  pick().__old = true;
  window.__seen = [];
  const note = (when) => {
    const mark = pick();
    if (!mark) return;
    const s = getComputedStyle(mark);
    window.__seen.push({ when, fresh: !mark.__old, cls: mark.getAttribute('class'), color: s.color, transform: s.transform,
      moving: mark.getAnimations().map((a) => a.transitionProperty || a.animationName || null) });
  };
  new MutationObserver(() => note('redraw')).observe(document.getElementById('root'), { childList: true, subtree: true });
  const tick = () => { note('frame'); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}"""


def _channels(colour):
    return tuple(float(n) for n in re.findall(r"[\d.]+", colour)[:3])


def _away(a, b):
    return sum(abs(x - y) for x, y in zip(_channels(a), _channels(b)))


def _scale(transform):
    return float(re.findall(r"[-\d.]+", transform)[0]) if transform.startswith("matrix") else 1.0
NONE = "none"
SCALE = "matrix(1.2, 0, 0, 1.2, 0, 0)"


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_in_a_real_browser_a_tab_is_no_plate_and_the_mark_alone_tells_the_pick(tabs_view, engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    dark, _ = _themes()
    red, ink = _css_rgb(dark["--accent"]), _css_rgb(dark["--text-primary"])
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, tabs_view)
        try:
            snap = lambda: page.evaluate(SNAP)
            mark_is = lambda tab, colour, transform: page.wait_for_function(MARK_IS, arg=[tab, colour, transform])
            # 1. Rest. No plate on any tab: no ground, no line, no shadow, no outline; the regular weight, the ink, the row's height.
            rest = snap()
            assert list(rest) == ["claude", "codex", "openrouter"]
            for tab, s in rest.items():
                assert s["background"] == "rgba(0, 0, 0, 0)" and s["image"] == "none" and s["shadow"] == "none", (tab, s)
                assert s["border"] == ["0px none"] * 4 or all(b.startswith("0px") for b in s["border"]), (tab, s["border"])
                assert s["outline"] == "none", (tab, s)
                # (a flex item of the row: its inline-flex is blockified to flex, a line of its parts either way)
                assert s["weight"] == "400" and s["color"] == ink and s["display"] in ("flex", "inline-flex"), (tab, s)
                assert s["height"] == 32, (tab, s["height"])
                assert s["name"]["weight"] == "400" and s["name"]["color"] == ink, (tab, s["name"])
            # The pick is the mark: red and grown on the picked tab, in the ink and at its size on the others.
            assert (rest["claude"]["pressed"], rest["codex"]["pressed"]) == ("true", "false")
            assert rest["claude"]["mark"]["color"] == red and rest["claude"]["mark"]["transform"] == SCALE
            for tab in ("codex", "openrouter"):
                assert rest[tab]["mark"]["color"] == ink and rest[tab]["mark"]["transform"] == NONE, (tab, rest[tab]["mark"])
            # The number and the dot: the accent; the dot semibold, the number regular; the dot stands between the name and the number.
            for tab in ("claude", "codex"):
                s = rest[tab]
                assert s["count"]["color"] == red and s["count"]["weight"] == "400", (tab, s["count"])
                assert s["dot"]["color"] == red and s["dot"]["weight"] == "600", (tab, s["dot"])
                assert s["name"]["right"] <= s["dot"]["left"] and s["dot"]["right"] <= s["count"]["left"], (tab, s)
                assert s["dot"]["width"] < 6, "a small dot: %s" % s["dot"]["width"]
            assert rest["openrouter"]["dot"] is None and rest["openrouter"]["count"] is None
            # 2. The pointer previews a tab not picked: its mark turns red and does not grow; nothing else of it changes.
            page.hover('[data-focus="harness:codex"]')
            mark_is("codex", red, NONE)
            hover = snap()
            assert hover["codex"]["background"] == "rgba(0, 0, 0, 0)" and hover["codex"]["shadow"] == "none"
            assert hover["codex"]["weight"] == "400" and hover["codex"]["color"] == ink
            assert hover["codex"]["name"] == rest["codex"]["name"] and hover["codex"]["count"] == rest["codex"]["count"]
            assert hover["codex"]["width"] == rest["codex"]["width"] and hover["claude"]["mark"] == rest["claude"]["mark"]
            page.mouse.move(2, 2)
            mark_is("codex", ink, NONE)
            # 3. The keyboard does the same (:focus-visible), with the base ring round the tab and no plate.
            page.keyboard.press("Shift")
            page.focus('[data-focus="harness:openrouter"]')
            assert page.evaluate("document.activeElement.matches(':focus-visible')") is True
            mark_is("openrouter", red, NONE)
            focus = snap()["openrouter"]
            assert focus["outline"] == "solid" and focus["outlineWidth"] == "2px" and focus["outlineOffset"] == "2px", focus
            assert focus["background"] == "rgba(0, 0, 0, 0)" and focus["shadow"] == "none" and focus["weight"] == "400"
            page.evaluate("document.activeElement.blur()")
            mark_is("openrouter", ink, NONE)
            # 4. The pick (a click on a tab not picked): the tab picked goes first and the others follow in their order (the row is read in the
            # order it is seen); the marks swap, the words stay: no tab changes its size, its text or its weight - it changes its place alone.
            page.evaluate("document.querySelector('[data-focus=\"harness:codex\"]').click()")
            mark_is("codex", red, SCALE)
            mark_is("claude", ink, NONE)
            page.wait_for_function(SLID)   # (the tabs slide to their new places: their boxes are read when they stand there)
            after = snap()
            assert list(after) == ["codex", "claude", "openrouter"], list(after)
            assert after["codex"]["name"]["left"] < after["claude"]["name"]["left"] < after["openrouter"]["name"]["left"], "the order in the row is the order seen"
            assert (after["claude"]["pressed"], after["codex"]["pressed"]) == ("false", "true")
            sized = lambda part: part and {k: v for k, v in part.items() if k not in ("left", "right")}
            for tab in rest:
                for key in ("background", "shadow", "weight", "color", "width", "height"):
                    assert after[tab][key] == rest[tab][key], (tab, key, rest[tab][key], after[tab][key])
                for key in ("name", "dot", "count"):
                    assert sized(after[tab][key]) == sized(rest[tab][key]), (tab, key, rest[tab][key], after[tab][key])
            # 5. In the light theme the red is the same red and the ink is the light ink.
            _, light = _themes()
            page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
            mark_is("codex", _css_rgb(light["--accent"]), SCALE)
            assert _css_rgb(light["--accent"]) == red
            mark_is("claude", _css_rgb(light["--text-primary"]), NONE)  # the mark's ink fades with the theme too (its own transition)
            lit = snap()
            assert lit["claude"]["mark"]["color"] == _css_rgb(light["--text-primary"]) and lit["codex"]["count"]["color"] == red
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_in_a_real_browser_a_pick_fades_the_colour_and_the_size_of_both_marks_by_their_own_transition_and_no_keyframe(tabs_view, engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    dark, _ = _themes()
    red, ink = _css_rgb(dark["--accent"]), _css_rgb(dark["--text-primary"])
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, tabs_view)
        try:
            # The marks fade by a transition: colour and size, --dur-5 on --ease.
            first = page.evaluate(SNAP)
            assert first["claude"]["mark"]["property"] == "color, transform", first["claude"]["mark"]
            assert first["claude"]["mark"]["duration"] == "0.22s, 0.22s", first["claude"]["mark"]
            # 1. The pick (claude -> codex), the page reading nothing of its own until the widget's frame has run: the two marks
            # that changed run a transition of colour and one of size (220ms) each, the third none, no keyframe animation anywhere,
            # and the start state is gone.
            ran = page.evaluate(PICK_AND_RUN, ["codex", [30, 110]])
            both = [{"kind": "CSSTransition", "property": "color", "duration": 220}, {"kind": "CSSTransition", "property": "transform", "duration": 220}]
            assert ran["running"] == {"claude": both, "codex": both, "openrouter": []}, ran["running"]
            assert ran["keyframed"] == 0 and ran["left"] == 0, ran
            assert all("mark-was" not in c for c in ran["classes"].values()), ran["classes"]
            # 2. In the middle of those transitions both marks are neither at the look they came from nor at the one they go to, in
            # colour and in size. The ease is quick at the start (cubic-bezier(.2,.8,.2,1)): the size is about 1.1 at 30ms, and at
            # 110ms it has nearly arrived, but not quite.
            early, late = ran["at"]["30"], ran["at"]["110"]
            for name, (came, goes, a, b) in {"codex": (ink, red, 1.0, 1.2), "claude": (red, ink, 1.2, 1.0)}.items():
                for when, lo, hi, far in ((early, 1.05, 1.16, 40), (late, 1.005, 1.195, 6)):
                    assert _away(when[name]["color"], came) > far and _away(when[name]["color"], goes) > far, (name, when[name])
                    assert lo < _scale(when[name]["transform"]) < hi, (name, when[name])
            assert early["openrouter"] == {"color": ink, "transform": NONE} == late["openrouter"]
            # Over, the marks are where the rules put them: the picked one red and grown, the other in the ink.
            page.wait_for_function(MARK_IS, arg=["codex", red, SCALE])
            page.wait_for_function(MARK_IS, arg=["claude", ink, NONE])
            # 3. The start state is the look they had: the mark picked in the tab's ink at its size, the mark left in the picked look
            # (read at the click, which the widget would do a frame later).
            start = page.evaluate(PICK_AND_START, "claude")
            assert "tab-mark-was-off" in start["claude"]["cls"].split() and (start["claude"]["color"], start["claude"]["transform"]) == (ink, NONE), start
            assert "tab-mark-was-on" in start["codex"]["cls"].split() and (start["codex"]["color"], start["codex"]["transform"]) == (red, SCALE), start
            assert start["openrouter"]["cls"] == "harness-initial tab-mark-icon", start
            page.wait_for_function("document.querySelectorAll('.tab-mark-was-on, .tab-mark-was-off').length === 0")
            page.wait_for_function(MARK_IS, arg=["claude", red, SCALE])
            page.wait_for_function(MARK_IS, arg=["codex", ink, NONE])
            # 4. A redraw of fresh data (the 30 seconds') draws none and starts none.
            page.evaluate("[...document.querySelectorAll('.tab-mark-icon')].forEach((m) => { m.__drawn = true; })")
            page.evaluate("document.querySelector('[data-focus=\"refresh\"]').click()")
            page.wait_for_function("![...document.querySelectorAll('.tab-mark-icon')].some((m) => m.__drawn)")
            assert page.evaluate("document.querySelectorAll('.tab-mark-was-on, .tab-mark-was-off').length") == 0
            assert page.evaluate("[...document.querySelectorAll('.tab-mark-icon')].every((m) => m.getAnimations().length === 0)") is True
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("how", ["pointer", "keyboard"])
def test_in_a_real_browser_a_tab_showing_its_preview_is_picked_without_its_mark_going_through_the_ink(tabs_view, engine, how):
    """The defect: under the pointer (or the keyboard) the tab's mark is already red, the pick drew a new mark from the ink, and the
    reader saw red - grey - red. Now no look the new mark has, from the redraw to the end of the motion, is the ink: it is red all the
    way, and the one thing that moves is the size (a transition of the transform, none of the colour)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    dark, _ = _themes()
    red, ink = _css_rgb(dark["--accent"]), _css_rgb(dark["--text-primary"])
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, tabs_view)
        try:
            tab = '[data-focus="harness:codex"]'
            if how == "pointer":
                page.hover(tab)
            else:
                page.keyboard.press("Shift")
                page.focus(tab)
                assert page.evaluate("document.activeElement.matches(':focus-visible')") is True
            page.wait_for_function(MARK_IS, arg=["codex", red, NONE])  # the preview is shown: red, not grown
            page.evaluate(WATCH_MARK, "codex")
            if how == "pointer":
                page.mouse.down()
                page.mouse.up()
            else:
                page.keyboard.press("Enter")
            page.wait_for_function(MARK_IS, arg=["codex", red, SCALE])
            seen = page.evaluate("window.__seen")
            fresh = [n for n in seen if n["fresh"]]
            # The new mark's first look, before any frame: the preview's red at its size, in the lit start state.
            assert fresh and fresh[0]["when"] == "redraw", seen
            assert fresh[0]["color"] == red and fresh[0]["transform"] == NONE, fresh[0]
            assert "tab-mark-was-lit" in fresh[0]["cls"].split() and "tab-mark-was-off" not in fresh[0]["cls"].split(), fresh[0]
            # And every look after it: red, never the ink; the size is the one thing that moves (no transition of the colour).
            assert {n["color"] for n in fresh} == {red}, [n for n in fresh if n["color"] != red]
            assert ink not in {n["color"] for n in seen if n["fresh"]}
            moving = {p for n in fresh for p in n["moving"]}
            assert moving == {"transform"}, moving
            assert [n for n in fresh if n["transform"] != NONE], "the size does grow"
            # The tab left is not touched by this: it goes from the picked look to the ink, as always. (The pick puts the tab picked first, and the
            # tab left slides to the place the pointer still stands on: a pointer on it is a preview of it, so the pointer goes off the head first.)
            if how == "pointer":
                page.mouse.move(2, 2)
            page.wait_for_function(MARK_IS, arg=["claude", ink, NONE])
        finally:
            browser.close()


# A page whose read of the quotas never answers: the widget stays on its first screen, the three ghosts.
HOLD_STUB = r"""
(view) => {
  window.fetch = (url, options = {}) => {
    return new Promise(() => {});
  };
}
"""
GHOSTS = """() => [...document.querySelectorAll('.tab-mark')].map((b) => {
  const cs = getComputedStyle(b);
  const icon = getComputedStyle(b.querySelector('.tab-mark-icon'));
  return { cls: b.getAttribute('class'), disabled: b.disabled, pressed: b.getAttribute('aria-pressed'), name: b.textContent,
    color: cs.color, background: cs.backgroundColor, border: cs.borderTopWidth, weight: cs.fontWeight, height: b.getBoundingClientRect().height,
    icon: { color: icon.color, transform: icon.transform } };
})"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_in_a_real_browser_the_ghosts_are_tabs_of_the_kit_faint_and_dead_and_the_preview_never_lights_them(engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    dark, _ = _themes()
    faint = _css_rgb(dark["--text-disabled"])
    with sync_api.sync_playwright() as playwright:
        try:
            browser = getattr(playwright, engine).launch()
        except Exception as exc:  # no browser build for this Playwright here
            pytest.skip("no %s for Playwright on this machine (%s)" % (engine, type(exc).__name__))
        try:
            page = browser.new_page()
            page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
            page.evaluate(with_i18n(HOLD_STUB), {})
            page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
            page.wait_for_selector(".tab-mark:disabled")
            ghosts = page.evaluate(GHOSTS)
            assert [g["name"] for g in ghosts] == ["Codex", "Claude", "Cursor"]
            for g in ghosts:
                # The look the ghost always had: the faint ink (the head's rule and the base's disabled ink agree), no plate, the row's
                # height, the regular weight; its mark in the same ink and at its size; nothing picked.
                assert g["disabled"] is True and g["pressed"] is None and g["cls"] == "tab-mark", g
                assert g["color"] == faint and g["icon"] == {"color": faint, "transform": NONE}, g
                assert g["background"] == "rgba(0, 0, 0, 0)" and g["border"] == "0px" and g["weight"] == "400" and g["height"] == 32, g
            # The pointer on a ghost lights nothing: the preview leaves a tab that is disabled out.
            page.hover(".tab-mark:disabled", force=True)
            assert page.evaluate("document.querySelector('.tab-mark:disabled').matches(':hover')") is True
            assert page.evaluate(GHOSTS)[0]["icon"] == {"color": faint, "transform": NONE}
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_in_a_real_browser_reduced_motion_cuts_the_fade_and_the_start_state_but_not_the_pick(tabs_view, engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    dark, _ = _themes()
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, tabs_view, reduced_motion="reduce")
        try:
            assert page.evaluate(SNAP)["claude"]["mark"]["property"] == "none", "no fade of the mark"
            looked = page.evaluate(PICK_AND_LOOK, "codex")
            assert all(not v["animations"] and v["transition"] == "none" for v in looked.values()), looked
            assert all("mark-was" not in v["cls"] for v in looked.values()), looked
            # The pick itself is there at once: red and grown, in the same frame.
            now = page.evaluate(SNAP)
            assert now["codex"]["mark"]["color"] == _css_rgb(dark["--accent"]) and now["codex"]["mark"]["transform"] == SCALE
            assert now["claude"]["mark"]["transform"] == NONE
        finally:
            browser.close()


def test_in_a_real_browser_a_finger_gets_a_tab_of_the_tap_height(tabs_view):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, "chromium", tabs_view, has_touch=True, is_mobile=True, viewport={"width": 390, "height": 844})
        try:
            if not page.evaluate("matchMedia('(pointer: coarse)').matches"):
                pytest.skip("this browser does not emulate a coarse pointer")
            heights = {tab: s["height"] for tab, s in page.evaluate(SNAP).items()}
            assert heights == {"claude": 44, "codex": 44, "openrouter": 44}, heights
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The number tab of the window row in a real browser: the colours the design sheet gives it, a preview that is the ink and never the
# red, a pick that moves colour alone, and nothing that grows - in both themes.

WINDOW_SNAP = """() => Object.fromEntries([...document.querySelectorAll('.wt-tab')].map((b) => {
  const part = (selector) => {
    const n = b.querySelector(selector);
    if (!n) return null;
    const s = getComputedStyle(n), r = n.getBoundingClientRect();
    return { color: s.color, weight: s.fontWeight, size: s.fontSize, transform: s.transform, left: r.left, width: r.width, height: r.height };
  };
  const cs = getComputedStyle(b), box = b.getBoundingClientRect();
  return [b.getAttribute('data-focus') ? b.getAttribute('data-focus').split(':')[2] : 'grey', {
    selected: b.getAttribute('aria-selected'), color: cs.color, weight: cs.fontWeight, size: cs.fontSize, background: cs.backgroundColor,
    shadow: cs.boxShadow, image: cs.backgroundImage, left: box.left, width: box.width, height: box.height, transform: cs.transform,
    name: part('.tab-mark-name'), dot: part('.tab-mark-dot'), count: part('.tab-mark-count'), note: part('.tab-mark-note'), ring: part('.tab-mark-ring') }];
}))"""
FIGURE_IS = """([seconds, colour]) => {
  const n = document.querySelector('[data-focus="win:claude:' + seconds + '"] .tab-mark-count');
  return !!n && getComputedStyle(n).color === colour;
}"""
NOTE_IS = """([seconds, colour]) => {
  const n = document.querySelector('[data-focus="win:claude:' + seconds + '"] .tab-mark-note');
  return !!n && getComputedStyle(n).color === colour;
}"""


TOKEN_INK = """(name) => { const p = document.createElement('i'); p.style.color = 'var(' + name + ')'; document.body.appendChild(p);
  const c = getComputedStyle(p).color; p.remove(); return c; }"""
RING_IS = """([seconds, scale]) => {
  const ring = document.querySelector('[data-focus="win:claude:' + seconds + '"] .tab-mark-ring');
  return !!ring && Math.abs(new DOMMatrix(getComputedStyle(ring).transform).a - scale) < 0.005;
}"""


def _boxes(snap):
    """Where each tab stands and how big it and its words are. A pick, a preview and a focus move colour, the weight of the word and the scale
    of the ring (a transform: no box), so the boxes stay the same through all of them."""
    out = {}
    for tab, s in snap.items():
        out[tab] = {"tab": [s["left"], s["width"], s["height"]]}
        # (the share, the badge with the short name and the star stand inside the ring's box and grow with it - one transform for all - so their
        # drawn boxes are the ring's to move; they are not boxes of the row's layout: the row's own box is what must stay)
        for part in ():
            out[tab][part] = s[part] and [s[part]["left"], s[part]["width"], s[part]["height"]]
    return out


def _same_boxes(a, b, give=1.5):
    """The boxes of two snapshots are the same: the row's own box and every height exactly; a word is as wide as its letters, so the semibold
    of the picked row takes up to a pixel and a half more of the line than the regular (the word and the star now stand inside the ring's box, the ring's to move, so only the row's own box and heights are compared)."""
    a, b = _boxes(a), _boxes(b)
    if sorted(a) != sorted(b):
        return False
    for tab in a:
        if a[tab]["tab"] != b[tab]["tab"]:
            return False
        for part in ():
            x, y = a[tab][part], b[tab][part]
            if (x is None) != (y is None):
                return False
            if x and not (abs(x[0] - y[0]) <= give and abs(x[1] - y[1]) <= give and abs(x[2] - y[2]) <= 0.01):
                return False
    return True


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_in_a_real_browser_a_rail_row_tells_the_pick_by_its_word_its_share_and_its_ring_and_a_preview_is_the_ink_never_the_red(fixture_file, engine):  # noqa: F811
    """kit: rail. The row: the short name 8 on 600 in the meta ink (the badge in the gap of the ring), the share 11 on 600 in the meta ink (inside the ring), the star
    9 on 600 (at the badge), the ring x1 on its track; the picked row takes the ink for its name, the red for its share (and its star), and its ring grows to x1.2; the
    pointer or the keyboard on a row that is not picked brings its word to the ink and its ring to x1.12 (never the red); a window the family does
    not report is no row at all (no grey place, nothing aria-disabled); a pick moves colour, the weight of the word and the scale of the ring, no
    box. The row has no plate, no shadow and no dot."""
    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    # Claude, both windows read in part: the 5-hour is picked and wears its star, the weekly is not picked and wears its own.
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, views["reading_both"])
        try:
            snap = lambda: page.evaluate(WINDOW_SNAP)
            ink_of = lambda name: page.evaluate(TOKEN_INK, name)
            ink, second, meta, red = (ink_of(t) for t in ("--text-primary", "--text-secondary", "--text-meta", "--accent"))
            figure_is = lambda seconds, colour: page.wait_for_function(FIGURE_IS, arg=[str(seconds), colour])
            ring_is = lambda seconds, scale: page.wait_for_function(RING_IS, arg=[str(seconds), scale])
            words = lambda s, tab: [s[tab]["name"]["color"], s[tab]["name"]["weight"]]
            # 1. Rest. The picked row: the word in the ink and the semibold weight, the share and the star in the red, the ring x1.2. The row not
            # picked: the word in the secondary ink (400), the share in the meta ink (600), the star the ink; the ring x1.
            figure_is(604800, meta)
            ring_is(18000, 1.2)
            ring_is(604800, 1)
            rest = snap()
            five, week = rest["18000"], rest["604800"]
            assert sorted(rest) == ["18000", "604800"], "only the windows the family reports are rows (Claude has no 30-day window: no third row)"
            assert page.evaluate("document.querySelectorAll('.wt-tab[aria-disabled=\"true\"]').length") == 0, "no grey place"
            assert (five["selected"], week["selected"]) == ("true", "false")
            assert [five["name"]["color"], five["name"]["weight"], five["name"]["size"]] == [ink, "600", "7.5px"], five
            assert [five["count"]["color"], five["count"]["weight"], five["count"]["size"]] == [red, "600", "11px"], five
            assert [five["note"]["color"], five["note"]["weight"], five["note"]["size"]] == [red, "600", "9px"], five
            assert [week["name"]["color"], week["name"]["weight"], week["name"]["size"]] == [meta, "600", "7.5px"], week
            assert [week["count"]["color"], week["count"]["weight"], week["count"]["size"]] == [meta, "600", "11px"], week
            assert [week["note"]["color"], week["note"]["weight"], week["note"]["size"]] == [ink, "600", "9px"], "the star of a row not picked is the ink"
            for tab in (five, week):
                # a row is no plate, and the dot of the number tab is not drawn in the rail
                assert tab["background"] == "rgba(0, 0, 0, 0)" and tab["shadow"] == "none" and tab["image"] == "none" and tab["height"] == 74, tab
                assert tab["dot"] is None, tab  # the rail draws no dot at all
            assert meta != ink != second and red not in (week["name"]["color"], week["count"]["color"], week["note"]["color"], five["name"]["color"])
            # 2. The pointer on the row not picked: its word rises to the ink and its ring to x1.12 - the red is not for a preview; the picked
            # row stays as it is; and nothing moves (the same boxes).
            page.hover('[data-focus="win:claude:604800"]')
            ring_is(604800, 1.12)
            hover = snap()
            assert hover["604800"]["name"]["color"] == ink and hover["604800"]["name"]["weight"] == "600", hover["604800"]
            assert red not in (hover["604800"]["count"]["color"], hover["604800"]["note"]["color"], hover["604800"]["name"]["color"])
            assert [hover["18000"][p]["color"] for p in ("name", "count", "note")] == [ink, red, red]
            ring_is(18000, 1.2)
            assert _same_boxes(hover, rest), "a preview moves no box"
            page.mouse.move(2, 2)
            ring_is(604800, 1)
            figure_is(604800, meta)
            # 3. The keyboard on it (:focus-visible) does the same.
            page.keyboard.press("Shift")
            page.focus('[data-focus="win:claude:604800"]')
            assert page.evaluate("document.activeElement.matches(':focus-visible')") is True
            ring_is(604800, 1.12)
            focus = snap()
            assert focus["604800"]["name"]["color"] == ink and red not in (focus["604800"]["count"]["color"], focus["604800"]["note"]["color"])
            assert _same_boxes(focus, rest)
            page.evaluate("document.activeElement.blur()")
            ring_is(604800, 1)
            # 4. There is no grey place to hover: the rail holds the two windows Claude reports and nothing else.
            assert page.evaluate("document.querySelectorAll('.rail .tab-mark-rail').length") == 2
            # 5. The pick: the weekly's word turns to the ink (600), its share and star red, its ring grows to x1.2; the 5-hour's word goes to the
            # secondary ink, its share to the meta ink, its star to the ink, its ring back to x1; no box moves.
            page.evaluate("document.querySelector('[data-focus=\"win:claude:604800\"]').click()")
            figure_is(604800, red)
            figure_is(18000, meta)
            ring_is(604800, 1.2)
            ring_is(18000, 1)
            page.wait_for_function(NOTE_IS, arg=["18000", ink])
            after = snap()
            assert (after["18000"]["selected"], after["604800"]["selected"]) == ("false", "true")
            assert [after["604800"][p]["color"] for p in ("name", "count", "note")] == [ink, red, red]
            assert words(after, "604800") == [ink, "600"] and words(after, "18000") == [meta, "600"]
            assert [after["18000"][p]["color"] for p in ("count", "note")] == [meta, ink]
            assert _same_boxes(after, rest), "a pick moves colour, weight and scale: no box"
            # 6. The light theme: the same roles, the light inks, the same red.
            page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
            l_ink, l_meta = ink_of("--text-primary"), ink_of("--text-meta")
            assert ink_of("--accent") == red and l_ink != ink and l_meta != meta
            page.wait_for_function(FIGURE_IS, arg=["18000", l_meta])
            page.wait_for_function(NOTE_IS, arg=["18000", l_ink])
            page.wait_for_function(NOTE_IS, arg=["604800", red])
            bright = snap()
            assert [bright["604800"][p]["color"] for p in ("name", "count", "note")] == [l_ink, red, red]
            assert [bright["18000"][p]["color"] for p in ("name", "count", "note")] == [l_meta, l_meta, l_ink]
            assert _same_boxes(bright, rest)
        finally:
            browser.close()


# The ring of the row of the weekly window and its star, frame by frame (requestAnimationFrame): the scale of the box of the ring and the colour of the star
# and of the number, found again by the row's id at every frame (a pick redraws the rail, so the node is a new one).
PRESS_REC = """() => {
  const rec = { on: false, frames: [], t0: 0 };
  const scaleOf = (n) => { const m = n && getComputedStyle(n).transform; const mm = m && m !== 'none' ? m.match(/matrix\\(([^)]+)\\)/) : null; return mm ? Number(mm[1].split(',')[0]) : 1; };
  const tick = (ts) => {
    if (!rec.on) return;
    const row = document.getElementById('quotas-win-604800');
    const q = (s) => row && row.querySelector(s);
    const left = document.getElementById('quotas-win-18000');
    rec.frames.push({ t: ts - rec.t0, scale: scaleOf(q('.tab-mark-ring')), width: q('.tab-mark-ring') ? q('.tab-mark-ring').getBoundingClientRect().width : 0,
                      picked: row && row.getAttribute('aria-selected') === 'true',
                      star: q('.tab-mark-note') && getComputedStyle(q('.tab-mark-note')).color, number: q('.tab-mark-count') && getComputedStyle(q('.tab-mark-count')).color,
                      leftStar: left && left.querySelector('.tab-mark-note') ? getComputedStyle(left.querySelector('.tab-mark-note')).color : null });
    requestAnimationFrame(tick);
  };
  window.__rec = { start() { rec.on = true; rec.frames = []; rec.t0 = performance.now(); requestAnimationFrame(tick); }, stop() { rec.on = false; return rec.frames; } };
}"""
# Where the stamp stands (in the plate, not at the foot of the rail; with the account card in the plate it stands in the card's summary, cardTake) and whether it is whole there.
STAMP_ROOM = """() => {
  const st = document.querySelector('.wt-stamp'), plate = document.querySelector('.wt-plate');
  return { room: st.clientWidth, scroll: st.scrollWidth, inRail: !!st.closest('.rail'), inPlate: !!st.closest('.wt-plate'), bot: !!document.querySelector('.rail-bot'),
           rail: document.querySelector('.rail').getBoundingClientRect().width, page: document.documentElement.scrollWidth, inner: innerWidth };
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_in_a_real_browser_a_row_pressed_and_let_go_goes_from_the_press_to_its_picked_size_and_its_star_fades_with_its_number(fixture_file, engine):  # noqa: F811
    """kit: rail, mark tab. A row not picked is held pressed for 200 ms (the mouse) and let go: the click picks it. Its ring is at --press (.94) when the
    button comes up and the drawing of the pick starts from that press, so the frames go from .94 to --mark-on (x1.2) on the spring with no leap (the step
    of a frame is never over twice the steps next to it; before, the first frame leapt from .94 to the preview's x1.12). Its star changes from the ink to
    the red with its number (not at once: the first frame of the pick still has the ink), and the star of the row left from the red to the ink. The stamp
    stands in the plate (the rail has no foot), whole."""
    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, views["reading_both"])
        try:
            ink, red, meta = (page.evaluate(TOKEN_INK, t) for t in ("--text-primary", "--accent", "--text-meta"))
            room = page.evaluate(STAMP_ROOM)
            assert room["inPlate"] and not room["inRail"] and not room["bot"], ("the stamp stands in the plate, the rail has no foot", room)
            assert room["scroll"] <= room["room"] + 1 and room["page"] <= room["inner"], ("the stamp is whole and the page has no sideways scroll", room)
            page.evaluate(PRESS_REC)
            sel = '[data-focus="win:claude:604800"]'
            page.wait_for_function(RING_IS, arg=["604800", 1])
            page.hover(sel)
            page.wait_for_function(RING_IS, arg=["604800", 1.12])
            page.evaluate("window.__rec.start()")
            page.wait_for_timeout(100)
            page.mouse.down()
            page.wait_for_function(RING_IS, arg=["604800", 0.94])
            page.wait_for_timeout(100)
            page.mouse.up()
            page.wait_for_timeout(900)
            frames = page.evaluate("window.__rec.stop()")
            first = next(i for i, f in enumerate(frames) if f["picked"])
            after = frames[first:]
            steps = [abs(after[i + 1]["scale"] - after[i]["scale"]) for i in range(len(after) - 1)]
            # the speed of a frame (a step over the time it took: a browser may hold a frame back), the leap is a speed far over its neighbours'
            speeds = [steps[i] / max(after[i + 1]["t"] - after[i]["t"], 1) for i in range(len(steps))]
            assert frames[first]["scale"] <= 0.96, ("the pick starts from the press, not from the preview's size", frames[first])
            assert frames[first - 1]["scale"] <= 0.96, "the press was held to the end"
            assert abs(after[-1]["scale"] - 1.2) < 0.005, after[-1]
            assert max(speeds) <= 2 * (max(speeds[1:4]) + 1e-9), ("no frame leaps more than twice the frames next to it", speeds[:6], [(round(f["t"], 1), round(f["scale"], 3)) for f in after[:7]])
            assert max(steps) <= 0.09, steps[:6]  # before: .94 -> 1.12, a step of .18 in a frame
            # the same in pixels: the width of the ring's box never leaps more than 4px in a frame (before: .18 of 47px = 8.5px)
            jumps = [abs(after[i + 1]["width"] - after[i]["width"]) for i in range(len(after) - 1)]
            assert max(jumps) <= 4, ("no frame of the pick leaps over 4px in the width of the ring", [round(j, 2) for j in jumps[:8]])
            # The star and the number: the ink at the first frame of the pick, the red at the end (a fade between, not a swap).
            assert after[0]["star"] == ink and after[-1]["star"] == red, (after[0]["star"], after[-1]["star"])
            assert after[0]["number"] != red and after[-1]["number"] == red, (after[0]["number"], after[-1]["number"])
            assert any(f["star"] not in (ink, red) for f in after), "the star passes through the colours between: it fades"
            # and the star of the row left fades the other way, from the red to the ink, with the same transition as its number
            assert after[0]["leftStar"] == red and after[-1]["leftStar"] == ink, (after[0]["leftStar"], after[-1]["leftStar"])
            assert any(f["leftStar"] not in (ink, red) for f in after), "the star of the row left fades too"
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_in_a_real_browser_a_row_picked_by_the_keyboard_starts_from_its_focus_size_not_from_the_press(fixture_file, engine):  # noqa: F811
    """kit: rail, mark tab. A pick by the keyboard (Enter on the focused row: a click with no pointer) draws as it did before the press state: its ring
    stands at the focus size (--mark-hover, x1.12) when the pick is drawn and goes to --mark-on (x1.2) with no leap; it never dips to --press (.94)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, views["reading_both"])
        try:
            page.evaluate(PRESS_REC)
            sel = '[data-focus="win:claude:604800"]'
            page.wait_for_function(RING_IS, arg=["604800", 1])
            page.keyboard.press("Shift")
            page.focus(sel)
            assert page.evaluate("document.activeElement.matches(':focus-visible')") is True
            page.wait_for_function(RING_IS, arg=["604800", 1.12])
            page.evaluate("window.__rec.start()")
            page.wait_for_timeout(100)
            page.keyboard.press("Enter")
            page.wait_for_timeout(900)
            frames = page.evaluate("window.__rec.stop()")
            first = next(i for i, f in enumerate(frames) if f["picked"])
            after = frames[first:]
            assert min(f["scale"] for f in frames) >= 1.09, ("the keyboard never presses the ring down", min(f["scale"] for f in frames))
            assert 1.09 <= frames[first]["scale"] <= 1.16, ("the pick starts from the focus size", frames[first])
            assert abs(after[-1]["scale"] - 1.2) < 0.005, after[-1]
            steps = [abs(after[i + 1]["scale"] - after[i]["scale"]) for i in range(len(after) - 1)]
            assert max(steps) <= 0.09, steps[:6]
        finally:
            browser.close()


# The words of the stamp as the plate draws them (one line in the row of the figure, on its right, before the chart word). The real stamp is given the words of a
# language (the number read, the oldest reading's age): the line must stay one line, whole (nothing cut, no scroll across), right of the figure and left of the chart word.
# English as the stand shows it and Russian as the app's table would give it.
STAMP_FIT = """(words) => {
  const st = document.querySelector('.wt-stamp'), r = (n) => n.getBoundingClientRect();
  const put = (cls, text) => { const n = st.querySelector(cls); if (n) n.textContent = text; };
  put('.wt-read', words[0] + ' '); put('.wt-n', words[1]); put('.wt-age', words[2]);
  const fig = document.querySelector('.wt-plate .l-fig'), ctl = document.querySelector('.wt-plate .l-chart'), plate = document.querySelector('.wt-plate');
  const line = parseFloat(getComputedStyle(st).lineHeight);
  return { text: st.textContent, room: st.clientWidth, scroll: st.scrollWidth, height: r(st).height, line, white: getComputedStyle(st).whiteSpace,
           box: [r(st).left, r(st).right], fig: r(fig).right, ctl: ctl ? r(ctl).left : null, plate: [r(plate).left, r(plate).right],
           page: document.documentElement.scrollWidth, inner: innerWidth };
}"""
STAMP_WORDS = {"en": ["Read", "13 of 16", "10 min ago"],
               "ru": ["Прочитано", "13 из 16", "10 мин назад"]}
STAMP_WORDS_LONG = {"en": ["Read", "113 of 116", "2 days ago"], "ru": ["Прочитано", "113 из 116", "2 дн. назад"]}


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [860, 1020, 1280])
@pytest.mark.parametrize("language", ["en", "ru"])
def test_in_a_real_browser_the_stamp_in_the_plate_is_one_whole_line_between_the_figure_and_the_chart_word_in_both_languages(fixture_file, engine, width, language):  # noqa: F811
    """capsule: window tabs, the stamp. On the wide shell (860px and over) the stamp stands in the row of the plate's figure, on its right, before the chart word: one line (the number,
    the dot, the age; no wrap), whole - its content does not overflow its box - right of the figure and left of the chart word, inside the plate, and the page has no sideways scroll,
    in English and with the longer Russian words ("Прочитано 113 из 116 · 2 дн. назад")."""
    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, views["reading_both"], viewport={"width": width, "height": 800})
        try:
            for words in (STAMP_WORDS[language], STAMP_WORDS_LONG[language]):
                got = page.evaluate(STAMP_FIT, words)
                where = (engine, width, language, got)
                assert words[0] in got["text"] and words[2] in got["text"], where
                assert got["white"] == "nowrap" and got["height"] <= got["line"] + 1, ("one line", where)
                assert got["scroll"] <= got["room"] + 1, ("the stamp's content does not overflow its box", where)
                assert got["box"][0] >= got["fig"] - 0.5 and got["box"][1] <= got["plate"][1] + 0.5, ("right of the figure, inside the plate", where)
                assert got["ctl"] is None or got["box"][1] <= got["ctl"] + 0.5, ("before the chart word", where)
                assert got["page"] <= got["inner"], ("no sideways scroll", where)
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# The rings stand on the axis of the rail (kit: rail) and sweep (kit: ring, motion). Rule: the axis is the middle of the WHOLE tab of the family picked, and the tab
# stands at the edge of the widget, as far from it as from the top (the page's --shell-card-gap, 8px): "the left is emptier than the top". The rail is what a ring needs at
# the axis of the widest tab of the row: half of that tab, half of the ring, the growth of the ring that is picked and the air at the card (--rail-w), the same for every
# family (the edge of the card does not move with the pick). Everything below is measured from the edge of the widget (x = 0) and the top (y = 0).
CENTRE = """() => {
  const r = (n) => n.getBoundingClientRect();
  const rail = r(document.querySelector('.rail')), card = r(document.querySelector('.shell-card')).left, c0 = (a, b) => (a + b) / 2;
  const tabs = [...document.querySelectorAll('.harness-seg .tab-mark')];
  const picked = tabs.find((t) => t.getAttribute('aria-pressed') === 'true');
  const rings = [...document.querySelectorAll('.tab-mark-rail')].map((t) => { const b = r(t.querySelector('.tab-mark-ring')), c = t.querySelector('.tab-mark-count');
    return { sel: t.getAttribute('aria-selected') === 'true', axis: c0(b.left, b.right), count: c ? c0(r(c).left, r(c).right) : null }; });
  const st = document.querySelector('.wt-stamp');
  const lead = document.querySelector('.rail-lead');
  const gap = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--shell-card-gap'));
  return { rail: rail.width, card, gap, widest: Math.max(...tabs.map((t) => r(t).width)), key: picked && picked.getAttribute('data-mark-key'),
           tab: picked && { left: r(picked).left, top: r(picked).top, mid: c0(r(picked).left, r(picked).right) }, rings,
           stamp: st && { inRail: !!st.closest('.rail'), inPlate: !!st.closest('.wt-plate'), bot: !!document.querySelector('.rail-bot') },
           lead: lead && c0(r(lead).left, r(lead).right), page: document.documentElement.scrollWidth, inner: innerWidth };
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [860, 1020, 1280])
def test_in_a_real_browser_the_axis_of_the_rail_is_the_middle_of_the_tab_picked_at_the_same_air_as_the_top_and_the_rail_has_no_stamp(fixture_file, engine, width):  # noqa: F811
    """kit: rail / capsule: window tabs, head. From 860px: the tab picked stands as far from the edge of the widget as from its top
    (--shell-card-gap, +-1), the rail is what the ring needs at the axis of the widest tab of the row (+-1): half of that tab, half of the ring, its growth when picked and 8px of air at
    the card (the same for every family: test_window_tabs, the four families), and the centre of every ring (the
    picked one, x1.2, too), the number inside it, the thread from the head and the tab picked are on one vertical (+-1); the stamp is not in the rail (it stands in the plate), and
    the page has no sideways scroll."""
    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, views["reading_both"], viewport={"width": width, "height": 800})
        try:
            at = page.evaluate(CENTRE)
            where = (engine, width, at)
            assert abs(at["tab"]["left"] - at["gap"]) <= 1 and abs(at["tab"]["top"] - at["gap"]) <= 1, ("the tab stands as far from the left as from the top", where)
            assert abs(at["rail"] - (at["widest"] / 2 + 47 / 2 + 47 * 0.2 / 2 + 8)) <= 1 and abs(at["card"] - (at["gap"] + at["rail"])) <= 1, ("the rail: what the ring asks for on the axis of the widest tab", where)
            assert at["rings"] and any(r["sel"] for r in at["rings"]) and any(not r["sel"] for r in at["rings"]), where
            for ring in at["rings"]:
                assert abs(ring["axis"] - at["tab"]["mid"]) <= 1, ("the ring on the middle of the tab", where)
                assert ring["count"] is None or abs(ring["count"] - at["tab"]["mid"]) <= 1.2, where
            assert abs(at["lead"] - at["tab"]["mid"]) <= 1, ("the thread from the head", where)
            # (the rail is rings and the ladder alone; the stamp stands in the plate, so it is no part of the axis)
            assert at["stamp"] and at["stamp"]["inPlate"] and not at["stamp"]["inRail"] and not at["stamp"]["bot"], where
            assert at["page"] <= at["inner"], where
        finally:
            browser.close()


# The sweep as a browser draws it, frame by frame: the window picked by a click (inside the page, at the first frame) is the ring that sweeps; each frame the
# arc is as long as the share less the dash offset, and the dot must stand at the end of that arc (the angle 120 + 3 x length, on the ring's radius).
SWEEP_REC = """() => new Promise((resolve) => {
  const frames = []; const t0 = performance.now();
  document.querySelector('.tab-mark-rail:not([aria-selected=true]):not([aria-disabled=true])').click();
  const step = () => {
    const rings = [...document.querySelectorAll('.tab-mark-rail .ring-sweep')];
    for (const ring of rings.slice(0, 1)) {
      const arc = ring.querySelector('.ring-arc'), dot = ring.querySelector('.ring-dot');
      const share = parseFloat(ring.style.getPropertyValue('--ring-share'));
      const off = parseFloat(getComputedStyle(arc).strokeDashoffset) || 0;
      const rect = ring.getBoundingClientRect(), d = dot.getBoundingClientRect(), k = rect.width / 184, len = share - off;
      const th = (120 + 3 * len) * Math.PI / 180;
      frames.push({ t: performance.now() - t0, share, len, err: Math.hypot((d.left + d.right) / 2 - (rect.left + (92 + 81 * Math.cos(th)) * k), (d.top + d.bottom) / 2 - (rect.top + (92 + 81 * Math.sin(th)) * k)) });
    }
    if (performance.now() - t0 < 1000) requestAnimationFrame(step);
    else resolve({ frames, left: document.getAnimations().filter((a) => String(a.animationName || '').indexOf('ring-sweep') === 0).length, swept: document.querySelectorAll('.ring-sweep').length });
  };
  requestAnimationFrame(step);
})"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [860, 1280])
def test_in_a_real_browser_the_ring_of_the_window_picked_draws_its_arc_from_nothing_to_its_share_and_the_dot_runs_round_with_it(fixture_file, engine, width):  # noqa: F811
    """kit: ring, motion. A window picked by a click: its ring's arc grows frame by frame, never shrinking, from about nothing to the share in 750 ms at most,
    and at every frame the dot stands at the end of the arc (within 1px); once it is done no sweep animation is left. Under reduced motion the ring is the
    plain ring at once: no sweep class, no animation, the arc whole."""
    sync_api = pytest.importorskip("playwright.sync_api")
    views = json.loads(Path(fixture_file).read_text())
    with sync_api.sync_playwright() as playwright:
        browser, page = _open(playwright, engine, views["reading_both"], viewport={"width": width, "height": 800})
        try:
            page.wait_for_timeout(300)
            got = page.evaluate(SWEEP_REC)
            frames = got["frames"]
            where = (engine, width, frames[:3], frames[-3:])
            assert len(frames) >= 10, where
            assert frames[0]["len"] <= 0.12 * frames[0]["share"] + 1, where
            assert all(b["len"] >= a["len"] - 0.05 for a, b in zip(frames, frames[1:])), where
            ends = [f for f in frames if f["len"] >= f["share"] - 0.05]
            assert ends and ends[0]["t"] <= 750 and abs(frames[-1]["len"] - frames[-1]["share"]) <= 0.05, where
            assert max(f["err"] for f in frames) <= 1, where
            assert got["left"] == 0, ("no sweep animation is left", got["left"])
        finally:
            browser.close()
        browser, page = _open(playwright, engine, views["reading_both"], viewport={"width": width, "height": 800}, reduced_motion="reduce")
        try:
            page.wait_for_timeout(300)
            page.locator('.tab-mark-rail:not([aria-selected=true]):not([aria-disabled=true])').first.click()
            page.wait_for_timeout(100)
            now = page.evaluate("""() => ({ swept: document.querySelectorAll('.ring-sweep').length, anims: document.getAnimations().filter((a) => String(a.animationName || '').indexOf('ring-sweep') === 0).length,
                off: [...document.querySelectorAll('.ring-arc')].map((a) => parseFloat(getComputedStyle(a).strokeDashoffset) || 0) })""")
            assert now["swept"] == 0 and now["anims"] == 0 and all(o == 0 for o in now["off"]), now
        finally:
            browser.close()
