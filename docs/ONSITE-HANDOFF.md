# 사내 이관 인수 런북 (ONSITE-HANDOFF)

대상: 사내에서 이 저장소를 clone 하여 개발을 이어갈 사람과 Claude Code 세션. 홈 세션의 메모리는 이관되지 않으므로
**이 문서와 `docs/DEV-GUIDE.md`, `CLAUDE.md` 상단 섹션이 유일한 인수인계 자료**이다. 제품 기획은 `docs/PLAN-company-llm-wiki.md`.

표기: **[검증됨]** = 작성자 PC 에서 실행/테스트로 확인. **[미검증]** = 코드나 문서상 그럴 것이나 실제로 확인 못함.
사내 환경 값(주소, 모델 id, AD 속성 이름 등)은 작성자가 알 수 없으므로 전부 현장 확인 대상이다.

---

## (a) 완료된 것 / 검증된 것

작성자 PC(Windows 11, Python 3.14.6)에서 마지막으로 실행한 결과: `pytest -q` **500 passed, 1 skipped**
(skip 1건은 waitress 가 설치되지 않아 건너뛴 실서버 테스트). 이 수치는 "작성자 PC 의 마지막 실행 기준"이며 사내에서 재실행해 확인해야 한다.

| 영역 | 상태 | 비고 |
|---|---|---|
| 설정 `config.py`, 인증 교체 지점 `auth.py`(AuthProvider), ACL `acl.py`, 감사 `audit.py` | 구현 + 테스트 | dev 인증은 development/test 에서만 |
| 인제스트 파서: md/txt/docx/xlsx/eml/pdf(텍스트 레이어) | 구현 + 테스트 | PDF 는 `pypdf` 가 있어야 동작 (`pip install -e .[pdf]`, 아래 b-1) |
| PII 마스킹(정규식) | 구현 + 테스트 | space별 정책은 파라미터만 있음(TODO) |
| 엔진: triage/compile/cascade, 검색(BM25+임베딩, RRF), 질의, lint(근거 검증) | 구현 + 테스트 | |
| ACL-first 검색, space 격리 컴파일 | 구현 + 누수 테스트 | `tests/test_engine_leak.py`, `tests/test_web_leak.py` |
| 파이프라인(inbox 폴더 감시, 큐, 재시도, 단일 컴파일 락) | 구현 + 테스트 | |
| 웹 서버(WSGI): 세션/CSRF/Origin/속도제한/보안헤더/업로드, 최소 UI | 구현 + 테스트 | |
| 서버: waitress(운영) / wsgiref(개발) 선택 | 구현 + 스텁 테스트 | **실제 waitress 로는 미실행** (이 PC 에 미설치) [미검증] |
| LLM 클라이언트: Bearer 키, SSE 스트리밍, think 모델(`<think>` 제거), 컨텍스트 초과 감지, 구조화출력 off, custom JSON 설정 | 구현 + 테스트(가짜 서버) | 실제 Gauss 와는 미연동 [미검증] |
| 임베딩: OpenAI 호환 /embeddings(LM Studio), custom | 구현 + 테스트 | 집의 LM Studio 로 실사용 확인 |
| doctor(`python -m llmwiki.doctor`) 자가진단 | 구현 + 테스트 | 아래 b-3 |
| 평가 하니스 `python -m llmwiki.eval` (build/eval/tune/final/report) | 구현 + 테스트 | 합성 코퍼스 기준 튜닝 결과 존재 (한계: f-3) |

**집에서 검증하지 못한 것**: Gauss 실호출, SAML/AD FS 전부, OCR 엔진, 48GB GPU 에서의 성능, 80명 동시 접속 부하,
waitress 실가동, 리버스 프록시/TLS 뒤 동작, Windows 서비스(작업 스케줄러) 실제 등록, 한국어 실문서 품질.

---

## (b) 사내에서 해야 할 일 (순서대로)

### b-1. 설치 / 환경 구성
1. 코드 가져오기: `git clone https://github.com/uknowsk/LLM_WIKI.git` (사내망에서 GitHub 접근이 막혀 있으면 승인된 미러/반입 매체 사용).
   **방향은 집 -> 회사 한 방향뿐**이다. 사내에서 만든 코드/문서/데이터를 집으로 되돌리지 않는다.
2. Python 3.14 확인(`python --version`). 3.11~3.13 은 `requires-python >=3.11` 이지만 **3.14 만 검증됨** [미검증: 3.11].
3. `scripts\setup-venv.ps1 -IndexUrl <사내 pip 미러> -TrustedHost <미러 호스트>` : `.venv` 생성 + `pip install -e .[dev,prod]`
   (dev=pytest, prod=waitress). **사내 pip 미러가 없으면 설치 불가**. 코어는 런타임 의존성이 0 이다.
4. PDF 텍스트 추출을 쓰려면 extra `pdf`(`pypdf>=4`)를 설치한다: `pip install -e .[dev,prod,pdf]` 또는 `-ExtraPackages pypdf`.
   `python -m llmwiki.pipeline` 은 `pypdf` 가 import 되면 `PypdfExtractor` 를 자동 주입하고, `WIKI_OCR_COMMAND` 가 있으면 OCR 엔진도 주입한다(b-5).
   `pypdf` 가 없으면 PDF 파일만 파일 단위로 실패한다(`pypdf is not installed`).
5. `copy config\env.example .env` 후 편집 (프로필 A: 사내). 비밀은 `*_FILE` 로. `.env` 는 커밋 금지(.gitignore 에 있음).
6. 테스트: `scripts\run-tests.ps1` -> 500 passed / 1 skipped 에 가까워야 한다. waitress 설치 후에는 skip 이 0 이 된다.

### b-2. LM Studio 모델 (같은 PC, 임베딩용)
- **필수**: `bge-m3` (모델 id `text-embedding-bge-m3`). 선택: gemma 계열(집에서 채팅 대체용으로 쓴 것; 사내 채팅은 Gauss).
- 서버 시작: LM Studio 의 Local Server 를 `127.0.0.1:1234` 로 켠다. 코드는 사설/루프백 주소만 허용(기본)하므로 127.0.0.1 은 통과한다.
- **로드 컨텍스트 길이 경고**: 집에서 관측한 LM Studio 의 로드 컨텍스트는 bge-m3 = **2048**, gemma = **8192** 였다. 이 값이 작으면
  긴 입력이 조용히 잘리거나(임베딩) 오류가 난다. 코드는 임베딩 입력을 4000 자로 자르는데(`engine/embed.py MAX_CHARS`), 한국어는
  1자 ~ 1토큰에 가까우므로 4000자 입력은 2048 토큰을 넘길 수 있다 -> **LM Studio 에서 bge-m3 의 Context Length 를 8192 로 올려 다시 로드**하라.
  올린 뒤 doctor(`b-3`)의 임베딩 항목과 `python -m llmwiki.engine.reindex` 결과(failed=0)로 확인.
- 임베딩 모델을 바꾸거나 컨텍스트 설정을 바꾼 뒤에는 `python -m llmwiki.engine.reindex` 로 벡터를 다시 채운다(멱등, 재개 가능).

### b-3. Gauss 설정 (환경변수 프로필) 과 doctor
Gauss Chat API = OpenAI 호환, Bearer 키, think(추론) 모델, 컨텍스트 64k, 스트리밍 지원, JSON schema 구조화 출력 없음, 임베딩 없음.

```
WIKI_ENV=production
WIKI_LLM_BASE_URL=<Gauss 베이스 URL, /v1 까지>       # 실제 주소는 현장 확인
WIKI_LLM_MODEL=<Gauss 모델 id>                       # 현장 확인
WIKI_LLM_API_KEY_FILE=C:\wiki-secrets\gauss.key       # 키 한 줄짜리 파일, 서비스 계정만 읽기
WIKI_LLM_STRUCTURED=off                              # response_format 을 보내지 않음
WIKI_LLM_STREAM=1
WIKI_LLM_TIMEOUT=300                                 # 스트리밍이면 소켓 읽기당 초
WIKI_LLM_MAX_TOKENS=4096                             # think 모델은 추론에 토큰을 씀: 4096 이상 (짧으면 "ended inside its reasoning" 오류)
WIKI_LLM_CONTEXT_TOKENS=65536
WIKI_EMBED_BASE_URL=http://127.0.0.1:1234/v1         # 반드시 명시: 비우면 채팅(Gauss) 주소로 임베딩을 보낸다
WIKI_EMBED_MODEL=text-embedding-bge-m3
```
- Gauss 호스트가 사내 DNS 이름이라 "사설 주소 검사"에 걸리면(`LLM endpoint must be on the intranet`) `WIKI_LLM_ALLOWED_HOSTS=<호스트>` 로 명시 허용.
  사내 CA 로 서명된 HTTPS 면 `WIKI_LLM_CA_BUNDLE=<PEM 경로>`. 프록시가 꼭 필요하면 `WIKI_LLM_PROXY`. **기본은 프록시/리다이렉트 모두 금지**.
- 채팅 키는 임베딩 호스트가 다르면 상속되지 않는다(키 유출 방지). 임베딩이 127.0.0.1 이고 채팅이 Gauss 이므로 임베딩에는 키가 안 간다. 정상.
- Gauss 요청/응답 형식이 OpenAI 호환에서 벗어나면 `WIKI_LLM_PROVIDER=custom` + `config/llm-custom.example.json` 복사본으로 맞춘다 [미검증].
- **가장 먼저 `scripts\run-doctor.ps1`**(= `python -m llmwiki.doctor`). 순서: 오프라인 `-SkipLlm -SkipEmbed` -> 전체 -> `-ProbeContext`
  (Gauss 의 실제 컨텍스트 한계를 합성 프롬프트로 탐지, 최대 약 25만 자 전송). FAIL 0 이 목표. 키/문서 내용은 출력되지 않는다.
  작성자 PC 에서 `-SkipLlm -SkipEmbed` 실행 결과: PASS 14 / WARN 10 / FAIL 0 (WARN 은 개발 모드/미설정 항목들) [검증됨].

### b-4. SAML 2.0 / AD FS 연동 (사내에서 직접 구현)
작성자는 AD FS 세부(메타데이터, 클레임 이름, 서명 방식)를 모른다. 연결 지점만 준비되어 있다.

현재 상태: `auth.get_provider()` 는 `WIKI_AUTH_PROVIDER=saml` 에서 `NotImplementedError` 를 던진다. `web/__main__.py main()` 은
`get_provider(...)` 를 호출하므로 **saml 로는 지금 서버가 뜨지 않는다**(의도된 상태). 구현할 것:

1. **`SamlAuthProvider`** (`src/llmwiki/auth_saml.py` 등 새 파일; `auth.get_provider` 의 saml 분기에서 생성).
   - AuthProvider 규약: `authenticate(credentials) -> User | None`, 잘못된 입력에 예외 금지.
   - 중요: SAML 은 브라우저 리다이렉트 흐름이라 `/login`(JSON 자격증명) 방식과 맞지 않는다. `/login` 은 saml 모드에서 비활성/무시하고 아래 ACS 를 쓴다.
2. **라우트** (`WikiApp.add_route`, `web/app.py`):
   - `GET /saml/login` : AuthnRequest 생성 + RelayState 를 서버에 저장(InResponseTo 대조용) 후 IdP 로 302.
   - `POST /saml/acs` : `app.add_route("POST", "/saml/acs", handler, auth=False, csrf=False, self_validated=True)`. 핸들러가
     **직접** 검증: XML 서명(IdP 인증서 고정), Issuer, Audience, Destination, `InResponseTo`, `NotBefore/NotOnOrAfter`(시계 오차 허용 최소),
     Assertion ID 재사용(replay) 차단, 서명 래핑 공격 방지. 성공 시 `app.start_session(req, user)` -> `grant.headers()` 를 302 응답에 넣는다
     (`start_session` 이 기존 세션 폐기 + 감사 로그 `login` 기록).
   - IdP 가 크로스사이트로 POST 하므로 `_check_origin` 이 막는다. IdP Origin 을 `WIKI_ALLOWED_ORIGINS` 에 넣거나, 이 경로만 Origin 검사를 예외 처리하는 방식 중 택일하고
     **택한 이유를 DEV-GUIDE 결정 로그에 기록**. `Request` 에는 form(urlencoded) 파서가 없다 -> `environ["wsgi.input"]` 에서 길이 상한을 두고 직접 읽어 `SAMLResponse` 를 얻는다.
   - `GET /saml/metadata` (SP 메타데이터; AD FS 에 등록할 때 필요), SLO 가 필요하면 `/saml/logout`.
3. **클레임 -> User/space 매핑**: `User(id, name, department, part, spaces, is_admin)`. AD 속성(예: `department`, `memberOf`, UPN)의 **실제 이름은 AD FS 발급 변환 규칙에 달림**.
   매핑은 코드가 아니라 설정 파일(예: `config/ad-space-map.json`)로: `그룹/부서/파트 -> space 이름(소문자, 숫자, '-', '/' 계층)`, `관리자 그룹 -> is_admin`.
   - **fail closed**: 매핑되지 않는 사용자는 `spaces = {}` (아무것도 못 봄). 클레임이 없거나 서명 검증 실패면 로그인 거부. 추정으로 space 를 부여하지 않는다.
   - space 이름 규칙은 `ingest/save.py validate_space`(세그먼트 `[a-z0-9][a-z0-9-]{0,62}`, `/` 로 계층)를 따라야 inbox/raw 폴더와 일치한다.
4. **로그아웃/폐기**: `POST /logout` 은 이미 있음(세션 삭제 + 감사). AD 에서 사용자를 비활성화/전보했을 때 즉시 끊으려면
   `app.session_auth.revoke_user(user_id)` 를 쓴다(주기 동기화 작업이나 관리자 화면에서 호출; 구현 필요). 세션 절대 만료 기본 8시간이 그룹 변경 반영 상한.
5. UI: `web/ui_js.py` 의 로그인 화면은 dev 사용자 선택기 기반이다. saml 모드에서는 "로그인" 버튼이 `/saml/login` 으로 이동하도록 수정 (`/api/login-info` 의 `provider` 값 활용 가능).
6. SAML 라이브러리: 코어는 stdlib 전용이다. XML 서명 검증은 직접 구현하지 말고 검증된 라이브러리(예: `python3-saml`/`signxml` 계열, 사내 pip 미러 가용 여부와 Windows 에서 `xmlsec` 빌드 가능 여부 확인)를 선택적 의존성으로 추가 [미검증].
   직접 만든 XML 서명 검증은 금지.
7. **다시 돌릴 테스트**(SAML 연결 후, 전부 통과해야 함): `tests/test_web_leak.py`, `tests/test_engine_leak.py`, `tests/test_web_auth.py`, `tests/test_web_hardening.py`
   (`scripts\run-tests.ps1 tests\test_web_leak.py tests\test_engine_leak.py tests\test_web_auth.py tests\test_web_hardening.py`), 이어서 전체. SAML 용 신규 테스트:
   서명 변조, 만료/미래 assertion, replay, 잘못된 Audience/InResponseTo, 매핑 없는 사용자 = 빈 spaces, 세션 고정, 크로스 space 누수.
8. 결정 필요(IT 확인): AD FS 유무, SAML 앱 등록 가능 여부, 응답에 부서/그룹 포함 여부, 부서/파트 구분 기준(department/OU/그룹명).

### b-5. OCR 연결
- 슬롯: `ingest/pdf.py` 의 `OcrEngine` 프로토콜 `ocr_page(data: bytes, page_index: int) -> str` (0 기반). `parse_pdf(data, name, extractor, ocr)` 가 텍스트 레이어가 없는 페이지에 호출.
- **구현됨**: `pipeline/ocr_command.py` 의 `CommandOcrEngine`. `WIKI_OCR_COMMAND`(외부 실행 파일 경로, 선택 `WIKI_OCR_ARGS` = JSON 문자열 배열)를
  셸 없이 실행한다(예: `WIKI_OCR_COMMAND=C:\ocr-venv\Scripts\python.exe`, `WIKI_OCR_ARGS=["C:\ocr\run.py"]`). 계약:
  - `WIKI_OCR_INPUT=pdf`(기본): **PDF 전체를 stdin** 으로 받고, **모든 페이지의 UTF-8 텍스트를 페이지 순서대로 form feed(``)로 구분해 stdout** 으로 출력(페이지당 정확히 1 조각).
    (`OcrEngine` 프로토콜은 PDF 바이트+페이지 번호만 주며 이 코드는 페이지를 이미지로 렌더링하지 않는다. 래퍼가 PDF->이미지->PaddleOCR 을 직접 수행해야 한다.)
    PDF 당 자식 프로세스는 1 회(결과를 페이지별로 재사용).
  - `WIKI_OCR_INPUT=image`: stdin 이 PNG/JPEG 한 장, stdout 이 텍스트. 호출자가 이미지 바이트를 줄 때만 쓰임(현재 파이프라인은 이미지를 주지 않음 - PDF 는 거부).
  - 자식에게 `WIKI_OCR_INPUT` 환경변수로 실제 입력 종류가 전달된다. **다른 `WIKI_*` 변수(비밀키 포함)는 자식에게 전달하지 않는다.**
  - 종료 코드 != 0, 타임아웃(`WIKI_OCR_TIMEOUT` 기본 120초), 출력 상한(`WIKI_OCR_MAX_OUTPUT` 기본 8 MiB), 비 UTF-8 출력 -> 예외(파일 `failed`로 기록). stderr 내용은 기록/반환하지 않고 바이트 수만 남긴다.
  - doctor: `ocr.command`(구성 여부), `python -m llmwiki.doctor --probe-ocr` 는 1x1 PNG(image 모드) 또는 빈 1페이지 PDF(pdf 모드)로 자가 테스트(기본은 실행하지 않음).
  - 설정 오류(없는 실행 파일, 잘못된 ARGS/숫자)는 파이프라인 **시작을 거부**한다(종료 코드 2).
- 조사 결과 권장은 **PaddleOCR 을 별도 Python 3.12 프로세스**로 실행(3.14 에서 의존성 설치가 어려울 수 있음)하는 것이나 **미검증**이다.
  구조 제안: `OcrEngine` 구현체가 하위 프로세스(`WIKI_OCR_PYTHON` 으로 지정한 3.12 인터프리터의 워커 스크립트)에 PDF 바이트/페이지 번호를 stdin 으로 넘기고 텍스트를 stdout 으로 받는다.
  타임아웃, 출력 크기 상한, 오류 시 빈 문자열이 아닌 예외(그래야 파이프라인이 실패로 기록)를 지킨다. 문서 내용을 로그에 남기지 않는다.
- 연결 위치: `pipeline/__main__.py` 가 `ocr_from_env()` 와 `PypdfExtractor` 를 `process_file` 에 넘긴다(위). 실제 PaddleOCR 래퍼는 현장에서 작성/검증(미검증).
- OCR 오인식 가능성: 원본 이미지/PDF 링크 유지(기획 Q4). 이미지 파일(png/jpg) 직접 인제스트는 현재 SUPPORTED 에 없음(`.eml .md .txt .docx .xlsx .pdf`).
- HWP/HWPX 는 미정(미구현).

### b-6. 운영 서빙 (waitress + TLS 리버스 프록시)
- 설치: `pip install -e .[prod]`. 기동: `scripts\run-web.ps1 -Server waitress` (WIKI_SERVER=waitress 도 가능). 기본값은 waitress 가 import 되면 waitress, 아니면 wsgiref.
  waitress 로 넘기는 값: `threads=WIKI_MAX_THREADS(64)`, `channel_timeout=WIKI_SOCKET_TIMEOUT(30)`, `max_request_body_size=업로드 상한+1MiB`, `ident='wiki'`.
  wsgiref 는 포화 시 즉시 503, waitress 는 큐잉한다(차이). 80명 동시 사용에서의 적정 `WIKI_MAX_THREADS` 는 부하 테스트로 정한다 [미검증]. LLM 호출이 느리므로(think 모델 수십 초) 스레드 점유 시간이 길다.
- 앱은 HTTP 만 말한다. **TLS 는 사내 리버스 프록시**(IIS ARR / nginx 등 IT 표준)에서 종단하고 `127.0.0.1:WIKI_PORT` 로 전달. `WIKI_HOST=127.0.0.1` 유지(외부 직접 접근 차단), 방화벽도 막는다.
- **`WIKI_ALLOWED_ORIGINS` 필수**(운영): 브라우저가 실제로 보는 `https://wiki.회사.도메인` 과 정확히 일치해야 한다. 프록시가 `Host` 를 바꿔도 서버는 Host 를 신뢰하지 않는다(strict_origin).
  불일치하면 로그인/업로드 POST 가 403 `csrf` 가 된다. 프록시는 `Host` 헤더를 원본 그대로 전달하고, `Origin`/`Cookie`/`X-CSRF-Token` 을 제거하지 않아야 한다.
- 쿠키는 운영에서 Secure(`__Host-` 접두) -> HTTPS 아니면 로그인이 유지되지 않는다. 프록시 최대 본문/타임아웃을 업로드 상한(50MiB)과 LLM 응답 시간(300초+)에 맞춘다(스트리밍 응답 버퍼링 끄기 [미검증]).
- **프록시 뒤 로그인 속도제한**: 기본은 `REMOTE_ADDR` 기준이라 프록시 뒤에서는 전체가 한 버킷을 공유한다. 해결: `WIKI_TRUSTED_PROXIES`(프록시의 정확한 IP, 쉼표 목록; CIDR 불가, 기본 빈 값 = 기존 동작).
  `REMOTE_ADDR` 가 이 목록에 있을 때만 `X-Forwarded-For` 를 오른쪽에서부터 읽어 **목록에 없는 첫(가장 오른쪽) 주소**를 클라이언트로 쓴다. 신뢰하지 않는 피어의 XFF, 해석 불가 값은 무시(피어 주소 사용).
  프록시는 XFF 를 덮어쓰거나 append 해야 한다(왼쪽 값은 공격자가 조작 가능).
- 서비스화: `scripts\install-service-taskscheduler.ps1 -ServiceAccount <도메인\svc-wiki> -LogDir D:\wiki-logs` (먼저 `-WhatIf`). 웹과 파이프라인을 각각 작업으로 등록,
  부팅 시작, 실패 시 재시작, 일별 로그 + 보존일 삭제. 서비스 계정 권한(저장소 읽기, 데이터/로그 쓰기, 비밀 파일 읽기)만 부여. [작성자 PC 에서 파싱만 확인, 실제 등록은 미검증]
  대안 NSSM 은 IT 승인 후 직접 반입(이 저장소는 다운로드하지 않음), 래핑 대상 명령은 스크립트 헤더 참고.

### b-7. 폴더 구성 / 백업 / 복구
```
<WIKI_DATA_DIR>\
  inbox\<space>\...        드롭 폴더 (웹 업로드도 여기로). _done _rejected _failed 는 예약 폴더
  raw\<space>\YYYY-MM-DD-slug.md    불변 원본(PII 마스킹이 켜져 있으면 마스킹본)
  wiki\<topic>\*.md , wiki\_meta\<space>\index.md|log.md   컴파일 결과(Obsidian 호환)
  wiki.db          SQLite(WAL): raw_files articles article_sources article_revs article_embeddings audit pipeline_jobs pipeline_sources
  sessions.db      세션(쿠키 sid 의 sha256 만 저장)
```
- 로컬 디스크에 둔다(네트워크 드라이브/OneDrive 는 WAL 문제; doctor `fs.wal`).
- **백업**: 서비스 정지(또는 `sqlite3 .backup`/VACUUM INTO 로 일관 스냅샷) 후 `WIKI_DATA_DIR` 전체 복사. wiki.db 와 raw/wiki 파일은 **한 시점**이어야 한다. `sessions.db` 는 백업 불필요(복구 후 전원 재로그인).
  권장: 매일 야간, 보존 N일, 백업본의 접근 권한도 space ACL 만큼 엄격하게(원본이 평문이다).
- **복구**: 서비스 정지 -> 데이터 폴더 교체 -> `scripts\run-doctor.ps1` -> `python -m llmwiki.engine.reindex`(벡터 누락 시) -> 기동. **복구 리허설은 아직 해본 적 없다** [미검증].
- git 기반 위키 이력/롤백(기획 Phase 4)은 **미구현**.

### b-8. AD 연동 / space 매핑 / PII / 감사
- space 계층은 부서 -> 파트(`dept-a`, `dept-a/part-1`). User.spaces 는 **정확한 space 이름 집합**이다(상위 space 가 하위를 자동 포함하지 않음 [코드 확인: `acl.can_read` 는 `doc_spaces <= user.spaces`]).
  부서장이 파트 문서도 보려면 매핑에서 하위 space 를 명시적으로 모두 부여해야 한다.
- 문서 열람 규칙: 출처 raw 들의 space **모두**를 보유해야 열람(가장 제한적인 교집합). 컴파일은 같은 space 안에서만 병합.
- **space별 PII 마스킹 정책**: 코드는 `process_file(..., mask_policy={space: bool})`(가장 가까운 상위 항목 우선) 파라미터를 지원하지만 **설정 파일에서 읽어오는 부분이 없다(TODO)**.
  현재는 `WIKI_MASK_PII` 전역 기본값만 CLI 경로에서 효과가 있다. 해야 할 일: 설정 파일 로드 -> `pipeline/__main__.py` 에서 `mask_policy` 전달. 마스킹 전 원본 보존 정책(기획 Q7)은 미구현.
- **감사 로그 보존**: `audit` 테이블은 무한 증가하며 보존/삭제/내보내기 정책이 없다(TODO). 웹은 반복 거부를 합쳐 쓰고 길이를 자르지만(`web/limits.py`) 정리 작업은 없다. 정책(보존 기간, 관리자 조회, 백업 포함 여부)을 정해 구현.
  `GET /api/audit` 는 `is_admin` 사용자만(403 아니면), 최근 일정 개수만 반환하고 조회 자체를 감사 기록한다. 필터/기간/내보내기는 없다.

### b-9. 공용 폴더(SMB 공유) 입력
- 직원 입력 경로는 **부서별 하위 폴더가 있는 공유 폴더**다. 부서 = 폴더, 쓰기 권한 = Windows(NTFS/공유 + AD 그룹). 파일 소유자/내용/AI 로 부서를 정하지 않는다(그래서 "모두가 쓰는 폴더 하나"는 미지원).
  inbox 루트의 파일은 계속 `_rejected` 로 간다. 설정 레시피(PowerShell, **[미검증: 이 PC에서 실행하지 않음]**), 직원/관리자 사용법은 `docs/OBSIDIAN-MANUAL.md` 3-5.
- 한글 폴더 이름: `WIKI_INBOX_ALIASES`(JSON, 64KiB 이하). 잘못된 파일이면 파이프라인이 시작을 거부한다. 확인: doctor `inbox.aliases`, `inbox.layout`.
- 파이프라인이 하는 일: 잠금 파일(`~$`)/`Thumbs.db`/`desktop.ini`/`.lnk`/숨김·시스템 파일은 조용히 무시, 열려 있거나 커지는 파일은 다음 스캔으로 미룸(실패 횟수 아님),
  심볼릭 링크/정션은 따라가지 않고 보고, 거부·최종 실패 시 같은 폴더에 `<이름>.처리결과.txt` 작성(사유 분류와 조치만, 내용 없음; 성공하면 삭제).
- **검증됨(이 PC, 합성 데이터 자동 테스트)**: 위 동작 전부와 NTFS 정션 미추적. **미검증**: 실제 SMB 공유/ABE/AD 그룹 ACL, 클라이언트(맥 등)의 NFD 폴더 이름, 서비스 계정 권한, 대용량 복사 중 동작.
- 잠긴 파일: 공유 위반(winerror 32/33)은 실패 횟수에 넣지 않고 다음 스캔에 재시도하되, 크기·수정시간이 그대로인 채 `WIKI_LOCKED_MAX_SCANS`(기본 60)회 연속 잠겨 있으면 포기(`_failed` + 메모 + 감사 기록). 접근 거부(winerror 5/EACCES)는 일반 실패로 3회 후 `_failed` + 메모. 위 구분은 Windows 의 winerror 값에 의존하므로 **실제 SMB 환경에서 미검증**(테스트는 winerror 를 주입한 예외로 확인).
- 보강(보안 검토 반영): 폴더 깊이 6단계 상한 + 서비스 루프 예외 보호(백오프), 긴 파일 이름(>200) 거부와 `_done/_rejected/_failed` 이름 220자 절단, 이동/사유 파일 쓰기 실패는 파이프라인을 죽이지 않음,
  `_` 로 시작하는 루트 폴더는 대소문자 무관 예약, 중첩 별칭은 상위 별칭의 부서 안에서만 허용, `process_file(verify_folder=True)` 가 큐의 space 를 폴더에서 다시 계산해 불일치를 거부(시작 시 대기 중 작업 폐기),
  실패 사유에는 예외 클래스 + 분류만 저장(원문 예외 텍스트 없음), `.processed.txt` 는 덮어쓰지 않음, 이동 불가로 포기한 파일은 크기/수정시간이 바뀔 때까지 재큐하지 않음(프로세스 메모리, 재시작 시 한 번 더 시도).
  개인 모드(`personal/`)는 고정 space 라 `verify_folder` 기본값 False 로 호출된다(중앙 파이프라인 `__main__` 만 True).
- `_rejected/` `_failed/` `_done/` 는 관리자 전용(모든 부서의 파일 이름·사유가 섞임). 조용히 무시되는 이름 패턴(`~$`, `.tmp`, `.part`, `.lnk`, `.처리결과.txt`, `.` 시작, 숨김/시스템)의 정상 파일은 처리되지 않으니 직원에게 알릴 것.
- 미해결: 연결 폴더 검사~읽기 사이 TOCTOU(Windows 권한으로 완화, 링크 생성에 SeCreateSymbolicLink 필요), 폴더/항목 수·큐 길이 상한 없음(순차 처리라 대량 투입이 다른 부서를 지연시킬 수 있음), 포기 플래그는 메모리 보관.
- 알려진 한계: `HR` 같은 영문 별칭 키는 정식 space 이름과 겹쳐 거부된다. 별칭 폴더의 하위 폴더는 별칭 space 를 상속한다(좁히려면 별칭 추가).
---

## (c) RAG 재튜닝 절차 (사내)

원칙: 검색(retrieval) 파라미터는 임베딩 모델(bge-m3)과 코퍼스 성격에 의존하므로 **집에서 얻은 값이 상당 부분 이전될 가능성이 있지만 보장은 없다**. 생성(generation) 파라미터
(temperature, 컨텍스트 길이, 프롬프트 크기 등)는 **채팅 모델에 의존하므로 Gauss 로 반드시 다시 튜닝**한다. 집의 합성 코퍼스 결과는 참고용일 뿐이다(f-3).

1. **실문서 소규모 QA 세트 만들기** (사람이 라벨링): 비식별 처리 가능한 실제 문서 50~100건 + 질문 30~50개. 코퍼스는 저장소 **밖**에 두고(실문서/실질문은 커밋 금지) 다음 구조:
   `<corpus>\<space>\파일들(.md/.txt/.docx/.xlsx/.eml/.pdf)` (최상위 폴더가 space).
2. `qa.jsonl` 한 줄 JSON 스키마(`eval/qa.jsonl` 의 기존 예시 참고): `id, space, question, type, gold_docs[], gold_facts[], gold_answer, split("tune"|"heldout"), leak_facts[]`
   - `type`: lookup | paraphrase | numeric | multi_doc | latest | table_merged | unanswerable | cross_space
   - `gold_docs`: 정답 근거 문서(코퍼스 상대경로, 예 `dept-a/x.md`). `gold_facts`: **사람이 확인한** 정답에 반드시 들어가야 할 문구/숫자. `unanswerable`/`cross_space` 는 근거 없음/타 space(답하면 안 됨).
   - `cross_space` 질문의 `leak_facts` 에는 **다른 space 문서에만 있는 고유 사실**을 넣는다. 답변에 나오면 누수로 판정.
   - **heldout 은 튜닝에 쓰지 않는다**: 질문의 약 20~30% 를 `split":"heldout"` 으로 미리 고정하고 `tune` 단계에서는 보지 않는다(코드가 강제: `tune` 은 `load_qa(..., "tune")` 만 사용).
   - 정답 라벨은 LLM 이 아니라 사람이 만든다(집의 합성 QA 는 한 명의 목소리로 생성되어 편향됨).
3. 명령 (저장소 루트, `.env` 로드된 셸에서; `scripts` 는 아래 명령을 감싸지 않으므로 직접 실행):
   ```
   python -m llmwiki.eval build  --corpus D:\eval\corpus --out D:\eval\snap          # 실제 파이프라인으로 인제스트(Gauss 호출, 재개 가능)
   python -m llmwiki.eval eval   --data D:\eval\snap --qa D:\eval\qa.jsonl --split tune --mode retrieval
   python -m llmwiki.eval tune   --data D:\eval\snap --qa D:\eval\qa.jsonl --stage retrieval  --trials 200
   python -m llmwiki.eval tune   --data D:\eval\snap --qa D:\eval\qa.jsonl --stage generation --from-results D:\eval\results\tune-retrieval.jsonl --results D:\eval\results
   python -m llmwiki.eval final  --data D:\eval\snap --qa D:\eval\qa.jsonl --config D:\eval\results\best-generation.json   # heldout 은 구성당 1회만 (가드: heldout_used.json)
   python -m llmwiki.eval report --results D:\eval\results --out D:\eval\report.md
   ```
   (`--results` 를 저장소 밖으로 주는 것을 권장. 기본값 `eval/results` 에는 LLM 응답 캐시가 쌓인다.) 종료 코드 3 = **누수 감지**.
4. 합격 기준: **leak = 0 (하드 실패, 하나라도 있으면 해당 설정 폐기)**, refusal_accuracy(`unanswerable`/`cross_space`), citation_accuracy, fact_recall, correct. 기준 수치는 사용자와 합의(기획 Phase 2).
   tune 점수와 heldout 점수의 gap 이 크면 과적합이다.
5. 결과를 환경에 반영: 채택한 `RetrievalParams` 값은 현재 **코드 기본값(`engine/params.py`)** 으로만 존재하고 환경변수/설정 파일 로드가 없다 -> 채택 시 `params.py` 기본값 수정 또는 로딩 방법 추가(결정 로그에 기록).
6. Gauss 호출량: tune/generation 은 수백 번 호출한다. 사내 사용량/요금/속도 제한을 확인하고, `eval` 의 LLM 응답 캐시(`llm_cache.sqlite`)를 활용(같은 입력은 재호출 안 함).

---

## (d) 문제 해결 표 (doctor 메시지 기준)

| doctor 항목 / 메시지 | 원인 | 조치 |
|---|---|---|
| `env.secret` FAIL: WIKI_SESSION_SECRET 미설정/짧음 | 운영 모드는 32자 이상 필수 | `.env` 에 32자+ 무작위 값 |
| `env.origins` FAIL: WIKI_ALLOWED_ORIGINS 미설정 | 운영 필수 | `https://wiki.회사.도메인` (경로 없음) |
| 웹 로그인/업로드가 403 `csrf` | Origin 불일치(프록시 도메인 != ALLOWED_ORIGINS), 프록시가 Origin/쿠키 제거 | 정확한 Origin 등록, 프록시 헤더 확인 |
| `web.config` FAIL | 환경변수 형식(정수/불리언) 오류 | 메시지의 변수명 확인 |
| `auth.provider` WARN: SAML 미구현 | b-4 미완료 | SAML 구현 전에는 운영 로그인 불가 |
| `fs.wal` WARN | 네트워크/동기화 폴더 | `WIKI_DATA_DIR` 를 로컬 디스크로 |
| `fs.data` FAIL 쓰기 불가 | 서비스 계정 권한 | 폴더 ACL |
| `llm.config`: `LLM endpoint must be on the intranet` | 호스트가 사설 주소로 해석되지 않음 | `WIKI_LLM_ALLOWED_HOSTS` (이름만, 포트 없음) |
| `llm.call`/`llm.auth`: HTTP 401/403 | 키 오류/헤더 형식 | `WIKI_LLM_API_KEY_FILE` 내용(개행 없이 한 줄), custom 이면 헤더 템플릿 |
| `llm.structured`/`llm.call`: HTTP 400 (response_format) | 구조화 출력 미지원 | `WIKI_LLM_STRUCTURED=off` |
| `LLM reply ended inside its reasoning (<think>)` | think 모델이 토큰/시간 부족 | `WIKI_LLM_MAX_TOKENS` 4096+, `WIKI_LLM_TIMEOUT` 300+, `WIKI_LLM_STREAM=1` |
| `LLM context window exceeded (n_ctx=...)` | 프롬프트가 컨텍스트 초과 | `WIKI_LLM_CONTEXT_TOKENS` 를 실제 값으로(검색 예산 조정), `WIKI_COMPILE_MAX_CHARS` 낮춤, `--probe-context` |
| `llm.ctxsetting` WARN 기본 8192 가정 | CONTEXT_TOKENS 미설정 | Gauss 는 65536 |
| 스트림 검사 실패/중단 | 프록시 버퍼링, 서버가 SSE 미지원 | `WIKI_LLM_STREAM=0` 로 비교, 게이트웨이 설정 |
| `embed.call`/`embed.endpoint` FAIL 연결 | LM Studio 서버 꺼짐/포트 | 127.0.0.1:1234 서버 시작, 모델 로드 확인 |
| 임베딩이 Gauss 주소로 감 | `WIKI_EMBED_BASE_URL` 미설정 | 127.0.0.1:1234/v1 명시 (doctor 가 경고) |
| 임베딩 HTTP 400/오류, 긴 문서만 실패 | LM Studio 로드 컨텍스트(2048) 초과 | bge-m3 컨텍스트를 8192 로 올려 재로드 후 reindex |
| `ocr.slot` WARN | OCR 미구성 | b-5 |
| PDF 처리 시 `pypdf is not installed` | 선택 의존성 누락 | `setup-venv.ps1 -ExtraPackages pypdf` + 추출기 주입(b-1) |
| 파일이 `inbox\_rejected` 로 이동 | space 폴더 밖/잘못된 space 이름 | `<inbox>\<space>\` 아래에 두기, `<name>.reason.txt` 확인 |
| 파일이 `inbox\_failed` | 3회 재시도 실패(LLM 오류 등) | `.reason.txt`, 로그 확인, 원인 해결 후 파일을 inbox 로 되돌림 |
| 질의가 항상 "근거 없음" | 접근 가능한 문서 없음/매핑 오류, 임베딩 없음 | `User.spaces` 확인, 문서 space 확인, reindex |
| 503 `busy` | wsgiref 동시 스레드 한도 | waitress 사용 / `WIKI_MAX_THREADS` |
| 429 `rate_limited` | 속도 제한 | `WIKI_RATE_USER_PER_MIN`/`WIKI_RATE_LOGIN_PER_MIN` (프록시 뒤 문제: b-6) |

---

## (e) 절대 약화하면 안 되는 보안 불변식

| 불변식 | 구현 위치 | 검증 테스트 |
|---|---|---|
| ACL 먼저: 권한 없는 문서는 읽기/토큰화/점수 계산/LLM 컨텍스트에 절대 들어가지 않는다 | `engine/retrieval.py`, `engine/query.py`, `acl.py`, `engine/store.py(_READABLE_SQL)` | `tests/test_engine_leak.py`, `tests/test_engine_retrieval.py`, `tests/test_engine_security.py` |
| Fail closed: space 라벨 없는 문서 열람 불가, 출처가 없는 문서 열람 불가, 설정 오류 시 거부(운영 기본), 매핑 없는 사용자는 빈 spaces | `acl.can_read`, `config.load_settings`(WIKI_ENV 기본 production) | `tests/test_foundation*.py`, `tests/test_web_auth.py` |
| space 간 병합 금지: 컴파일은 같은 space 문서만 후보로 보고 수정 | `engine/compile.py` | `tests/test_engine_compile*.py`, `tests/test_engine_leak.py` |
| 혼합 출처 문서는 모든 출처 space 를 가진 사용자만 | `acl.can_read` (부분집합 규칙) | `tests/test_foundation.py`, leak 테스트 |
| dev 인증은 development/test 에서만 | `auth.get_provider`, `WikiApp.__init__` | `tests/test_web_auth.py`, `tests/test_foundation_hardening.py` |
| 비밀/문서 본문은 로그·오류·doctor 출력에 남지 않는다 (키는 `Secret`, repr 에 값 없음) | `engine/providers_http.py`, `web/app.py`(쿼리스트링/본문 미기록) | `tests/test_providers_*.py`, `tests/test_web_hardening.py`, `tests/test_doctor_run.py` |
| 프록시/리다이렉트 기본 금지, 사설 주소만 허용(명시 opt-in 만 예외) | `engine/llm.py _check_endpoint`, `providers_http.NetPolicy/NoRedirect` | `tests/test_providers_net.py`, `tests/test_engine_hardening.py` |
| 세션: 서버측 저장(sid 해시), CSRF 토큰, Origin 검사(운영은 Host 불신), 고정 방지, 절대/유휴 만료 | `web/sessions.py`, `web/app.py` | `tests/test_web_auth.py`, `tests/test_web_hardening.py` |
| 업로드는 inbox/<space>/ 에만, 파일명은 경로로 쓰지 않음 | `web/upload.py`, `pipeline/inbox.py` | `tests/test_web_hardening.py`, `tests/test_pipeline_flow.py` |

SAML/OCR/기능 추가 후에는 반드시 전체 테스트 + 누수 테스트를 다시 돌린다. 프롬프트 인젝션(문서 안의 지시문)은 원문을 데이터로만 취급하는 설계를 유지한다.

---

## (f) 알려진 한계 / 미해결 이슈

1. **하이브리드 검색은 "모른다"를 낼 수 없다**: BM25 는 항상 상위 결과를 돌려주므로(검색 단계에서 거절 불가) 근거 없는 질문은 LLM 의 거절 판단에 의존한다. 거절 정확도는 생성 모델/프롬프트 의존.
2. **컴파일 경로에는 컨텍스트 초과 재시도가 없다**: 질의 경로는 초과 시 1회 재시도하지만 컴파일은 `ContextExceeded` 시 입력 상한(`WIKI_COMPILE_MAX_CHARS`)에 의존.
3. **합성 평가 코퍼스 편향**: 집에서 튜닝에 쓴 코퍼스/QA 는 생성된 것이고 소규모, 한 가지 문체. 실사용 정확도를 뜻하지 않는다. 사내 재튜닝 필수(c).
4. **Python 3.11 미검증**: `requires-python >=3.11` 이나 3.14 에서만 테스트(코드에 3.12+ 의 `math.sumprod` 대체 경로는 있음).
5. **끊긴 스트림 수용**: SSE 읽기는 `[DONE]` 없이 **정상 EOF** 로 끝나도(`iter_sse_lines` 가 EOF 에서 그냥 종료) 지금까지 받은 텍스트를 완성 답변으로 받아들인다. 서버/프록시가 연결을 깔끔히 닫으면 잘린 답변이 정상 답변으로 처리될 수 있다. (소켓 오류/타임아웃은 `LLM stream interrupted` 예외로 처리됨.) 수정 시 `finish_reason` 확인 또는 `[DONE]` 필수화를 검토 [코드로 확인됨, 실서버에서의 빈도는 미검증].
6. **Gauss/SAML/OCR/GPU 성능/80명 부하/실제 waitress/TLS 프록시: 모두 미검증**(a).
7. 위키 git 이력/롤백, 관리자 화면, 백업 자동화, HWP: 미구현. 감사 로그는 **기본 무기한 보존(자동 삭제 없음)**; 필요 시 관리자가 `python -m llmwiki.audit_admin prune --days N --yes`(N>=30, `audit_prune` 행 기록)
   와 `export --since ISO --out file.jsonl` 사용. 웹의 감사 조회 API 는 읽기 전용 그대로.
8. (해결됨, 아래 f-A) 프록시 뒤 로그인 속도제한: `WIKI_TRUSTED_PROXIES`.
9. 단일 프로세스 가정: 세션/속도제한/검색 캐시가 프로세스 메모리 또는 SQLite. 웹을 여러 프로세스로 늘리는 것은 검증되지 않았다(컴파일 락 `COMPILE_LOCK` 도 프로세스 내부 락; 웹과 파이프라인은 별도 프로세스로 같은 SQLite 를 WAL 로 공유).
10. RetrievalParams 채택값을 환경/설정에서 읽는 경로 없음(c-5).
11. (해결됨) `eval/results/*.sqlite` 는 .gitignore 에 추가됨.
12. (해결됨, f-A) 파이프라인 CLI 의 PDF 추출기/OCR 주입.
13. **SAML 제공자는 현장에서 작성**: ACS 핸들러는 `add_route(..., self_validated=True)` + `Request.read_form()` 사용(DEV-GUIDE 4-3). 서명/InResponseTo/시간 창/재생 검증은 핸들러 책임.

### f-A. 이번에 해결된 항목 (환경변수는 config/env.example 7번 섹션)
- extra `pdf = ["pypdf>=4"]`; 파이프라인 CLI 가 PypdfExtractor 자동 주입.
- `WIKI_OCR_COMMAND` 외부 OCR 연결(`CommandOcrEngine`, b-5) + doctor `ocr.command`/`--probe-ocr`.
- `WIKI_TRUSTED_PROXIES`: 프록시 뒤 클라이언트 주소 기반 로그인 속도제한(b-6).
- `Request.read_form(max_bytes)`: SAML ACS 용 폼 본문 리더.
- `WIKI_PII_POLICY_FILE`: space별 PII 마스킹 정책(JSON, 가장 가까운 상위 space 우선, 없으면 `WIKI_MASK_PII`). 파일이 읽히지 않거나 잘못되면 **파이프라인 시작 거부**(조용히 마스킹이 꺼지지 않음). doctor `pii.policy`.
- 감사 로그 보존 도구 `llmwiki.audit_admin`(prune/export). 자동 호출 없음, 웹 계층에서 접근 불가.

---

## (g) 첫날 체크리스트

- [ ] clone, `scripts\setup-venv.ps1`(사내 미러) 성공, `scripts\run-tests.ps1` 통과 수 기록
- [ ] LM Studio: bge-m3 로드, **컨텍스트 8192 이상**, 서버 127.0.0.1:1234
- [ ] `.env` 작성(프로필 A), 키는 `*_FILE`, `.env`/키 파일 권한 제한
- [ ] `scripts\run-doctor.ps1 -SkipLlm -SkipEmbed` -> 전체 -> `-ProbeContext`; FAIL 0
- [ ] Gauss 로 소량(문서 2~3개) `scripts\run-pipeline.ps1 -Once` -> `data\wiki` 에 문서 생성, `_failed` 없음 확인
- [ ] 모델 동작 확인: think 오류/컨텍스트 오류/스트림 이상 없음, 필요 시 `WIKI_LLM_*` 조정
- [ ] `pip install waitress` 확인(`[prod]`) 후 `scripts\run-web.ps1 -Server waitress` + `tests\test_web_server.py` 의 skip 해제 확인
- [ ] 리버스 프록시 담당과 Origin/Host/TLS/타임아웃/버퍼링 합의, `WIKI_ALLOWED_ORIGINS` 설정
- [ ] IT 에 AD FS/SAML 질문(b-4-8) 전달
- [ ] 첫 SAML 로그인 전까지 운영 사용자를 받지 않는다(dev 로그인은 production 에서 불가)
- [ ] 백업 1회 + 복구 리허설 계획
- [ ] 실문서 소규모 QA 세트 준비 시작(c)
