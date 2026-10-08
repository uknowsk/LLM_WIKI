## Project: 사내 LLM Wiki (이 저장소의 목적과 규칙)

**무엇**: 회사 문서(회의록/이슈/메일 .eml/Office/PDF)를 `inbox/<space>/` 에 넣으면 LLM 이 연관 문서로 묶은 Markdown 위키(Obsidian 호환)로
컴파일하고, 구성원이 자연어로 질문하면 **권한이 있는 문서만** 근거로 출처 인용 답변을 하는 사내 전용 지식창고. Python, 코어는 stdlib 전용(`src/llmwiki/`).

**사내 PC 에 clone 한 직후라면 가장 먼저 `docs/ONSITE-START.md` 를 읽고 그 단계 순서(환경 구성 -> 사람에게 필요한 정보를 한 번에 질문 -> 설정/모델/OCR/SAML/폴더/서빙/백업/재튜닝)대로 스스로 진행한다.**
사내 산출물은 외부(GitHub 등)로 push 하지 않는다.

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

## 세컨드 브레인 (옵시디언, Karpathy LLM Wiki 방식) — 지식 정리·산출물 작업일 때 적용

사용자가 새 정보를 주거나, 지식을 물어보거나, 글/문서 산출물을 요청하면 이 절을 따른다(스킬 `karpathy-llm-wiki` 와 같은 방식이며 폴더 이름만 아래로 대응: raw→`10_raw_data`, wiki→`20_ai_wiki`, index→`_index.md`, log→`_log.md`).
위의 사내 LLM Wiki **코드** 규칙(보안 불변식 등)은 코드 작업에서 그대로 유효하다.

**3계층** (루트의 옵시디언 볼트 폴더):
- `10_raw_data/` — 원본 보관소(회의록, 고객 CS, 일기, 수집 텍스트). **원본은 절대 고치거나 지우지 않는다.** 정정은 새 파일 + 기존 파일 `[[링크]]`.
- `20_ai_wiki/` — 원본을 검색/참조하기 좋게 요약·재구성한 가공 지식. `_index.md`(문서 목록), `_log.md`(변경 이력) 포함. 하위 폴더는 주제별 한 단계만.
- `30_outputs/` — 업무 지시로 만든 최종 산출물(마케팅 대본, 블로그, 사업계획서, 뉴스레터 등).
- 각 폴더의 `_templates/` 에 양식이 있다. 새 파일은 양식을 따른다.

**수집(Ingest)**
1. 새 정보를 받으면 **먼저** `10_raw_data/YYYY-MM-DD_제목.md` 로 원본 그대로 저장한다(요약/해석 금지, 출처·수집일·게시일은 프론트매터, 모르면 `Unknown`). 같은 이름이 있으면 `-2` 를 붙인다.
2. **분류(Triage)**: 저장 후 위키를 핵심 용어와 동의어로 검색해 판정을 말한다 — **New**(새 문서), **Update**(기존 문서에 병합), **Disputed**(기존 내용과 충돌; New/Update 와 함께 가능), **No material**(새로 알게 되는 것이 없음: 원본은 두고 `_log.md` 에만 기록하고 멈춘다. 얇은 원본으로 억지로 문서를 만들지 않는다).
3. **컴파일**: 같은 핵심 주제가 있으면 그 문서에 병합(sources 추가), 새 개념이면 새 문서(파일명은 개념 이름). 여러 주제에 걸치면 가장 가까운 폴더에 두고 `관련 문서` 에 `[[링크]]`.
4. **파급 갱신(Cascade)**: 위키 전체를 핵심 용어로 검색해 영향받는 문서를 모두 갱신하고 `updated` 를 고친다. 새 원본이 옛 주장을 대체/반박하면 옛 주장을 지우지 말고 표시한다: `> **Status: Outdated** (YYYY-MM-DD, 이유)` 또는 `> **Status: Disputed** (이유, 양쪽 `[[링크]]`)`. 역사를 조용히 다시 쓰지 않는다.
5. `_index.md` 와 `_log.md` 를 갱신한다. 로그 형식: `- YYYY-MM-DD ingest|query|lint [[문서명]] — Disposition, 근거 [[원본]]`.

**근거 원칙(Grounding)**: 위키 문서의 숫자·날짜·직접 인용은 `sources` 로 링크한 원본에 **그대로** 있어야 한다. 쓰기 전에 원본에서 찾고, 원본이 `42K` 면 `42,000` 으로 바꿔 쓰지 않는다. 계산한 값은 구성 요소를 함께 적는다. 못 찾으면 정확한 값을 쓰지 말고 빼거나 정밀도 없이 말한다.

**질의(Query)**: `_index.md` 를 먼저 보고 위키 본문도 동의어로 검색한다(둘 다 비었을 때만 "없다"고 하되 검색했다고 말한다). 답은 위키 내용을 우선하고 근거 문서를 `[[문서명]]` 으로 밝힌다. 원본끼리 상충하거나 위키에 없으면 추측하지 않는다. 사용자가 요청하지 않으면 파일을 쓰지 않는다. "저장해줘" 면 새 위키 문서(요약 앞에 `[Archived]`)로 만들고 기존 문서에 병합하지 않는다.

**점검(Lint)**: 먼저 `.venv\Scripts\python -m llmwiki.vault_lint`(루트에서, `PYTHONPATH=src`)를 실행하고 오류 0 이 될 때까지 고친다. 새 원본을 넣은 뒤와 주 1회 실행한다. 이 도구가 보는 것: 깨진 `[[링크]]`, 이름 중복, 인덱스와 파일 불일치(날짜 포함), 위키의 숫자·날짜·인용이 `sources` 원본에 그대로 있는지, 산출물 숫자가 `based_on` 위키에 있는지, Status 블록 형식, 필수 머리말, 원본 변경·삭제, 미처리 원본, 고아 문서, 30일 넘은 draft, `visibility: private` 문서가 외부용 산출물 근거로 쓰였는지. 원본을 새로 넣었다면 점검 후 `--accept` 로 기준선을 갱신한다(원본이 의도치 않게 바뀌면 이후 오류로 보인다). (사람이 판단할 것, 보고만) 문서 간 모순, 대체됐는데 Status 표시 없는 낡은 주장, 자주 언급되지만 문서가 없는 개념. 결과를 `_log.md` 에 기록.

**추가 규칙**
- **허브**: 새 위키 문서는 `홈.md`(`type: hub`, 지식 지도)의 영역 중 한 곳 이상에 링크한다. 어디서도 링크되지 않으면 고아로 보고된다.
- **공개 범위**: 일기·건강·개인 메모 문서는 머리말에 `visibility: private`. 산출물은 `audience`(기본 internal)를 정하고, private 문서는 `audience: private` 산출물에만 쓴다.
- **별칭**: 다른 이름이 흔하면 `aliases` 에 적는다(질의 때 동의어 검색에도 쓴다).
- **날짜**: 위키에는 절대 날짜를 쓴다. 원본의 "월요일", "다음 주" 같은 상대 표현은 **원본 날짜를 기준으로 계산하고 계산 근거를 함께 적는다**(근거 원칙의 파생값 규칙).
- **첨부 원본**(PDF·이미지 등)은 `10_raw_data/_files/` 에 두고 원본 노트에서 링크한다. 점검 도구의 해시 보호는 이 폴더의 모든 파일 형식에 적용된다.
- **개인정보**: 고객 실명·연락처·주민번호 등은 원본 저장 전에 가명/마스킹한다(원본은 평문 파일이다).
- **검색**: 이 폴더들은 `.gitignore` 대상이라 `Grep`/`Glob` 이 기본으로 건너뛴다. 루트 `.ignore` 가 세 폴더를 다시 보이게 한다. 검색 결과가 이상하게 비면 `.ignore` 를 먼저 확인한다.
- **백업**: 이 폴더는 git 이력이 없다(개인 내용이라 커밋하지 않음). 디스크 장애나 실수로 지우면 복구할 수 없으므로 정기 백업이 필요하다.

**산출물**: 지시를 받으면 관련 위키 문서를 먼저 읽고, 완성한 최종 파일을 `30_outputs/YYYY-MM-DD_제목.md` 로 저장한다. 근거 위키 문서는 `based_on` 에 `[[링크]]` 로 남긴다.

**연결 규칙**: 관련된 기존 위키 문서나 원본이 있으면 반드시 옵시디언 위키링크 `[[문서명]]` (확장자 없음)로 상호 연결한다. 링크 대상이 없으면 만들지 말고 `_index.md` 의 처리 대기에 적는다.

**주의**
- 이 세 폴더의 내용은 `.gitignore` 로 커밋되지 않는다(템플릿만 추적). 이 저장소의 원격은 외부 GitHub 이므로 **사내 문서/비밀/개인정보를 여기에 넣지 않는다.** 사내 문서는 사내 위키 파이프라인(`inbox/<space>/`, 권한 관리됨)으로 넣는다.
- 이 세컨드 브레인은 권한 관리(ACL)가 없는 개인용 폴더다. 여러 사람이 쓰는 지식에는 사내 위키 시스템을 쓴다.
- 스킬의 `scripts/check_evidence.py` 는 `raw/`·`wiki/` 이름과 상대 경로 링크를 가정하므로 이 구조에서는 쓰지 않는다. 대신 위 `llmwiki.vault_lint` 가 같은 근거 검사(`engine.lint.check_text`)를 `[[링크]]` 구조에 맞춰 수행한다.

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
