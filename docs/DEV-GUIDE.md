# 개발 가이드 (DEV-GUIDE)

대상: 이 저장소를 이어서 개발하는 사람/Claude Code. 먼저 `docs/ONSITE-HANDOFF.md`(할 일/런북)를 읽고, 이 문서로 구조와 규칙을 파악한다.
코드 식별자는 영어, 설명은 한국어. 확인하지 못한 것은 **[미검증]** 으로 표시.

## 1. 아키텍처 지도 (패키지별)

저장소: `src/llmwiki/` (stdlib 전용 코어, `dependencies = []`), `tests/`, `config/`(예시 설정), `eval/`(평가 코퍼스/QA/결과), `docs/`, `scripts/`.

| 패키지 / 파일 | 역할 |
|---|---|
| `config.py` | `Settings`(env, data_dir, auth_provider, llm_base_url, llm_model, mask_pii_default). `WIKI_ENV` 기본 **production**(fail closed) |
| `auth.py` | `User(id,name,department,part,spaces,is_admin)`, `AuthProvider` 프로토콜, `DevAuthProvider`, `get_provider`(saml 은 NotImplementedError) |
| `acl.py` | `can_read(user, doc_spaces)`: 문서 space 집합이 user.spaces 의 부분집합이어야 함. 빈 집합 = 거부 |
| `audit.py` | `AuditLog.record(user, action, target, detail)` -> SQLite `audit` 테이블 |
| `models.py` | `ParsedDocument`(파서 출력), `RawRecord`(raw_path, space, sha256) |
| `ingest/` | 파서(`text docx xlsx eml pdf` + `ooxml tablefmt numfmt`), `pii.py`(정규식 마스킹), `save.py`(`save_raw`: `raw/<space>/<날짜-슬러그>.md`), `limits.py`(입력 크기 상한), `validate_space` |
| `engine/store.py` | SQLite 저장소(스레드 안전, WAL). 테이블은 아래 3절. `_READABLE_SQL` 이 사용자별 열람 가능 문서를 한 쿼리로 계산 |
| `engine/compile.py`, `triage.py`, `article.py` | triage(New/Update 판단) -> 작성/병합 -> `_meta/<space>/index.md,log.md` -> cascade. 구조화출력 불가 시 평문 JSON + 1회 재시도 + 안전 폴백(같은 space 에 New) |
| `engine/retrieval.py`, `search.py`, `params.py`, `embed.py` | ACL-first 하이브리드 검색(BM25 한국어 바이그램 + 임베딩 코사인, RRF). `RetrievalParams` 가 모든 노브 |
| `engine/query.py` | `QueryService.query/read_article/retrieve`. ACL 필터 -> 랭킹 -> 컨텍스트 패킹(`pack_context`) -> LLM -> 출처 인용 검증 |
| `engine/llm.py`, `providers*.py` | `OpenAICompatClient`(Bearer, SSE, think 제거, 컨텍스트 초과 감지), `CustomChatClient`(JSON 설정), `NetPolicy`(사설주소/프록시/CA), 임베딩 클라이언트 |
| `engine/lint.py`, `reindex.py` | 근거 검증 lint(숫자/날짜/인용이 raw 에 있는지), 임베딩 재색인 CLI |
| `pipeline/` | `inbox.py`(폴더 규칙, 예약 폴더), `watch.py`(폴링), `queue.py`(SQLite 작업 + `COMPILE_LOCK` 단일 직렬 처리, 최대 3회 시도), `run.py`(`process_file`: 파싱 -> save_raw -> 등록 -> compile) |
| `web/` | `app.py`(WSGI, 라우팅, 세션/CSRF/Origin, 보안헤더), `sessions.py`, `api.py`, `upload.py`, `limits.py`(속도제한, 감사 가드), `http.py`, `ui*.py`(최소 UI), `webconfig.py`, `__main__.py`(서버 기동: waitress/wsgiref) |
| `doctor/` | `python -m llmwiki.doctor` 자가진단(PASS/WARN/FAIL, 비밀 마스킹) |
| `eval/` | `python -m llmwiki.eval` build/eval/tune/final/report. 점수/튜너/heldout 가드/LLM 캐시 |

### 데이터 흐름
```
inbox/<space>/파일 (또는 POST /api/upload, 텍스트 메모는 POST /api/capture)
  -> pipeline.watch/queue (직렬 컴파일 락)
  -> process_file: 파서(확장자별) -> [PII 마스킹] -> save_raw  => raw/<space>/YYYY-MM-DD-slug.md (불변, 덮어쓰기 금지)
  -> Store.add_raw (raw_files)  -> Compiler.compile: triage(LLM) -> wiki/<topic>/<article>.md 작성/병합
        -> article_sources(문서<->raw) 기록, _meta/<space>/index.md / log.md 갱신, 같은 space 안에서만 cascade
  -> embed_article => article_embeddings (실패해도 BM25 만으로 동작)
질의: POST /api/query -> 세션 확인 -> QueryService.query(user, question)
  -> ACL 로 열람 가능 문서 집합 계산(DB 1쿼리) -> 그 집합만 BM25/임베딩 랭킹 -> RRF -> 컨텍스트 패킹 -> LLM -> 인용
  -> 감사 로그 기록
```

## 2. 디스크 구조 (`WIKI_DATA_DIR`)
```
inbox/<space>/...  (_done _rejected _failed 예약)   raw/<space>/*.md   wiki/<topic>/*.md   wiki/_meta/<space>/{index,log}.md
wiki.db (WAL)   sessions.db
```
space 는 `/` 로 계층이고 각 세그먼트는 `[a-z0-9][a-z0-9-]{0,62}`; `_` 로 시작하는 세그먼트는 inbox 예약. 위키 문서 front matter 에 `sources`/`related` 가 JSON 으로 저장되며 이 값만 구조로 신뢰한다(본문은 재파싱하지 않음).

## 3. SQLite 테이블 (파라미터화 SQL만 사용)
| DB | 테이블 | 주요 컬럼 |
|---|---|---|
| wiki.db | `raw_files` | raw_path PK, space, sha256 |
| | `articles` | path PK, title, updated |
| | `article_sources` | (article_path, raw_path) PK — ACL 의 근거 |
| | `article_revs` | path PK, rev (캐시 무효화) |
| | `article_embeddings` | article_path PK, model, dim, text_sha256, vector BLOB(float32, L2 정규화) |
| | `audit` | id, ts, user_id, action, target, detail |
| | `pipeline_jobs` | id, path UNIQUE, space, status(new/done/failed), attempts, last_error, created_at, updated_at |
| | `pipeline_sources` | (space, sha256) PK, raw_path, parent_raw (중복 제거, 메일 첨부 연결) |
| sessions.db | `sessions` | sid_hash PK(sha256), user_id, user_json(User 스냅샷), csrf, created, last_seen |
| (평가 결과) | `llm_cache` | key, reply, latency_ms |

## 4. 확장 방법

### 4-1. 파서 추가
1. `ingest/<형식>.py` 에 `parse_<형식>(data: bytes, source_name: str) -> ParsedDocument`. 첫 줄에서 `limits.check_size(data, source_name)`. 외부 파일은 신뢰하지 않는다
   (zip 폭탄/XML 엔티티/거대 입력 상한, 예외는 `ValueError` 로 일관).
2. `pipeline/run.py` 의 `SUPPORTED` 튜플과 `_parse` 분기에 확장자 추가.
3. 테스트: `tests/test_ingest_*.py` 처럼 **메모리에서 만든 합성 바이트**로(실문서 금지). 경계: 빈 파일, 상한 초과, 깨진 파일.
4. 파서는 PII 마스킹을 하지 않는다(`save_raw` 가 정책에 따라 적용). 새 의존성이 필요하면 선택적 import + 명확한 RuntimeError, `pyproject.toml` 에 선택 extra 로 선언 (현재 `pypdf` 는 선언 누락 — ONSITE-HANDOFF b-1).

### 4-2. LLM / 임베딩 provider 추가
- OpenAI 호환이면 코드 변경 없이 환경변수만. 형식이 다르면 `WIKI_LLM_PROVIDER=custom` + JSON 설정(`config/llm-custom.example.json`, 스키마는 `engine/providers_config.py`)으로 해결 가능한지 먼저 본다.
- 새 클래스가 필요하면 `LLMClient` 프로토콜 `complete(system, prompt, temperature=None[, response_format=None]) -> str` 구현. 지켜야 할 것: `_check_endpoint`(사설 주소만), 리다이렉트/프록시 기본 금지(`providers_http.build_opener`), 응답 크기 상한, 키는 `Secret`(repr/로그/예외에 값 금지), 프롬프트/응답 본문을 로그·예외 메시지에 넣지 않음, 컨텍스트 초과는 `ContextExceeded` 로 변환.
- 임베딩은 `Embedder` 프로토콜(`model: str`, `embed(texts) -> list[list[float]]`, 선택적 `embed_query`). `embedder_from_env` 에 분기를 추가.
- 테스트: 로컬 가짜 HTTP 서버(`tests/test_providers_*.py` 참고)로 — 외부 네트워크 금지.

### 4-3. AuthProvider 추가 (SAML 등)
- `auth.py` 의 `AuthProvider` 프로토콜과 `get_provider` 분기. 웹과의 접점은 `WikiApp.add_route(method, path, handler, auth=, csrf=, self_validated=)` 와
  `WikiApp.start_session(req, user) -> SessionGrant`(세션 고정 방지, 감사 `login`), `app.session_auth.revoke_user(user_id)`. 핸들러 시그니처 `handler(app, req, session_or_None) -> Response`.
- POST + `auth=False` 라우트는 `self_validated=True` 없이는 등록 거부(ValueError) — 실수로 CSRF 를 건너뛰지 못하게 한 장치. 상세 절차: ONSITE-HANDOFF b-4.
- SAML ACS 핸들러 예(IdP 가 `application/x-www-form-urlencoded` 로 POST):
  ```python
  app.add_route("POST", "/saml/acs", acs, auth=False, csrf=False, self_validated=True)
  def acs(app, req, session):
      form = req.read_form(max_bytes=256 * 1024)   # dict[str, list[str]]; 415 형식 오류 / 413 초과 / 400 잘못된 본문은 HttpError 로 자동 변환
      xml = base64.b64decode(form["SAMLResponse"][0])  # KeyError/IndexError 는 직접 400 처리
      user = verify_and_map(xml, form.get("RelayState", [""])[0])  # 서명, InResponseTo, NotBefore/NotOnOrAfter, 재생 방지
      grant = app.start_session(req, user)
      return Response(303, b"", "text/plain", [("Location", "/")] + grant.headers())
  ```
  Origin 검사는 그대로 적용되므로 IdP 의 Origin 을 `WIKI_ALLOWED_ORIGINS` 에 넣거나 IdP 가 Origin 을 보내지 않아야 한다.
- 프록시 뒤 클라이언트 주소: `WIKI_TRUSTED_PROXIES`(정확한 IP 목록)와 `web/app.py client_addr`. 감사 로그 정리는 `llmwiki.audit_admin`(관리자 CLI, 웹에서 접근 불가). space별 PII 정책은 `pipeline/pii_policy.py`.
- 어떤 provider 든 **User.spaces 를 정확히 채우고, 모르면 빈 집합**.

## 5. 테스트 규칙
- 실행: `scripts\run-tests.ps1` 또는 `.venv\Scripts\python -m pytest -q` (`pyproject` 의 `pythonpath=["src"]`). 환경에 `WIKI_*` 가 남아 있지 않게 한다(스크립트가 지움).
- **네트워크 금지**: LLM 은 `FakeLLM(responder)`(기본: 프롬프트를 그대로 반환해 누수가 눈에 띔), 임베딩은 `FakeEmbedder`(해시 바이그램, 결정적). 웹은 `tests/test_web_support.py` 의 `make_web(tmp_path, **cfg)` + 쿠키 유지 `Client`(소켓 없이 WSGI 직접 호출). 실제 소켓이 필요할 때만 `build_server(..., "127.0.0.1", 0)`.
- 엔진 픽스처: `tests/test_engine_support.py` 의 `make_env`, 사용자 `UA/UB/UAB`, `triage(...)` 헬퍼. 데이터는 전부 합성.
- 시간/속도제한은 주입 가능한 `clock` 으로 제어(`Clock`).
- 보안 변경은 누수 테스트(`test_engine_leak.py`, `test_web_leak.py`)에 케이스를 **먼저** 추가한다. 선택 의존성(waitress 등)이 필요한 테스트는 `pytest.importorskip` + 스텁 테스트를 함께 둔다(`tests/test_web_server.py`).
- 한 테스트 파일 500 줄 미만. 새 파일은 기존 이름 규칙(`test_<영역>_<주제>.py`).

## 6. 코드 규칙
- 코어는 **stdlib 만**. 외부 패키지는 선택적 import 로 격리하고 없을 때 명확히 실패.
- 파일당 500 줄 미만(실제 파일은 대부분 100~300 줄).
- **비밀/문서 본문을 로그·예외·감사 detail·doctor 출력에 넣지 않는다**. 접근 로그는 `repr` 처리된 메서드/경로/상태/사용자 id/시간만(쿼리스트링, 본문 제외).
- SQL 은 항상 파라미터 바인딩(`?`). 문자열 연결 금지.
- 사용자 입력 경로는 항상 검증 후 `resolve().is_relative_to(root)` 로 가둔다. 클라이언트 파일명은 경로로 쓰지 않는다.
- 경계에서만 입력 검증(웹 요청, 파일 파서, 환경변수). 내부 함수는 신뢰.
- 실패는 닫는 쪽으로(fail closed). 예외 메시지에는 변수 이름은 되지만 값은 안 된다.
- 하드코딩된 경로/주소/모델명 금지 -> 환경변수(`config/env.example` 에 반드시 추가). 환경변수 완전성 점검:
  `src` 에서 `WIKI_[A-Z0-9_]+` 를 정규식으로 모아 `config/env.example` 에 없는 것을 찾는다(접두어만 있는 `WIKI_LLM_` 같은 항목은 제외). 새 변수를 만들면 이 파일도 갱신.
- 변경은 최소로, 인접 코드 정리 금지. 새 기능마다 테스트.

## 7. Claude Code 사용 시 주의 (훅/게이트)
- `.claude/settings.json` 에 **ruflo 훅**(PreToolUse/PostToolUse/SessionStart 등; `.claude/helpers/hook-handler.cjs` 를 node 로 호출)이 설정돼 있다. 파일이 없으면 `IF EXIST` 로 건너뛴다. 사내에 node 가 없으면 훅은 조용히 동작하지 않거나 오류가 날 수 있다 [미검증] — 그 경우 훅을 비활성화해도 프로젝트 기능과는 무관하다.
- 환경에 따라 **GateGuard 계열 훅**이 첫 편집 전에 "사실 진술"(파일의 역할, 영향받는 곳, 사용자 지시 인용 등)을 요구해 편집을 한 번 막을 수 있다. 막히면 요구한 사실을 진술한 뒤 같은 편집을 재시도한다. 이 훅이 사내에 설치되어 있는지는 [미검증].
- `.env*` 이름의 파일은 일부 권한 규칙에서 쓰기/읽기가 막힐 수 있다(홈 세션에서 `.env.example` 쓰기가 거부되어 `config/env.example` 로 둠).
- `CLAUDE.md` 의 ruflo 관련 절(스웜, 메모리, 에이전트 라우팅)은 **선택 도구**다. 프로젝트 규칙은 상단 "Project: 사내 LLM Wiki" 섹션이 우선.
- 대규모 변경 중 병렬 에이전트를 쓸 때는 한 worktree 에 writer 를 둘 두지 않는다(CLAUDE.md 규칙).
- 커밋 메시지에 Co-Authored-By 를 붙이지 않는다(CLAUDE.md 규칙). 푸시/강제푸시/사용자 파일 삭제는 사용자에게 먼저 묻는다.

## 8. 결정 로그 (사용자 확정 사항과 이유)

출처: `docs/PLAN-company-llm-wiki.md`(2026-10-02 확정) 및 이관 브리핑. 사용자가 확정한 것만 적었다. 이유가 문서에 없는 것은 "(이유 미기록)".

| # | 결정 | 이유 / 영향 |
|---|---|---|
| D1 | LLM 은 사내 자체 호스팅 (Q1) | 외부 API 금지, 아웃바운드 차단 가능. 코드는 사설 주소만 기본 허용 |
| D2 | 인증은 AD 연동 SAML 2.0, AD FS 가 IdP, 위키가 SP (Q2) | 부서/그룹 클레임으로 space 권한 부여. AD FS 유무는 IT 확인 필요 |
| D3 | 메일은 EML 파일 (Q3) | stdlib `email` 파서, Message-ID/References 로 스레드 |
| D4 | 서버: HP, Windows OS, 운영 GPU 48GB+, 개발 GPU 5GB, 동시 약 80명 (Q5) | WSL/Docker 는 사내에서 **차단**됨(이관 브리핑) -> Windows 네이티브 서빙(waitress, 작업 스케줄러) |
| D5 | 권한은 소속 부서원 한정, 필요 시 파트 단위 세분화 (Q6) | space 계층(부서 -> 파트), AD 그룹/OU <-> space 설정 매핑 |
| D6 | 스캔 PDF OCR 필요, HWP/HWPX 미정 (Q4) | OcrEngine 슬롯만 준비, 엔진은 사내에서 연결 |
| D7 | PII 마스킹 옵션 제공, space별 설정 (Q7) | 마스킹 on/off 를 space 별로. 코드는 파라미터만(설정 로딩 TODO) |
| D8 | 위키 문서의 ACL = 출처 raw ACL 의 교집합, 컴파일은 space 안에서만 | 합쳐진 문서를 통한 권한 누수 방지(기획 2.2) |
| D9 | 검색 단계에서 ACL 필터 선적용, LLM 에게 권한 판단을 맡기지 않음 | 프롬프트 인젝션/누수 방어 |
| D10 | 인증은 `AuthProvider`/`User` 단일 교체 지점. dev 가짜 로그인은 development/test 에서만 | 사외 개발 -> 사내 SAML 교체(기획 2.5) |
| D11 | 감사 로그 인터페이스를 처음부터 포함 | 사내에서는 사용자 식별만 실제 값으로 |
| D12 | 경로/주소/모델명은 환경설정, 의존성 최소(stdlib 코어) | 사내 반입 가능성, 오프라인 설치 |
| D13 | 권한 침투 테스트는 사외에서 통과시키고 사내에서 재실행 | 누수 테스트 2종을 SAML 후 재실행(ONSITE b-4) |
| D14 | 개발 방식: 사외 개발 후 사내 clone 으로 인증 연동. **코드는 집 -> 회사 한 방향만**(GitHub `uknowsk/LLM_WIKI` 또는 승인 미러) | 사내에서 만든 것은 되돌리지 않음. 홈 세션 메모리 이관 불가 -> 모든 인수 정보는 저장소 문서에 |
| D15 | 채팅 LLM 은 사내 'Gauss Chat API'(OpenAI 호환, Bearer, think 모델, 64k, 스트리밍 가능, JSON schema 구조화출력 없음, 임베딩 없음) | `WIKI_LLM_STRUCTURED=off`, `WIKI_LLM_STREAM=1`, think 토큰 처리, 컴파일은 평문 JSON 폴백 |
| D16 | 임베딩은 LM Studio bge-m3(`text-embedding-bge-m3`)를 웹 서버와 **같은 PC** 의 `127.0.0.1:1234/v1` 에서 | Gauss 에 임베딩이 없음. 채팅 키가 다른 호스트로 새지 않도록 임베딩 키/정책 상속은 같은 host:port 일 때만 |
| D17 | 외부로 나가는 경로 기본 차단: 프록시 환경변수 무시, 리다이렉트 금지, 사설 주소만 | 데이터 유출 경로 최소화. 필요 시 `*_ALLOWED_HOSTS/_PROXY/_CA_BUNDLE` 로 명시 opt-in |
| D18 | 운영 서버는 waitress(우선), wsgiref 는 개발/폴백. TLS 는 사내 리버스 프록시 | Windows 네이티브에서 다중 사용자 서빙. 운영은 `WIKI_ALLOWED_ORIGINS` 필수, Host 불신 |
| D19 | OCR 은 사내에서 연결(PaddleOCR 별도 Python 3.12 프로세스를 권장했으나 미검증) | 3.14 호환성 불확실 |
| D20 | 단계 사이에 사용자 의견을 묻지 않고 계속 진행. 단, 강제 푸시 / 사용자 파일 삭제 / 푸시는 먼저 확인 | 이관 브리핑의 작업 방식 지시 |
| D21 | 검색 파라미터는 사내 코퍼스로 재검증, 생성 파라미터는 Gauss 로 반드시 재튜닝. heldout 으로 튜닝 금지, 누수 0 | 집의 합성 코퍼스는 편향(ONSITE c, f-3) |
| D22 | (이유 미기록) 세션 유휴 30분 / 절대 8시간, 업로드 50MiB, 사용자별 분당 120 요청 | 기본값. AD 그룹 변경은 재로그인으로 전파 |
| D23 | 빠른 메모 `POST /api/capture`: 서버는 출처 URL 을 가져오지 않고 텍스트로만 기록. 공간은 사용자 본인의 space 만, 파일명은 정제된 제목+날짜+중복 시 번호, 업로드의 `_publish` 재사용, 감사 기록에는 크기만 | 서버측 URL 접속은 SSRF/외부 접속 금지 원칙 위반. 업로드와 같은 신뢰 규칙. `web/capture.py`, `tests/test_web_capture.py` |
| D24 | `web/api.py query_service()` 가 `RetrievalParams.from_env()` 를 항상 전달 (`WIKI_TOP_K`, `WIKI_MAX_CONTEXT_CHARS`) | 이전에는 `params` 없이 호출되어 튜닝 기본값(top_k 8) 대신 옛 k=5 가 쓰였음 |
| D25 | 충돌 표시: 컴파일러가 붙이는 `## Disputed` 절을 `engine/lint.is_disputed` 로 감지, `QueryResult.disputed`(이미 ACL 통과해 읽은 문서에서만 계산)와 `/api/query` 인용의 `disputed` 플래그, UI ⚠. 충돌 문서 목록은 관리자 CLI `python -m llmwiki.lint_admin disputed`(공간 필터 없음, 웹에서 접근 불가) | 사용자가 상충 내용을 모른 채 한쪽 답을 믿는 것을 막음. 새 노출 경로 없음. `engine/lint.py`, `engine/query.py`, `web/api.py`, `lint_admin.py`, `tests/test_web_disputed.py`. 한계: `Outdated`(대체됨) 표시는 아직 자동화하지 않음 |
| D26 | 답변 피드백 `POST /api/feedback {rating: up\|down, paths[]}`: 인용 경로마다 감사 행 1개(`feedback_up/down`, 대상=경로), 질문/답변 텍스트와 자유 서술은 저장하지 않음. 읽을 수 있는 문서만(`QueryService.can_open`), 하나라도 읽기 불가/없음이면 전체 404(존재 탐지 방지). 관리자 집계: `python -m llmwiki.lint_admin feedback` | 새 테이블 없이 기존 감사 로그와 보존 정책(`audit_admin`) 재사용. 개인정보가 섞일 수 있는 자유 서술은 의도적으로 제외. `web/feedback.py`, `engine/query.py`, `lint_admin.py`, `tests/test_web_feedback.py` |
| D27 | 이미지(.png/.jpg/.jpeg) 수집: `ingest/image.py parse_image` 가 OCR 엔진(`ocr_page(data, 0)`, 이미지 바이트면 이미지 모드)으로만 글자를 읽음. OCR 없음/글자 없음/이미지 아님은 파일 단위 실패(빈 문서 생성 금지). 업로드는 매직 바이트(PNG/JPEG)를 검사. **메일 첨부 이미지는 처리하지 않음**(`ATTACHMENT_SUPPORTED` 제외) | 서명 로고/배너마다 OCR 호출·오류가 생기는 것을 막음. `ingest/image.py`, `pipeline/run.py`, `web/upload.py`, `tests/test_pipeline_image.py` |
| D28 | 후속 질문: `/api/query` 가 선택적 `history`([{q,a}], 최대 3턴, q≤1000자 a≤2000자, 위반 시 400 `bad_history`)를 받아 `QueryService.query(history=)` 로 전달. 직전 질문을 검색어에 덧붙이고 이전 턴은 프롬프트에 **증거 아님**으로 표시(history 가 있을 때만 시스템 프롬프트 한 문장 추가, 없으면 기존과 동일). 서버는 이력을 저장하지 않고 감사 로그에도 남기지 않음(현재 질문만 기존대로). UI 는 최근 3턴을 메모리에만 두고 '새 대화'/로그아웃 시 비움 | ACL 은 검색 전에 그대로 적용되어 이력이 접근 범위를 넓히지 못함(누수 테스트). 이력은 클라이언트가 위조할 수 있으나 본인 문서 접근 범위 안에서 프롬프트에만 영향. `engine/query.py`, `web/api.py`, `web/ui_js.py`, `tests/test_web_followup.py` |
| D29 | 관리자 리포트 확장 `lint_admin gaps`(근거 없음으로 끝난 질문, 빈도순 = 지식 공백)·`stats`(질문/근거 없음/사용자/피드백 수)와 사용자용 `GET /api/recent`('새 소식': 읽을 수 있는 문서의 경로·제목·날짜만, 최신순, limit 1~50 기본 20, 본문 없음). 모두 기존 감사 로그/기존 ACL 단일 쿼리(`readable_articles`)를 재사용하고 새 저장소 없음 | 능동적 기능(공백 파악, 새 지식 알림)을 보안 표면을 늘리지 않고 제공. gaps 는 사용자의 질문 원문을 보여 주므로 관리자 전용 CLI(웹 비노출). `lint_admin.py`, `engine/store.py article_meta`, `engine/query.py recent`, `web/api.py recent`, `tests/test_web_recent.py`, `tests/test_lint_admin_reports.py` |
| D30 | 세컨드 브레인 점검 도구 `python -m llmwiki.vault_lint`: `[[링크]]` 볼트(10_raw_data/20_ai_wiki/30_outputs)를 읽기 전용으로 검사. 근거 검사는 엔진의 `engine.lint.check_text` 재사용. 원본 보호는 `10_raw_data/.manifest.json`(모든 파일 형식의 sha256, `--accept` 로 갱신), 위키 `visibility: private` 는 `audience` 가 private 이 아닌 산출물의 근거로 금지, 링크 검사는 코드 표기(`...`)를 무시, 허브(`type: hub`)는 sources·고아 검사 면제. 루트 `.ignore` 가 `.gitignore` 된 세 폴더를 ripgrep(Grep/Glob)에 다시 보이게 함 | 스킬의 check_evidence.py 는 raw/wiki 폴더명·상대경로 링크를 가정해 쓸 수 없었음. gitignore 된 폴더는 검색 도구가 건너뛰어 "위키에 없음"이라는 거짓 답을 낼 수 있음(실제로 겪음). `vault_lint.py`, `.ignore`, `tests/test_vault_lint.py`. 한계: 모순·낡은 주장의 의미 판단은 사람/LLM 몫, 옵시디언 설정은 미검증 |
| D31 | 파이프라인 공정성·상한: 대기 작업이 `WIKI_MAX_PENDING_JOBS`(500)에 닿으면 `Watcher.scan_once` 가 새 파일을 큐에 넣지 않고 inbox 에 그대로 둠(`Watcher.deferred`, 경고 로그, `--once` 출력에 표시, 데이터 손실·이동 없음). `JobQueue.run_pending` 은 부서(space)별 라운드로빈으로 처리하고 서비스 루프는 `WIKI_BATCH_JOBS`(10)개씩 처리한 뒤 재스캔. `scan_once` 반환 딕셔너리 모양은 유지 | 한 부서의 대량 투입이 다른 부서를 오래 지연시키고 작업 테이블이 무한히 커지는 것을 막음. 완전한 공정 스케줄링(가중치, 부서별 상한)은 아님. `pipeline/queue.py`, `pipeline/watch.py`, `tests/test_pipeline_fairness.py` |
| D32 | 세컨드 브레인 백업·정기 점검: `llmwiki.vault_backup`(세 폴더 전체를 zip 스냅샷, 내용 digest 가 같으면 생략, 임시 파일에 쓰고 재열람 검증 후 이름 변경, N개 보관, 복원은 빈 폴더만·경로 이탈 거부, 백업 폴더는 볼트 폴더 안 금지). `scripts/run-vault-maintenance.ps1` = 백업 + `vault_lint` + 보고서, `scripts/install-vault-maintenance-task.ps1` = 현재 사용자 작업 스케줄러에 매일 21:00 등록(`-WhatIf`, `-Uninstall`). 점검은 모델을 호출하지 않음(토큰 비용 0). 자동 실행은 `vault_lint --accept` 를 절대 하지 않음 | 개인 노트는 git 이력이 없어 지우면 복구 불가. 매일 주기는 백업이 변경 없을 때 비용이 거의 없고 원본 변조를 하루 안에 알리기 위함. `vault_backup.py`, `tests/test_vault_backup.py`, `scripts/*vault-maintenance*`. 한계: PC 가 꺼져 있거나 로그오프면 다음 기회에 실행, 같은 PC 디스크의 백업이라 디스크 장애에는 약함(다른 드라이브·외장 디스크 권장), 알림은 보고서 파일뿐 |
| D33 | 두뇌(LLM) 선택·자동 전환 `llmwiki.brains`: `config/brains.json` 프로필(local/gemini/gauss/claude…, 각각 base_url·model·cost·허용 데이터 등급·키 파일·허용 호스트·custom_config·env)과 `WIKI_BRAIN=<이름>\|auto`, `WIKI_BRAIN_DATA=public\|personal\|company\|private`(기본 company). 허용되지 않은 등급은 폴백으로도 절대 보내지 않고(`BrainPolicyError`), 프로필마다 **격리된 환경**(프로세스 환경 상속 없음: 한 두뇌의 키/허용 호스트가 다른 두뇌로 새지 않음)으로 클라이언트를 만든다. auto 는 (cost, 파일 순서)로 시도하고 `LLMError`(HTTP 한도/장애/타임아웃)면 다음으로, 실패한 두뇌는 60초 쉼. `ContextExceeded` 는 전환하지 않고 호출자에게 돌려줌. 명시 선택은 전환 없음. Claude(Anthropic 형식)는 기존 `custom` 공급자의 JSON(`config/llm-claude.example.json`)으로 표현. 연결 지점은 `OpenAICompatClient.from_settings`/`llm_from_env` 의 한 줄 분기뿐(`WIKI_BRAIN` 이 없으면 기존 동작). doctor 는 WIKI_BRAIN 이 있으면 단일 클라이언트 점검 대신 `llmwiki.brains list/probe` 안내 | 사내에서 Gemini 가 무료라 두뇌를 바꿔 쓸 가능성, 데이터 등급별 외부 전송 통제, 한도·장애 시 무중단. `brains.py`, `engine/llm.py`, `doctor/checks_llm.py`, `config/brains.example.json`, `tests/test_brains.py`. 한계: 실제 Gemini/Claude/Gauss 호출은 이 PC에서 미검증(요청 형태와 클라이언트 생성만 검증), `WIKI_LLM_CONTEXT_TOKENS` 는 프로세스 전체 값, 작업하는 코딩 에이전트(Claude Code/Gemini CLI)는 이 기능과 별개(사용자가 실행하는 도구가 정함) |

새 결정은 같은 형식으로 여기에 추가한다(결정, 이유, 영향 파일).
