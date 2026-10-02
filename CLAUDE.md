## Project: 사내 LLM Wiki (이 저장소의 목적과 규칙)

**무엇**: 회사 문서(회의록/이슈/메일 .eml/Office/PDF)를 `inbox/<space>/` 에 넣으면 LLM 이 연관 문서로 묶은 Markdown 위키(Obsidian 호환)로
컴파일하고, 구성원이 자연어로 질문하면 **권한이 있는 문서만** 근거로 출처 인용 답변을 하는 사내 전용 지식창고. Python, 코어는 stdlib 전용(`src/llmwiki/`).

**먼저 읽을 것** (홈 세션의 메모리는 이관되지 않는다. 필요한 정보는 전부 저장소에 있다):
1. `docs/ONSITE-HANDOFF.md` — 완료/미완료/사내에서 할 일의 순서, 환경 프로필(Gauss, LM Studio), 트러블슈팅, 첫날 체크리스트
2. `docs/DEV-GUIDE.md` — 아키텍처 지도, 디스크/DB 구조, 확장 방법, 테스트/코드 규칙, 결정 로그
3. `docs/PLAN-company-llm-wiki.md` — 기획과 확정 요구사항. 환경변수는 `config/env.example`.

**실행**: 테스트 `scripts\run-tests.ps1` (= `.venv\Scripts\python -m pytest -q`, 항상 통과 유지) ·
진단 `scripts\run-doctor.ps1` (= `python -m llmwiki.doctor`; 코드/환경 변경 후 첫 확인) · 웹 `scripts\run-web.ps1` · 파이프라인 `scripts\run-pipeline.ps1`.
셸이 PowerShell 이므로 `&&` 대신 `;`/`if ($?)`. 설치되지 않았다면 `PYTHONPATH=src`. 사내에는 WSL/Docker 가 없다(Windows 네이티브만).

**절대 약화하지 말 것 (보안 불변식)**:
- ACL 먼저: 열람 권한 없는 문서는 검색/토큰화/점수/LLM 컨텍스트/인용 어디에도 들어가지 않는다. 권한 판단을 LLM 에 맡기지 않는다.
- Fail closed: space 라벨/출처 없는 문서는 열람 불가, `WIKI_ENV` 미설정 = production, 매핑 없는 사용자 = 빈 spaces.
- space 간 병합 금지(컴파일은 같은 space 안에서만). 혼합 출처 문서는 모든 출처 space 를 가진 사용자만 열람.
- dev 가짜 로그인은 `WIKI_ENV` 가 development/test 일 때만. 운영 인증은 SAML(AD FS) — 직접 만든 XML 서명 검증 금지.
- 비밀/문서 본문을 로그·예외·감사·doctor 출력에 남기지 않는다. 키는 `*_FILE` 로. 프록시/리다이렉트는 기본 금지, 사설 주소만.
- 보안 관련 변경 후에는 `tests/test_web_leak.py`, `tests/test_engine_leak.py` 를 포함한 전체 테스트를 돌리고, 누수 테스트 케이스를 먼저 추가한다.

**작업 규칙**: 파일당 500줄 미만 · 파라미터화 SQL 만 · 테스트는 네트워크 금지(`FakeLLM`/`FakeEmbedder`) · 새 `WIKI_*` 변수는 `config/env.example` 에 추가 ·
확인하지 못한 것은 "검증됨/미검증"을 구분해 문서에 정직하게 쓴다 · 코드는 **집 -> 회사 한 방향**으로만 이동한다(사내 산출물을 외부로 되돌리지 않는다).

**진행 방식**: 단계 사이에 사용자 의견을 묻지 말고 계속 진행한다(`docs/ONSITE-HANDOFF.md` (b)의 순서). 단, **강제 푸시, 사용자 파일 삭제, git push** 는 먼저 묻는다.
막히면(예: AD FS 정보 부재) 가정으로 채우지 말고 무엇이 필요한지 정리해 보고한다.

**아래 Ruflo 절은 선택 도구**다(스웜/메모리/에이전트 라우팅). 사용하지 않아도 된다. 훅(`.claude/settings.json`)이나 GateGuard 류가 첫 편집을 막으면
요구한 사실을 진술하고 재시도한다. 이 섹션과 아래 규칙이 충돌하면 이 섹션이 우선한다.

# Ruflo — Claude Code Configuration

## Rules

- Do what has been asked; nothing more, nothing less
- NEVER create files unless absolutely necessary — prefer editing existing files
- NEVER create documentation files unless explicitly requested
- NEVER save working files or tests to root — use `/src`, `/tests`, `/docs`, `/config`, `/scripts`
- ALWAYS read a file before editing it
- NEVER commit secrets, credentials, or .env files
- NEVER add a `Co-Authored-By` trailer to user commits unless this project's `.claude/settings.json` has `attribution.commit` set (#2078). The Claude Code Bash tool may suggest one in its default commit-message template — ignore it. `Co-Authored-By` is semantic authorship attribution under git/GitHub convention; the tool is the facilitator, not a co-author.
- Keep files under 500 lines
- Validate input at system boundaries

## Ruflo Capability Brain & Implementation Loop

Ruflo is the coordination ledger and policy decision point. Claude Code is the
executor: after a Ruflo coordination call, continue implementing the task.

When it is registered, call
`guidance_brain({ mode: "recommend", task: "..." })` before complex Ruflo
work. Use its live registry instead of guessing tool names. Treat
`registered`, `configured`, `reachable`, `healthy`, and `authorized`
as separate facts. If the brain is unavailable, continue with the compatible
`guidance_recommend` tool, CLI discovery, and repository instructions.

Follow the returned loop:

1. Recall memory and ADR constraints.
2. Inspect source, runtime, dependencies, policy, and health.
3. Route to the smallest capable topology, agents, skills, and tools.
4. Plan acceptance criteria, safety envelope, ownership, and validation.
5. Execute in isolated scopes; the coding agent performs the work.
6. Test focused, regression, and failure paths.
7. Validate types, security, policy, compatibility, and artifacts.
8. Benchmark a source-bound candidate against a source-bound baseline.
9. Optimize measured bottlenecks without weakening safety.
10. Bind claims and evidence to exact source/build receipts.
11. Reconcile concurrent handoffs and disclose limitations.
12. Publish only through a separately authorized release gate.

### Concurrency and authority

- Never allow two writers in one worktree; give each writing agent an isolated
  worktree and explicit file ownership.
- Read-only research may run concurrently and report findings to the owner.
- Only the integration owner edits shared manifests and lockfiles or reconciles
  overlapping changes.
- A child may drop capabilities but cannot add tools, network, secrets, spend,
  concurrency, namespaces, or delegation depth.
- A lease or claim coordinates ownership; it does not authorize a side effect.
- Darwin, Flywheel, MetaHarness, memory, and neural systems may propose or
  evaluate candidates but cannot self-promote or expand their SafetyEnvelope.
- Bind tests, benchmarks, policy decisions, and release evidence to an exact
  commit or immutable dirty-worktree snapshot.

## Agent Comms (SendMessage-First Coordination)

Named agents coordinate via `SendMessage`, not polling or shared state.

```
Lead (you) ←→ architect ←→ developer ←→ tester ←→ reviewer
              (named agents message each other directly)
```

### Spawning a Coordinated Team

```javascript
// ALL agents in ONE message, each knows WHO to message next
Agent({ prompt: "Research the codebase. SendMessage findings to 'architect'.",
  subagent_type: "researcher", name: "researcher", run_in_background: true })
Agent({ prompt: "Wait for 'researcher'. Design solution. SendMessage to 'coder'.",
  subagent_type: "system-architect", name: "architect", run_in_background: true })
Agent({ prompt: "Wait for 'architect'. Implement it. SendMessage to 'tester'.",
  subagent_type: "coder", name: "coder", run_in_background: true })
Agent({ prompt: "Wait for 'coder'. Write tests. SendMessage results to 'reviewer'.",
  subagent_type: "tester", name: "tester", run_in_background: true })
Agent({ prompt: "Wait for 'tester'. Review code quality and security.",
  subagent_type: "reviewer", name: "reviewer", run_in_background: true })

// Kick off the pipeline
SendMessage({ to: "researcher", summary: "Start", message: "[task context]" })
```

### Patterns

| Pattern | Flow | Use When |
|---------|------|----------|
| **Pipeline** | A → B → C → D | Sequential dependencies (feature dev) |
| **Fan-out** | Lead → A, B, C → Lead | Independent parallel work (research) |
| **Supervisor** | Lead ↔ workers | Ongoing coordination (complex refactor) |

### Rules

- ALWAYS name agents — `name: "role"` makes them addressable
- ALWAYS include comms instructions in prompts — who to message, what to send
- Spawn ALL agents in ONE message with `run_in_background: true`
- After spawning, continue independent local work; wait only when a dependency
  genuinely blocks progress
- Do not poll repeatedly — agents message back or complete automatically
- Give every writing agent an isolated worktree and a non-overlapping file scope

## Swarm & Routing

### Config
- **Topology**: hierarchical-mesh (anti-drift)
- **Max Agents**: 15
- **Memory**: hybrid
- **HNSW**: Enabled
- **Neural**: Enabled

```bash
npx @claude-flow/cli@latest swarm init --topology hierarchical --max-agents 8 --strategy specialized
```

### Agent Routing

| Task | Agents | Topology |
|------|--------|----------|
| Bug Fix | researcher, coder, tester | hierarchical |
| Feature | architect, coder, tester, reviewer | hierarchical |
| Refactor | architect, coder, reviewer | hierarchical |
| Performance | perf-engineer, coder | hierarchical |
| Security | security-architect, auditor | hierarchical |

### When to Swarm
- **YES**: 3+ files, new features, cross-module refactoring, API changes, security, performance
- **NO**: single file edits, 1-2 line fixes, docs updates, config changes, questions

### 3-Tier Model Routing

| Tier | Handler | Use Cases |
|------|---------|-----------|
| 1 | Agent Booster (WASM) | Simple transforms — skip LLM, use Edit directly |
| 2 | Haiku | Simple tasks, low complexity |
| 3 | Sonnet/Opus | Architecture, security, complex reasoning |

## Memory & Learning

### Before Any Task
```bash
npx @claude-flow/cli@latest memory search --query "[task keywords]" --namespace patterns
npx @claude-flow/cli@latest hooks route --task "[task description]"
```

### After Success
```bash
npx @claude-flow/cli@latest memory store --namespace patterns --key "[name]" --value "[what worked]"
npx @claude-flow/cli@latest hooks post-task --task-id "[id]" --success true --store-results true
```

### MCP Tools (use `ToolSearch("keyword")` to discover)

| Category | Key Tools |
|----------|-----------|
| **Memory** | `memory_store`, `memory_search`, `memory_search_unified` |
| **Bridge** | `memory_import_claude`, `memory_bridge_status` |
| **Swarm** | `swarm_init`, `swarm_status`, `swarm_health` |
| **Agents** | `agent_spawn`, `agent_list`, `agent_status` |
| **Hooks** | `hooks_route`, `hooks_post-task`, `hooks_worker-dispatch` |
| **Security** | `aidefence_scan`, `aidefence_is_safe`, `aidefence_has_pii` |
| **Hive-Mind** | `hive-mind_init`, `hive-mind_consensus`, `hive-mind_spawn` |

### Background Workers

| Worker | When |
|--------|------|
| `audit` | After security changes |
| `optimize` | After performance work |
| `testgaps` | After adding features |
| `map` | Every 5+ file changes |
| `document` | After API changes |

```bash
npx @claude-flow/cli@latest hooks worker dispatch --trigger audit
```

## Agents

**Core**: `coder`, `reviewer`, `tester`, `planner`, `researcher`
**Architecture**: `system-architect`, `backend-dev`, `mobile-dev`
**Security**: `security-architect`, `security-auditor`
**Performance**: `performance-engineer`, `perf-analyzer`
**Coordination**: `hierarchical-coordinator`, `mesh-coordinator`, `adaptive-coordinator`
**GitHub**: `pr-manager`, `code-review-swarm`, `issue-tracker`, `release-manager`

Any string works as a custom agent type.

## Build & Test

- ALWAYS run tests after code changes
- ALWAYS verify build succeeds before committing

```bash
npm run build && npm test
```

## CLI Quick Reference

```bash
npx @claude-flow/cli@latest init --wizard           # Setup
npx @claude-flow/cli@latest swarm init --v3-mode     # Start swarm
npx @claude-flow/cli@latest memory search --query "" # Vector search
npx @claude-flow/cli@latest hooks route --task ""    # Route to agent
npx @claude-flow/cli@latest doctor --fix             # Diagnostics
npx @claude-flow/cli@latest security scan            # Security scan
npx @claude-flow/cli@latest performance benchmark    # Benchmarks
```

26 commands, 140+ subcommands. Use `--help` on any command for details.

## Setup

```bash
claude mcp add claude-flow -- npx -y ruflo@latest mcp start
npx ruflo@latest doctor --fix
```

> The background `daemon` is optional. It runs interval workers that each spawn
> a headless `claude` session, so it consumes tokens continuously. Start it only
> if you want those sweeps: `npx ruflo@latest daemon start` (self-stops after 12h
> by default; `--ttl 0` to disable, `daemon status --all` to audit running daemons).

**Agent tool** handles execution (agents, files, code, git). **MCP tools** handle coordination (swarm, memory, hooks). **CLI** is the same via Bash.
