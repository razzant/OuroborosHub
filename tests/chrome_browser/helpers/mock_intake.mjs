// A stand-in for the host's file intake (action "file_intake" of ouroboros/mcp_browser_guard.py's callback):
// each named file the test admits is copied into a retention folder, and the answer names that copy, its
// original basename, its size and its sha256. The admission rule is the test's (the host's is read_file's);
// `roots` maps a root name to a folder for entries that name one, joined with the bucket and skill_name an entry
// names ("skill_payload/user_repo/report-skill"); an empty one counts as not named. Every message it answered is kept.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const sha256 = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');

export function retainingIntake({ dir, admit = () => '', roots = {} }) {
  let next = 0;
  const asked = [];
  const intake = (message) => {
    asked.push(message);
    for (const entry of message.files) {
      const block = admit(entry);
      if (block)
        return { block };
    }
    const files = message.files.map((entry) => {
      const root = [entry.root, entry.bucket, entry.skill_name].filter(Boolean).join('/');
      const source = entry.root ? path.join(roots[root], entry.path) : entry.path;
      const bytes = fs.readFileSync(source);
      const copy = path.join(dir, `retained-${next++}`);
      fs.writeFileSync(copy, bytes);
      return { path: copy, name: path.basename(source), size: bytes.length, sha256: sha256(bytes) };
    });
    return { block: '', files };
  };
  intake.asked = asked;
  return intake;
}
