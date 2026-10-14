# Attribution

**Idea and original proposal:** [@Glassscale](https://github.com/Glassscale), in
[razzant/ouroboros#1221](https://github.com/razzant/ouroboros/issues/1221) — a
check-in that wakes Ouroboros after a period of silence, lets the agent itself
judge the situation and write to a trusted contact, and keeps the human in
charge of turning it on. Their design notes also shaped this version: the
anti-fabrication rule lives inside the wake task text itself, the contact stage
cannot be armed without a configured recipient, and the skill leaves restart and
background-consciousness policy to the host.

**Source of record and license.** Glassscale confirmed in
[comment 6001989746](https://github.com/razzant/ouroboros/issues/1221#issuecomment-6001989746)
(2026-10-05 20:01:40 UTC) that the module and smoke test attached to the issue
are MIT-licensed, as posted there, and that no original commit exists: the issue
post is the source of record.

**This payload** is a new implementation written for OuroborosHub from the
requirements in that issue and the owner's design decisions (explicit check-in
agreements; owner first, then at most one message to one consenting contact;
the live agent decides and writes; one attempt per missed streak). The design
work behind it read the issue and the module attached to it, so this is **not**
a clean-room implementation. It does not copy code from the attached module.

**Git co-author credit:** the identity Glassscale asked for is

```
Co-authored-by: Glassscale <glassscale@agentmail.to>
```

The substantive commit that publishes this skill must carry exactly that
trailer and reference https://github.com/razzant/ouroboros/issues/1221. This
file states the agreed credit; it is not itself proof that such a commit exists.
