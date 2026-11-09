"""The style system's canons that a test can hold: every line of the STYLE array a comment, a marker of the known form or one
double-quoted string (what is written any other way is read by no check below); the order and the opening line of every
section; a rule written once; colours and numbers by name (spaces and radii, type sizes, heights, line widths, times; a layer
and a shadow by name or by a listed reason); every token read, and the light theme only a change of the dark one; the
keyframes in the kit with the reduced-motion pair of every motion (a transition-property and an animation-name are motions
too); no !important but the one override; the kit and the base naming no class of a part but the rules listed; and no
capsule styling the class of another. Each check reads the STYLE array of widget.js (the tokens, the base, the kit, the
capsules) and says what it found."""
import re
from pathlib import Path

RANK = {"base": 0, "kit": 1, "capsule": 2}
# A marker names its section in lower-case words, one space or one hyphen between them: no digit, no capital, no doubled blank.
MARKER = re.compile(r"^\s*/\* (base|kit: [a-z]+(?:[ -][a-z]+)*|capsule: [a-z]+(?:[ -][a-z]+)*) \*/$", re.M)


def _style():
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    return source.split("var STYLE = [", 1)[1].split("].join('');", 1)[0]


def _sections(css):
    """{name: the section's css}: the tokens before the first marker, then each marker's strings joined."""
    out, name = {"tokens": ""}, "tokens"
    for line in css.splitlines():
        marker = MARKER.match(line)
        if marker:
            name = marker.group(1)
            out[name] = ""
            continue
        string = re.fullmatch(r'\s*"((?:[^"\\]|\\.)*)",?', line)
        if string:
            out[name] += string.group(1)
    return out


def _blocks(text):
    """[(prelude, body)] of the top-level rules of a css text; an at-rule keeps its rules in the body."""
    out, depth, start, pre, opened = [], 0, 0, "", 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                pre, opened = text[start:i].strip(), i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append((pre, text[opened:i]))
                start = i + 1
    return out


def _rules(text, context=""):
    """[(media, selector list, declarations)]: the rules of a css text with the at-rules they stand in ('' for none). Any
    grouping at-rule (@media, @supports, @layer, @container, nested ones) is entered, its prelude joined to the context, so no
    rule escapes the checks below by standing inside one; @keyframes and @font-face hold no selector rules."""
    out = []
    for pre, body in _blocks(text):
        if pre.startswith("@keyframes") or pre.startswith("@font-face"):
            continue
        if pre.startswith("@"):
            out.extend(_rules(body, (context + " " + pre).strip()))
        else:
            out.append((context, pre, body))
    return out


def _selectors(listing):
    out, depth, cur = [], 0, ""
    for ch in listing:
        depth += ch in "([" and 1 or (ch in ")]" and -1 or 0)
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return [s for s in out + [cur.strip()] if s]


def _declarations(body):
    return [tuple(d.split(":", 1)) for d in re.split(r";(?![^(]*\))", body) if ":" in d]


def _compounds(selector):
    """[compound]: a selector cut at its combinators (space > + ~), not inside (...) or [...]."""
    out, depth, cur = [], 0, ""
    for ch in selector.strip():
        depth += ch in "([" and 1 or (ch in ")]" and -1 or 0)
        if ch in " >+~" and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur] if cur else out


def _classes(compound):
    """([the classes the compound is], [the classes it only names inside :not() :is() :where() :has()]); an [attribute] and the
    argument of any other function are left out."""
    plain, named = re.sub(r"\[[^\]]*\]", "", compound), []
    for inner in re.findall(r":(?:not|is|where|has|matches)\(([^()]*)\)", plain):
        named += re.findall(r"\.([A-Za-z_][\w-]*)", inner)
    plain = re.sub(r"::?[\w-]+\([^()]*\)", "", plain)
    return re.findall(r"\.([A-Za-z_][\w-]*)", plain), named


# The classes of a part that are written under its prefix belong to its capsule, whatever rule they are first named in.
PREFIX_OWNERS = {"wt-": "capsule: window tabs"}


def _owners(sections, by_prefix=True):
    """{class: the section its base rule stands in}. A base rule is a selector of one compound with one class (the class, its
    states and its pseudo-elements), each selector of a list on its own; where the kit and a capsule both write one, the
    capsule owns the class. A class with no base rule (a state, a hook inside a part) has no owner."""
    owner = {}
    for name, text in sections.items():
        for _, listing, _ in _rules(text):
            for selector in _selectors(listing):
                compounds = _compounds(selector)
                found = _classes(compounds[0])[0] if len(compounds) == 1 else []
                if len(found) == 1 and (found[0] not in owner or (
                        name.startswith("capsule") and not owner[found[0]].startswith("capsule"))):
                    owner[found[0]] = name
    # A part's classes under its prefix (the plate's wt-plate, wt-in, wt-num ... have no one-class base rule, they are hooks of the markup):
    # the capsule of that prefix owns them all, so a kit rule that names one is seen (and listed with its reason) like any other.
    for text in sections.values() if by_prefix else ():
        for _, listing, _ in _rules(text):
            for selector in _selectors(listing):
                for compound in _compounds(selector):
                    for c in _classes(compound)[0] + _classes(compound)[1]:
                        for prefix, capsule in PREFIX_OWNERS.items():
                            if c.startswith(prefix) and c not in owner:
                                owner[c] = capsule
    return owner


def _tokens(sections):
    """{name: value} of the first block of the tokens, the dark one: the default theme, which declares them all."""
    return {n.strip(): v.strip() for n, v in _declarations(_blocks(sections["tokens"])[0][1]) if n.strip().startswith("--")}


def _literals(value):
    """[(number, unit)] a value writes itself, not through var(), url() or a string; a percentage is not one."""
    bare = re.sub(r"var\((?:[^()]|\([^()]*\))*\)|url\([^)]*\)|\"[^\"]*\"", " ", value)
    return [(float(n), unit.lower()) for n, unit in re.findall(r"(?<![\w.#-])(-?\d*\.?\d+)(px|ms|s|rem|em)?(?![\w%.])", bare, re.I)]


def test_the_sections_come_in_order_and_each_opens_with_a_line_of_what_it_draws():
    css = _style()
    markers = MARKER.findall(css)
    assert len(markers) == len(set(markers)) and markers[0] == "base"
    ranks = [RANK[m.split(":")[0]] for m in markers]
    assert ranks == sorted(ranks), "the base, then the kit, then the capsules"
    capsules = [m for m in markers if m.startswith("capsule")]
    assert capsules[0] == "capsule: shell" and capsules[-1] == "capsule: window tabs"
    assert markers.index("kit: ring") < markers.index("capsule: shell"), "the ring, then the capsules"
    # The passport: under every marker, before its first rule, a line says what the section draws.
    assert len(re.findall(MARKER.pattern[:-1] + r"\n\s*//", css, re.M)) == len(markers)


def test_a_rule_is_written_once():
    seen = {}
    for name, text in _sections(_style()).items():
        if name == "tokens":
            continue
        for media, listing, _ in _rules(text):
            key = (media, tuple(sorted(_selectors(listing))))
            assert key not in seen, "%s written twice: %s and %s" % (key, seen.get(key), name)
            seen[key] = name


# The colours CSS knows by a word; a rule writes none of them (transparent, currentColor and inherit are not colours of their own).
COLOURS = set((
    "aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood cadetblue "
    "chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey "
    "darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue darkslategray "
    "darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen "
    "fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki "
    "lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen "
    "lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime "
    "limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue "
    "mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive "
    "olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum "
    "powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue "
    "slateblue slategray slategrey snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke "
    "yellow yellowgreen").split())
COLOUR_PROP = re.compile(r"^(color|background(-color|-image)?|border(-(top|right|bottom|left))?(-color)?|outline(-color)?|"
                         r"box-shadow|text-shadow|fill|stroke|text-decoration(-color)?|caret-color|accent-color|column-rule(-color)?)$")


def test_colours_and_numbers_are_named_not_written():
    sections = _sections(_style())
    tokens = sections["tokens"]
    space = {int(n) for n in re.findall(r"--space-[0-9h]+:(\d+)px", tokens)}
    radius = {int(n) for n in re.findall(r"--radius-[a-z]+:(\d+)px", tokens)}
    assert space >= {2, 4, 6, 8, 12, 16, 24} and radius >= {3, 6, 8}
    shape = re.compile(r"^(margin|padding)(-[a-z-]+)?$|^(row-|column-)?gap$")
    for name, text in sections.items():
        if name == "tokens":
            continue
        for _, listing, body in _rules(text):
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix|color)\(", body), (name, listing)
            for prop, value in _declarations(body):
                words = set(re.findall(r"[a-z]+", re.sub(r"var\((?:[^()]|\([^()]*\))*\)|url\([^)]*\)|\"[^\"]*\"", " ", value.lower())))
                assert not (COLOUR_PROP.match(prop.strip()) and words & COLOURS), (
                    "%s: %s:%s is a colour written by its name, write the token" % (listing, prop, value))
                known = space if shape.match(prop) else radius if re.match(r"^border(-[a-z]+)*-radius$", prop) else None
                for number in re.findall(r"(?<![\w.-])-?(\d+)px", value) if known else ():
                    assert int(number) == 0 or int(number) not in known, (
                        "%s: %s:%s is a token's value, write the token" % (listing, prop, value))


def test_every_token_is_declared_and_read():
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    css = _style()
    declared = set(_tokens(_sections(css)))   # the dark block: the default theme, which a rule reads when no theme is set
    read = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    outside = source.replace(css, "")
    assert read <= declared, sorted(read - declared)
    assert not [t for t in declared if t not in read and t not in outside], "declared and read nowhere"


def _comments(css):
    """{section: its // comments, flat}: the comment lines between a marker and the next one, joined into one line of text."""
    out, name = {}, None
    for line in css.splitlines():
        marker = MARKER.match(line)
        if marker:
            name = marker.group(1)
            out[name] = []
        elif name and line.lstrip().startswith("//"):
            out[name].append(line.lstrip()[2:].strip())
    return {key: " ".join(value) for key, value in out.items()}


def test_every_kit_names_the_tokens_its_rules_read():
    """A kit is read by its contract: the line "Tokens it reads" (and "Tokens it reads, besides") of its comment names every token its rules read, the ones its
    keyframes read included, and each name is a declared token. A contract may name more than the rules read (a kit's token that the touch rules or the
    script's own reading use); it may never name fewer, so no kit hides what it needs from a part that wears it."""
    css = _style()
    sections = _sections(css)
    declared = set(_tokens(sections))
    comments = _comments(css)
    kits = [name for name in sections if name.startswith("kit: ")]
    assert len(kits) >= 15, kits
    for name in kits:
        lines = re.findall(r"Tokens it reads(?:, besides)?: (.*?)\.(?:\s|$)", comments[name])
        assert lines, name + " has no line of the tokens it reads"
        named = set(re.findall(r"--[a-z0-9-]+", " ".join(lines)))
        read = set(re.findall(r"var\((--[a-z0-9-]+)", sections[name]))
        assert read <= named, "%s reads %s and its contract does not name them" % (name, sorted(read - named))
        assert named <= declared, "%s names %s, which are not declared" % (name, sorted(named - declared))


def test_the_light_theme_only_changes_a_token_the_dark_one_declares():
    blocks = _blocks(_sections(_style())["tokens"])
    assert [pre for pre, _ in blocks] == [":root", ":root[data-theme=light]"], "the dark default, then the light changes, no third"
    dark, light = [{n.strip() for n, _ in _declarations(body) if n.strip().startswith("--")} for _, body in blocks]
    assert light <= dark, "declared in the light block alone: %s" % sorted(light - dark)


def test_the_keyframes_are_the_kits_and_every_motion_has_its_reduced_pair():
    sections = _sections(_style())
    for name, text in sections.items():
        for pre, body in _blocks(text):
            if pre.startswith("@keyframes"):
                assert name.startswith("kit: "), "%s stands in %s, the kit keeps the keyframes" % (pre, name)
                # The one exception: the arc of a ring draws by its dash offset (ring-sweep-arc, kit: ring, motion), on a ring of 47px (a repaint the size of
                # the ring, cheap). It is the arc's alone; every other keyframe moves transform and opacity.
                allowed = {"transform", "opacity"} | ({"stroke-dashoffset"} if pre.split()[1] == "ring-sweep-arc" else set())
                assert set(re.findall(r"([a-z-]+):", body)) <= allowed, pre
    # A motion is the shorthand or the property that starts it (the animation's name, the property a transition runs on);
    # `none` under reduced motion, written either way, is its pair.
    kind_of = {"animation": "animation", "animation-name": "animation", "transition": "transition", "transition-property": "transition"}
    still = {"animation": set(), "transition": set()}
    moving = {"animation": [], "transition": []}
    for name, text in sections.items():
        for media, listing, body in _rules(text):
            for prop, value in _declarations(body):
                kind = kind_of.get(prop.strip())
                if kind:
                    for selector in _selectors(listing):
                        if "reduced-motion" in media and value.strip() == "none":
                            still[kind].add(selector)
                        elif "reduced-motion" not in media and value.strip() != "none":
                            moving[kind].append((kind, selector))
    assert moving["animation"] and moving["transition"], "the motions were not found"
    bare = [m for kind in moving for m in moving[kind] if m[1] not in still[m[0]]]
    assert not bare, "a motion without its reduced pair: %s" % bare


def test_no_important_is_written():
    # The bars no longer carry a width the script sets (the row is the plate's width, a bar is a flex share: --bar-min .. --bar-max), so the one override
    # of that width (`width:auto!important`) is gone and the sheet has no `!important` at all.
    css = _style()
    assert css.count("!important") == 0


# The kit and the base draw the kit's classes and the page's elements. A rule of theirs that names a class of a part (a class
# whose base rule stands in a capsule), in any place of its selector, is listed here by its media and its selector, with the
# reason: the first pass's debt is paid (each part wears a kit class in the markup: .btn, .btn-compact, .meta-line, .tbl,
# .card, .focus-ring ...) and what is left is decided by hand. A rule counts, not a class: a second rule on a listed class is
# new. A listed rule that is gone leaves the list.
PLATE = ("the plate of the window-tabs capsule (every .lrow is one): its columns and areas, the figure and the chart word in them, the figure's "
         "headline and its rise are written here, in the kit's own row and figure, not in the capsule, which styles only its own wt- classes "
         "(test_window_tabs.py); wt-plate, wt-in, wt-num and wt-has-scope are hooks of the markup with no base rule")
FOLDED_CARD = ("the card of an account in its fold (the chart capsule's .ins-wrap > .ins-fold): the same band as the kit's card under a list's rows (.lrows > .inspector), "
               "so the two selectors share the one rule written there, not two bodies")
PRESS_ONCE = ("the press of a control, written once in kit: press for the controls it lists (the window row's limit line, the banner's retry and the scope option) and its pair"
              " under reduced motion, not a body in each capsule")
RING_ONCE = ("the focus ring of a thing the keyboard reaches, written once in kit: focus ring for the things it lists (outside it, and inside it for a thing that fills "
             "its box to the card's edge), not a body in each capsule")
REDUCED = "@media(prefers-reduced-motion:reduce)"
KIT_RULES_NAMING_A_PART = {
    **{(media, selector): PRESS_ONCE for media in ("", REDUCED) for selector in (".banner .banner-retry:active",)},
    **{("", selector): RING_ONCE for selector in (
        ".lg .legend-item:focus-visible", ".nx[tabindex]:focus-visible", ".wt-foot-g:focus-visible")},
}


def test_the_kit_and_the_base_name_no_class_of_a_part_but_the_rules_listed():
    sections = _sections(_style())
    owner = _owners(sections)
    named = {}
    for name, text in sections.items():
        if name == "base" or name.startswith("kit: "):
            for media, listing, _ in _rules(text):
                for selector in _selectors(listing):
                    found = {c for compound in _compounds(selector) for part in _classes(compound) for c in part
                             if owner.get(c, "").startswith("capsule")}
                    if found:
                        named[(media, selector)] = sorted(found)
    assert set(named) <= set(KIT_RULES_NAMING_A_PART), "a new kit rule names a part: %s" % {
        k: v for k, v in named.items() if k not in KIT_RULES_NAMING_A_PART}
    assert set(KIT_RULES_NAMING_A_PART) <= set(named), "a listed rule is gone, take it off the list: %s" % sorted(
        set(KIT_RULES_NAMING_A_PART) - set(named))


def test_a_capsule_styles_no_class_of_another_capsule():
    # The window-tabs capsule has its own, stricter test (only wt- classes, test_window_tabs.py). Here, for every selector of a
    # capsule, the class it draws (its subject's; where the subject has none with a base rule, the nearest one before it) stands
    # in the kit or in that capsule. What a part changes in another part's class is written with that part (the plate's rules).
    sections = _sections(_style())
    owner = _owners(sections, by_prefix=False)
    foreign, checked = [], 0
    for name, text in sections.items():
        if name.startswith("capsule: "):
            for media, listing, _ in _rules(text):
                for selector in _selectors(listing):
                    for compound in reversed(_compounds(selector)):
                        owned = [c for c in _classes(compound)[0] if c in owner]
                        if owned:
                            checked += 1
                            foreign += [(name, media + selector, c, owner[c]) for c in owned
                                        if owner[c].startswith("capsule") and owner[c] != name]
                            break
    assert checked > 100, "the capsules' rules were not found"
    assert not foreign, "a capsule styles a class that another capsule owns: %s" % foreign


def test_every_line_of_the_style_array_is_a_comment_a_known_marker_or_one_double_quoted_string():
    # What is written any other way - a single-quoted string, two strings joined with +, a note after a string, a marker of
    # another form (a digit, a capital) - is read by no check here (the sections skip it), so it may not stand in the array.
    strings = []
    for number, line in enumerate(_style().splitlines(), 1):
        text = line.strip()
        if text and not text.startswith("//") and not MARKER.match(line):
            assert re.fullmatch(r'"(?:[^"\\]|\\.)*",?', text), "array line %d is no comment, marker or one string: %s" % (number, text[:70])
            strings.append(text)
    assert strings and not [s for s in strings[:-1] if not s.endswith(",")], "a string without its comma"


# A layer and a shadow are named by a token (a --*shadow* one, or an --elev-* one: the app's elevations), or listed here with the reason why
# they are written: a new one is a decision.
Z_INDEX_WRITTEN = {
    (".chart-tip", "5"): "the tip stands over the plot, its lines and its cursor",
}
SHADOWS_WRITTEN = {
    (".meter", "0 var(--border-w) 0 var(--edge-strong)"): "the meter's base line: one hairline under the track",
    (".meter.restricted", "0 var(--border-w) 0 var(--status-warn)"): "the meter's base line in the held colour",
    (".meter.spent", "0 2px 0 var(--status-bad)"): "the spent meter's base: the red line, 2px",
}
# Where a number would repeat a token's value, the token is written: (what it is, the properties, the tokens that hold it).
SIZES = [
    ("a height", r"(?:min-|max-)?height", r"--(?:row-h|row-h-sm|tap-h)"),
    ("a line", r"border(?:-(?:top|right|bottom|left|inline|block)(?:-(?:start|end))?)?(?:-width)?|outline(?:-width)?",
     r"--(?:border-w|ring-w)"),
    ("the offset of a ring", r"outline-offset", r"--ring-offset"),
]


def test_sizes_lines_times_layers_and_shadows_are_named_not_written():
    sections = _sections(_style())
    token = _tokens(sections)
    held = {what: {float(v[:-2]): n for n, v in token.items() if re.fullmatch(names, n) and v.endswith("px")}
            for what, _, names in SIZES}
    assert all(held.values()), "the tokens of a size were not found: %s" % held
    layers, shadows = set(), set()
    for name, text in sections.items():
        if name == "tokens":
            continue
        for _, listing, body in _rules(text):
            for prop, value in _declarations(body):
                prop, value = prop.strip(), value.strip()
                where = "%s { %s: %s }" % (listing, prop, value)
                if prop == "font-size":
                    assert re.fullmatch(r"var\(--type-[a-z]+\)|inherit", value), "%s: a type size is --type-*" % where
                if prop == "font":
                    assert value.startswith(("var(--type-", "inherit")), "%s: the size of a font is --type-*" % where
                if re.fullmatch(r"transition(-duration|-delay)?|animation(-duration|-delay)?", prop):
                    assert not [x for x, unit in _literals(value) if unit in ("ms", "s") and x], "%s: a time is --dur-*" % where
                for what, props, _ in SIZES:
                    if re.fullmatch(props, prop):
                        for x, unit in _literals(value):
                            assert unit != "px" or x not in held[what], "%s: %gpx is %s, write var(%s)" % (
                                where, x, what, held[what][x])
                if prop == "z-index" and not value.startswith("var(--z-"):
                    layers.add((listing, value))
                if prop in ("box-shadow", "text-shadow") and value != "none" and not re.fullmatch(r"var\(--(?:[a-z-]*shadow[a-z-]*|elev-[a-z]+)\)", value):
                    shadows.add((listing, value))
    assert layers == set(Z_INDEX_WRITTEN), "a layer written, or listed and gone: %s" % sorted(layers ^ set(Z_INDEX_WRITTEN))
    assert shadows == set(SHADOWS_WRITTEN), "a shadow written, or listed and gone: %s" % sorted(shadows ^ set(SHADOWS_WRITTEN))


def test_the_style_uses_only_the_at_rules_the_checks_know():
    """@media and @keyframes are the at-rules of this style; another one (@supports, @layer, @container…) is entered by the
    checks above, but is a new kind of rule and is added here on purpose, not by accident."""
    found = set(re.findall(r"@([a-z-]+)", "".join(_sections(_style()).values())))
    assert found <= {"media", "keyframes"}, "an at-rule the style does not use yet: %s" % sorted(found - {"media", "keyframes"})


# ---- the tests' own canon: a fake node is never an operand of a comparison -------------------------------------------------------------
# A comparison of Node's assert prints both operands when it fails, and a fake DOM node holds its parent, its children and its document,
# which hold it back: the print has no end, and the process grows without limit. So a test says
# of two nodes that one is the other with ok(a === b, 'what'), never with equal / deepEqual; the memory guard (conftest.py) kills a run
# at 1 GiB, but the failure is then a size, not a message.
_COMPARES = re.compile(r"\bassert\.(?:equal|notEqual|strictEqual|notStrictEqual|deepEqual|deepStrictEqual|notDeepEqual|notDeepStrictEqual)\(")
_NODE_PROPERTY = re.compile(r"(?:^|[.\s(])(?:activeElement|parentNode|parentElement|firstChild|lastChild|firstElementChild|lastElementChild|"
                            r"nextSibling|previousSibling|nextElementSibling|previousElementSibling|childNodes|children|ownerDocument)"
                            r"(?:\[[^\]]*\])?$")
_NODE_FINDER = re.compile(r"^(?:byFocus|plateOf|nxOf|lgOf|rowOf|cardOf|blockOf|controlsOf|heroOf|pickedTab|stampOf|footOf|focusTab|jointSwitch)"
                          r"\((?:[^()]|\([^()]*\))*\)$")


def _first_two_arguments(text, start):
    """The first two top-level arguments of the call whose opening parenthesis stands just before `start` (strings are skipped over)."""
    depth, quote, arguments, current, i = 1, None, [], [], start
    while i < len(text) and depth and len(arguments) < 2:
        ch = text[i]
        if quote:
            current.append(ch)
            if ch == "\\":
                i += 1
                current.append(text[i])
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            if depth:
                current.append(ch)
        elif ch == "," and depth == 1:
            arguments.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    if len(arguments) < 2:
        arguments.append("".join(current).strip())
    return arguments


def test_no_test_compares_a_fake_dom_node_with_equal_or_deep_equal():
    found = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for call in _COMPARES.finditer(text):
            operands = _first_two_arguments(text, call.end())
            if any(_NODE_PROPERTY.search(x) or _NODE_FINDER.match(x) for x in operands):
                found.append("%s:%d  %s" % (path.name, text.count("\n", 0, call.start()) + 1, " , ".join(x[:60] for x in operands)))
    assert not found, "a node is an operand of equal / deepEqual (write ok(a === b, 'what') instead):\n" + "\n".join(found)


def test_the_canon_above_sees_what_it_is_written_for():
    # The samples are written in two pieces, so that this file holds no comparison of its own for the canon to find.
    def sees(call):
        text = "assert." + call
        operands = _first_two_arguments(text, _COMPARES.search(text).end())
        return any(_NODE_PROPERTY.search(x) or _NODE_FINDER.match(x) for x in operands)

    for call in ("equal(s.childNodes[0], labels[i], 'x')", "deepEqual(seg.childNodes, tabs)",
                 "equal(env.document.activeElement, byFocus(env.root, 'a'))", "equal(byFocus(env.root, 'a'), undefined)",
                 "notEqual(node.parentNode, other)"):
        assert sees(call), call
    for call in ("equal(b[8].childNodes.length, 0)", "deepEqual(row.childNodes.map((n) => n.className), ['a'])",
                 "equal(byFocus(env.root, 'a').getAttribute('x'), null)", "equal(classes(row.parentNode, 'bars').length, 1)"):
        assert not sees(call), call


def test_a_pick_of_a_family_moves_no_tab_and_the_slide_of_the_head_is_gone():
    """The reader's pick of a family redraws the head's tabs in the same order: the picked tab wears its ground (kit: mark tab) and nothing
    slides, grows or dims. The keyframes of the old slide (rail-slide, rail-yield), the kit that held them (kit: rail) and the start states of
    the marks are gone from the sheet and from the script; the one motion of a tab is its fade of ground and ink, with its reduced pair."""
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    css = _style()
    sections = _sections(css)
    assert "kit: rail" not in sections and "rail-slide" not in css and "rail-yield" not in css
    for trace in ("tab-mark-slide", "tab-mark-yield", "tab-mark-ride", "tab-mark-still", "tab-mark-was-", "--flip-x", "--dur-flip", "--mark-on",
                  "--rail-elapsed", "--rail-axis", "--rail-tab-w", "markStart(", "settleMarks(", "markWas(", "markFlip(", "markSlide(", "railFit("):
        assert trace not in source, "a trace of the slide is left: %s" % trace
    kit = sections["kit: mark tab"]
    assert not re.search(r"transform|animation|scale\(", kit), "nothing of a tab moves or grows"
    moving = [(media, listing) for media, listing, body in _rules(kit) for prop, value in _declarations(body)
              if prop.strip() == "transition" and value.strip() != "none"]
    assert moving == [("", ".tab-mark")], moving
    assert re.search(r"@media\(prefers-reduced-motion:reduce\)\{\.tab-mark\{transition:none\}\}", kit)
    # The order of the tabs is the widget's memory (first seen, first), written by orderedFamilies alone, and a pick moves no tab.
    order = re.search(r"\n    function orderedFamilies\(.*?\n    }\n", source, re.S).group(0)
    assert "unshift" not in order and "splice" not in order and "selected" not in order
    assert "will-change" not in "".join(sections.values()), "no will-change is held (a comment may say so)"
