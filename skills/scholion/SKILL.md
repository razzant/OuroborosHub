---
name: scholion
description: Local second-opinion layer over the owner's own medical data — genome (VCF), laboratory history, prescriptions, wearables. 40 tools; answers carry provenance and say what the data cannot support. Not a medical device.
version: 0.7.1
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
os: any
permissions: [tool, fs, net, subprocess, widget, route, companion_process, read_settings]
env_from_settings: [SCHOLION_OFFLINE]
companion_processes:
  - name: web
    command: [python3, web_companion.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 3
ui_tab:
  tab_id: start
  title: Scholion
  render:
    kind: module
    entry: widget.js
    start: auto
    appearance: host
    span: 2
    max_height: 1600
install_specs:
  - kind: pip
    package: scholion>=0.7.1
when_to_use: The owner asks about their own medical data — check a new prescription as a second opinion (pharmacogenetics, interactions with the current regimen, monitoring labs), review laboratory results and trends, look up a locus or clinically significant ClinVar findings in their VCF, polygenic scores, longevity findings, sleep and lifestyle metrics, movement toward a health goal, which tests to take next, or a pre-visit summary.
timeout_sec: 120
---

# Scholion — a second opinion over your own medical data, locally

Scholion reads one person's genome (a full VCF), years of laboratory forms,
prescriptions and wearable exports **against each other** and shows where every
statement came from. It states plainly what the data **cannot** support — a
negative answer is qualified by coverage, not implied by silence.

**Not a medical device.** It does not diagnose, does not start or stop
therapy, does not adjust doses. Everything it produces is material for the
owner's own study and for a conversation with a physician.

## What the agent gets

40 tools over one local engine, among them:

- `check_prescription` — a new drug as a second opinion: pharmacogenetics,
  interactions with the current regimen, monitoring labs, open questions for
  the physician; its gene list comes through the body system the drug acts on
- `check_drug_gene` / `analyze_labs` / `suggest_tests` — the classic trio
- `system` — one body system as one card: laboratory now and its movement,
  the genetic half and how much of it was read, prescriptions acting on it,
  the clinician's target, what to test, what to ask; `screen` — a class of
  disease gene by gene, never «clear» where a gene was not read
- `genome_lookup`, `clinvar_findings`, `acmg`, `prs`, `longevity`,
  `lipid_genetics`, `array` — the genome layer, always with read-vs-assumed
  status
- `overview`, `second_opinion`, `limits`, `radar`, `brief`, `focus`,
  `phenoage`, `lifestyle`, `health_metrics`, `goal`, `goal_suggest`,
  `provenance`, `flag_rate`, `sources` — the living picture and its honest
  boundaries
- `genome_status` reports input readiness; `genome_status(updates=true)` explains
  whether changed catalogue sites need explicit genotyping. `selfcheck` checks
  profile integrity, `medications` shows the recorded regimen, and `capabilities`
  describes the available doors and their requirements.
- `overview(visit_sheet=true)` prepares a focused visit brief; `period` selects
  a supported observation window. `insight_snapshot` previews a review and saves
  it only with the confirmation token returned by that preview, after the person
  explicitly asks to save it. A later preview can compare it with current data.
- Five tools can record data, and each preserves a source or explicit user action rather
  than anything a model concluded: `ingest_labs` transcribes the owner's own
  laboratory PDFs from a folder they named (never the current directory);
  `focus_log` records what the person said happened on a day; `lab_draw`
  records why one day holds two draws; `marker_propose` files a marker name as
  a proposal a person still has to confirm; `insight_snapshot` saves the explicitly
  confirmed review described above. Preview does not save a review.
  `recompute` does not start a rebuild on
  this host: the Hub runs isolated calls in a fresh process that ends with the call,
  so a background rebuild would die unfinished. It says so and names the command
  the person runs in their own terminal (`scholion recompute --yes`). Its
  `stop=true` operation requests cancellation of existing work and requires the
  person's instruction. No tool changes therapy, invents a measurement or sets
  the person's demographic fields.

- `version` / `update` — the installed build and whether PyPI has a newer one
  (asked at most once a day). On this host the host installs the package, so
  `update` installs nothing: it says that the host's catalogue brings the newer
  build, rather than running pip in the host's interpreter. While a newer build
  is out, one answer a day ends with a one-line note about it.
- The package starts local programs when they are installed — `bcftools` for
  the genome, `pdftotext` / `pdftoppm` / `tesseract` for PDF forms — hence the
  `subprocess` permission.

- `rules` — the safety canon this product is operated under, in full. A model
  reaching Scholion through the tool interface is handed a list of tools and no
  instruction with it; this is where the instruction comes from, and among
  Scholion's own documents it comes first. Call it before
  relaying anything from the other tools.

`limits` deserves a special mention: it answers "what can this data NOT say,
and what would close the gap" — call it before making any negative claim.
Missing trend data alone do not justify ordering more tests. A trend or a
change during treatment does not establish causation. A polygenic percentile
is a population position, not a disease probability; saved PGS values without
verified calibration remain explicitly unverified.

Every person-related answer identifies its technical container. Keep that ID
with the answer; if it changes during the conversation, stop and resolve whose
data is being discussed. Switching containers, exporting and erasing them are
human operations. Load `rules` before interpretation and preserve its current
qualifications, including genetic evidence levels and patient-reported versus
measured changes.

## Reaching it another way

The same engine is available through the command line (`scholion <command>`),
the local web app, this Hub skill, the classic Ouroboros tools module
(`import scholion.ouroboros_tools`), the Model Context Protocol server
(`scholion mcp`), and the packaged skills/plugins described by `capabilities`.
A host that has this skill does not also need the MCP server.

**There is no key, token, account or credential for any of them**, and nothing
to authenticate against — the analysis runs on the machine that holds the data.
If a host asks for a Scholion credential, it is a host that assumes every tool
server is remote; leave the fields empty. The build answers this itself:
`scholion capabilities --json` carries an `access` block with every door and a
list, derived from its own source, of the environment variables it reads — none
of which is a secret. The full instructions are in
`scholion doc connecting-an-agent`.

## Setup

Open **Widgets → Scholion**. Choose **Create my profile**, or expand
**I already use Scholion** and choose the existing data/profile folder. The
connection is previewed before you confirm it; originals are not copied. This
installation then keeps that connection across restarts. It does not silently
switch an already connected profile to another person's folder.

**Open Scholion** shows the installed product's own web interface inside the
widget: radar, laboratory history, body systems, genome/PGS, longevity and
lifestyle. **Add laboratory results** opens the laboratory screen, with the
native folder choice and a manual-path fallback, then explicit import. Genetic
and wearable inputs use the product's supported connection/import operations;
copying a file alone does not analyse it. Missing genome data does not block
laboratory or prescription work. Source paths and the technical container ID
are available under **Settings and data folders**, rather than filling the
welcome screen. English is the default; the start screen and product support
Russian too.

The host supervises `web_companion.py`. It waits for the person's selection,
then binds a dynamically assigned **loopback** port. The module widget uses the
host's authenticated fetch bridge, not direct browser requests to localhost.
The private loopback credential is created by the software; no account or key
is requested from the person. Both paths load the same installed Scholion and
saved profile connection. An old widget refuses requests after that connection
changes; it never falls back to a different server on port 1521. Disabling the
plugin stops the companion; reopening the UI does not create another profile.

The visual interface is a human surface. Its explicit confirmations and
background operations do not add write permissions to model tools. The
agent's `recompute` start remains unavailable as documented above. Source links and file downloads use the host's own external-link and download
bridges. No model is required for local analysis, although the
host's own first-run model setup may still cover its main interface.

`SCHOLION_OFFLINE=1` disables Scholion lookups, not the host's model requests or
dependency installation. Returned tool results follow the host conversation's
model/provider settings. The plugin needs Scholion 0.7.1 or newer; the host
installs it in the skill's isolated dependency environment.

## Safety rules

The package carries its own assistant rules (`scholion skill --rules`); among
Scholion's own documents they come first, this file included.
