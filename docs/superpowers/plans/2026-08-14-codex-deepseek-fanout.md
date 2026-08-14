# Codex DeepSeek Fan-out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有单一 DeepSeek 子代理管理器改造成 Codex 原生多子代理方案：主任务继续使用用户当前的 OpenAI 模型，`default`、`worker`、`explorer` 子代理统一使用经在线验证的 `deepseek-v4-pro`，默认允许 8 路并发，并提供安全安装、验收、停用、恢复和卸载能力。

**Architecture:** 保留 `codex_deepseek.py` 作为稳定命令入口，把配置编辑、角色生成、凭据、兼容性验证、事务和业务编排拆成标准库模块。安装分为“构造候选状态 → 隔离 Codex Home 验证 → 原子写入真实 Codex Home → 原生单代理和 fan-out 验收”四段；任何一段失败都不留下半套配置。模型名称和最高思考强度由真实 API 与 Codex 运行时共同确认，未确认时绝不写入正式配置。

**Tech Stack:** Python 3.11+ 标准库、`unittest`、TOML (`tomllib`)、JSON、SQLite、Windows Credential Manager / macOS Keychain、Codex Responses provider 与原生 subagent 配置。

**Protocol addendum:** 当前 Codex 自定义 Provider 只接受 Responses，而 DeepSeek V4 官方接口使用 Chat Completions。因此最终实现包含只绑定 `127.0.0.1:42137` 的 Responses→Chat 适配层，并以本机 Codex 二进制完成文本、命名空间工具、工具结果回传与 reasoning 续接的端到端测试；这不改变 V2 多代理，也不修改主模型目录。

## Global Constraints

- 不修改顶层 `model`、`model_provider` 或 `model_reasoning_effort`；当前主模型保持 `gpt-5.6-sol` 与 `max`。
- 不写 `multi_agent_version = "v1"`，不关闭 `features.multi_agent_v2`，不创建或选中正式 `model_catalog_json`。
- 只在隔离的临时 Codex Home 中允许创建一次性模型目录；正式配置若依赖目录注入则安装失败并完整回滚。
- API Key 只进入操作系统凭据库；不得进入 TOML、JSON、Markdown、命令参数、日志、异常文本、Git 或临时文件。
- `default`、`worker`、`explorer` 三个内置角色必须全部指向同一 DeepSeek provider、已验证模型和已验证的最高思考强度。
- 默认 `agents.max_concurrent_threads_per_session = 8`，保留用户显式设置更高值；低于 8 时由受管配置提升到 8，卸载时恢复原值。
- 只有两个及以上互相独立、边界清楚的工作项才自动 fan-out；共享可变状态、重叠文件写入或强顺序任务不得并发。
- 每项行为变更先写失败测试，再写最小实现；每个任务结束后运行相关测试并提交。

---

### Task 1: 建立模块边界并冻结现有命令入口

**Files:**
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/__init__.py`
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/errors.py`
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/paths.py`
- Modify: `codex-deepseek-subagent/scripts/codex_deepseek.py`
- Modify: `scripts/test_manager.py`

**Interfaces:**

```python
class ManagerError(RuntimeError):
    code: str
    details: dict[str, object]

@dataclass(frozen=True)
class Paths:
    home: Path
    config: Path
    agents_dir: Path
    instruction_file: Path
    state_dir: Path
    manifest: Path

def resolve_paths(codex_home: str | None) -> Paths: ...
```

- [ ] Add a failing test that imports `ManagerError`, `Paths`, and `resolve_paths` from `deepseek_fanout`, and verifies `resolve_paths()` points to `config.toml`, `agents/`, `AGENTS.md`, and `codex-deepseek-subagent/manifest.json` without a live catalog path.
- [ ] Run `python -m unittest scripts.test_manager.ManagerTests.test_public_package_paths -v`; expect import failure.
- [ ] Create the package, move only the error/path types into it, and re-export them from `deepseek_fanout.__init__`.
- [ ] Convert `codex_deepseek.py` into a compatibility entrypoint that imports these types while leaving the existing CLI behavior intact.
- [ ] Run `python scripts/test_manager.py`; expect the new import test and all unchanged baseline tests to pass.
- [ ] Run `python -m py_compile codex-deepseek-subagent/scripts/codex_deepseek.py codex-deepseek-subagent/scripts/deepseek_fanout/*.py`.
- [ ] Commit: `refactor: establish DeepSeek fan-out package boundaries`.

### Task 2: 实现不影响主模型的受管 Codex 配置

**Files:**
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/toml_config.py`
- Create: `scripts/test_toml_config.py`
- Modify: `scripts/test_manager.py`

**Interfaces:**

```python
PROVIDER_BEGIN = "# BEGIN CODEX-DEEPSEEK-FANOUT PROVIDER"
PROVIDER_END = "# END CODEX-DEEPSEEK-FANOUT PROVIDER"

def build_provider_block(auth_command: list[str]) -> str: ...
def apply_codex_config(original: str, auth_command: list[str], concurrency: int = 8) -> str: ...
def remove_codex_config(managed: str, original_values: dict[str, object]) -> str: ...
def validate_parent_unchanged(before: str, after: str) -> None: ...
```

- [ ] Add failing tests for empty config, quoted TOML table names, CRLF input, repeated setup, and a config containing `model = "gpt-5.6-sol"`, `model_reasoning_effort = "max"`.
- [ ] Assert candidate output contains a user-level `[model_providers.deepseek]` Responses provider, provider auth command, `[features] multi_agent = true`, `[agents] enabled = true`, and `max_concurrent_threads_per_session = 8`.
- [ ] Assert candidate output contains neither `multi_agent_version`, `multi_agent_v2 = false`, `model_catalog_json`, nor a changed parent model/provider/effort.
- [ ] Run `python -m unittest scripts.test_toml_config -v`; expect failures for missing module and functions.
- [ ] Implement marked-block replacement and TOML table/key updates with parse-before-return validation.
- [ ] Record only overwritten managed fields in a serializable `original_values` structure so uninstall restores exact prior values.
- [ ] Run `python -m unittest scripts.test_toml_config -v` and `python scripts/test_manager.py`.
- [ ] Commit: `feat: preserve parent while enabling native multi-agent concurrency`.

### Task 3: 生成三个内置角色并解析最高可用思考强度

**Files:**
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/roles.py`
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/model_capabilities.py`
- Create: `scripts/test_roles.py`

**Interfaces:**

```python
ROLE_NAMES = ("default", "worker", "explorer")
EFFORT_PREFERENCE = ("max", "xhigh", "high", "medium", "low", "minimal")

@dataclass(frozen=True)
class ModelSelection:
    requested_model: str
    resolved_model: str
    reasoning_effort: str | None
    effort_source: str

def resolve_effort(codex_values: set[str], provider_values: set[str]) -> tuple[str | None, str]: ...
def render_agent(role: str, selection: ModelSelection) -> str: ...
def expected_agent_files(agents_dir: Path, selection: ModelSelection) -> dict[Path, bytes]: ...
```

- [ ] Add failing table-driven tests proving the DeepSeek preference order is `max`, `xhigh`, `high`, `medium`, `low`, `minimal`, and proving no common value returns `None` with `provider_default`.
- [ ] Add failing tests proving all three role files use `model_provider = "deepseek"`, the resolved model, the same compatible effort, role-specific descriptions, and text-only developer instructions.
- [ ] Add a test proving a missing compatible Codex reasoning key omits `model_reasoning_effort` instead of inventing a value.
- [ ] Run `python -m unittest scripts.test_roles -v`; expect missing-module failures.
- [ ] Implement immutable model selection and deterministic UTF-8 TOML rendering.
- [ ] Parse every rendered role TOML before returning it and reject role names outside the exact built-in set.
- [ ] Run `python -m unittest scripts.test_roles -v`.
- [ ] Commit: `feat: map built-in child roles to verified DeepSeek capabilities`.

### Task 4: 安装可重复、可移除的 fan-out 指令块

**Files:**
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/instructions.py`
- Create: `scripts/test_instructions.py`

**Interfaces:**

```python
INSTRUCTIONS_BEGIN = "<!-- BEGIN CODEX-DEEPSEEK-FANOUT -->"
INSTRUCTIONS_END = "<!-- END CODEX-DEEPSEEK-FANOUT -->"

def render_fanout_block(max_children: int = 8) -> str: ...
def apply_fanout_instructions(original: str, max_children: int = 8) -> str: ...
def remove_fanout_instructions(text: str) -> str: ...
```

- [ ] Add failing tests that apply the block twice and receive byte-identical output, preserve unrelated `AGENTS.md` content, and remove only the managed block.
- [ ] Assert the text requires at least two independent bounded work items, one child per work item, maximum 8 children, `explorer` for read-heavy research, `worker` for isolated writes, no overlapping file writes, wait-for-all, parent verification, and parent-only execution for trivial/sequential work.
- [ ] Run `python -m unittest scripts.test_instructions -v`; expect missing-module failures.
- [ ] Implement deterministic CommonMark text with managed HTML markers and normalized final newline.
- [ ] Run `python -m unittest scripts.test_instructions -v`.
- [ ] Commit: `feat: add safe automatic fan-out policy`.

### Task 5: 强化操作系统凭据与零泄露命令流程

**Files:**
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/credentials.py`
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/credential_helper.py`
- Create: `scripts/test_credentials.py`
- Modify: `codex-deepseek-subagent/scripts/codex_deepseek.py`

**Interfaces:**

```python
CREDENTIAL_TARGET = "codex-deepseek-api-key"

class CredentialStore(Protocol):
    def exists(self) -> bool: ...
    def store(self, secret: str) -> None: ...
    def read(self) -> str | None: ...
    def remove(self) -> bool: ...

def provider_auth_command() -> list[str]: ...
def prompt_and_store(store: CredentialStore) -> None: ...
```

- [ ] Add failing Windows tests proving the secret is passed as credential blob only and never appears in subprocess argv, emitted JSON, manifest, exception text, or temporary files.
- [ ] Add failing macOS tests proving `security add-generic-password` receives the secret over standard input or a protected API path, never as an argument.
- [ ] Add a helper test proving stdout contains exactly the raw secret for Codex provider auth while stderr contains no secret.
- [ ] Add a CLI test proving `setup` uses `getpass` when the credential is absent and never accepts `--api-key`.
- [ ] Run `python -m unittest scripts.test_credentials -v`; expect missing-module failures.
- [ ] Move platform credential logic into the module, use a stable helper path independent of the shell working directory, and redact all credential-related errors.
- [ ] Run `python -m unittest scripts.test_credentials -v` and search tracked files for strings matching `sk-[A-Za-z0-9_-]{8,}`; expect no matches.
- [ ] Commit: `security: keep DeepSeek credentials out of process arguments and files`.

### Task 6: 增加模型实存验证与隔离 Codex 兼容性门禁

**Files:**
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/provider_api.py`
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/compatibility.py`
- Create: `scripts/test_compatibility.py`

**Interfaces:**

```python
REQUESTED_MODEL = "deepseek-v4-pro"

@dataclass(frozen=True)
class CompatibilityReport:
    model: str
    effort: str | None
    provider_initialized: bool
    single_child_passed: bool
    fanout_passed: bool
    tools_passed: bool
    resume_passed: bool
    child_metadata_passed: bool
    parent_unchanged: bool

def discover_model(api_key: str, requested: str = REQUESTED_MODEL) -> str: ...
def probe_efforts(codex_bin: str, isolated_home: Path, model: str) -> ModelSelection: ...
def run_isolated_gate(codex_bin: str, original_home: Path, selection: ModelSelection) -> CompatibilityReport: ...
```

- [ ] Add mocked HTTP tests for exact model present, case-insensitive ambiguity rejection, authentication failure, unavailable model, malformed response, timeout, and redacted errors.
- [ ] Add subprocess tests proving the gate uses a fresh temporary `CODEX_HOME`, copies no user credential/config databases, and leaves the real home byte-identical.
- [ ] Assert the isolated prompt creates one child, then three concurrent children, exercises a read tool, waits for all, follows up/resumes one child, and produces exact non-secret tokens.
- [ ] Add metadata fixtures proving child provider/model/effort must match the selection while parent provider/model/effort must match the pre-gate snapshot.
- [ ] Add a failing test proving any requirement for a live `model_catalog_json` returns `unsupported` before formal installation.
- [ ] Run `python -m unittest scripts.test_compatibility -v`; expect missing-module failures.
- [ ] Implement API discovery with short timeouts, bounded response reads, structured redacted errors, and disposable-home cleanup.
- [ ] Implement the gate so every checkbox must be true; partial success is failure.
- [ ] Run `python -m unittest scripts.test_compatibility -v`.
- [ ] Commit: `feat: gate installation on real model and native fan-out compatibility`.

### Task 7: 重写事务安装、停用、恢复和卸载

**Files:**
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/transaction.py`
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/manager.py`
- Create: `scripts/test_transaction.py`
- Modify: `codex-deepseek-subagent/scripts/codex_deepseek.py`
- Modify: `scripts/test_manager.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class InstallPlan:
    files: dict[Path, bytes]
    manifest: dict[str, object]
    backup_dir: Path

class FanoutManager:
    def status(self) -> dict[str, object]: ...
    def setup(self) -> dict[str, object]: ...
    def test(self) -> dict[str, object]: ...
    def disable(self) -> dict[str, object]: ...
    def enable(self) -> dict[str, object]: ...
    def uninstall(self, remove_credential: bool = False) -> dict[str, object]: ...
```

- [ ] Add failing tests for lock contention, parse-before-write, backups, manifest schema, interrupted writes at every target file, failed post-install test, repeated setup, adoption of identical existing files, and conflicts with non-identical user-owned role files.
- [ ] Assert setup calls model discovery and the isolated compatibility gate before the first write to real `config.toml`, `agents/*.toml`, or `AGENTS.md`.
- [ ] Assert rollback restores exact bytes and permissions of every pre-existing file and removes only newly created files.
- [ ] Assert `disable` removes the three managed role files and fan-out block while retaining provider and credential; `enable` restores the validated last selection without network access; `uninstall` restores all overwritten config/instruction values; credential removal occurs only with `--remove-credential`.
- [ ] Assert migration removes the legacy `DeepSeek.toml`, legacy markers, managed catalog selection, `multi_agent_version = "v1"`, and managed `multi_agent_v2 = false` only when ownership is proven by the old manifest/markers.
- [ ] Run `python -m unittest scripts.test_transaction scripts.test_manager -v`; expect failures.
- [ ] Implement a single operation lock, immutable pre-write snapshots, same-directory atomic replacement, manifest checksums, ownership checks, and one rollback path shared by every mutation.
- [ ] Replace CLI commands with `status`, `setup`, `test`, `disable`, `enable`, and `uninstall [--remove-credential]`; keep `repair` as a documented alias of validated `setup` for existing users.
- [ ] Run `python -m unittest scripts.test_transaction scripts.test_manager -v`.
- [ ] Commit: `feat: install and roll back DeepSeek fan-out transactionally`.

### Task 8: 完成真实单代理、三路 fan-out、工具与续接验收

**Files:**
- Create: `codex-deepseek-subagent/scripts/deepseek_fanout/native_test.py`
- Create: `scripts/test_native_test.py`
- Modify: `codex-deepseek-subagent/scripts/deepseek_fanout/manager.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class NativeEvidence:
    session_id: str
    child_thread_ids: tuple[str, ...]
    role_names: tuple[str, ...]
    tokens: tuple[str, ...]
    metadata: tuple[dict[str, object], ...]

def run_native_acceptance(codex_bin: str, paths: Paths, selection: ModelSelection) -> NativeEvidence: ...
def verify_native_evidence(evidence: NativeEvidence, selection: ModelSelection, parent: dict[str, object]) -> None: ...
```

- [ ] Add fixture-based tests rejecting zero/one/two/four fan-out children, duplicate child IDs, parent-forged tokens, missing tool evidence, missing resume evidence, wrong role, wrong provider/model/effort, and changed parent metadata.
- [ ] Add a passing fixture with exactly three parallel children covering `default`, `worker`, and `explorer`, all waited to completion, plus one resumed child.
- [ ] Run `python -m unittest scripts.test_native_test -v`; expect missing-module failures.
- [ ] Implement bounded SQLite discovery/query retries without printing raw event payloads, and verify both returned tokens and authoritative thread metadata.
- [ ] Make `setup` roll back formal installation if native acceptance fails; make `test` read-only except for ephemeral Codex session state.
- [ ] Run `python -m unittest scripts.test_native_test scripts.test_manager -v`.
- [ ] Commit: `test: verify native DeepSeek fan-out and parent isolation`.

### Task 9: 更新 Skill、用户文档、评测与持续集成

**Files:**
- Modify: `codex-deepseek-subagent/SKILL.md`
- Modify: `codex-deepseek-subagent/references/compatibility.md`
- Modify: `codex-deepseek-subagent/evals/evals.json`
- Modify: `README.md`
- Modify: `.github/workflows/test.yml`

**Required user command:**

```powershell
py -3 "F:\Skill\Codex\codex-deepseek-subagent\codex-deepseek-subagent\scripts\codex_deepseek.py" setup
```

- [ ] Replace all single-role, v1 downgrade, disabled-v2, live catalog, `deepseek-v4-flash`, and chat-delivered-Key instructions with the approved built-in-role fan-out flow.
- [ ] Document that setup opens a masked local prompt and stores the Key under Windows Credential Manager target `codex-deepseek-api-key`; the user never pastes it into ChatGPT.
- [ ] Document the exact three role files, concurrency 8, main-model preservation, status meanings, enable/disable/uninstall behavior, rollback locations, and model-unavailable outcome.
- [ ] Rewrite evals as valid UTF-8 JSON covering setup, missing credentials, model unavailable, concurrent research, isolated code writes, sequential-task non-fan-out, disable/enable, uninstall, and secret redaction.
- [ ] Update CI to discover every `scripts/test_*.py`, compile the complete package, parse all JSON, and scan for legacy forbidden configuration writes.
- [ ] Run `python -m unittest discover -s scripts -p "test_*.py" -v` and `python -m json.tool codex-deepseek-subagent/evals/evals.json`.
- [ ] Commit: `docs: explain secure DeepSeek fan-out setup and lifecycle`.

### Task 10: 全量验证与正式配置前交付

**Files:**
- Verify: all tracked source, test, Skill, reference, CI, and documentation files

- [ ] Run the complete unit/integration suite on the current platform with `python -m unittest discover -s scripts -p "test_*.py" -v`.
- [ ] Compile every Python file with `python -m compileall -q codex-deepseek-subagent/scripts scripts`.
- [ ] Parse every tracked JSON file and every generated TOML fixture.
- [ ] Run `status --json --codex-home` against a fresh temporary home; verify it reports `credential_missing` or `not_configured` without writing.
- [ ] Run mocked setup success and injected-failure matrices; verify exact rollback and no secret in stdout, stderr, manifests, backups, or Git diff.
- [ ] Search tracked content for legacy writes to `multi_agent_version`, `multi_agent_v2 = false`, formal `model_catalog_json`, legacy role `DeepSeek.toml`, and hard-coded `deepseek-v4-flash`; allow occurrences only in migration tests and historical design records.
- [ ] Inspect `git diff --check`, `git status --short`, and the final commit list.
- [ ] Confirm the real `D:\Codex work\.codex\config.toml` is byte-identical to its pre-implementation hash because live installation must wait for the user's local masked Key entry and successful model validation.
- [ ] Commit any final test-only corrections as `test: complete DeepSeek fan-out acceptance coverage`.
- [ ] Hand off the exact setup command, Credential Manager target, expected success state, backup location, and reversible uninstall command.
