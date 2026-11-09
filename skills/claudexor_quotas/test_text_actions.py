"""The head's two buttons are text actions (kit: text action): "About · Refresh" ("О виджете · Обновить" in the install's
language) at the right end of the head, the words alone, as the sidebar's version check is (the design sheet's third idea,
chosen over icon buttons). Each is a button that is its word and nothing else: no drawing, no aria-label, no title (the
word is seen, and it is the name), no plate and no border, on hover either, no padding sideways, no status on About (the
banners and the status line say what is wrong). Between them stands one dot, the accent, out of the reading order: one of the
two things the accent paints within the text action; it blinks, by the kit's keyframes and through tokens, while a request is in the air (under reduced
motion it stays dimmed, still). The word is in the meta size with the sidebar's tracking, in the secondary ink (--text-secondary,
Ouroboros' own role) at rest and the primary ink when lit (the pointer, the keyboard, a press, an open state; not when
disabled), both with 4.5:1 on the head's ground in both themes. Refresh's word never changes (inFlight is also set by every
passive read, the 30 s poll among them, so a changing word would flicker): while a request is in the air it is the same
button with the same word, disabled and aria-busy, and its rest ink wins over the base's disabled ink. Every motion has its
reduced-motion pair, a finger gets 44px, the focus ring is the base ring, and nothing of the icon kit is left in widget.js.
The capital (on hover the word needs some animation: the capital letter turns red): the
word is made by one helper for both buttons, wordButton: a span of its own (.btn-word-cap) with the first character of what
t() says, and the rest of the word as a plain text node right after it; the span has no attribute, so the name is still the
whole word, and the button is an inline-block (a span that is a flex item is block-level, and a browser names the word "A bout").
At rest the capital is the word's ink (it takes the button's, it has no colour of its own); when the word is lit (the same
states, and not when disabled, a busy Refresh included) it turns the accent while the rest of the word turns the primary ink;
it fades with the word's own fade, and only its colour moves. Within the text action the accent paints two things: the dot and the lit capital.
The row and the contract (all of it is in the widget's kit, so that wherever similar buttons appear later they
come out right at once): the row of words is the kit's too, not the head's (.btn-word-row: a centred flex line with the 2px gap
that, with the dot's 8px margin, makes the approved air of about 12px ink to ink); the head's markup wears it beside its own
.action-seg, and that rule keeps only the head's place (margin-left:auto, min-height). wordButton, wordDot and wordRow are the only
builders of a word, a dot and a row, and the section opens with a usage contract (what it is, when, how, states, motion, tokens,
accessibility, what not to do) whose key phrases and token list these tests hold against the rules.
The rules are read from the STYLE array of widget.js; the markup runs in the widget's own fake-DOM run, and what a browser
really does (the colours, the fade, the name it gives the button) is checked in Chromium and WebKit where Playwright has them."""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import test_quotas
from test_order_focus import REAL_PAGE_STUB
from test_reserve import _node, _widget_fixture, with_i18n
from test_style_canons import _blocks, _declarations, _rules, _sections, _selectors, _style, _tokens

WIDGET = Path(__file__).with_name("widget.js")
ROW = ".btn-word-row"       # the kit's row of words: the line that holds the words and the dots between them
KIT = "kit: text action"    # the section of the style that draws the text action (its marker)
MARK = "kit: mark tab"      # the section of the style that draws the mark tab (its marker; test_family_tabs.py holds it)

# The word, as the markup makes it: the capital in a span of its own (the first character of what t() says) and the rest as a
# plain text node right after it. The span has no attribute, no listener and no child, so the name is still the whole word.
# Put before the scripts below, which hold `heard`.
NODE_THE_WORD = r"""
function theWord(btn, word) {
  assert.equal(btn.textContent, word, 'the button reads as the whole word');
  assert.equal(heard(btn), word, 'and is named by it');
  assert.equal(btn._text, '', 'the button has no text of its own: the word is in the span and in the node after it');
  assert.equal(btn.childNodes.length, 2, 'the span of the capital and the rest of the word, nothing else');
  const [cap, rest] = btn.childNodes;
  assert.equal(cap.tagName, 'SPAN');
  assert.equal(cap.className, 'btn-word-cap', 'the kit class and nothing else');
  assert.equal(cap.textContent, Array.from(word)[0], 'the capital is the first character of the word');
  assert.equal(cap.childNodes.length, 0);
  assert.deepEqual(cap.attributes, {}, 'no attribute at all on the span: no aria, no role, no title, no tabindex');
  assert.deepEqual(cap.listeners, {});
  assert.equal(rest.tagName, '#TEXT', 'the rest is a plain text node, right after the span');
  assert.equal(rest.textContent, word.slice(1));
  assert.equal(rest.childNodes.length, 0);
  assert.equal(btn.childNodes.filter((n) => n.tagName !== '#TEXT').length, 1, 'one element child: the capital');
}
// A word with a mark (Refresh): the kit's stroke icon, out of the reading order, then the word in a span of its own, split as every word is.
function theMarkedWord(btn, word) {
  assert.equal(btn.textContent, word, 'the button reads as the whole word');
  assert.equal(heard(btn), word, 'and is named by it');
  assert.equal(btn.childNodes.length, 2, 'the mark and the word, nothing else');
  const [mark, body] = btn.childNodes;
  assert.equal(mark.tagName, 'SVG');
  assert.equal(mark.getAttribute('aria-hidden'), 'true', 'the mark is out of the reading order');
  assert.ok(String(mark.className || '').split(/\s+/).includes('btn-word-mark'), 'the kit class on the mark: ' + mark.className);
  assert.equal(body.tagName, 'SPAN');
  assert.equal(body.className, 'btn-word-body', 'the word in the kit\'s span');
  assert.deepEqual(body.attributes, {}, 'no attribute on the span of the word');
  theWord(body, word);
}
"""

NODE_WORDS = r"""
const classOf = (node) => String((node && node.className) || '').split(/\s+/);
// What a reader of the screen hears from a node: what stands under an aria-hidden node is out of the reading order.
const heard = (node) => (node.getAttribute('aria-hidden') === 'true' ? '' : (node._text || '') + node.childNodes.map(heard).join(''));
const drawings = (node) => walk(node).filter((n) => n.tagName === 'SVG');
// What the icon buttons and their captions carried; nothing may carry it now.
const NAMES = ['aria-label', 'title', 'data-tip', 'data-tip-off'];

// A text action: a button that is its word alone, named by the word that is seen.
function textAction(btn, key, spoken) {
  assert.ok(btn, key + ' is drawn');
  assert.equal(btn.tagName, 'BUTTON');
  assert.equal(btn.getAttribute('type'), 'button');
  assert.ok(classOf(btn).includes('btn-word'), 'it wears the kit class: ' + btn.className);
  assert.equal(btn.getAttribute('data-focus'), key);
  assert.equal(heard(btn), spoken, key + ': its name is the word that is seen');
  for (const name of NAMES) assert.equal(btn.getAttribute(name), null, key + ': no ' + name + ', the word is seen');
  assert.equal(drawings(btn).length, key === 'refresh' ? 1 : 0, key + ': ' + (key === 'refresh' ? 'the one mark, the arrows' : 'no drawing in it'));
  assert.equal(classes(btn, 'pip').length + classes(btn, 'state-dot').length, 0, key + ': no status dot');
  assert.ok(!classOf(btn).includes('has-problem') && !classOf(btn).includes('btn-icon'), key + ': no colour of the trouble, no icon');
  assert.equal(btn.listeners.pointerenter, undefined, key + ': no handler on the pointer');
  assert.equal(btn.listeners.focus, undefined, key + ': no handler on the keyboard');
}

// About: its word is the button's whole content (the capital's span and the rest of the word) and nothing else is in it.
function aboutWord(btn, open) {
  textAction(btn, 'about', 'About');
  theWord(btn, 'About');
  assert.equal(btn.getAttribute('aria-expanded'), open ? 'true' : 'false');
  assert.equal(btn.getAttribute('aria-controls'), 'quotas-about');
  assert.equal(btn.disabled, false);
}

// Refresh: one word, idle or busy. Busy it is the same button with the same word, disabled and aria-busy (no attribute at rest).
function refreshWord(btn, busy) {
  textAction(btn, 'refresh', 'Refresh');
  theMarkedWord(btn, 'Refresh');  // the same word, idle or busy, in the same nodes
  assert.equal(btn.disabled, busy);
  assert.equal(btn.getAttribute('aria-busy'), busy ? 'true' : null, 'aria-busy while a request is in the air, no attribute at rest');
  assert.ok(!classOf(btn).includes('is-busy'), 'no busy class on the button: the word stands, the dot blinks');
  assert.equal(btn.className, 'btn-word btn-word-short', 'the kit classes and nothing else, idle or busy');
  assert.equal(btn.getAttribute('aria-expanded'), null);
}

// The two and the dot between them, nothing else in the row, and the row is no group with a name of its own.
function theRow(head, busy) {
  const seg = classes(head, 'action-seg')[0];
  assert.deepEqual(classOf(seg), ['btn-word-row', 'action-seg'], 'the kit row, then the head\'s own place: nothing else');
  assert.equal(seg.tagName, 'DIV');
  assert.equal(seg.getAttribute('role'), null, 'the row is no group');
  assert.equal(seg.getAttribute('aria-label'), null, 'and has no name: the two words are their own names');
  assert.equal(seg.childNodes.length, 3, 'About, a dot, Refresh and nothing else');
  const [about, dot, refresh] = seg.childNodes;
  assert.equal(about.getAttribute('data-focus'), 'about');
  assert.equal(refresh.getAttribute('data-focus'), 'refresh');
  assert.equal(dot.tagName, 'SPAN');
  assert.ok(classOf(dot).includes('btn-word-dot'));
  assert.equal(classOf(dot).includes('is-busy'), busy, 'the dot blinks while the work runs, and only then');
  assert.equal(dot.textContent, '·', 'the middle dot, U+00B7');
  assert.equal(dot.childNodes.length, 0);
  assert.equal(dot.getAttribute('aria-hidden'), 'true', 'the dot is out of the reading order');
  assert.equal(dot.getAttribute('tabindex'), null);
  assert.equal(dot.listeners.click, undefined);
  assert.equal(heard(seg), 'AboutRefresh', 'the reader hears the two words, the same busy or idle, and no dot');
  assert.equal(drawings(seg).length, 1, 'one drawing in the two: the arrows of Refresh');
  assert.equal(classes(seg, 'action-btn').length, 0, 'the old plate button is gone');
  return { about, dot, refresh };
}

(async () => {
  const click = (env, key) => byFocus(env.root, key).listeners.click[0]({ stopPropagation() {} });
  const posts = (env) => env.calls.filter((c) => c.method === 'POST').length;

  // 1. The healthy screen: the two words and the dot, About closed, Refresh idle, the dot still.
  let env = await boot(view([account('p1', quota())]));
  let head = env.root.childNodes[0];
  assert.deepEqual(env.root.childNodes.map((n) => classOf(n)[0]), ['control-bar', 'shell']);
  let row = theRow(head, false);
  aboutWord(row.about, false);
  refreshWord(row.refresh, false);
  // The words are in the head and nowhere else on the card as buttons of the kind.
  assert.equal(classes(env.root, 'btn-word').length, 2);
  assert.equal(classes(env.root, 'btn-word-dot').length, 1);

  // 2. About opens in place and stays a word; the open state is aria-expanded, which the kit lights. It opens the panel in the card.
  click(env, 'about');
  aboutWord(byFocus(env.root, 'about'), true);
  assert.ok(classes(env.root, 'about-panel').length, 'About opens');
  theRow(env.root.childNodes[0], false);
  click(env, 'about');
  aboutWord(byFocus(env.root, 'about'), false);
  assert.equal(classes(env.root, 'about-panel').length, 0, 'and closes');

  // 3. A system in trouble: About wears nothing of it (no colour, no dot, no word added to its own); the banner says it, in the open.
  const down = view([account('p1', quota())]);
  down.daemon = { state: 'stopped', engine_version: '3.9.4' };
  env = await boot(down);
  row = theRow(env.root.childNodes[0], false);
  aboutWord(row.about, false);
  refreshWord(row.refresh, false);
  assert.ok(/Claudexor daemon is stopped/.test(env.root.textContent), 'the banner says it');
  assert.ok(classes(env.root, 'banner').length >= 1);

  // 4. Nothing read yet: the same two words and the dot.
  env = await boot({ ok: true, transport_error: '', facets: {}, groups: [] });
  row = theRow(env.root.childNodes[0], false);
  aboutWord(row.about, false);
  refreshWord(row.refresh, false);

  // 5. Refresh in the air: the same button with the same word, disabled and aria-busy="true"; the dot blinks.
  env = await boot(view([account('p1', quota())]));
  assert.equal(posts(env), 0);
  byFocus(env.root, 'refresh').listeners.click[0]();
  row = theRow(env.root.childNodes[0], true);
  aboutWord(row.about, false);
  refreshWord(row.refresh, true);
  // A click on the button in the air starts nothing (a disabled button gets none from a browser; the handler holds as well).
  row.refresh.listeners.click[0]();
  await settle();
  assert.equal(posts(env), 1, 'two clicks, one request: Refresh calls the host only when no request is in the air');
  row = theRow(env.root.childNodes[0], false);
  refreshWord(row.refresh, false);
  aboutWord(row.about, false);

  // 5b. A passive read (the 30 s poll) sets inFlight too, and the head shows it the same way: the same word, so it cannot flicker on a
  // read nobody asked for; disabled and aria-busy while it runs, the dot blinking, and all of it gone when the read ends.
  env = await boot(view([account('p1', quota())]));
  const pending = [];
  env.window.fetch = (url, options = {}) => {
    env.calls.push({ url, method: options.method || 'GET' });
    return new Promise((resolve) => pending.push(resolve));
  };
  env.interval()();
  await settle();
  assert.equal(pending.length, 1, 'the poll started one read');
  assert.equal(posts(env), 0, 'and it is no Refresh');
  row = theRow(env.root.childNodes[0], true);
  refreshWord(row.refresh, true);
  pending[0](response(view([account('p1', quota())])));
  await settle();
  row = theRow(env.root.childNodes[0], false);
  refreshWord(row.refresh, false);

  // 6. Escape closes About, as it did, and neither button carries a handler for a caption.
  env = await boot(view([account('p1', quota())]));
  click(env, 'about');
  assert.equal(byFocus(env.root, 'about').getAttribute('aria-expanded'), 'true');
  env.document.listeners.keydown[0]({ key: 'Escape' });
  aboutWord(byFocus(env.root, 'about'), false);
  const carriers = walk(env.root).filter((n) => NAMES.some((name) => n.getAttribute(name) !== null && /\bbtn-word\b/.test(n.className || '')));
  assert.equal(carriers.length, 0, 'no text action carries a label, a title or a caption attribute');
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""

NODE_RUSSIAN = r"""
const heard = (node) => (node.getAttribute('aria-hidden') === 'true' ? '' : (node._text || '') + node.childNodes.map(heard).join(''));
(async () => {
  // The install's language: the words are said by t(): About and Refresh. Neither is English, neither has a label or a title, and
  // Refresh is the one word whether a request is in the air or not.
  let env = await boot(view([account('p1', quota())]));
  const seg = () => classes(env.root.childNodes[0], 'action-seg')[0];
  const about = byFocus(env.root, 'about');
  const refresh = byFocus(env.root, 'refresh');
  // Each word is split by its first character, whatever t() says: the capital of 'О виджете' and of 'Обновить' is 'О'.
  theWord(about, 'О виджете');
  theMarkedWord(refresh, 'Обновить');
  assert.equal(about.childNodes[0].textContent, 'О');
  assert.equal(about.childNodes[1].textContent, ' виджете');
  assert.equal(refresh.childNodes[1].childNodes[1].textContent, 'бновить');
  for (const btn of [about, refresh]) {
    for (const name of ['aria-label', 'title', 'data-tip', 'data-tip-off']) assert.equal(btn.getAttribute(name), null, name);
    assert.ok(!/[A-Za-z]/.test(btn.textContent), 'no English in the button: ' + btn.textContent);
  }
  assert.equal(refresh.getAttribute('aria-busy'), null, 'no aria-busy at rest');
  assert.equal(heard(seg()), 'О виджетеОбновить', 'the reader hears the two words and no dot');
  refresh.listeners.click[0]();
  const busy = byFocus(env.root, 'refresh');
  assert.equal(busy.disabled, true);
  assert.equal(busy.getAttribute('aria-busy'), 'true');
  theMarkedWord(busy, 'Обновить');  // the same word while busy, in the same nodes
  assert.equal(heard(seg()), 'О виджетеОбновить');
  await settle();
  const done = byFocus(env.root, 'refresh');
  assert.equal(done.disabled, false);
  assert.equal(done.getAttribute('aria-busy'), null);
  theMarkedWord(done, 'Обновить');
  console.log('ok');
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exitCode = 1; });
"""


def _run_node(script, i18n=None):
    node = _node()
    assert node is not None, "Node is required"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    # The run hands its fake window to the test, which puts a fetch of its own on it (a read held in the air).
    harness = harness.replace("    root: made.root,", "    window,\n    root: made.root,", 1)
    env = {**os.environ, "WIDGET_PATH": str(WIDGET.resolve())}
    if i18n is not None:
        harness = harness.replace("const isI18n =", "Object.assign(I18N_ANSWER, JSON.parse(process.env.I18N_OVERRIDE));\nconst isI18n =", 1)
        env["I18N_OVERRIDE"] = json.dumps(i18n)
        env["WIDGET_ROUTE_PREFIX"] = test_quotas._widget_route_prefix(WIDGET.resolve())
    result = subprocess.run([str(node), "-e", harness + script], cwd=Path(__file__).parent, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")


def _sheet():
    css = _style()
    return css, _sections(css)


def _text_rules():
    """[(media, selector, {prop: value})]: every rule of the style with `.btn-word` in a selector (the word, its text, its dot),
    one entry per selector."""
    css, sections = _sheet()
    out = []
    for name, section in sections.items():
        if name == "tokens":
            continue
        for media, listing, body in _rules(section):
            decls = {p.strip(): v.strip() for p, v in _declarations(body)}
            for selector in _selectors(listing):
                if ".btn-word" in selector:
                    out.append((media, selector, decls))
    return out


def _busy_rules():
    """[(media, {prop: value})]: the rules of the one blink class, `.is-busy`, which stand in kit: motion (the dot of a word and the stamp's)."""
    css, sections = _sheet()
    return [(media, {p.strip(): v.strip() for p, v in _declarations(body)}) for media, listing, body in _rules(sections["kit: motion"])
            for selector in _selectors(listing) if selector == ".is-busy"]


def _one(selector, media=""):
    found = [d for m, s, d in _text_rules() if s == selector and m == media]
    assert len(found) == 1, "%s under %r: %d rules" % (selector, media, len(found))
    return found[0]


LIT = re.compile(r"^\.btn-word:is\(([^()]*)\):not\(:disabled\)$")
LIT_CAPITAL = re.compile(r"^\.btn-word:is\(([^()]*)\):not\(:disabled\) \.btn-word-cap$")
LIT_WORD_SELECTOR = ".btn-word:is(:hover,:focus-visible,:active,[aria-expanded=true]):not(:disabled)"
PRESSED_WORD_SELECTOR = ".btn-word[aria-pressed=true]:not(:disabled)"   # a toggle word (Chart): its ink at rest while what it opens is shown
LIT_CAPITAL_SELECTOR = LIT_WORD_SELECTOR + " .btn-word-cap"


def _lit():
    """(the ways to be lit, the declarations) of the one rule that lights a text action that is not disabled."""
    found = [(m.group(1).split(","), decls) for media, selector, decls in _text_rules()
             for m in [LIT.match(selector)] if m and media == ""]
    assert len(found) == 1, len(found)
    return found[0]


def _lit_capital():
    """(the ways to be lit, the declarations) of the one rule that turns the capital of a text action that is not disabled."""
    found = [(m.group(1).split(","), decls) for media, selector, decls in _text_rules()
             for m in [LIT_CAPITAL.match(selector)] if m and media == ""]
    assert len(found) == 1, len(found)
    return found[0]


def _themes():
    """({token: value} of the dark default, {token: value} of the light set laid over it)."""
    blocks = _blocks(_sheet()[1]["tokens"])
    pairs = [{n.strip(): v.strip() for n, v in _declarations(body) if n.strip().startswith("--")} for _, body in blocks]
    return pairs[0], {**pairs[0], **pairs[1]}


def _rgb(value, ground):
    value = value.replace(" ", "")
    if value.startswith("#"):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b, a = re.fullmatch(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)", value).groups()
    return tuple(round(float(a) * int(c) + (1 - float(a)) * back) for c, back in zip((r, g, b), ground))


def _contrast(a, b):
    def lum(rgb):
        r, g, bl = (c / 255 / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _specificity(selector):
    """(ids, classes + attributes + pseudo-classes, elements) of a compound selector with no :is() or :not()."""
    bare = re.sub(r"\[[^\]]*\]", "[]", selector)
    return (len(re.findall(r"#[\w-]+", bare)),
            len(re.findall(r"\.[\w-]+", bare)) + bare.count("[]") + len(re.findall(r"(?<!:):[\w-]+", bare)),
            len(re.findall(r"(?:^|(?<=[\s>+~]))[a-z][\w-]*", bare)))


def test_about_and_refresh_are_buttons_with_their_word_only_and_one_dot_between_them():
    _run_node(NODE_THE_WORD + NODE_WORDS)


def test_the_words_are_said_in_the_installs_language_by_t_and_refresh_keeps_its_one_word_while_busy():
    russian = {"language": "ru", "revision": 3, "plural_select": None,
               "strings": {"btn.about": "О виджете", "btn.refresh": "Обновить"}}
    _run_node(NODE_THE_WORD + NODE_RUSSIAN, russian)


def test_a_text_action_has_no_border_no_plate_and_no_padding_sideways_not_even_on_hover():
    css, sections = _sheet()
    assert "kit: text action" in sections and "kit: icon button" not in sections
    base = _one(".btn-word")
    assert base["border"] == "0" and base["background"] == "none", "the word is the button"
    assert base["padding"] == "0", "no padding sideways: the button is as wide as its word"
    assert base["border-radius"] == "var(--radius-sm)" and _tokens(sections)["--radius-sm"] == "6px", "the ring's corner"
    assert base["appearance"] == "none"
    # Not one other rule that names the word (hover, lit, busy, its dot, touch) draws a plate, a line, a shadow or a
    # padding; the ring is the base's, so no outline either.
    plate = re.compile(r"^(background.*|border(?!-radius).*|box-shadow|outline.*|text-decoration.*|padding.*)$")
    for media, selector, decls in _text_rules():
        if (media, selector) != ("", ".btn-word"):
            drawn = [p for p in decls if plate.match(p)]
            assert not drawn, "%s draws a plate: %s" % (selector, decls)
    # The old plate button, the status dot on About and the trouble ink are gone; nothing moves the box.
    assert ".action-btn" not in css and "has-problem" not in css
    assert not [s for _, s, _ in _text_rules() if "pip" in s]
    assert not [s for _, s, d in _text_rules() if "transform" in d or "scale(" in "".join(d.values())], "nothing grows or turns"


def test_the_accent_paints_the_words_dot_and_lit_capital_and_the_mark_tabs_dot_number_and_mark_and_nothing_else():
    css, sections = _sheet()
    dark, light = _themes()
    assert dark["--accent"] == "#c93545", "the sidebar's red"
    assert "--accent:" not in sections["tokens"].split(":root[data-theme=light]{", 1)[1], "one red in both themes, as the sidebar's"
    # Exactly these rules read the accent (the nine below, then the picked bar's, the numbers over the fill, the heads of the plate's notes), each in its colour and each once (thirteen selectors: the preview is one rule of two selectors, the keyboard's and the pointer's; the rule of the picked mark also names the
    # mark of the tab just left in its start state, .tab-mark-was-on, so the one look is written once; so do the rule of the number
    # tab's picked dot and number and the rule of its picked star). In the text action (kit: text
    # action): the dot between the words and the capital of a lit word. In the mark tab (kit: mark tab): the tab's dot and its
    # number, and its mark, picked (aria-pressed) or previewed (hover, focus-visible, the tab not picked, not disabled), and the mark of
    # a tab picked while it showed its preview, in the start state .tab-mark-was-lit (the preview's red, held for the first frame); and the
    # number tab's dot, number and star (the sign after the number), red when it is picked (aria-selected) and in the start state of the
    # tab just left - and never on a preview: the number tab's preview is the ink. No other rule reads it
    # (no border, no shadow, no background, no second text), outside @media or inside it; the swap has no keyframes, the mark's
    # transition carries the colour (the canon lets a keyframe move transform and opacity alone: test_style_canons.py).
    readers = sorted((name, media, selector, prop.strip()) for name, section in sections.items() if name != "tokens"
                     for media, listing, body in _rules(section) for prop, value in _declarations(body)
                     if "var(--accent)" in value for selector in _selectors(listing))
    # (the mark tab reads the accent no more: its pick is the crimson ground of the plate, --q-sel, as the ring's and the card's)
    assert readers == sorted([
        (KIT, "", ".btn-word-dot", "color"),
        (KIT, "", LIT_CAPITAL_SELECTOR, "color"),
        # The head of a note of the plate ("At the limit · 3": its dot and its count, one rule for the heads of all the notes) and the star of a ring whose
        # accounts are not all read (the quota plate's rail). The bars of the plate are green, amber and red by their own tokens: none reads the accent.
        ("capsule: window tabs", "", ".wt-head-rest", "color"),
        ("capsule: window tabs", "", ".q-star", "color"),
    ]), readers
    assert css.count("var(--accent)") == 4 and len(readers) == 4, (
        "the accent paints two things in the text action, the head of a note of the plate and the star of a ring: nothing else", readers)
    # The red of the number tab is the pick's alone: no rule that answers a pointer or a keyboard on it reads the accent.
    assert not [s for n, _, s, _ in readers if n == MARK and re.search(r":hover|:focus", s) and ".tab-mark-num" in s]
    # Within the text action it is still the two: the dot and the lit capital (the contract says so, in those words).
    assert sorted(s for n, _, s, _ in readers if n == KIT) == sorted([".btn-word-dot", LIT_CAPITAL_SELECTOR])
    assert _lit_capital()[1] == {"color": "var(--accent)"}
    dot = _one(".btn-word-dot")
    assert dot["color"] == "var(--accent)"
    assert dot["font-weight"] == "var(--weight-semibold)" and _tokens(sections)["--weight-semibold"] == "600"
    assert dot["margin"] == "0 var(--space-2)" and _tokens(sections)["--space-2"] == "8px", "8px of air each side, its own"
    assert dot["user-select"] == "none", "a dot is not to be selected"
    # The dot is set like the words, so the air round it is the same on both sides (the tracking follows each letter).
    assert dot["font-size"] == "var(--type-meta)" and dot["letter-spacing"] == "var(--track-meta)"
    # The row (the kit's, .btn-word-row) adds a small gap of its own to the dot's margin: 8px + 2px of box-to-box air each side
    # (about 12px ink to ink, once the dot's side bearing and the tracking of the word's last letter are added; the approved air).
    assert _one(ROW)["gap"] == "var(--space-0)"
    assert _tokens(sections)["--space-0"] == "2px"
    # The dot is a mark between two words, and the lit capital is one letter that answers the pointer: both keep 3:1 against the
    # head's ground in both themes. As text of 12px the capital would need 4.5:1, which the one red gives on the light ground
    # (4.76) and not on the dark one (3.81, measured); the rest of the word, in the primary ink, keeps 4.5:1 in both (below).
    for theme in (dark, light):
        ground = _rgb(theme["--shell-bg"], (0, 0, 0))
        assert _contrast(_rgb(theme["--accent"], ground), ground) >= 3.0, theme["--accent"]


def test_the_word_is_the_meta_size_with_the_sidebars_tracking_in_the_secondary_ink_and_keeps_4_5_in_both_themes():
    css, sections = _sheet()
    dark, light = _themes()
    base = _one(".btn-word")
    assert base["font-size"] == "var(--type-meta)" and _tokens(sections)["--type-meta"] == "12px"
    assert base["letter-spacing"] == "var(--track-meta)" and _tokens(sections)["--track-meta"] == ".06em", "the sidebar's tracking"
    assert base["line-height"] == "var(--lh-ui)" and base["white-space"] == "nowrap"
    assert base["color"] == "var(--text-secondary)", "quiet at rest: Ouroboros' secondary text, quieter than the meta ink"
    ways, lit = _lit()
    assert ways == [":hover", ":focus-visible", ":active", "[aria-expanded=true]"]
    assert lit == {"color": "var(--text-primary)"}
    # The role is declared in both sets, with Ouroboros' own values (the dark one is a white at .58 over the ground, as 7.8.0's ui.css).
    light_set = {n.strip(): v.strip() for n, v in _declarations(_blocks(sections["tokens"])[1][1])}
    assert dark["--text-secondary"] == "rgba(255,255,255,.58)" and light_set["--text-secondary"] == "#626977"
    assert "Ouroboros' secondary text" in css, "the token says what it is, in one line"
    # Text of 12px: 4.5:1 on the head's ground (the shell's), the rest ink and the lit ink, in both themes; the rest ink is
    # still the quieter of the two roles it could have been (the meta ink is brighter on both grounds).
    for theme in (dark, light):
        ground = _rgb(theme["--shell-bg"], (0, 0, 0))
        for token in ("--text-secondary", "--text-primary"):
            assert _contrast(_rgb(theme[token], ground), ground) >= 4.5, (token, theme[token])
        assert _contrast(_rgb(theme["--text-secondary"], ground), ground) < _contrast(_rgb(theme["--text-meta"], ground), ground)


def _winning_colour(matching):
    """The `color` the cascade gives an element: of the rules (outside any @media, in source order) whose selector is one of
    `matching`, the one of the highest specificity, the later of equals."""
    best = None
    for name, section in _sheet()[1].items():
        if name == "tokens":
            continue
        for media, listing, body in _rules(section):
            decls = {p.strip(): v.strip() for p, v in _declarations(body)}
            for selector in _selectors(listing):
                if not media and selector in matching and "color" in decls:
                    if best is None or _specificity(selector) >= best[0]:
                        best = (_specificity(selector), decls["color"])
    return best[1]


def test_lit_by_the_pointer_the_keyboard_a_press_or_an_open_state_and_not_when_disabled_and_a_busy_word_keeps_its_rest_ink():
    css, sections = _sheet()
    base = _one(".btn-word")
    assert base["transition"] == "color var(--dur-5) var(--ease)" and _tokens(sections)["--dur-5"] == "220ms"
    ways, lit = _lit()
    assert ":hover" in ways and ":focus-visible" in ways and ":active" in ways and "[aria-expanded=true]" in ways
    assert [s for _, s, _ in _text_rules() if s.endswith(":active")] == [], "a press is in the list, never alone"
    # The lit rule is for a button that is not disabled: nothing lights a disabled one, a busy Refresh included.
    assert "button:disabled{color:var(--text-disabled);cursor:default}" in css
    # The trap: the base's `button:disabled` (0,1,1) is stronger than `.btn-word` (0,1,0), so a disabled word would grey out to
    # --text-disabled. A busy Refresh is disabled and must keep its rest ink, so a rule of its own beats the base.
    assert _specificity("button:disabled") > _specificity(".btn-word")
    assert _winning_colour({"button", "button:disabled", ".btn-word"}) == "var(--text-disabled)", "without that rule it would grey out"
    busy = _one(".btn-word[aria-busy=true]")
    assert busy == {"color": "var(--text-secondary)"}, "busy: the rest ink, set again for the disabled button"
    assert busy["color"] == base["color"]
    assert _specificity(".btn-word[aria-busy=true]") > _specificity("button:disabled"), "and it wins over the base's disabled ink"
    assert _winning_colour({"button", "button:disabled", ".btn-word", ".btn-word[aria-busy=true]"}) == "var(--text-secondary)"
    # The word's rest ink wins over the base button ink for any button that is not disabled; and nothing is lit by being busy.
    assert _specificity(".btn-word") > _specificity("button")
    assert not [s for _, s, _ in _text_rules() if "is-busy" in s and "btn-word-dot" not in s], "the busy class is the dot's alone"


def test_the_capital_is_the_words_ink_at_rest_and_the_accent_in_the_same_lit_states_and_not_when_disabled_or_busy():
    css, sections = _sheet()
    base, capital = _one(".btn-word"), _one(".btn-word-cap")
    # At rest the capital has no colour of its own: it takes the button's, so it is the secondary ink of an idle word, the rest ink
    # a busy Refresh keeps, and whatever the word is. It fades as the word does (the word's own transition, nothing else added).
    assert "color" not in capital, "no colour of its own at rest: it inherits the word's"
    assert capital == {"transition": "color var(--dur-5) var(--ease)"} and capital["transition"] == base["transition"]
    # Lit: the same four ways as the word, the same :not(:disabled); the word goes to the primary ink and the capital to the accent.
    word_ways, word_lit = _lit()
    ways, lit = _lit_capital()
    assert ways == word_ways == [":hover", ":focus-visible", ":active", "[aria-expanded=true]"]
    assert lit == {"color": "var(--accent)"} and word_lit == {"color": "var(--text-primary)"}
    assert [s for m, s, _ in _text_rules() if not m and ":not(:disabled)" in s] == [LIT_WORD_SELECTOR, PRESSED_WORD_SELECTOR, LIT_CAPITAL_SELECTOR], (
        "the lit word, the pressed word (a toggle: what it opens is shown) and the lit capital, in this order, and no fourth rule of the kind")
    # The pressed word is the primary ink and nothing else: the capital stays the word's own (no red capital on every open plate), and a
    # disabled word is not pressed-lit either.
    assert _one(PRESSED_WORD_SELECTOR) == {"color": "var(--text-primary)"}
    # A disabled word is not lit, and a busy Refresh is disabled: no red capital. The busy rule is for the button (it keeps the
    # rest ink against the base's disabled ink); the capital has no rule for a busy or disabled word at all, so it takes that ink.
    assert not [s for _, s, d in _text_rules() if ".btn-word-cap" in s and "color" in d and s != LIT_CAPITAL_SELECTOR]
    assert not [s for _, s, _ in _text_rules() if "aria-busy" in s and ".btn-word-cap" in s]
    # Only the colour moves, and the capital is no box of its own: no transform, no weight, no size, no display; it stays an inline
    # span in the word, the word's own letter.
    assert not [s for _, s, d in _text_rules() if ".btn-word-cap" in s and set(d) - {"transition", "color"}]


def test_busy_the_dot_blinks_by_the_kits_keyframes_through_tokens_and_only_in_opacity():
    css, sections = _sheet()
    tokens = _tokens(sections)
    # The sidebar's own numbers: its dots dim to .15 and back in .7s on an ease-in-out, for as long as it checks.
    assert tokens["--blink-dim"] == ".15" and tokens["--dur-blink"] == "700ms"
    assert _busy_rules() == [("", {"animation": "blink var(--dur-blink) var(--ease) infinite"}),
                             ("@media(prefers-reduced-motion:reduce)", {"animation": "none", "opacity": "var(--blink-dim)"})], "one .is-busy in kit: motion, on the sheet's --ease"
    assert re.search(r"@keyframes blink\{50%\{opacity:var\(--blink-dim\)\}\}", css)
    assert "@keyframes blink" in sections["kit: motion"], "the keyframes are the kit's"
    # The dot at rest does not blink: only the modifier does, and only the dot.
    assert "animation" not in _one(".btn-word-dot")
    assert [s for m, s, d in _text_rules() if "animation" in d and "reduced-motion" not in m] == [], "the text action writes no animation: the blink is kit: motion's .is-busy"
    # Nothing is left of the spinner.
    assert "@keyframes spin" not in css and "animation:spin" not in css


def test_the_button_flows_its_word_inline_so_the_capital_is_a_true_inline_span_and_refresh_holds_one_word():
    css, sections = _sheet()
    base = _one(".btn-word")
    # An inline-block, never a flex or grid row: a button centres its content by itself in the 26px height, and the span of the
    # capital stays inline in the word. As a flex item it would be block-level, and Chrome and WebKit name the button "A bout" (a gap
    # after the capital), which the fake DOM cannot see; the browser test below reads the name a browser really gives.
    assert base["display"] == "inline-block" and "align-items" not in base, "one word, flowing inline in the 26px height"
    # The cell of the two words is gone from the kit: no grid, no area, no hidden word, no class of a word inside the button.
    assert "btn-word-text" not in css and "aria-hidden=true" not in "".join(s for _, s, _ in _text_rules())
    # (The one width a word may have is the finger's minimum under a coarse pointer, held by the touch test.)
    for media, selector, decls in _text_rules():
        if selector == ROW:
            continue  # the row of words is the kit's flex line (held by its own test below); the word and its dot are no row
        if selector == ".btn-word-short" or media == "@media(max-width:520.98px)":
            continue  # the fold of a marked word on the narrow shell: the word in a screen reader's box inside its button (the kit's own rule)
        sizing = [p for p in decls if re.fullmatch(r"grid.*|justify-.*|align-.*|gap|visibility|(min-|max-)?width|flex-basis|position|float", p)]
        if media == "@media(pointer:coarse)" and selector == ".btn-word":
            sizing = [p for p in sizing if (p, decls[p]) != ("min-width", "var(--tap-h)")]
        assert not sizing, (selector, decls)
        assert "grid" not in decls.get("display", "") and "flex" not in decls.get("display", ""), (selector, decls)
        assert decls.get("display") in (None, "inline-block"), (selector, decls)
    # The markup agrees: one helper makes both words, from t(): the first character in the span of the capital, the rest as a
    # plain text node after it; Refresh and About are built by it, and nothing else makes a button of the kit.
    source = WIDGET.read_text(encoding="utf-8")
    assert "var btn = wordButton('btn.refresh', null, 'refresh');" in source and "var aboutBtn = wordButton('btn.about');" in source
    assert source.count("wordButton('btn.") == 2 and source.count("function wordButton(") == 1
    assert source.count("el('button', 'btn-word'") == 1, "the kit's button is made in the one helper"
    # The capital is the first character as a person sees it (Array.from walks code points, so a surrogate pair stays whole).
    assert "var first = Array.from(word)[0] || '';" in source
    assert "home.appendChild(el('span', 'btn-word-cap', first));" in source
    assert "home.appendChild(document.createTextNode(word.slice(first.length)));" in source
    # The helper passes its params on (the word over the bars, "Accounts . N", is made by it from t('acc.word', {n})): still what t() says.
    assert "var word = t(key, params);" in source, "the word is what t() says"
    assert "btn-word-text" not in source and "btn.refreshing" not in source


def test_every_motion_of_the_text_action_has_its_reduced_motion_pair():
    css, sections = _sheet()
    media = "@media(prefers-reduced-motion:reduce)"
    assert _one(".btn-word", media) == {"transition": "none"}
    # The capital fades with the word, so its pair is the same: the colour changes at once.
    assert _one(".btn-word-cap", media) == {"transition": "none"}
    # Busy would show nothing without the blink, so the reduced pair of the blink is the still dim dot: the keyframes' own low end.
    assert _busy_rules()[1] == (media, {"animation": "none", "opacity": "var(--blink-dim)"}), "the pair of .is-busy stands in kit: motion"
    assert "opacity" not in _busy_rules()[0][1] and "opacity" not in _one(".btn-word-dot"), "motion draws the dim, not a rule"
    assert re.search(r"@keyframes blink\{50%\{opacity:var\(--blink-dim\)\}\}", css) and _tokens(sections)["--blink-dim"] == ".15"
    # Every rule of the text action under reduced motion is one of these two: nothing else is left to fade (the blink's pair is kit: motion's).
    assert sorted(s for m, s, _ in _text_rules() if m == media) == [".btn-word", ".btn-word-cap"]
    # And the motion it has is exactly this: the colour fading (the word's and the capital's, both only in colour); the dot's blink is kit: motion's.
    moving = {(p, d[p].split()[0]) for m, s, d in _text_rules() for p in ("transition", "animation")
              if p in d and "reduced-motion" not in m}
    assert moving == {("transition", "color")}, moving
    assert sorted(s for m, s, d in _text_rules() if "transition" in d and "reduced-motion" not in m) == [".btn-word", ".btn-word-cap"]


def test_a_finger_gets_44px_and_the_focus_ring_is_the_base_ring():
    css, sections = _sheet()
    token = _tokens(sections)
    assert token["--row-h-sm"] == "26px" and token["--tap-h"] == "44px"
    assert _one(".btn-word")["min-height"] == "var(--row-h-sm)"
    # Under a finger the word is a full target, 44 by 44 at least (a short word such as "About" is narrower than a fingertip).
    coarse = {}
    for m, s, d in _text_rules():
        if m == "@media(pointer:coarse)" and s == ".btn-word":
            coarse.update(d)
    assert coarse == {"min-height": "var(--tap-h)", "min-width": "var(--tap-h)"}, coarse
    assert not [s for m, s, d in _text_rules() if "min-width" in d and m != "@media(pointer:coarse)"], "a fine pointer: the word's width"
    # The ring is the base ring of every button, unmoved; no rule of the text action writes an outline.
    assert "button:focus-visible,summary:focus-visible,.focus-ring:focus-visible{outline:var(--focus-line);outline-offset:var(--ring-offset)}" in css
    assert not [s for _, s, d in _text_rules() if any(p.startswith("outline") for p in d)]
    assert not [s for _, s, _ in _text_rules() if "focus-visible" in s and s not in (LIT_WORD_SELECTOR, LIT_CAPITAL_SELECTOR)], (
        "the keyboard lights the word and its capital, by the one list of ways, and nothing else")


def test_nothing_of_the_icon_kit_is_left_in_the_widget():
    source = WIDGET.read_text(encoding="utf-8")
    css, sections = _sheet()
    # Exact identifiers only: a generic phrase ("icon button", "data-tip", "pointerenter") is the name of things that may stand
    # in the widget for other reasons. btn-word-text and btn.refreshing held the second word Refresh once showed while it read; the word no longer changes.
    for trace in ("BUTTON_ICONS", "accentIcon", "btn-icon", "ic-accent", "ic-turn", "icon-tilt", "icon-turn", "is-refreshing",
                  "action-about", "action-refresh", "@keyframes spin", "animation:spin", "kit: icon button",
                  "function named(", "--z-tip", "btn-word-text", "btn.refreshing"):
        assert trace not in source, "a trace of the icon kit is left in widget.js: %s" % trace
    assert "btn.refreshing" not in Path(__file__).with_name("strings_en.json").read_text(encoding="utf-8"), "the busy word is no phrase"
    tokens = _tokens(sections)
    for token in ("--accent-w", "--arrow-w", "--turn-tilt", "--turn-half", "--dur-6", "--dur-7", "--dur-spin", "--icon-quiet",
                  "--icon-lit", "--radius-lg"):
        assert token not in source, "a token of the icon kit is left: %s" % token
        assert token not in tokens
    # The tokens the text action added are declared once and read.
    for token in ("--track-meta", "--blink-dim", "--dur-blink"):
        assert token in tokens and css.count("var(%s)" % token) >= 1, token
    assert "svgCanvas" in source and "svgEl" in source, "the drawing helpers other drawings use stay"


# ---------------------------------------------------------------------------
# The row of words and the contract: what makes the text action a part of the kit that the next part wears as it is.

# The three builders, run on their own text: el, wordButton, wordDot and wordRow are cut out of widget.js and built over the fake DOM,
# so a row of any number of words is seen, which the head (two words) cannot show. Put after NODE_THE_WORD, whose theWord it calls.
NODE_HELPERS = r"""
const src = fs.readFileSync(process.env.WIDGET_PATH, 'utf8');
const grab = (name) => {
  const found = src.match(new RegExp('\\n    function ' + name + '\\([\\s\\S]*?\\n    }\\n'));
  assert.ok(found, name + ' is in the widget');
  return found[0];
};
const { document } = makeDocument();
const phrases = { 'w.one': 'One', 'w.two': 'Two', 'w.three': 'Three' };
const kit = new Function('document', 't', grab('el') + grab('wordButton') + grab('wordDot') + grab('wordRow') +
  'return { wordButton: wordButton, wordDot: wordDot, wordRow: wordRow };')(document, (key) => phrases[key]);
const heard = (node) => (node.getAttribute('aria-hidden') === 'true' ? '' : (node._text || '') + node.childNodes.map(heard).join(''));
const word = (key) => kit.wordButton(key);

// A dot: a span with the middle dot, out of the reading order, still unless it is told that the work runs.
for (const [busy, cls] of [[undefined, 'btn-word-dot'], [false, 'btn-word-dot'], [true, 'btn-word-dot is-busy']]) {
  const dot = kit.wordDot(busy);
  assert.equal(dot.tagName, 'SPAN');
  assert.equal(dot.className, cls);
  assert.equal(dot.textContent, '·');
  assert.equal(dot.getAttribute('aria-hidden'), 'true');
  assert.equal(dot.childNodes.length, 0);
}
// A row of three words: word, dot, word, dot, word; no dot before the first or after the last; the dots busy all together or none.
for (const busy of [undefined, false, true]) {
  const row = kit.wordRow([word('w.one'), word('w.two'), word('w.three')], busy);
  assert.equal(row.tagName, 'DIV');
  assert.equal(row.className, 'btn-word-row', 'the kit class and nothing else: the part adds its own place');
  assert.deepEqual(row.childNodes.map((n) => n.tagName), ['BUTTON', 'SPAN', 'BUTTON', 'SPAN', 'BUTTON']);
  assert.deepEqual(row.childNodes.filter((n) => n.tagName === 'BUTTON').map(heard), ['One', 'Two', 'Three'], 'the words keep their order');
  for (const dot of row.childNodes.filter((n) => n.tagName === 'SPAN')) {
    assert.equal(dot.className, busy ? 'btn-word-dot is-busy' : 'btn-word-dot', 'every dot of the row is busy, or none');
    assert.equal(dot.getAttribute('aria-hidden'), 'true');
    assert.equal(dot.textContent, '·');
  }
  assert.equal(heard(row), 'OneTwoThree', 'a reader hears the words, and no dot');
  assert.equal(row.getAttribute('role'), null, 'the row is no group');
  assert.equal(row.getAttribute('aria-label'), null, 'and has no name: the words are their own names');
}
// Two words: one dot. One word: no dot. None: an empty row.
assert.deepEqual(kit.wordRow([word('w.one'), word('w.two')], true).childNodes.map((n) => n.tagName), ['BUTTON', 'SPAN', 'BUTTON']);
assert.deepEqual(kit.wordRow([word('w.one')], true).childNodes.map((n) => n.tagName), ['BUTTON']);
assert.equal(kit.wordRow([], true).childNodes.length, 0);
// The word is split by its first character, as the head's two are.
theWord(word('w.three'), 'Three');
console.log('ok');
"""


def _source():
    return WIDGET.read_text(encoding="utf-8")


def _function(source, name):
    """The text of `function name(...) {...}` as widget.js writes it: four spaces in, closed by a brace in the same column."""
    found = re.search(r"\n    function %s\(.*?\n    }\n" % name, source, re.S)
    assert found, "function %s is not in widget.js" % name
    return found.group(0)


def _contract():
    """The comment that opens the section of the text action: its // lines from the marker to the first rule, the slashes off,
    joined by a space."""
    lines = _style().splitlines()
    first = next(i for i, line in enumerate(lines) if line.strip() == "/* %s */" % KIT) + 1
    out = []
    for line in lines[first:]:
        if not line.strip().startswith("//"):
            break
        out.append(line.strip()[2:].strip())
    return " ".join(out)


def test_the_row_of_words_is_the_kits_and_the_head_wears_it_and_gives_it_only_its_place():
    css, sections = _sheet()
    # The row is the kit's: its one rule stands whole in the section of the text action - a flex line, its items centred on each
    # other, the gap that with the dot's margin makes the approved air - and in no other section.
    assert _one(ROW) == {"display": "flex", "align-items": "center", "flex-wrap": "wrap", "gap": "var(--space-0)"}
    assert [name for name, section in sections.items() if ROW in section] == [KIT]
    # No rule outside that section and the kit's touch line names a class of the text action: no capsule styles any of it.
    outside = [(name, selector) for name, section in sections.items() if name not in ("tokens", KIT, "kit: touch")
               for _, listing, _ in _rules(section) for selector in _selectors(listing) if "btn-word" in selector]
    assert outside == [], outside
    # The head keeps only what is its own: the place of the row (the right end of the bar, the height of a row of controls). It sets
    # no display, no alignment, no gap and no wrap on the row: those are the kit's. Only the head's section names the class, and the
    # rule the two rows of the bar once shared is gone: the families' row has a rule of its own.
    head = sections["capsule: head"]
    own = [(media, listing, {p.strip(): v.strip() for p, v in _declarations(body)})
           for media, listing, body in _rules(head) if "action-seg" in listing]
    assert own == [("", ".action-seg", {"margin-left": "auto", "min-height": "var(--row-h)"})], own
    assert not [name for name, section in sections.items() if name != "capsule: head" and "action-seg" in section]
    assert not [listing for _, listing, _ in _rules(head) if "harness-seg" in listing and "action-seg" in listing]
    families = [{p.strip(): v.strip() for p, v in _declarations(body)} for media, listing, body in _rules(head)
                if listing == ".harness-seg" and not media]
    assert families == [{"flex": "0 1 auto", "min-width": "0"}], "the families' row is the kit's mark tab row (kit: mark tab); the head gives it the room it needs"
    # rule: on the wide shell the row no longer stands off the edge to put the mark of its first tab on the axis of the rail's rings
    # (padding-left): the family picked stands at the edge of the widget and the axis is the middle of its tab (railFit).
    axis = [(media, {p.strip(): v.strip() for p, v in _declarations(body)}) for media, listing, body in _rules(head)
            if listing == ".harness-seg" and media]
    assert axis == [], axis
    # The air of the approved shot: the dot's 8px margin and the row's 2px gap, a side each.
    assert _one(".btn-word-dot")["margin"] == "0 var(--space-2)" and _tokens(sections)["--space-0"] == "2px"
    # The head's markup wears the kit row and then its own class (the fake-DOM run holds the classes: theRow); it does not draw the
    # row by hand.
    source = _source()
    assert "var actionSeg = wordRow([aboutBtn, refreshButton()], inFlight);" in source
    assert "actionSeg.className += ' action-seg';" in source
    assert "el('div', 'action-seg')" not in source


def test_wordButton_wordDot_and_wordRow_are_the_only_builders_of_a_word_a_dot_and_a_row():
    source, css = _source(), _style()
    for name in ("wordButton", "wordDot", "wordRow"):
        assert source.count("function %s(" % name) == 1, name
    # The code of the widget, the style array and every comment line taken out: the strings of the markup that name a class of the
    # text action stand in these three helpers, one each, and nowhere else.
    code = "\n".join(line for line in source.replace(css, "").splitlines() if not line.strip().startswith("//"))
    assert sorted(re.findall(r"'(btn-word[\w-]*)", code)) == ["btn-word", "btn-word-body", "btn-word-cap", "btn-word-dot", "btn-word-mark", "btn-word-row"]
    button = _function(source, "wordButton")
    assert "el('button', 'btn-word' + (iconName ? ' btn-word-short' : ''))" in button and "el('span', 'btn-word-cap', first)" in button
    assert "el('span', 'btn-word-dot' + (busy ? ' is-busy' : ''), '·')" in _function(source, "wordDot")
    assert "el('div', 'btn-word-row')" in _function(source, "wordRow")
    # The dot is drawn by wordRow alone, the row by the head alone (through wordRow): no part builds either by hand. Busy is told to
    # the helpers by their caller; none of them reads inFlight for the look.
    calls = lambda name: len(re.findall(r"(?<!function )\b%s\(" % name, code))
    assert calls("wordDot") == 1 and "wordDot(busy)" in _function(source, "wordRow")
    assert calls("wordRow") == 1
    assert "inFlight" not in _function(source, "wordDot") + _function(source, "wordRow")
    # What the three do, over the fake DOM: any number of words, the dots between and only between, busy all together.
    _run_node(NODE_THE_WORD + NODE_HELPERS)


def test_the_section_opens_with_the_usage_contract_and_names_every_token_its_rules_read():
    contract = _contract()
    # The eight parts of the contract, once each and in this order.
    parts = ["What it is:", "When to use it:", "How to use it:", "States:", "Motion:", "Tokens it reads:", "Accessibility:", "Not to do:"]
    at = [contract.find(part) for part in parts]
    assert all(i >= 0 for i in at) and at == sorted(at), dict(zip(parts, at))
    assert all(contract.count(part) == 1 for part in parts)
    # Its list of tokens is the rules' own: every token the rules of the text action read (the touch line and the dim of the reduced
    # blink included) and no other, each declared. The contract names a token, it does not read one: the accent is read twice.
    named = set(re.findall(r"--[a-z0-9-]+", contract.split("Tokens it reads:")[1].split("Accessibility:")[0]))
    read = {token for _, _, decls in _text_rules() for value in decls.values() for token in re.findall(r"var\((--[a-z0-9-]+)", value)}
    assert named == read, "the contract lists %s that the rules do not read; the rules read %s that it does not list" % (
        sorted(named - read), sorted(read - named))
    assert named <= set(_tokens(_sheet()[1]))
    assert "var(" not in contract


# ---------------------------------------------------------------------------
# In a real browser (Chromium and WebKit where Playwright has them): what the fake DOM cannot see. The colours a lit word and its
# capital really get, the fade and its reduced-motion pair, no red capital on a busy Refresh, and the name a browser really gives
# a button whose word is split in two nodes (a span that is a flex item is block-level, and the name becomes "A bout").

@pytest.fixture(scope="module")
def widget_view(tmp_path_factory):
    return _widget_fixture(tmp_path_factory.mktemp("fixture"))["view"]


def _open_page(playwright, engine, view, **context):
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


def _css_rgb(value):
    assert value.startswith("#"), value
    return "rgb(%d, %d, %d)" % _rgb(value, (0, 0, 0))


WORD_COLOURS = """() => Object.fromEntries(['about', 'refresh'].map((key) => {
  const btn = document.querySelector('[data-focus="' + key + '"]');
  const cap = btn.querySelector('.btn-word-cap');
  return [key, { word: getComputedStyle(btn).color, cap: getComputedStyle(cap).color, display: getComputedStyle(cap).display }];
}))"""
CAP_IS = """([key, colour]) => getComputedStyle(document.querySelector('[data-focus="' + key + '"] .btn-word-cap')).color === colour"""
WORD_IS = """([key, colour]) => getComputedStyle(document.querySelector('[data-focus="' + key + '"]')).color === colour"""
LIGHT_IT = """() => {
  const btn = document.querySelector('[data-focus="about"]');
  const cap = btn.querySelector('.btn-word-cap');
  getComputedStyle(cap).color;
  btn.setAttribute('aria-expanded', 'true');
  const colour = getComputedStyle(cap).color;
  return { cap: colour, fades: cap.getAnimations().map((a) => [a.transitionProperty, a.effect.getTiming().duration]) };
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_in_a_real_browser_a_lit_word_has_a_red_capital_a_busy_one_has_none_and_the_name_is_the_whole_word(widget_view, engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    dark, light = _themes()
    red = _css_rgb(dark["--accent"])
    assert _css_rgb(light["--accent"]) == red, "one red in both themes"
    ink = {"dark": _css_rgb(dark["--text-primary"]), "light": _css_rgb(light["--text-primary"])}
    with sync_api.sync_playwright() as playwright:
        browser, page = _open_page(playwright, engine, widget_view)
        try:
            colours = lambda: page.evaluate(WORD_COLOURS)
            cap_is = lambda key, colour: page.wait_for_function(CAP_IS, arg=[key, colour])
            word_is = lambda key, colour: page.wait_for_function(WORD_IS, arg=[key, colour])
            # 1. Rest: the capital takes the word's ink, which is not red, and it is an inline span in the word.
            rest = colours()
            for key in ("about", "refresh"):
                assert rest[key]["cap"] == rest[key]["word"] != red and rest[key]["display"] == "inline", rest[key]
            # 2. The name a browser gives each button is the whole word, with no gap after the capital (Chromium: its own tree).
            for key, word in (("about", "About"), ("refresh", "Refresh")):
                assert page.locator('[data-focus="%s"]' % key).aria_snapshot().strip() == '- button "%s"' % word
            if engine == "chromium":
                tree = page.context.new_cdp_session(page).send("Accessibility.getFullAXTree")
                buttons = {n["name"]["value"] for n in tree["nodes"]
                           if n.get("role", {}).get("value") == "button" and n.get("name", {}).get("value")}
                assert {"About", "Refresh"} <= buttons and not buttons & {"A bout", "R efresh"}, sorted(buttons)
            # 3. The pointer lights the word: the capital turns the accent and the rest of the word the primary ink; away, back to rest.
            page.hover('[data-focus="about"]')
            cap_is("about", red)
            word_is("about", ink["dark"])
            assert colours()["refresh"] == rest["refresh"], "only the word under the pointer is lit"
            page.mouse.move(2, 2)
            cap_is("about", rest["about"]["word"])
            word_is("about", rest["about"]["word"])
            # 4. The keyboard lights it (:focus-visible): a key press makes the page a keyboard's (WebKit's Tab skips buttons, so the
            # focus is moved by script), the focus goes to About, to Refresh, and away.
            page.keyboard.press("Shift")
            page.focus('[data-focus="about"]')
            assert page.evaluate("document.activeElement.matches(':focus-visible')") is True
            cap_is("about", red)
            word_is("about", ink["dark"])
            page.focus('[data-focus="refresh"]')
            assert page.evaluate("document.activeElement.matches(':focus-visible')") is True
            cap_is("refresh", red)
            cap_is("about", rest["about"]["word"])
            page.evaluate("document.activeElement.blur()")
            cap_is("refresh", rest["refresh"]["word"])
            # 5. An open state lights it with no pointer and no focus (aria-expanded=true), in both themes.
            page.click('[data-focus="about"]')
            page.mouse.move(2, 2)
            page.evaluate("document.activeElement.blur()")
            assert page.get_attribute('[data-focus="about"]', "aria-expanded") == "true"
            cap_is("about", red)
            word_is("about", ink["dark"])
            page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
            word_is("about", ink["light"])
            cap_is("about", red)
            page.evaluate("document.documentElement.removeAttribute('data-theme')")
            page.click('[data-focus="about"]')
            page.mouse.move(2, 2)
            page.evaluate("document.activeElement.blur()")
            cap_is("about", rest["about"]["word"])
            # 6. A busy Refresh (disabled, aria-busy) has no red capital, under the pointer either; the wait is long enough for
            # the fade to have finished had the capital been lit.
            page.click('[data-focus="refresh"]')
            page.wait_for_function("window.__pending.length === 1")
            page.hover('[data-focus="refresh"]', force=True)
            page.wait_for_timeout(450)
            assert page.evaluate("""() => { const b = document.querySelector('[data-focus="refresh"]');
                return [b.disabled, b.getAttribute('aria-busy')]; }""") == [True, "true"]
            if engine == "chromium":
                assert page.evaluate("document.querySelector('[data-focus=\"refresh\"]').matches(':hover')") is True
            busy = colours()["refresh"]
            assert busy["cap"] == busy["word"] == rest["refresh"]["word"] != red, busy
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("reduced", [False, True])
def test_in_a_real_browser_the_capital_fades_with_the_word_and_changes_at_once_under_reduced_motion(widget_view, engine, reduced):
    sync_api = pytest.importorskip("playwright.sync_api")
    red = _css_rgb(_themes()[0]["--accent"])
    with sync_api.sync_playwright() as playwright:
        browser, page = _open_page(playwright, engine, widget_view, reduced_motion="reduce" if reduced else "no-preference")
        try:
            lit = page.evaluate(LIGHT_IT)
            if reduced:
                assert lit["fades"] == [] and lit["cap"] == red, lit
            else:
                assert lit["fades"] == [["color", 220]] and lit["cap"] != red, lit
                page.wait_for_function(CAP_IS, arg=["about", red])
        finally:
            browser.close()
