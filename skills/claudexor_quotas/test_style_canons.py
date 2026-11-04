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
    assert markers.index("kit: ring") < markers.index("kit: rail") < markers.index("capsule: shell"), "the ring, the rail, then the capsules"
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


def test_the_ring_sweep_is_the_kits_one_motion_of_the_ring_the_arc_and_the_dot_run_in_step_and_dash_offset_is_the_arcs_alone():
    """kit: ring / kit: motion. A ring that is asked to (ringGauge's sweep) draws its arc from nothing to its share and the dot runs round the ring
    with it. The canon holds: the keyframes are the kit's, and of all of them only ring-sweep-arc moves anything but transform and opacity - the
    dash offset, which no rule and no other keyframes may write (the one documented exception, on a ring of 47px: the repaint is the ring's); the arc
    and the dot run the same time on the same curve (so the dot is at the end of the arc at every frame), `backwards` (the end state is the plain
    ring), by tokens, the time 600 to 700 ms and a curve that does not rebound; every motion has its reduced pair (the arc at once in place); a
    ring in a row of the rail waits for its row; and the span the script keeps for the sweep holds the last row of the rail."""
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    sections = _sections(_style())
    tokens = _tokens(sections)
    frames = {pre.split()[1]: body.replace(" ", "") for pre, body in _blocks(sections["kit: motion"]) if pre.startswith("@keyframes ring-")}
    assert frames == {
        "ring-sweep-arc": "from{stroke-dashoffset:var(--ring-share)}to{stroke-dashoffset:0}",
        "ring-sweep-dot": "from{transform:rotate(calc(var(--ring-share)*-3deg))}to{transform:rotate(0deg)}"}, frames
    # The exception is the arc's alone: no other keyframes and no rule writes the dash offset (the dash is the svg's own attribute).
    for name, text in sections.items():
        for pre, body in _blocks(text):
            if pre.startswith("@keyframes") and pre != "@keyframes ring-sweep-arc":
                assert "stroke-" not in body, (name, pre)
        for media, listing, body in _rules(text):
            assert not [d for d in _declarations(body) if d[0].strip() == "stroke-dashoffset"], (name, listing)
    ring = sections["kit: ring"]
    starts = {listing: {prop.strip(): value.strip() for prop, value in _declarations(body)} for media, listing, body in _rules(ring)
              if not media and "ring-sweep" in listing and ".ring-sweep-row" not in listing}
    assert set(starts) == {".ring-sweep .ring-arc", ".ring-sweep .ring-dot"}, starts
    arc, dot = starts[".ring-sweep .ring-arc"], starts[".ring-sweep .ring-dot"]
    assert arc["animation"] == "ring-sweep-arc var(--dur-sweep) var(--ease-sweep) backwards", arc
    assert dot["animation"] == "ring-sweep-dot var(--dur-sweep) var(--ease-sweep) backwards", dot
    assert arc["animation"].split(" ", 1)[1] == dot["animation"].split(" ", 1)[1], "the dot runs in the arc's time, on the arc's curve"
    assert arc["animation-delay"] == dot["animation-delay"] == "calc(var(--sweep-at) - var(--sweep-elapsed))", "and starts with it"
    assert dot["transform-box"] == "view-box" and dot["transform-origin"] == "50% 50%", "the dot turns about the centre of the svg's box"
    assert re.search(r"@media\(prefers-reduced-motion:reduce\)\{\.ring-sweep \.ring-arc,\.ring-sweep \.ring-dot\{animation:none\}\}", ring), "the reduced pair"
    assert ".ring-sweep-row{--sweep-at:calc(var(--rail-row-at) + var(--i) * var(--rail-row-step))}" in ring, "a ring waits for its row as the row comes down"
    # The time and the curve are tokens; the arc does not rebound (the curve stays in the unit square) and does not drag.
    dur = float(re.fullmatch(r"(\d+)ms", tokens["--dur-sweep"]).group(1))
    assert 600 <= dur <= 700, dur
    x1, y1, x2, y2 = (float(n) for n in re.fullmatch(r"cubic-bezier\(([\d.]+),([\d.]+),([\d.]+),([\d.]+)\)", tokens["--ease-sweep"]).groups())
    assert 0 <= y1 <= 1 and 0 <= y2 <= 1 and 0 <= x1 <= 1 and 0 <= x2 <= 1, "the soft landing: no rebound on the arc"
    for name in ("--ring-share", "--sweep-at", "--sweep-elapsed"):
        assert name in tokens, "declared at its rest value: " + name
    # The script keeps the sweep for as long as the last row of a family needs (up to five rows) to come down and sweep.
    span = int(re.search(r"var SWEEP_MS = (\d+);", source).group(1))
    row_at, step = (float(re.fullmatch(r"(\d+)ms", tokens[n]).group(1)) for n in ("--rail-row-at", "--rail-row-step"))
    assert row_at + 4 * step + dur <= span, (row_at, step, dur, span)
    # The builder is the one that sweeps; the sweep is for the reader's pick (the poll and Refresh never ask for it).
    assert source.count("function ringGauge(pct, form, size, badge, sweep) {") == 1 and source.count("'ring-sweep'") == 1
    assert source.count("sweepPending = 'all';") == 1 and "sweepPending = sweep || '';" in source


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
    **{(media, selector): PRESS_ONCE for media in ("", REDUCED) for selector in (".banner .banner-retry:active", ".wt-scope .wt-scope-opt:active")},
    **{("", selector): RING_ONCE for selector in (
        ".lg .legend-item:focus-visible", ".nx[tabindex]:focus-visible", ".wt-foot-g:focus-visible",
        ".wt-plate>.tl-wrap>.tl-strip:focus-visible", ".wt-plate .kt-toggle:focus-visible")},
    ("", ".lrows>.ins-wrap>.ins-fold>.inspector"): FOLDED_CARD,
    ("", ".lrow.wt-plate"): PLATE,
    ("", ".wt-plate>.l-chart"): PLATE,
    ("", ".wt-plate .wt-num"): PLATE,
    ("", ".wt-plate .wt-num.none"): PLATE,
    ("", ".wt-in>.wt-fact"): PLATE,
    ("", ".wt-in>.wt-limit"): PLATE,
    (REDUCED, ".wt-in>.wt-fact"): PLATE,
    (REDUCED, ".wt-in>.wt-limit"): PLATE,
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
    (".bar.last>.fill", "inset 0 1.5px 0 var(--share-ink)"): "the hairline mark on the top of a last-known fill, between the line and the ring",
    # rule: the selector of the row that is still (.bars-still: the pointer opens nothing under a pointer that has not moved since the pick) is the key now
    (".bar.last:is(:hover:not(:disabled):not(:where(.bars-still>.bar)),.sel,.bar-on)>.fill", "inset 0 1.5px 0 currentColor"): "the same mark under the pointer, on the picked bar or on one a pressed step opened: the hatch stays, the line goes to the bar's ink (primary; the accent when picked)",
    (".bar.unknown", "inset 0 0 0 var(--border-w) var(--share-track)"): "the ring of an unknown bar: a hairline drawn inside in the tone of the track, not a border",
    (".bar:is(.sel,.bar-on)", "inset 0 0 0 var(--trace-w) currentColor"): "the ring of the picked bar or of one a pressed step opened, in its ink (the accent), of the width of the line of the pick (--trace-w, whole device pixels): drawn inside, so the row that scrolls does not cut it; an unknown picked bar wears it too",
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


def test_the_movement_of_a_family_pick_is_the_kits_ends_within_its_span_and_the_rail_stands_on_the_axis():
    """The reader's pick of a family moves the rail in: the tabs slide (the keyframes rail-slide, from the old place the script sets as
    --flip-x and --flip-y to none; the tabs that give way dim by rail-yield, opacity alone, in the same time), a thread is drawn from the
    picked mark down to the first ring, the windows come down one after another, the foot comes last. The canon holds its parts: the keyframes
    are the kit's and move transform and opacity alone, every animation runs backwards (the end state is the plain style, so nothing stays at
    opacity 0 if an animation never starts) and has its reduced pair, the times are tokens that end within the span the script keeps, and the
    head, the rows and the threads all derive from the one axis."""
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    css = _style()
    sections = _sections(css)
    tokens = _tokens(sections)
    rail = sections["kit: rail"]
    frames, bodies = {}, {}
    for pre, body in _blocks(sections["kit: motion"]):
        if pre.startswith("@keyframes rail-"):
            frames[pre.split()[1]] = set(re.findall(r"([a-z-]+):", body))
            bodies[pre.split()[1]] = body.replace(" ", "")
    assert set(frames) == {"rail-draw", "rail-drop", "rail-fade", "rail-slide", "rail-yield"}, frames
    assert all(props and props <= {"transform", "opacity"} for props in frames.values()), frames
    # The slide is from the old place of the tab (the two shifts the script sets on it, once, at the pick) to none; the dimming is opacity alone,
    # to a token half way, and back (no from and no to: the tab's own look).
    assert bodies["rail-slide"] == "from{transform:translate(var(--flip-x),var(--flip-y))}to{transform:none}", bodies["rail-slide"]
    assert frames["rail-yield"] == {"opacity"} and bodies["rail-yield"] == (
        "0%{opacity:var(--yield-from)}25%,80%{opacity:var(--yield-dim)}100%{opacity:1}"), bodies["rail-yield"]
    # The animations start under .rail-moving alone (the script puts it on the rail for the pick and for the drawings in its span),
    # run `backwards` and keep no state; each has its `none` under reduced motion.
    starts = [(media, listing, value.strip()) for media, listing, body in _rules(rail) for prop, value in _declarations(body)
              if prop.strip() == "animation" and value.strip() != "none" and "ladder" not in listing]
    # (the ladder of resets has its own animations, held by test_the_ladder_of_resets_is_the_kits_...: the head and the steps come down under .rail-moving with the rings)
    # (two under .rail-moving: the thread, a row (the foot is gone with the stamp: the stamp stands in the plate); two on the head's tabs: the slide, and the slide with the dimming of the tabs that
    # give way, which is one animation property because a tab that gives way runs both)
    assert len(starts) == 4 and all(m == "" and "backwards" in value and "both" not in value and "forwards" not in value
                                    for m, listing, value in starts), starts
    assert len([s for s in starts if ".rail-moving" in s[1]]) == 2 and not [s for s in starts if "rail-bot" in s[1]] and sorted(s[1] for s in starts if ".tab-mark-slide" in s[1]) == [
        ".tab-mark-slide", ".tab-mark-slide.tab-mark-yield"], starts
    still = {s for media, listing, body in _rules(rail) if "reduced-motion" in media
             for prop, value in _declarations(body) if prop.strip() == "animation" and value.strip() == "none"
             for s in _selectors(listing)}
    assert {s for _, listing, _ in starts for s in _selectors(listing)} <= still, still
    # The slide of the tabs is the keyframes rail-slide in --dur-flip on --ease-flip (no transition: the shifts are set once, at the pick, and a
    # drawing inside the span goes on by a negative delay from --rail-elapsed, as the rows do), the tab that gives way runs rail-yield beside it in
    # the same time, and both have their reduced pair; no will-change is held; nothing that costs a layout or a paint is moved by the rail's rules.
    slide = {listing: {prop.strip(): value.strip() for prop, value in _declarations(body)} for media, listing, body in _rules(rail)
             if ".tab-mark-slide" in listing and not media}
    assert set(slide) == {".tab-mark-slide", ".tab-mark-slide.tab-mark-yield"}, slide
    assert slide[".tab-mark-slide"] == {"animation": "rail-slide var(--dur-flip) var(--ease-flip) backwards",
                                        "animation-delay": "calc(0ms - var(--rail-elapsed))"}, slide
    assert slide[".tab-mark-slide.tab-mark-yield"] == {
        "animation": "rail-slide var(--dur-flip) var(--ease-flip) backwards,rail-yield var(--dur-flip) var(--ease-flip) backwards",
        "animation-delay": "calc(0ms - var(--rail-elapsed))"}, slide
    assert "tab-mark-flip" not in css and "tab-mark-slide{transition" not in css.replace(" ", ""), "the slide is no transition any more"
    assert re.search(r"@media\(prefers-reduced-motion:reduce\)\{\.tab-mark-slide,\.tab-mark-slide\.tab-mark-yield\{animation:none\}\}", rail)
    assert "will-change" not in "".join(sections.values()), "no will-change is held (a comment may say so)"
    assert not re.search(r"(?:transition|animation)[^;{}]*\b(?:all|width|height|top|left|filter|box-shadow)\b", rail), "only transform and opacity move"
    # The thread from the head does not exist on the narrow shell.
    assert [(m, l) for m, l, b in _rules(rail) if ".rail-lead" in l and m == "@media(max-width:859.98px)" and "display:none" in b.replace(" ", "")], "no thread on the narrow shell"
    # The tokens: declared, and read.
    names = ["--rail-axis", "--rail-tab-w", "--mark-px", "--head-h", "--lead-top", "--lead-end", "--lead-end-on", "--dur-flip", "--ease-flip", "--yield-dim",
             "--dur-rail-thread", "--rail-thread-at", "--dur-rail-row", "--rail-row-at", "--rail-row-step", "--dur-rail-foot",
             "--i", "--rail-rows", "--rail-elapsed", "--flip-x", "--flip-y", "--yield-from", "--z-flip"]
    for name in names:
        assert name in tokens, name + " is not declared"
        assert "var(%s" % name in css, name + " is read by nothing"
    # The times fit the span the script keeps (1300 ms): the slide, the thread (from its start), the rows one after another (three windows at
    # most in the places, so three, and the ladder of resets numbered on from them: its head, four steps and the word "N more" that comes down after the last step,
    # nine rows in all, the word's the foot's own), the foot (and the note) after the last row. The thread is drawn before the rows are down, the foot comes
    # last, and the foot's fade is the only thing that may end after the rows.
    ms = lambda name: float(tokens[name].rstrip("ms"))
    span = int(re.search(r"var RAIL_MOTION_MS = (\d+);", source).group(1))
    rows = 3 + 1 + 4
    ends = {"slide": ms("--dur-flip"), "thread": ms("--rail-thread-at") + ms("--dur-rail-thread"),
            "rows": ms("--rail-row-at") + (rows - 1) * ms("--rail-row-step") + ms("--dur-rail-row"),
            "more": ms("--rail-row-at") + rows * ms("--rail-row-step") + ms("--dur-rail-row"),
            "foot": ms("--rail-row-at") + rows * ms("--rail-row-step") + ms("--dur-rail-foot")}
    assert span <= 1300 and max(ends.values()) <= span, (span, ends)
    # The script's own count of the slide (a drawing inside the span puts the slide on its tabs only while it runs) is the token's, and the
    # dimming goes to a token between nothing and the full look.
    assert int(re.search(r"var FLIP_MS = (\d+);", source).group(1)) == ms("--dur-flip"), "FLIP_MS is --dur-flip"
    assert 0 < float(tokens["--yield-dim"]) < 1, tokens["--yield-dim"]
    # The dimming is held while the tab picked passes over the words of the ones that give way: 0 -> the look it starts from (--yield-from, 1 at
    # rest, set by the script on a second pick inside the movement), a quarter of the time -> --yield-dim, four fifths -> --yield-dim, the end -> 1.
    assert tokens["--yield-from"] == "1", tokens["--yield-from"]
    # The script gives the animations of the movement the start time of the pick, once for each drawing and by no frame of its own; the names it
    # asks for are the kit's five keyframes and nothing else (a transition is left alone).
    named = re.search(r"var RAIL_KEYFRAMES = \[(.*?)\];", source).group(1)
    assert sorted(re.findall(r"'([a-z-]+)'", named)) == sorted(frames), named
    clock = re.search(r"\n    function clockSet\(.*?\n    }\n", source, re.S).group(0)
    assert "document.getAnimations()" in clock and ".startTime = start" in clock and clock.count("startTime") == 1, clock
    assert not re.search(r"requestAnimationFrame|setTimeout|setInterval", clock), "no JS by frames: one set of the start time for each animation"
    assert source.count("clockSet(RAIL_KEYFRAMES, railStart)") == 1 and "if (railClock) clockSet(RAIL_KEYFRAMES, railStart);" in source, "once, at the end of a drawing of the movement"
    assert ends["thread"] < ends["rows"] < ends["foot"], ends
    # The foot and the note start after the last row has started (they are never seen alone in an empty rail), and the first row after the
    # thread has started and the slide is well on its way.
    assert ms("--rail-thread-at") < ms("--rail-row-at") < ms("--rail-row-at") + (rows - 1) * ms("--rail-row-step") + ms("--rail-row-step")
    # The curve of the slide is its own token (a soft start, no bounce): a cubic-bezier whose y never leaves 0..1, which in the first two
    # frames of a 60 fps slide goes at most 15% of the way and in no frame more than twice the average step; the thread starts when the tabs
    # have gone about 70% of the way.
    curve = [float(n) for n in re.fullmatch(r"cubic-bezier\(([^,]+),([^,]+),([^,]+),([^)]+)\)", tokens["--ease-flip"]).groups()]
    x1, y1, x2, y2 = curve
    assert 0 <= x1 <= 1 and 0 <= x2 <= 1 and 0 <= y1 <= 1 and 0 <= y2 <= 1, "no bounce: " + tokens["--ease-flip"]

    def way(at):
        bezier = lambda a, b, u: 3 * (1 - u) ** 2 * u * a + 3 * (1 - u) * u ** 2 * b + u ** 3
        low, high = 0.0, 1.0
        for _ in range(60):
            mid = (low + high) / 2
            low, high = (mid, high) if bezier(x1, x2, mid) < at else (low, mid)
        return bezier(y1, y2, (low + high) / 2)

    frame, duration = 1000 / 60, ms("--dur-flip")
    path = [way(min(1.0, i * frame / duration)) for i in range(int(duration / frame) + 2)]
    steps = [b - a for a, b in zip(path, path[1:])]
    assert path[2] <= 0.15, "the first two frames go %.1f%% of the way" % (path[2] * 100)
    assert max(steps) <= 2 * (frame / duration), "a frame goes %.1f times the average step" % (max(steps) / (frame / duration))
    on_the_way = next(i * frame for i in range(1000) if way(min(1.0, i * frame / duration)) >= 0.7)
    assert abs(on_the_way - ms("--rail-thread-at")) <= 20, "70%% of the way is at %.0f ms, the thread starts at %s" % (on_the_way, ms("--rail-thread-at"))
    # The foot fades in with the rail's keyframes, in its own time (--dur-rail-foot), and the note under the rows fades in the same way: both
    # start at the rail's row of the foot, run backwards, and have their `none` under reduced motion.
    foot_rule = [(m, l, b) for m, l, b in _rules(sections["capsule: window tabs"]) if l == ".wt-foot-in"]
    assert len(foot_rule) == 1 and not foot_rule[0][0], foot_rule
    moving = foot_rule[0][2]
    assert "animation:rail-fade var(--dur-rail-foot) var(--ease) backwards" in moving and "--rail-rows" in moving, moving
    assert any("reduced-motion" in m and "animation:none" in b for m, l, b in _rules(sections["capsule: window tabs"]) if ".wt-foot-in" in l)
    # The layer of the tab that rides over the others is a token, and the thread over a first ring that is picked ends as high as the ring has grown.
    assert re.search(r"\.tab-mark-ride\{z-index:var\(--z-flip\)\}", rail) and tokens["--z-flip"] == "1"
    assert tokens["--lead-end-on"] == "calc(var(--lead-end) - var(--ring-grow))"
    assert tokens["--ring-grow"] == "calc((var(--mark-on) - 1) * var(--ring-box) / 2)", "the ring grows about its centre: half of the growth at each end"
    # The thread keeps --thread-gap from the badge over it (it starts that gap under the badge, and the badge's drop is told in the ring's unit).
    # The thread between two rows keeps 2px under the badge above and 2px over the box of the ring below, whichever ring is picked, so it is the room that is
    # left: --thread-len at rest, less the growth of the ring below (picked: x1.2) or of the badge above (picked: its bottom goes down with the ring). The row is
    # 74px: the least row whose shortest thread (the badge above picked) is 8px or more. The rail's top is such that the box of the first ring (and the
    # thread from the head) stands where it stood with the ring of 56px (31px under the top of the rail).
    assert tokens["--rail-row-h"] == "74px" and tokens["--rail-top"] == "17.5px" and tokens["--ring-size-lg"] == "47px"
    assert tokens["--thread-len"] == "calc(var(--rail-row-h) - var(--ring-box) / 2 - var(--badge-drop) - 2 * var(--thread-gap))"
    ring_px, unit = 47, 47 / 184
    drop = (173 + 43.5) * unit - ring_px / 2
    assert abs(drop - 31.80) <= 0.01 and abs(74 - ring_px / 2 - drop - 4 - 14.70) <= 0.02, "the thread at rest is 14.7px in a row of 74px"
    assert 74 - ring_px / 2 - drop * 1.2 - 4 >= 8 - 0.0, "the badge above picked: the thread keeps 8px (the top gap is the thread-gap under the grown badge, the bottom one the thread-gap over the box)"
    assert 74 - ring_px / 2 * 1.2 - drop - 4 >= 8, "the ring below picked: the thread keeps 8px"
    assert abs(15.5 + (87 - 56) / 2 - (17.5 + (74 - 47) / 2)) <= 0.01, "the box of the first ring stands where it stood"
    assert tokens["--thread-top"] == "calc(var(--thread-gap) + var(--badge-drop) - var(--rail-row-h) / 2)"
    assert tokens["--badge-grow"] == "calc((var(--mark-on) - 1) * var(--badge-drop))", "the badge grows with the ring about the centre of the box: its bottom goes down by that"
    assert tokens["--thread-gap"] == "var(--space-0)" and tokens["--ring-box"] == "var(--ring-size-lg)", "the box of a ring is the ring"
    # The badge in the gap of the ring is told in the ring's own unit (the size over the 184 of the viewBox): its outer radius (the circle 41 and half of the 5 of the stroke),
    # the height of its centre (the lowest point of the path, 92 + 81), and how far its bottom stands under the middle of the box.
    assert tokens["--ring-unit"] == "calc(var(--ring-size-lg) / 184)" and tokens["--badge-r"] == "calc(var(--ring-unit) * 43.5)" and tokens["--badge-cy"] == "calc(var(--ring-unit) * 173)"
    assert re.search(r"var BADGE_R = 41;\s+var BADGE_STROKE = 5;", source) and "'stroke-width': BADGE_STROKE" in source, "the badge is drawn with a stroke thinner than the track's 7"
    assert 41 + 5 / 2 == 43.5 and 92 + 81 == 173 and "lg: { px: 47, r: 81," in source, "the tokens' 43.5 and 173 mirror the script: BADGE_R + BADGE_STROKE / 2, and 92 + the radius of the large ring's path"
    # The room inside the badge for the name at 7.5px (semibold, tabular; the ink of "30D" 15.0 x 5.6px and of "30д" 14.5 x 6.5px, measured in the system face): the corners of
    # the ink are 1px or more from the circle; the circle stands over the ends of the arc (their round caps) by 2px at most; the share fits the hole with 2px to spare.
    inner = (41 - 5 / 2) * unit
    for w, h in ((14.98, 5.55), (14.54, 6.46)):
        assert inner - ((w / 2) ** 2 + (h / 2) ** 2) ** .5 >= 1, ("the name has a pixel of air inside the badge", w, h, inner)
    arc_end = ((40.5) ** 2 + (173 - 162.15) ** 2) ** .5
    assert ((41 + 5 / 2) + 9 / 2 - arc_end) * unit <= 2, "the badge stands over the round end of the arc by 2px at most"
    hole = (81 - 9 / 2) * unit
    assert hole - ((26.0 / 2) ** 2 + (8.2 / 2) ** 2) ** .5 >= 2, "'100%' at 11px (26 x 8.2px) has 2px of air from the arc"
    assert (173 - 43.5) * unit - ring_px / 2 - 8.2 / 2 >= 2, "and from the top of the badge"
    assert tokens["--badge-drop"] == "calc(var(--badge-cy) - var(--ring-size-lg) / 2 + var(--badge-r))"
    assert tokens["--type-badge"] == "7.5px" and tokens["--type-star"] == "9px", "the badge's name is 7.5px (semibold) at least, the star 9px"
    assert ".rail-lead-on{height:calc(var(--lead-end-on) - var(--lead-top))}" in rail
    # The axis: the rows of the rail, the threads between them, the thread from the head and the row of the head all derive from it, and
    # the mark of a family is as wide as the script draws it.
    assert tokens["--rail-axis"] == "calc(var(--rail-tab-w) / 2)", \
        "the axis it falls back to: the middle of the widest tab (railFit puts the middle of the tab picked)"
    # rule: the rail is what a ring needs at the axis of the widest tab of the head's row (the same for every family, railFit puts the width of the widest tab in
    # --rail-tab-w, 114px until it does): half of that tab, half of the ring, the growth of the ring that is picked and the air at the card; the axis is the middle of
    # the tab picked (railFit puts it in --rail-axis; the formula above is what it falls back to). It was a fixed 72px, then the widest tab and the gap of the page.
    assert tokens["--shell-card-gap"] == "var(--space-2)" and tokens["--rail-air"] == "var(--space-2)"
    assert tokens["--rail-w"] == "calc(var(--rail-tab-w) / 2 + var(--ring-box) / 2 + var(--ring-grow) + var(--rail-air))", "the rail: what the ring on the axis of the widest tab asks for"
    assert tokens["--rail-tab-w"] == "114px", "the widest tab known until the script reads the row (Antigravity, 113.6px)"
    assert tokens["--mark-px"] == "%spx" % re.search(r"var FAMILY_MARK_PX = (\d+);", source).group(1)
    # The place that holds the chart while it is read has the layout of the drawn block: the plot's own canvas (.chart-svg, so it scales as the plot
    # does, no height written here), a line of legend (.calm-legend) and the row of Details held as high as its summary; no fixed height, no token.
    chart = sections["capsule: chart"]
    assert ".chart-wait{position:relative;margin-top:var(--space-3)}" in chart and ".chart-hold{min-height:var(--row-h)}" in chart
    assert "@media(pointer:coarse){.chart-hold{min-height:var(--tap-h)}}" in chart and "--chart-h" not in css
    assert ".chart-wait-text{position:absolute;top:0;right:0;bottom:0;left:0;display:flex;align-items:center;justify-content:center;text-align:center}" in chart
    assert "padding:0 var(--space-2) 0 calc(var(--rail-axis) - var(--ring-box) / 2)" in rail
    # The compact rail: a row is the box of the ring alone (the share inside it, the short word as the badge in the gap, the star at the badge's upper right).
    assert ".tab-mark-rail{position:relative;display:flex;align-items:center;width:100%;min-height:var(--rail-row-h);" in rail
    assert ".tab-mark-ring{position:relative;display:grid;flex:none;place-items:center;width:var(--ring-box);height:var(--ring-box);transition:transform var(--dur-nav) var(--nav-spring)}" in rail, "nothing stands at the right of the ring: no margin there"
    assert ".tab-mark-ring>*{grid-area:1/1}" in rail, "the ring and its share stand in the same place of the box"
    assert ".tab-mark-rail .tab-mark-count{font-size:var(--type-ring);font-weight:var(--weight-semibold);line-height:1;white-space:nowrap}" in rail
    assert ".tab-mark-rail .tab-mark-unit{font-size:var(--type-unit)}" in rail
    # A dash (nothing read) is no case of its own: it is --text-meta at rest (the number tab's rule) and --accent when its row is picked, like a number; nothing in the rail
    # holds it in the meta ink.
    assert "count-none" not in css and "count-none" not in Path(__file__).with_name("widget.js").read_text(encoding="utf-8"), "the dash wears the number's colours: no rule and no class of its own"
    assert ".tab-mark-num[aria-selected=true] .tab-mark-count,.tab-mark-num .tab-mark-count.tab-mark-was-on{color:var(--accent)}" in sections["kit: mark tab"], "picked: the accent"
    assert ".tab-mark-num .tab-mark-count{color:var(--text-meta);transition:color var(--dur-5) var(--ease)}" in sections["kit: mark tab"], "at rest: the meta ink"
    assert tokens["--type-ring"] == "11px" and tokens["--type-unit"] == "8px" and tokens["--ring-size-lg"] == "47px"
    # rule: the rail is what a ring needs at the axis of the widest tab (--rail-w: half of that tab, half of the ring, the growth of the ring that is picked and the air at the card); the ring (56.4px picked) stands on the axis,
    # the middle of the tab picked, and the narrowest tab (90px) keeps more than the ring and its air in its half (45px axis: 17px of air at the left of the picked ring).
    assert float(re.fullmatch(r"(\d+)px", tokens["--rail-tab-w"]).group(1)) >= 2 * (float(tokens["--ring-size-lg"].rstrip("px")) * float(tokens["--mark-on"]) / 2 + 8), "the ring picked fits the axis of the narrowest tab"
    assert "--rail-star-gap" not in css, "nothing stands at the right of the ring now: no margin of a word"
    # The short name is the badge in the gap of the ring on the wide rail (in the box of the ring, over the badge circle, on its centre, semibold, the meta ink, the ink when picked
    # or under the pointer, its colour fading by --dur-5); the star of a window not read whole is a bead on the axis, on the lower edge of the badge (the middle of its glyph
    # --star-rise over the edge, so it hangs under it by half a pixel and the thread, 2px under the badge, is 1.5px from it). The narrow shell hides the circle.
    assert ("@media not all and (max-width:859.98px){.tab-mark-ring .tab-mark-name{position:absolute;left:50%;top:var(--badge-cy);overflow:visible;text-overflow:clip;transform:translate(-50%,-50%);"
            "color:var(--text-meta);font-size:var(--type-badge);font-weight:var(--weight-semibold);line-height:1;font-variant-numeric:tabular-nums;transition:color var(--dur-5) var(--ease)}"
            ".tab-mark-ring .tab-mark-note{position:absolute;left:50%;top:var(--star-top);margin:0;transform:translateX(-50%);font-size:var(--type-star);line-height:1}}") in rail
    assert tokens["--star-rise"] == "1.2px" and tokens["--star-top"] == "calc(var(--badge-cy) + var(--badge-r) - var(--star-rise) - var(--type-star) * .38)"
    assert "--star-x" not in css and "--star-y" not in css, "the star no longer stands at the upper right"
    # Where the star falls (the system face: the middle of the asterisk's ink is .38 of the font size under the top of a one-line box; the ink is 3.4px high at 9px):
    # the middle is 1.2px over the lower edge of the badge, so the ink hangs 0.5px under it and the star is 1.5px (1 to 3) from the thread, which starts 2px under the badge.
    edge = (173 + 43.5) * unit
    middle = edge - 1.2
    assert abs(((edge - 1.2 - 9 * .38) + 9 * .378) - middle) <= 0.05 and 0 < (middle + 3.36 / 2) - edge <= 1 and 1 <= 2 - ((middle + 3.36 / 2) - edge) <= 3
    assert "@media not all and (max-width:859.98px){.tab-mark-rail[aria-selected=true] .tab-mark-name,.tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .tab-mark-name{color:var(--text-primary)}}" in rail
    assert "@media(max-width:859.98px){.ring-badge{display:none}}" in rail and ".ring-badge{fill:var(--shell-bg);stroke:var(--ring-badge-edge)}" in css
    # The edge of the badge is its own token, built on the ink like the rest of the ring: stronger than the track (.16) and quieter than the arc (.55); .30 on the dark
    # ground, .26 on the light one (declared in both blocks); the track keeps its own.
    assert tokens["--ring-badge-edge"] == "rgba(var(--neutral-rgb),.3)" and tokens["--ring-track"] == "rgba(var(--neutral-rgb),.16)"
    assert "--ring-badge-edge:rgba(var(--neutral-rgb),.26)" in css and css.count("--ring-badge-edge:rgba(") == 2, "the light theme sets .26 of its own"
    # The threads between the rings keep their gap from a ring that is picked (it has grown): the thread over it ends higher, the one under it starts lower.
    assert "@media not all and (max-width:859.98px){.tab-mark-rail[aria-selected=true]::before{height:calc(var(--thread-len) - var(--ring-grow))}" in rail, "the thread over a ring that is picked ends as high as the ring has grown"
    assert ".tab-mark-rail[aria-selected=true]+.tab-mark-rail::before{top:calc(var(--thread-top) + var(--badge-grow));height:calc(var(--thread-len) - var(--badge-grow))}}" in rail, "and starts as low as the badge above has gone"
    # The hover and the press of a row are the app's, made of tokens (the sidebar's: the spring --nav-spring for the way back, a quick step in --dur-1 for the press, --press for how
    # far the ring is pressed in); the preview darkens the arc, the dot and the edge of the badge a step, between the rest and the picked look, by a colour transition.
    assert tokens["--press"] == ".94" and tokens["--nav-spring"] == "cubic-bezier(.34,1.5,.64,1)" and tokens["--dur-1"] == "120ms" and tokens["--dur-5"] == "220ms"
    hover = ":is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true])"
    for part, prop, token in ((".ring-arc", "stroke", "--ring-arc-hover"), (".ring-dot", "fill", "--ring-dot-hover"), (".ring-badge", "stroke", "--ring-badge-edge-hover")):
        assert (".tab-mark-rail%s %s{%s:var(%s)}" % (hover, part, prop, token)) in rail, part
    assert ".tab-mark-rail :is(.ring-arc,.ring-dot,.ring-badge){transition:stroke var(--dur-5) var(--ease),fill var(--dur-5) var(--ease)}" in rail
    arc, dot, edge_t = (float(re.search(r"rgba\(var\(--neutral-rgb\),(\.\d+)\)", tokens[k]).group(1)) for k in ("--ring-arc", "--ring-dot", "--ring-badge-edge"))
    arc_h, dot_h, edge_h = (float(re.search(r"rgba\(var\(--neutral-rgb\),(\.\d+)\)", tokens[k]).group(1)) for k in ("--ring-arc-hover", "--ring-dot-hover", "--ring-badge-edge-hover"))
    arc_on, dot_on = (float(re.search(r"rgba\(var\(--neutral-rgb\),(\.\d+)\)", tokens[k]).group(1)) for k in ("--ring-arc-on", "--ring-dot-on"))
    assert arc < arc_h < arc_on and dot < dot_h < dot_on and edge_t < edge_h < arc, "a step darker than the rest and lighter than the picked"
    assert "--ring-badge-edge-hover:rgba(var(--neutral-rgb),.4)" in css and css.count("--ring-badge-edge-hover:rgba(") == 2, "the light theme sets its own step"
    press = ".tab-mark-rail:not([aria-selected=true]):not([aria-disabled=true]):active .tab-mark-ring{transform:scale(var(--press));transition:transform var(--dur-1) var(--ease)}"
    press_on = ".tab-mark-rail[aria-selected=true]:active .tab-mark-ring{transform:scale(calc(var(--mark-on) * var(--press)));transition:transform var(--dur-1) var(--ease)}"
    assert press in rail and press_on in rail and rail.index(press) > rail.index(".tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .tab-mark-ring{transform:scale(var(--mark-hover))}"), "the press stands after the preview"
    assert ".tab-mark-ring{position:relative" in rail and "transition:transform var(--dur-nav) var(--nav-spring)}" in rail, "let go, the ring springs back on --nav-spring (the base rule's transition)"
    assert (".tab-mark-rail:not([aria-selected=true]):not([aria-disabled=true]):active .ring{transform:scale(var(--press));transition:transform var(--dur-1) var(--ease)}"
            ".tab-mark-rail[aria-selected=true]:active .ring{transform:scale(calc(var(--mark-on) * var(--press)))") in rail, "on the narrow shell (a finger) the ring itself is pressed"
    # The pair of reduced motion: no fade, no spring, and a press that does not shrink the ring (the picked one stays at its size).
    pair = [b for m, l, b in _rules(rail) if "reduced-motion" in m and ":active" in l and "rail-bead" not in l]
    assert len(pair) >= 4 and all(("transform:none" in b or "transform:scale(var(--mark-on))" in b) and "transition:none" in b for b in pair), pair
    assert any("reduced-motion" in m and ".tab-mark-rail :is(.ring-arc,.ring-dot,.ring-badge)" in l and "transition:none" in b for m, l, b in _rules(rail)), "no fade of the ring's colours"
    assert not re.search(r"@keyframes[^}]*press", css) and "transition:all" not in css, "only transform and colour move, no keyframes for the press"
    # The narrow shell: the box of the ring is no box, the share stands after the word in the line of the capsule, the ring itself grows.
    assert "@media(max-width:859.98px){.tab-mark-ring{display:contents}" in rail and ".tab-mark-rail .tab-mark-count{position:absolute;top:0;right:var(--space-2h);bottom:0;display:flex;align-items:center;font-size:var(--type-meta)}" in rail
    assert tokens["--share-w"] == "32px" and "padding:0 calc(var(--space-2h) + var(--share-w)) 0 0" in rail, "the capsule keeps room for the share at its end"
    assert rail.count("left:calc(var(--rail-axis) - var(--border-w) / 2)") == 2, "the thread from the head and the threads between the rows"
    assert rail.count("left:calc(50% - var(--border-w) / 2)") == 1, "the threads between the steps of the ladder stand in the middle of a step, which is the ladder's, on the axis"
    # rule: the row of the head no longer stands off the edge by the axis (it was padding-left): the tab picked stands at the edge of the
    # widget, as far from it as from the top, and the axis is the middle of that tab (railFit).
    assert "padding-left" not in sections["capsule: head"].split(".harness-seg{flex:1 1 280px}")[1].split("About and Refresh")[0], "no air of the axis at the left of the families' row"
    assert "@media not all and (max-width:859.98px){.harness-seg" not in css
    # The rail has no caption.
    assert "rail-cap" not in "".join(sections.values()) and "rail.cap" not in source and "rail-cap" not in source


# What the prose of the script says of a place without a window. The rail draws only the windows a family reports (windowRow): a place the
# family has no window for is not drawn at all - no grey place, no hint - and a window the reader hid in Rows (the family's still) keeps the
# one dotted, aria-disabled row. The words that said such a place "stays grey" went stale with that, and a comment that says the wrong
# thing is worse than none: each pattern here is one of them (what it said, and what is true now).
_STALE_PLACE_PROSE = (
    (r"\b(?:stay|stays|remain|remains)\b,? (?:grey|gray)\b", "a place without a window is not drawn: it does not stay grey"),
    (r"\b(?:is|are)\b,? (?:grey|gray),? still\b", "a place without a window is not a grey tab that keeps its hint"),
    (r"(?<!no )\b(?:grey|gray) places?\b", "there is no grey place (a window the reader hid in Rows is a dotted row, not a place)"),
    (r"aria-disabled[^.;]{0,60}does not report", "an aria-disabled row is a window the reader hid in Rows, not one the family does not report"),
)


def _flat(text):
    # A comment breaks between lines: its lines are one text (the break, the indent and the comment mark are one space).
    return re.sub(r"\s*\n\s*(?://\s*)?", " ", text)


def _stale_place_prose(text):
    flat = _flat(text)
    return ["...%s...  (%s)" % (flat[max(0, hit.start() - 60):hit.end() + 60], why)
            for pattern, why in _STALE_PLACE_PROSE for hit in re.finditer(pattern, flat)]


def test_no_prose_of_the_script_says_a_place_without_a_window_is_grey():
    """windowRow draws a row only for a window the family reports; Cursor (a budget per billing cycle, no time window) has the one row, the
    30-day place, and a family with no window and no budget has no rail. So no comment of the script says that a place the family has no
    window for stays grey, keeps a hint, or is a grey place; the one aria-disabled row is a window the reader hid in Rows."""
    # The check sees what it is written for: the sentences that went stale, and the ones that replaced them.
    for stale in ("5-hour and Weekly stay grey, and their hint says why.",
                  "A place the family has no window for stays, grey, with its reason on\n       hover",
                  "A place the family has no window for is grey, still, and keeps its hint;",
                  "(then the three places stay grey)",
                  "A tab greyed with aria-disabled (a window the family does not\n            // report) is skipped as a disabled one is",
                  "the third place is a grey place"):
        assert _stale_place_prose(stale), stale
    for good in ("a place it has no window for is not drawn at all (no grey place)",
                 "A tab greyed with aria-disabled (a window the reader hid in Rows)\n            // is skipped as a disabled one is",
                 "Green is \"read, and fine\"; grey is no live reading", "A word that waits is disabled, and the base's ink would grey it"):
        assert not _stale_place_prose(good), good
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    found = _stale_place_prose(source)
    assert not found, "prose of the script that says a place without a window is grey (windowRow draws no such place):\n" + "\n".join(found)
    # And the places that carried the stale words say what is true now.
    flat = _flat(source)
    assert re.search(r"aria-disabled \(a window the reader hid in Rows\) is skipped", flat), "the arrow keys' comment names the one aria-disabled row"
    assert "it is the one row: 5-hour and Weekly are not drawn" in flat, "the Cursor branch of the reserve says the other places are not drawn"
    assert "it is the one tab of the row, 5-hour and Weekly are not drawn" in flat, "the note on the billing-cycle budget says the same"
    assert "a place it has no window for is not drawn at all (no grey place)" in flat, "windowRow says what it draws"


def test_the_ladder_of_resets_is_the_kits_part_of_the_rail_in_tokens_with_its_contract_its_motions_and_their_reduced_pairs():
    """kit: rail, the ladder of resets under the rings. Held here: the section's comment is the ladder's contract (what it is, how to use it, the tokens
    it reads, what not to do) and every token the contract names is declared; the sizes are tokens (the bead 7px, the hairline 22px, 11px type, the heights
    the lines fit); the zone is the room that is left and adds nothing to the rail (flex 1 1 0, min-height 0, cut at its edge: the card is never stretched), the
    narrow shell has none of it and the plate's row of the next reset is hidden on the wide shell alone; the red is the ink of the bead under the pointer and
    nowhere at rest; the motions are the kit's (the keyframes in kit: motion, transform and opacity, run `backwards`, each with its `none` under reduced motion),
    the family's pick brings the head and the steps down with the rail's own keyframes, numbered on from the rings, and the pick of a window moves the ladder alone;
    the spans the script keeps hold them; and the fit is one read of the layout, with no observer and no frame."""
    source = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    css = _style()
    sections = _sections(css)
    tokens = _tokens(sections)
    rail = sections["kit: rail"]
    rules = {(media, listing): {p.strip(): v.strip() for p, v in _declarations(body)} for media, listing, body in _rules(rail)}
    # The contract: the comment that opens the ladder's part of the section (as the ring's does for the ring).
    contract = re.search(r"// The ladder of resets \(kit: rail, its second part.*?\n(?=\s+\"\.rail-ladder-zone)", css, re.S)
    assert contract, "the ladder's contract stands in kit: rail"
    comment = contract.group(0)
    for part in ("What it is:", "Motion:", "How to use it:", "Tokens it reads:", "Not to do:"):
        assert part in comment, part
    named = re.search(r"Tokens it reads: (.*?)\.\n", comment, re.S).group(1)
    for name in re.findall(r"--[a-z0-9-]+", named):
        assert name in tokens, name + " is named in the contract and not declared"
    # The sizes are tokens: the bead, the hairline, the type, the room of the head, of a step and of the word that counts.
    assert tokens["--ladder-bead"] == "7px" and tokens["--ladder-hair"] == "22px" and tokens["--type-ladder"] == "11px"
    assert tokens["--ladder-bead-grow"] == "1.4" and tokens["--ladder-bead-rest"] == "rgba(var(--neutral-rgb),.38)"
    head, step, more = (float(tokens[n][:-2]) for n in ("--ladder-title-h", "--ladder-step-h", "--ladder-more-h"))
    assert head >= 12 + 1 + 8 + 11 * 1.35 and step >= 12 + 7 + 4 + 12 * 1.25 + 11 * 1.25, "the lines (how long to the reset, the amount) fit the room the heights give them"
    assert step < 12 + 7 + 4 + 12 * 1.25 + 2 * 11 * 1.25, "and the step is the one of two lines: the line of the day is gone"
    # The zone adds nothing to the rail's height, is cut at its edge, and the ladder stands on the axis (a field of 64px, 32 each side).
    assert rules[("", ".rail-ladder-zone")] == {"position": "relative", "flex": "1 1 0", "min-height": "0", "overflow": "hidden"}
    # rule: the ladder is as wide as the rail leaves round the axis (the same at both sides, --ladder-air short of the card where the axis is near it), never wider than the rail
    assert tokens["--ladder-w"] == "calc(min(var(--rail-axis), var(--rail-w) - var(--rail-axis) - var(--ladder-air)) * 2)" and tokens["--ladder-air"] == "var(--space-1)"
    assert rules[("", ".rail-ladder")]["width"] == "var(--ladder-w)" and rules[("", ".rail-ladder")]["margin-left"] == "calc(var(--rail-axis) - var(--ladder-w) / 2)", "the ladder stands on the axis"
    assert rules[("", ".rail-ladder-title::before")]["width"] == "var(--ladder-hair)" and rules[("", ".rail-ladder-title::before")]["background"] == "var(--rail-thread)"
    assert rules[("", ".rail-ladder-title")]["font-size"] == "var(--type-ladder)" and rules[("", ".rail-ladder-title")]["color"] == "var(--text-secondary)"
    assert rules[("", ".rail-step-time")]["font-weight"] == "var(--weight-semibold)" and rules[("", ".rail-step-time")]["color"] == "var(--text-primary)"
    assert rules[("", ".rail-step-amount")]["font-size"] == "var(--type-ladder)"
    # The amount is the quiet 11px (the step's own meta ink); the decimal point of the amount is the crimson ink; the nearest reset's bead too. The step has no line of the day.
    assert ("", ".rail-step-day") not in rules and "rail-step-day" not in source, "the day is gone from the step"
    assert "color" not in rules[("", ".rail-step-amount")], "the amount is in the step's meta ink"
    assert rules[("", ".rail-step")]["color"] == "var(--text-meta)"
    assert rules[("", ".rail-step-dot")] == {"color": "var(--tone-crimson-ink)"}, "the point of the amount"
    assert rules[("", ".rail-step-first .rail-bead")] == {"background": "var(--tone-crimson-ink)"}, "the bead of the first step, the nearest reset"
    assert rules[("", ".rail-bead")]["background"] == "var(--ladder-bead-rest)" and rules[("", ".rail-bead")]["width"] == "var(--ladder-bead)"
    assert rules[("", ".rail-step~.rail-step::before")]["background"] == "var(--rail-thread)", "a thread between two steps, as between two rings"
    hover = rules[("", ".rail-step:is(:hover,:focus-visible) .rail-bead")]
    assert hover == {"transform": "scale(var(--ladder-bead-grow))", "background": "var(--tone-crimson-ink)"}, hover
    assert "var(--press)" in rules[("", ".rail-step:active .rail-bead")]["transform"], "the press is the kit's"
    assert rules[("", ".rail-more")]["text-decoration"] == "underline dotted" and rules[("", ".rail-more")]["font-size"] == "var(--type-ladder)"
    assert rules[("@media(max-width:859.98px)", ".rail-ladder-zone")] == {"display": "none"}, "no ladder on the narrow shell"
    assert rules[("@media not all and (max-width:859.98px)", ".nx-in-ladder.nx-in-ladder")] == {"display": "none"}, "the plate's row hides on the wide shell alone (the mark twice: it outweighs the row's own display:flex without naming the part)"
    none = rules[("", ".rail-step-none")]
    assert none["white-space"] == "normal" and none["max-width"] == "calc(100% - var(--space-1))", "a long phrase wraps inside the field of the ladder (no line wider than the ladder)"
    assert rules[("", ".rail-step-time")]["font-size"] == "var(--type-meta)" and tokens["--type-meta"] == "12px", "how long to the reset is the kit's meta size, 12px, 600: the longest of it (2d 23h) stands in 60px"
    assert rules[("", ".rail-fit-off")] == {"display": "none"}
    # The red at rest is the first bead and the points of the amounts (the ink); the others come under the pointer or the keyboard. No rule of the ladder names a status colour or
    # the crimson fill, and the accent is named by ONE rule alone: the time of the step that is pressed (a step is a button, and the pressed one is seen
    # by its time in --accent, nothing else of it changes). Changed on purpose: until then the canon said no rule of the ladder names the accent at all.
    ladder_rules = {k: v for k, v in rules.items() if "ladder" in k[1] or "rail-step" in k[1] or "rail-bead" in k[1] or "rail-more" in k[1]}
    assert ladder_rules and not [k for k, v in ladder_rules.items() if any(x in " ".join(v.values()) for x in ("--status-", "--tone-crimson)"))]
    assert {k[1]: v for k, v in ladder_rules.items() if "--accent" in " ".join(v.values())} == {".rail-step[aria-pressed=true] .rail-step-time": {"color": "var(--accent)"}}
    red = {k[1] for k, v in ladder_rules.items() if "--tone-crimson-ink" in " ".join(v.values())}
    assert red == {".rail-step-first .rail-bead", ".rail-step-dot", ".rail-step:is(:hover,:focus-visible) .rail-bead"}, red
    # The motions: the keyframes of the kit (transform and opacity), backwards, with a reduced pair for each; the family's pick uses the rail's own drop.
    frames = {pre.split()[1]: body.replace(" ", "") for pre, body in _blocks(sections["kit: motion"]) if pre.startswith("@keyframes ladder-")}
    assert frames == {"ladder-drop": "from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}", "ladder-leave": "from{opacity:1}"}, frames
    family = rules[("", ".rail-moving .rail-ladder-title,.rail-moving .rail-step,.rail-moving .rail-more")]
    assert family["animation"] == "rail-drop var(--dur-rail-row) var(--ease) backwards", "the head and the steps come down as a window does"
    assert family["animation-delay"] == "calc(var(--rail-row-at) + var(--i) * var(--rail-row-step) - var(--rail-elapsed))", "numbered on from the rings"
    window = rules[("", ".rail-ladder-moving .rail-ladder-title,.rail-ladder-moving .rail-step,.rail-ladder-moving .rail-more")]
    assert window["animation"] == "ladder-drop var(--dur-rail-row) var(--ease) backwards"
    assert window["animation-delay"] == "calc(var(--ladder-at) + var(--i) * var(--rail-row-step) - var(--ladder-elapsed))"
    out = rules[("", ".rail-ladder-out")]
    assert out["animation"] == "ladder-leave var(--dur-ladder-out) var(--ease) backwards" and out["opacity"] == "0", "the old steps stand at nothing when the fade is over"
    assert out["pointer-events"] == "none"
    pair = [b for m, l, b in _rules(rail) if "reduced-motion" in m and ".rail-ladder-out" in l and "animation:none" in b.replace(" ", "")]
    assert pair, "the reduced pair of the animations"
    still = {s for m, l, b in _rules(rail) if "reduced-motion" in m for p, v in _declarations(b) if p.strip() in ("animation", "transition") and v.strip() == "none"
             for s in _selectors(l)}
    for sel in (".rail-moving .rail-ladder-title", ".rail-moving .rail-step", ".rail-moving .rail-more", ".rail-ladder-moving .rail-ladder-title", ".rail-ladder-moving .rail-step",
                ".rail-ladder-moving .rail-more",
                ".rail-ladder-out", ".rail-bead", ".rail-step-amount", ".rail-step:active .rail-bead"):
        assert sel in still, sel + " has no reduced-motion pair"
    # The times: the window's pick ends within LADDER_MS (the head, four steps and the word that counts), the family's within RAIL_MOTION_MS (the rings, the head, four steps,
    # the word, the foot).
    ms = lambda name: float(tokens[name].rstrip("ms"))
    ladder_ms = int(re.search(r"var LADDER_MS = (\d+);", source).group(1))
    assert ms("--ladder-at") + 5 * ms("--rail-row-step") + ms("--dur-rail-row") <= ladder_ms and ms("--dur-ladder-out") <= ms("--ladder-at") + ms("--rail-row-step")
    rail_ms = int(re.search(r"var RAIL_MOTION_MS = (\d+);", source).group(1))
    assert ms("--rail-row-at") + (3 + 1 + 4) * ms("--rail-row-step") + max(ms("--dur-rail-foot"), ms("--dur-rail-row")) <= rail_ms, "the word's row (the foot's own number) and the foot end within the span"
    assert int(re.search(r"var LADDER_STEPS = (\d+);", source).group(1)) == 4, "up to four steps"
    assert re.search(r"var LADDER_KEYFRAMES = \['ladder-drop', 'ladder-leave'\];", source)
    # The layout is one read of the layout: the height of the zone and of three parts (and the one token of the window), in the one pass, at the end of a drawing or at the press of the word; no observer, no frame.
    fit = re.search(r"\n    function ladderLayout\(.*?\n    }\n", source, re.S).group(0)
    assert fit.count(".clientHeight") == 1 and fit.count(".offsetHeight") == 3 and "getBoundingClientRect" not in fit and fit.count("getComputedStyle") == 1, fit
    assert "ladderLayout(z, false)" in re.search(r"\n    function ladderFit\(\) \{.*?\n    }\n", source, re.S).group(0)
    assert not re.search(r"requestAnimationFrame|setTimeout|setInterval|ResizeObserver", fit), "no script by frames and no observer"
    assert source.count("ladderFit();") == 1 and "        ladderFit();\n        askForChart();" in source, "once, at the end of a drawing"
    assert source.count("if (ladderClock) clockSet(LADDER_KEYFRAMES, ladderStart);") == 1
    clock = re.search(r"\n    function clockSet\(.*?\n    }\n", source, re.S).group(0)
    assert "document.getAnimations()" in clock and clock.count("startTime") == 1 and not re.search(r"requestAnimationFrame|setTimeout|setInterval", clock)
