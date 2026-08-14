# Codex DeepSeek Relay README Visual System Design

## Objective

Replace the generic photographic hero and disconnected SaaS-style workflow
graphic with one restrained, recognizable visual system that explains the
relay architecture before it decorates it.

The visual system must make this sequence understandable in three seconds:

Codex parent task → protected visible handoff → local relay → eight DeepSeek
child slots across default, worker, and explorer roles.

## Design Principle

The identity is “Signal Relay Console”: precise network topology with a small
amount of terminal character. Recognition comes from the repeated one-input,
eight-output relay geometry, not from third-party logos, photorealism, heavy
effects, or decorative interface clutter.

Information hierarchy is fixed:

1. The first read is the parent-to-relay-to-children topology.
2. The second read is eight-way fan-out across three native roles.
3. The third read is local loopback, auditable handoff, verification, and
   rollback.

## Brand Tokens

All primary README visuals use the same semantic palette:

- Ink: #08111f — the single dark surface.
- Panel: #0d1a2d — cards and local-boundary surfaces.
- Text: #edf3fb — primary labels.
- Muted: #8495ad — secondary labels and unselected routes.
- Route: #4d6bfe — active task and tool-call traffic.
- Verified: #42d392 — verified states and successful child endpoints only.
- Warning: #f2b84b — credential or rollback state only; never decorative.
- Failure: #ef6a72 — failure state only; absent from the happy-path hero.

Typography uses a modern system sans for titles and a monospace stack for
technical labels. Generated assets must remain readable when GitHub scales
them to approximately 900 pixels wide.

## Visual Restraint

- No OpenAI, ChatGPT, Codex, or DeepSeek trademark logos in any asset.
- No physical cable metaphor, device mockup, 3D object, photography, glass,
  chrome, or glossy lighting.
- No rainbow gradients, particle fields, multiple glows, or fake screenshots.
- A single subtle relay-core bloom is allowed below 10 percent opacity.
- Background grid and scan texture remain below 7 percent opacity.
- Every decorative mark must either show direction, status, grouping, or
  locality.
- A maximum of one highlighted route and one verified-state color appears in
  the hero.

## Repeated Relay Mark

The project mark is a square relay gate with one incoming route on the left
and eight aligned output terminals on the right. A narrow central channel
connects the two sides. It must remain recognizable at 32 pixels and must not
resemble either vendor’s logo.

The mark appears in the hero, architecture diagram, social preview, and a
standalone relay-mark.svg.

## Asset Set

### Hero

- Files: assets/readme/hero.svg and assets/readme/hero.png
- Canvas: 1800 × 620
- Composition: title and concise value statement on the left; live topology
  on the right.
- Topology: CODEX HOST enters one RELAY CORE over a protected handoff route;
  the relay fans out to eight child terminals grouped by default, worker, and
  explorer.
- Required labels: LOCAL LOOPBACK, AUDITED HANDOFF, NATIVE FAN-OUT ×8,
  default, worker, explorer, and 127.0.0.1:42137.
- The PNG is a raster export of the SVG and replaces the existing photograph.

### Architecture

- File: assets/readme/architecture.svg
- Canvas: 1600 × 780
- Shows four explicit stages: Codex parent, visible handoff, local relay, and
  DeepSeek children.
- The local-only boundary surrounds the handoff resolver and protocol bridge.
- A lower return route explains tool calls, tool results, and safe progress
  summaries without exposing private chain of thought.
- The eight child terminals are visible and countable.

### Verification Workflow

- File: assets/readme/workflow.svg
- Canvas: 1600 × 460
- Shows credential, model probe, transactional install, native fan-out, and
  verify or rollback.
- The happy path is route blue ending in verified green.
- Rollback is a small conditional warning branch, not a competing main path.

### Social Preview

- Files: assets/readme/social-preview.svg and
  assets/readme/social-preview.png
- Canvas: 1280 × 640
- Uses the relay mark, project name, one-line value statement, and a simplified
  one-to-eight topology.
- It remains legible when rendered as a small repository card.

### Standalone Mark

- File: assets/readme/relay-mark.svg
- Canvas: 512 × 512
- Contains only project-owned geometry and palette tokens.

## README Placement

The hero remains first. The architecture diagram follows the initial value
proposition and “能得到什么” section so readers see the mechanism before
installation instructions. The verification workflow stays immediately
before quick start, where it explains why setup is stricter than a normal
configuration script.

The README keeps a real HTML h1 and text description outside the image for
searchability and accessibility.

## Build and Validation

scripts/readme_visuals.py owns palette values, SVG generation, dimensions,
and structural labels. scripts/build_readme_assets.py writes all SVG assets
and validates the checked-in PNG exports.

Automated checks must verify:

- all generated SVG files parse as XML;
- dimensions and view boxes match this specification;
- the hero and architecture contain exactly eight child terminals;
- required role, loopback, handoff, verification, and rollback labels exist;
- forbidden vendor-logo image embeddings do not exist;
- the hero and social PNGs have the exact expected dimensions;
- every local README image reference resolves to a checked-in file.

Visual QA renders every SVG to PNG and inspects the full-size image plus a
900-pixel-wide thumbnail. Labels may not overlap, crop, or become illegible.

## Acceptance Criteria

- The old photographic hero is fully replaced.
- All README visuals look like one system.
- A new reader can explain the relay path after viewing the hero alone.
- The eight-way fan-out is visible rather than merely stated.
- Visual texture never competes with the route.
- No third-party logo is required for recognition.
- Generation and validation commands complete without warnings.
- The full repository test suite remains green on Windows and macOS.
