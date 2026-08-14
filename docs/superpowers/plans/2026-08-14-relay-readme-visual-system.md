# Relay README Visual System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Replace the current generic README imagery with a coherent,
project-owned Signal Relay Console visual system that explains protected local
handoff and native eight-way DeepSeek fan-out.

**Architecture:** A dependency-free Python module emits deterministic SVG
assets from one palette and geometry contract. Checked-in PNG exports provide
GitHub-compatible hero and social-preview files, while tests validate generated
structure, dimensions, image references, and the absence of embedded vendor
logos.

**Tech Stack:** Python 3.11 standard library, SVG 1.1, unittest, bundled Sharp
for local raster export and visual QA, GitHub-flavored Markdown and HTML.

## Global Constraints

- Hero canvas is exactly 1800 × 620.
- Architecture canvas is exactly 1600 × 780.
- Workflow canvas is exactly 1600 × 460.
- Social preview canvas is exactly 1280 × 640.
- Standalone relay mark canvas is exactly 512 × 512.
- Core colors are #08111f, #0d1a2d, #edf3fb, #8495ad, #4d6bfe, #42d392,
  #f2b84b, and #ef6a72 with the semantic roles defined in the design spec.
- Hero and architecture each show exactly eight countable child terminals.
- No SVG contains an image element, data URI, vendor logo, photography, 3D
  object, or decorative effect without an information role.
- The hero’s active route is #4d6bfe and verified child state is #42d392.
- Background texture opacity is at most 0.07 and the single relay glow opacity
  is at most 0.10.
- README keeps a real h1 and descriptive text outside the hero.
- Runtime installation remains dependency-free; visual build dependencies are
  development-only and are not imported by the Skill manager.

---

### Task 1: Deterministic visual asset generator and contract tests

**Files:**

- Create: scripts/readme_visuals.py
- Create: scripts/test_readme_visuals.py
- Modify: scripts/build_readme_assets.py
- Generate: assets/readme/hero.svg
- Generate: assets/readme/architecture.svg
- Generate: assets/readme/workflow.svg
- Generate: assets/readme/social-preview.svg
- Generate: assets/readme/relay-mark.svg

**Interfaces:**

- Produces: hero_svg() -> str
- Produces: architecture_svg() -> str
- Produces: workflow_svg() -> str
- Produces: social_preview_svg() -> str
- Produces: relay_mark_svg() -> str
- Produces: write_svg_assets(output_dir: pathlib.Path) -> dict[str, pathlib.Path]
- Produces: read_png_size(path: pathlib.Path) -> tuple[int, int]
- Produces: validate_png(path: pathlib.Path, expected: tuple[int, int]) -> None
- Consumes: no external package and no network input.

- [ ] **Step 1: Write the failing generator tests**

  Add scripts/test_readme_visuals.py with tests that:

  - call write_svg_assets() in tempfile.TemporaryDirectory;
  - parse each output with xml.etree.ElementTree;
  - assert the exact width, height, and viewBox literals from Global
    Constraints;
  - count data-node="child" elements in hero.svg and architecture.svg and
    require exactly eight;
  - assert the role labels default, worker, and explorer and the labels
    AUDITED HANDOFF, LOCAL LOOPBACK, 127.0.0.1:42137, VERIFY, and ROLLBACK;
  - reject image elements and data:image content in every generated SVG;
  - create a hand-built 24-byte PNG header and prove read_png_size returns its
    literal dimensions;
  - prove validate_png raises ValueError for a wrong PNG signature and a wrong
    dimension.

  The structural helper in the test must inspect real XML output:

      def elements_with(root, key, value):
          return [
              node for node in root.iter()
              if node.attrib.get(key) == value
          ]

- [ ] **Step 2: Run the focused test and verify RED**

  Run:

      python -m unittest scripts.test_readme_visuals -v

  Expected: FAIL because scripts/readme_visuals.py does not exist.

- [ ] **Step 3: Implement the minimal generator**

  Add scripts/readme_visuals.py using only pathlib, struct, textwrap, and
  xml.sax.saxutils.escape. Define one immutable PALETTE mapping and one
  ASSET_SPECS mapping. Build all five SVG strings from reusable functions for:

  - root SVG and accessibility title/description;
  - background grid;
  - label chips;
  - route lines and arrowheads;
  - the one-input/eight-output relay gate;
  - child terminal groups.

  Each child terminal must carry data-node="child" and data-role set to
  default, worker, or explorer. All text must be real SVG text, never outlined
  or rasterized.

  Refactor scripts/build_readme_assets.py so main() calls
  write_svg_assets(ASSETS), validates hero.png at (1800, 620), and validates
  social-preview.png at (1280, 640). Keep PNG parsing dependency-free.

- [ ] **Step 4: Generate the SVG assets**

  Run:

      python -c "from pathlib import Path; from scripts.readme_visuals import write_svg_assets; write_svg_assets(Path('assets/readme'))"

  Expected: five SVG paths are written and XML parsing succeeds.

- [ ] **Step 5: Run the focused test and verify GREEN**

  Run:

      python -m unittest scripts.test_readme_visuals -v

  Expected: all visual generator tests pass.

- [ ] **Step 6: Commit the asset generator unit**

  Stage only the files owned by Task 1 and commit:

      git add scripts/readme_visuals.py scripts/test_readme_visuals.py scripts/build_readme_assets.py assets/readme/hero.svg assets/readme/architecture.svg assets/readme/workflow.svg assets/readme/social-preview.svg assets/readme/relay-mark.svg
      git commit -m "feat: build relay README visual system"

---

### Task 2: README narrative and image-link integration

**Files:**

- Modify: README.md
- Modify: GITHUB_UPLOAD.md
- Modify: scripts/test_skill_contract.py

**Interfaces:**

- Consumes: assets/readme/hero.png, assets/readme/architecture.svg, and
  assets/readme/workflow.svg.
- Produces: a README image sequence whose local paths all resolve.

- [ ] **Step 1: Write a failing broken-image regression test**

  In scripts/test_skill_contract.py, add a test that extracts every local
  src="./..." value from README.md, resolves it against ROOT, and asserts each
  path is a regular file. Add literal assertions that architecture.svg and
  workflow.svg are both present in that resolved set.

  This test catches a renamed or missing checked-in README image, rather than
  merely checking prose.

- [ ] **Step 2: Run the focused test and verify RED**

  Run:

      python -m unittest scripts.test_skill_contract.SkillContractTests.test_readme_local_images_resolve -v

  Expected: FAIL because README.md does not yet reference architecture.svg.

- [ ] **Step 3: Integrate the visual explanation**

  Keep the top hero.png reference and real centered h1. Tighten the opening
  copy to one sentence of purpose and one sentence of implementation.

  Insert architecture.svg after the “能得到什么” bullets with alt text that
  describes the Codex parent, local relay, and eight DeepSeek children.

  Keep workflow.svg immediately before “快速开始” and update its alt text to
  mention validation and rollback.

  Do not add badges, emoji headings, comparison tables, vendor logos, or a
  visual gallery.

  Update GITHUB_UPLOAD.md so upload verification checks hero.png,
  architecture.svg, workflow.svg, relay-mark.svg, and social-preview.png. Add
  one direct instruction to upload social-preview.png in GitHub repository
  settings after the first push.

- [ ] **Step 4: Run the focused test and verify GREEN**

  Run:

      python -m unittest scripts.test_skill_contract.SkillContractTests.test_readme_local_images_resolve -v

  Expected: PASS.

- [ ] **Step 5: Commit the documentation integration**

  Stage only Task 2 files and commit:

      git add README.md GITHUB_UPLOAD.md scripts/test_skill_contract.py
      git commit -m "docs: explain relay architecture visually"

---

### Task 3: Raster exports and visual QA

**Files:**

- Replace: assets/readme/hero.png
- Create: assets/readme/social-preview.png
- Inspect: assets/readme/hero.svg
- Inspect: assets/readme/architecture.svg
- Inspect: assets/readme/workflow.svg
- Inspect: assets/readme/social-preview.svg
- Inspect: assets/readme/relay-mark.svg

**Interfaces:**

- Consumes: deterministic SVGs from Task 1.
- Produces: exact-dimension PNG exports and visually approved assets.

- [ ] **Step 1: Render exact PNG exports**

  Use the bundled Sharp package to render hero.svg at 1800 × 620 and
  social-preview.svg at 1280 × 640. Render architecture.svg, workflow.svg,
  relay-mark.svg, and 900-pixel-wide thumbnails into a temporary QA directory;
  do not commit QA renders.

- [ ] **Step 2: Validate the PNG contract**

  Run:

      python scripts/build_readme_assets.py

  Expected: exit 0 after regenerating the SVGs and validating both PNG sizes.

- [ ] **Step 3: Inspect full-size and thumbnail renders**

  Check each render for:

  - a readable three-second parent → relay → eight children story;
  - exactly eight visible child terminals;
  - no label collision, crop, unintended line crossing, or tiny unreadable
    status copy;
  - one visual surface and consistent relay mark;
  - restrained grid and glow;
  - no accidental resemblance to vendor logos.

  If an issue exists, first add or tighten a structural test when possible,
  verify the test fails, patch the generator, regenerate all outputs, and
  inspect again.

- [ ] **Step 4: Commit raster exports and QA fixes**

  Stage the two PNGs plus any generator/SVG/test adjustments and commit:

      git add assets/readme/hero.png assets/readme/social-preview.png assets/readme/*.svg scripts/readme_visuals.py scripts/test_readme_visuals.py
      git commit -m "feat: ship relay visual assets"

---

### Task 4: Full verification and repository handoff

**Files:**

- Verify: all changed files

**Interfaces:**

- Consumes: Tasks 1–3.
- Produces: a clean, reviewable branch ready to fast-forward into main.

- [ ] **Step 1: Run the complete test suite**

  Run:

      python -m unittest discover -s scripts -p "test_*.py" -v

  Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Run syntax and runtime contract checks**

  Run:

      python -m compileall -q codex-deepseek-subagent/scripts scripts
      python scripts/check_runtime_contract.py

  Expected: both commands exit 0 without warnings from project code.

- [ ] **Step 3: Check generated asset stability**

  Record SHA-256 hashes of the five generated SVG files, run
  python scripts/build_readme_assets.py again, and confirm the hashes remain
  unchanged. Confirm hero.png is 1800 × 620 and social-preview.png is
  1280 × 640.

- [ ] **Step 4: Audit the branch**

  Run:

      git diff --check main...HEAD
      git status --short
      git log --oneline main..HEAD

  Expected: no whitespace errors, only intended files are present, and every
  implementation unit has a focused commit.

- [ ] **Step 5: Request final code and visual-contract review**

  Give the reviewer the design spec, this plan, the full main...HEAD diff, and
  the visual QA evidence. Resolve every Critical or Important finding and
  re-run its covering checks.

- [ ] **Step 6: Fast-forward main after review**

  From the original checkout, fast-forward main to the reviewed feature branch
  without rewriting history:

      git merge --ff-only feat/relay-readme-visuals

  Re-run the full test suite from F:\Github\codex-deepseek-relay after the
  fast-forward.
