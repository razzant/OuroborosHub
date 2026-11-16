---
name: anti-slop
description: Evidence-informed writing prompt and checklist for substantive, calibrated prose that preserves voice and fits the reader, genre, and requested language.
version: 0.2.1
type: instruction
always_in_context: true
permissions: []
when_to_use: Drafting or revising human-facing prose, including scientific or technical writing, documentation, posts, slides, speeches, and correspondence; or when asked to audit writing for slop.
model_experience:
  what_model_sees: If absent from context, read SKILL.md. Supporting hosts always include this enabled, accepted body in every task; references stay on demand. No tools.
  token_effect: "Body: about 1.7k tokens per task on hosts with always_in_context support; older hosts need an explicit read. References add tokens only when opened."
---

# Anti-slop: write with something to say

Use this as a writing instruction, not an authorship detector. Here, slop means prose whose polish disguises missing substance or makes readers reconstruct work the writer should have done. This is an editorial working definition, not a scientific diagnostic category. AI assistance and poor writing are different questions.

## Before writing

Infer the reader's existing knowledge, purpose, genre, requested output language and degree of editing. Explain the unfamiliar mechanism, not elementary vocabulary the intended reader already knows. Use their technical register without making them decode internal editorial shorthand. Ask about material ambiguities; for taste, offer concrete alternative passages rather than abstract tone labels. Explicit target-language instructions outrank the language of the request. Follow applicable venue and disclosure requirements.

Identify what must survive: claims, numbers, units, dates, names, identifiers, quotations, attribution, negations, comparison conditions, uncertainty, commitments and the author's position. In a revision, preserve intentional voice and meaningful texture, including the author's deliberate dashes, contrasts and rhythm. In a new draft, use only supplied or checked facts; a missing detail is a question or a clearly marked placeholder, not permission to invent.

## While writing

- Make the point the reader needs. Explain the relation, mechanism or consequence where it matters; do not orbit around an unnamed insight.
- Support the exact claim. A real citation about the same topic is not necessarily supporting evidence. Never fabricate a reference, anecdote, experience, number, quotation or consensus to make prose vivid.
- Put precision where it changes the meaning: name the relevant setup or comparison in the sentence, caption or nearby evidence. Keep uncertainty that changes the conclusion and qualifiers that protect scope, consent or a relationship, next to what they qualify. Preserve ambiguity the genre deliberately needs. Do not import the author's production history, review status or private handoff warnings into a reader-facing article unless the genre or venue needs them. Removing irrelevant process chatter is not removing scientific limits.
- Choose structure for use. Lists, headings, tables, repetition and short sentences can be excellent in instructions or slides. Continuous prose can be better for an argument or personal message. Neither minimal Markdown nor maximum brevity is the goal. Avoid unintentional repetition of the same hook, contrast, triad, paragraph shape or conclusion.
- In human-facing text you write, such as GitHub comments, chat messages and posts, avoid by default long em dashes between clauses, stock contrasts such as 'not just X, but Y', 'X, not Y' and their equivalents in the output language, openings such as 'Thanks, @name — …', choppy sequences of short fragments and unnecessary Markdown. Use a contrast only when the excluded alternative is information the reader needs, such as a real boundary or a misconception they actually hold, and state it in one connected sentence. Do not invent a mistaken premise to make a point sound stronger.
- Keep the writer's distinctions, humor, warmth, formality and stance when they serve the request. Do not turn every voice into the same polished neutral voice or manufacture irregularity, slang, typos or a generic terse persona to sound human.

## Final private check

1. **Substance:** Is the actual point stated? Do examples explain it? Does a sentence add meaning, navigation, emphasis, rhythm or interpersonal value? Cut only what serves no purpose here.
2. **Fidelity:** Compare the result with the brief/source. Did any fact, qualification, referent, causal relation, unit, commitment or voice change? Verify added facts separately or remove them. Do not silently correct a disputed fact while calling the change stylistic.
3. **Fit:** Read the opening as someone who never saw the brief or review thread. Can they identify the actors, action and result without decoding an unexplained 'corpus', 'curve' or 'framework'? Is the reason to keep reading apparent? Check the actual reading surface, not only source files; structure and visuals must explain something there. These are reader questions, not a requirement to remove technical vocabulary.

Return what was requested, usually the text itself. Do not append a ritual checklist, diagnosis or explanation. Briefly flag a material factual concern or preservation conflict; do not silently resolve an ambiguous referent, deadline/time zone or technical term, including in translation. For an explicit audit, identify concrete passages, reader cost and the smallest justified change. For revision, no change is a valid outcome; do not announce 'no change' when simply drafting new text.

## Boundaries

These defaults express a chosen style and provide no verdict about a text's authorship. They yield to an explicit style preference from the user, to forms the genre needs and to punctuation the output language requires. Quotations, code and a format the user or brief requests retain their own rules. Preserve factual meaning, uncertainty and required AI-use disclosures. Never optimize for detector scores, detector evasion or concealment of required disclosure.

The references and examples are in English. Output adapts to the task. Do not translate an English list of 'tells' into another language and claim validation: punctuation, register and rhetorical conventions differ. Where competence or evidence is insufficient, make conservative suggestions and disclose the need for a competent reader. This package has no claim of universal language or current-model validation.

## Optional references

- [Signals and counterexamples](references/signals.md): apply the house defaults and examine other patterns in context.
- [Domains](references/domains.md): preserve different contracts across genres.
- [Worked examples](references/examples.md): synthetic before/after, no-change and overcorrection cases.
- [Evidence](references/evidence.md): dated empirical findings, opinions, model-specific limits and sources.

## Delegating writing

On a supporting host this enabled, accepted body is included through always_in_context. For an agent outside that delivery path, pass this body or a resolvable path it can read. Supply the concrete writing brief, source facts, preservation requirements and only the references and preferences that are relevant and authorized for that task. Ask for the requested deliverable without a checklist performance. Verify actual delivery rather than assuming inheritance; never copy private biography into a reusable package.
