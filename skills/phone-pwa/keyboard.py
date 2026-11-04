"""Temporary response backport of the exact PR2727 keyboard-dismiss delta.

The gateway reads the selected UI, never edits it. Refuse an unknown binding rather
than silently pretend the owner-added requirement is present. Remove this overlay
once the selected UI carries the upstream change.
"""
OLD = '    const disposeReadingGestures = reading.bindGestures(navigateHistoryAtEdge);'
NEW = '''    const disposeReadingGestures = reading.bindGestures(direction => {
        // Only the reader's deliberate movement toward older messages dismisses
        // a phone keyboard. Layout, incoming messages and downward following do not.
        if (direction < 0 && input.ownerDocument.activeElement === input
                && input.ownerDocument.body.classList.contains('keyboard-open')) input.blur();
        navigateHistoryAtEdge(direction);
    });'''


def patch_chat(source):
    opening = 'if (preview.show()) { updateMessagesPadding(); reading.reflow(); }'
    landed = 'if (preview.show()) { updateMessagesPadding(); reading.followAfterLayout(); }'
    if source.count(opening) == 1:
        source = source.replace(opening, landed, 1)
    elif source.count(landed) != 1:
        raise ValueError('Selected UI preview opening differs from the verified backport.')
    if source.count(OLD) == 1:
        return source.replace(OLD, NEW, 1)
    if source.count(NEW) == 1:
        return source
    raise ValueError('Selected UI keyboard binding differs from the verified PR2727 backport.')
