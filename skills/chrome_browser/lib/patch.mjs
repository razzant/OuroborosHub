// Strict unified-diff application for one file.
//
// Every hunk must apply at exactly the line numbers it states, and every context and
// removed line must match byte for byte. There is no fuzz, offset search or whitespace
// tolerance: a source that differs anywhere a hunk touches is refused. The caller also
// pins the whole-file sha256 before and after, so an unexpected byte elsewhere is refused too.

export class PatchError extends Error {
  constructor(code, message) {
    super(`${code}: ${message}`);
    this.name = 'PatchError';
    this.code = code;
  }
}

const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

// Text before the first "--- " line is a free-form preamble (provenance and licence notes).
export function parseUnifiedDiff(text) {
  const lines = text.split('\n');
  if (lines.length && lines[lines.length - 1] === '')
    lines.pop();
  let index = lines.findIndex((line) => line.startsWith('--- '));
  if (index === -1)
    throw new PatchError('PATCH_MALFORMED', 'no "--- " file header');
  const oldPath = lines[index].slice(4).trim();
  const plus = lines[index + 1] || '';
  if (!plus.startsWith('+++ '))
    throw new PatchError('PATCH_MALFORMED', 'the "--- " header is not followed by "+++ "');
  const newPath = plus.slice(4).trim();
  index += 2;
  const hunks = [];
  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith('--- '))
      throw new PatchError('PATCH_MALFORMED', 'more than one file in the patch');
    const match = HUNK_HEADER.exec(line);
    if (!match)
      throw new PatchError('PATCH_MALFORMED', `unexpected line ${index + 1} outside a hunk`);
    const hunk = {
      oldStart: Number(match[1]),
      oldCount: match[2] === undefined ? 1 : Number(match[2]),
      newStart: Number(match[3]),
      newCount: match[4] === undefined ? 1 : Number(match[4]),
      lines: [],
    };
    index += 1;
    let oldSeen = 0;
    let newSeen = 0;
    while (index < lines.length && (oldSeen < hunk.oldCount || newSeen < hunk.newCount)) {
      const body = lines[index];
      if (body.startsWith('\\'))
        throw new PatchError('PATCH_MALFORMED', `"no newline at end of file" markers are not supported (line ${index + 1})`);
      const op = body[0];
      if (op === ' ') {
        oldSeen += 1;
        newSeen += 1;
      } else if (op === '-') {
        oldSeen += 1;
      } else if (op === '+') {
        newSeen += 1;
      } else {
        throw new PatchError('PATCH_MALFORMED', `hunk line ${index + 1} has no " ", "-" or "+" prefix`);
      }
      hunk.lines.push({ op, text: body.slice(1) });
      index += 1;
    }
    if (oldSeen !== hunk.oldCount || newSeen !== hunk.newCount)
      throw new PatchError('PATCH_MALFORMED', `hunk at -${hunk.oldStart} does not have the line counts its header states`);
    hunks.push(hunk);
  }
  if (!hunks.length)
    throw new PatchError('PATCH_MALFORMED', 'the patch has no hunks');
  return { oldPath, newPath, hunks };
}

function preview(text) {
  const shown = text.length > 120 ? `${text.slice(0, 117)}...` : text;
  return JSON.stringify(shown);
}

export function applyUnifiedDiff(source, patch) {
  const parsed = typeof patch === 'string' ? parseUnifiedDiff(patch) : patch;
  if (!source.endsWith('\n'))
    throw new PatchError('PATCH_PREIMAGE_MISMATCH', 'the source does not end with a newline');
  const lines = source.slice(0, -1).split('\n');
  const out = [];
  let cursor = 0;
  parsed.hunks.forEach((hunk, number) => {
    const start = hunk.oldCount === 0 ? hunk.oldStart : hunk.oldStart - 1;
    if (start < cursor || start > lines.length)
      throw new PatchError('PATCH_MALFORMED', `hunk ${number + 1} starts at line ${hunk.oldStart}, before the end of the previous hunk or past the file`);
    for (; cursor < start; cursor += 1)
      out.push(lines[cursor]);
    const expectedNewStart = hunk.newCount === 0 ? out.length : out.length + 1;
    if (expectedNewStart !== hunk.newStart)
      throw new PatchError('PATCH_MALFORMED', `hunk ${number + 1} states new line ${hunk.newStart}, but it lands at ${expectedNewStart}`);
    for (const { op, text } of hunk.lines) {
      if (op === '+') {
        out.push(text);
        continue;
      }
      const actual = lines[cursor];
      if (actual !== text)
        throw new PatchError('PATCH_PREIMAGE_MISMATCH', `hunk ${number + 1}, line ${cursor + 1}: expected ${preview(text)}, found ${actual === undefined ? 'end of file' : preview(actual)}`);
      if (op === ' ')
        out.push(actual);
      cursor += 1;
    }
  });
  for (; cursor < lines.length; cursor += 1)
    out.push(lines[cursor]);
  return out.join('\n') + '\n';
}
