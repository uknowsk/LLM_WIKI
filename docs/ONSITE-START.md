# 사내 시작 지침 (clone 직후 AI 에이전트(Claude Code, Gemini 등)가 가장 먼저 읽는 문서)

이 저장소를 사내 PC 에 clone 해서 작업 폴더로 열었다면, **이 문서의 순서대로 진행**한다. 상세는 `docs/ONSITE-HANDOFF.md`(런북),
`docs/DEV-GUIDE.md`(구조/규칙), `CLAUDE.md`(불변식). 홈 세션의 메모리는 없다. 저장소가 유일한 자료다.

표기: **[검증됨]** 작성자 PC 에서 실행으로 확인 / **[미검증]** 사내에서 처음 확인. 사내 값(주소, 모델 id, AD 속성)은 작성자가 모른다. **가정으로 채우지 말고 사람에게 묻는다.**

## 0. 지켜야 할 것

1. 보안 불변식(CLAUDE.md "절대 약화하지 말 것")은 어떤 이유로도 약화하지 않는다. 보안 관련 변경은 **누수 테스트를 먼저 추가**하고 `tests/test_web_leak.py`, `tests/test_engine_leak.py` 포함 전체를 돌린다.
2. **코드는 집 -> 회사 한 방향.** 사내에서 만든 코드/문서/로그/데이터를 GitHub 등 외부로 push 하지 않는다. 사내 git 서버가 있으면 사용자 승인 후 그쪽으로만. 사내 커밋은 로컬에 둔다.
3. 비밀(키, 비밀번호, 토큰)을 **읽거나 채팅/로그/문서에 쓰지 않는다.** 키는 사용자가 파일(`*_FILE`)로 직접 만들고, 코드는 경로만 안다. 실문서 본문도 로그/커밋/문서에 남기지 않는다.
4. 질문은 **한 번에 모아서** 묻는다(아래 2단계 표). 단계 사이에 의견을 구하지 않는다. 막히면(예: AD FS 정보 없음) 무엇이 필요한지 정리해 보고하고, 막히지 않은 다음 단계로 간다.
5. 확인 못 한 것은 문서에 **검증됨/미검증**을 구분해 정직하게 쓴다. 실패한 테스트/명령 출력은 숨기지 않는다.
6. 셸은 PowerShell(`&&` 대신 `;`/`if ($?)`). 사내에는 WSL/Docker 가 없다. 파일당 500줄 미만, 파라미터화 SQL 만, 테스트는 네트워크 금지(`FakeLLM`/`FakeEmbedder`), 새 `WIKI_*` 변수는 `config/env.example` 에 추가.
7. 진행 기록은 `docs/ONSITE-LOG.md`(없으면 만든다)에 단계별로 남긴다: 날짜, 단계, 결과(PASS/FAIL 개수, 에러 **분류**), 남은 일. **비밀/내부 주소/문서 내용은 쓰지 않는다**(호스트 이름도 일반화: "Gauss 호스트").

## 1. 환경 구성 (사람 입력 불필요)

| 할 일 | 명령 | 성공 기준 |
|---|---|---|
| Python 확인 | `python --version` | 3.14 권장(3.14 만 검증됨, 3.11~3.13 미검증) |
| 가상환경 | `scripts\setup-venv.ps1 -IndexUrl <사내 pip 미러> -TrustedHost <호스트>` (미러 주소는 사람에게 묻는다) | `.venv` 생성, 설치 성공. 코어는 런타임 의존성 0 |
| 선택 의존성 | `-ExtraPackages pypdf` (PDF), waitress 는 `[prod]` | |
| 전체 테스트 | `scripts\run-tests.ps1` | 작성자 PC 기준 **944 passed, 2 skipped**(skip 은 waitress 미설치 등). 사내 수치를 기록 |
| 오프라인 진단 | `scripts\run-doctor.ps1 -SkipLlm -SkipEmbed` | FAIL 0 (WARN 은 미설정 항목) |

테스트가 사내 환경 때문에 실패하면(경로, 권한, 인코딩) 원인을 분류해 고치되 **테스트를 약화/삭제하지 않는다.**

## 2. 사람에게 한 번에 물을 것

아래를 한 메시지로 묻고, 답이 오는 대로 해당 단계를 진행한다. 답이 없는 항목의 단계는 건너뛰고 로그에 "정보 대기"로 남긴다.

| # | 질문 | 쓰이는 단계 |
|---|---|---|
| 1 | Gauss: 베이스 URL(/v1 까지), 모델 id, 컨텍스트 길이, **호출 비용/횟수 제한 여부**, 키 파일을 둘 경로(키는 사용자가 직접 파일로 만든다) | S4, S10 |
| 2 | LM Studio 에 bge-m3 가 로드되어 있고 Context Length 를 8192 로 올렸는가 | S3, S4 |
| 3 | **Doxa API**: 엔드포인트, 인증 방식, 요청 형식(PDF 통째/페이지 이미지, 동기/비동기), 응답 형식(페이지별 텍스트? 표/좌표?), 파일 크기·페이지·호출 제한, 사내 서비스인지 외부인지, **OCR 대상 문서를 보내도 되는 보안 기준** | S5 |
| 4 | AD FS: 사용 여부, SAML 앱 등록 가능 여부, 응답에 포함되는 속성(부서/그룹/UPN), 부서·파트 구분 기준, 관리자 그룹 | S6 |
| 5 | 서비스 도메인(`https://wiki.회사.도메인`), 리버스 프록시 담당/표준(IIS ARR/nginx), 프록시 IP | S8 |
| 6 | 공용 폴더(SMB) 경로, 부서 목록과 폴더 이름(한글이면 별칭), 부서별 AD 그룹 | S7 |
| 7 | 데이터 디렉터리(`WIKI_DATA_DIR`, 로컬 디스크), 서비스 계정, 로그/백업 위치 | S3, S8, S9 |
| 8 | 처음 시험할 비식별 문서 2~3개(저장소 밖 경로) | S4 |
| 9 | **Gemini**(사내에서 무료로 쓸 수 있다고 함): 접속 방식(공식 API 키 / 사내 게이트웨이 / 기타), OpenAI 호환 엔드포인트 제공 여부와 주소, 인증 방식, 사내 문서를 Gemini 로 보내도 되는 보안 등급·정책, 프록시 필요 여부, 호출 제한. 사내 문서를 보낼 수 없다면 Gemini 는 공개 자료·개인 노트에만 쓴다 | S4, 모델 선택 |

## 3. 단계별 진행 (순서대로)

**S3 설정**: `copy config\env.example .env`(프로필 A: 사내) -> 위 답으로 값 채움. 키는 `*_FILE`. `.env` 는 커밋 금지.

**S4 모델 연결**: `scripts\run-doctor.ps1`(전체) -> `-ProbeContext`. FAIL 0 목표. 이어서 시험 문서 2~3개로 `scripts\run-pipeline.ps1 -Once` -> `data\wiki` 에 문서가 생기고 `_failed` 가 없어야 한다.
think 오류/컨텍스트 초과/스트림 이상이면 ONSITE-HANDOFF (d) 표대로 `WIKI_LLM_*` 조정. 질의 때 LLM 에 넣는 문서 수/크기는 `WIKI_TOP_K`(기본 8), `WIKI_MAX_CONTEXT_CHARS`(기본 0=무제한) — Gauss 가 호출 제한/비용이 있으면 낮춘다(예: 4, 6000).

**S5 OCR (Doxa)**: 코어를 바꾸지 않고 **래퍼 스크립트**로 붙인다. 계약(`pipeline/ocr_command.py` 상단, HANDOFF b-5): `WIKI_OCR_COMMAND`/`WIKI_OCR_ARGS` 로 지정한 실행 파일이 **PDF 전체를 stdin** 으로 받아 **페이지별 UTF-8 텍스트를 form feed 로 구분해 stdout**(페이지당 정확히 1 조각)으로 출력, 실패는 비 0 종료 코드.
- 자식 프로세스는 다른 `WIKI_*` 변수를 받지 못한다 -> Doxa 키는 **키 파일 경로를 `WIKI_OCR_ARGS` 로 넘겨** 래퍼가 읽는다. 키 내용은 출력/예외에 쓰지 않는다.
- 페이지 수와 조각 수가 같아야 한다(다르면 코드가 거부). Doxa 응답이 페이지 구분을 주지 않으면 pypdf 로 페이지 수를 세어 맞추는 방법을 사람과 상의한다.
- 네트워크 호출은 사설 주소만(프로젝트 규칙). 외부 서비스면 보안팀 승인 확인 전에는 연결하지 않는다. OCR 텍스트가 이후 PII 마스킹을 거치는지 코드로 확인해 로그에 기록한다.
- 테스트는 **가짜 HTTP 서버**로(실제 Doxa 호출 금지). 래퍼는 stdlib 만, 위치는 `tools/`(새 폴더) 또는 `scripts/`. 완료 후 `python -m llmwiki.doctor --probe-ocr` 와 스캔 PDF 1개 실처리로 확인하고 HANDOFF b-5 를 갱신.

**S6 SAML**: HANDOFF b-4 의 1~8 을 따른다. XML 서명 검증은 **검증된 라이브러리**만(직접 구현 금지; 사내 pip 미러 가용성과 Windows 빌드 가능 여부 먼저 확인). 매핑은 설정 파일, 매핑 없는 사용자 = 빈 spaces. 연결 후 누수/인증 테스트 4종 + 전체. AD FS 정보가 없으면 이 단계는 "정보 대기"로 두고 계속 진행.

**S7 공용 폴더/ACL**: HANDOFF b-9 + `docs/OBSIDIAN-MANUAL.md` 3-5. 부서 폴더 = space, 쓰기 권한은 Windows ACL 로(사람이 IT 와 설정). 한글 폴더는 `WIKI_INBOX_ALIASES`. doctor `inbox.layout`/`inbox.aliases` 확인. **실제 SMB/ABE/AD 그룹은 미검증**: 서로 다른 두 부서 계정으로 읽기/쓰기 분리를 사람이 확인하도록 안내.

**S8 운영 서빙**: HANDOFF b-6. waitress 설치 후 `tests\test_web_server.py` 의 skip 해제 확인, `WIKI_ALLOWED_ORIGINS`, `WIKI_TRUSTED_PROXIES`, 프록시 헤더/타임아웃/버퍼링 합의, 작업 스케줄러 등록은 `-WhatIf` 먼저.

**S9 백업/복구**: HANDOFF b-7. 백업 1회와 **복구 리허설**(미해본 항목).

**S10 재튜닝**: HANDOFF (c). 실문서 QA 세트는 **사람이 라벨링**(저장소 밖). Gauss 호출 비용/제한을 확인한 뒤 진행. heldout 은 구성당 1회. leak != 0 이면 그 설정 폐기. 채택값은 `engine/params.py` 기본값 수정 대신 가능하면 `WIKI_TOP_K` 등 환경변수로(문서에 기록).

**두뇌 선택·자동 전환(Claude/Gemini/Gauss/로컬)**: `config\brains.example.json` 을 `config\brains.json` 으로 복사해 프로필을 채우고 `WIKI_BRAIN=auto`(비용 낮은 순 + 오류 시 자동 전환) 또는 `WIKI_BRAIN=gemini`(그 두뇌만)로 고른다. `WIKI_BRAIN_DATA=company`(기본)이면 프로필에 `company` 가 허용된 두뇌(예: gauss, local)로만 가고 Gemini/Claude 로는 **폴백으로도** 가지 않는다 — 사내 문서를 Gemini 로 보내도 된다는 승인(질문 9번)이 나온 뒤에만, **개인 키 프로필(`gemini`)이 아니라 사내에서 발급받은 별도 접속·별도 키 파일의 프로필(`gemini-corp`, 샘플에 있음)** 에 `company` 를 허용한다. 개인 키로는 사내 문서를 보내지 않는다. 허용 범위는 실행 단위(`WIKI_BRAIN_DATA`)라서 "A 부서는 되고 B 부서는 안 됨" 같은 부서별 구분은 되지 않는다(필요하면 부서별로 다른 설정으로 파이프라인을 따로 실행). 보내기 전 개인정보 마스킹(`WIKI_PII_POLICY_FILE`)을 켜는 것을 권한다(정규식 기반이라 완전하지 않다). 확인: `python -m llmwiki.brains list`(네트워크 없음), `probe <이름>`(실제 호출 1회). 아래 `WIKI_LLM_*` 단일 설정은 `WIKI_BRAIN` 을 쓰지 않을 때의 기본 방식이다.

**모델 선택(Gemini 포함)**: 위키 엔진이 쓰는 LLM 은 `WIKI_LLM_*` 환경변수로 고른다(Gauss, LM Studio, Gemini 의 OpenAI 호환 엔드포인트 등). 외부 호스트는 `WIKI_LLM_ALLOWED_HOSTS` 에 정확한 호스트 이름을 넣어야 통과하고, 프록시가 필요하면 `WIKI_LLM_PROXY` 를 명시한다(기본은 둘 다 금지). 키는 `*_FILE` 경로로만. 에이전트(지금 작업하는 AI) 가 Claude 가 아니어도 이 문서의 단계와 보안 규칙은 같고, 저장소 루트의 `GEMINI.md` 가 같은 지침 위치를 안내한다. 엔진 설정 예시는 `config/env.example` 과 `config/env.personal.example` 의 프로필 B 를 따른다.

개인 모드(`python -m llmwiki.personal`, `docs/PERSONAL-MODE.md`)는 사용자 본인 PC 용이며 중앙 서비스와 별개다. 중앙 위키를 PC 로 동기화하는 기능은 보안팀 승인 전이라 **만들지 않는다.**

## 4. 완료 판정과 보고

단계마다 `docs/ONSITE-LOG.md` 에 기록하고, 아래를 만족하면 "1차 운영 준비 완료"로 보고한다(못 한 항목은 이유와 함께 목록으로).
- 전체 테스트 통과(수치 기록), doctor FAIL 0, Gauss 로 시험 문서 처리 성공
- OCR: 스캔 PDF 1건 처리 성공 또는 정보 대기 사유
- SAML: 로그인 + 매핑 없는 사용자 거부 + 누수 테스트 통과, 또는 정보 대기 사유
- 두 부서 계정으로 space 분리 확인(사람 확인)
- 백업/복구 리허설 결과

보고 형식: 한 일 / 검증된 것 / 미검증·막힌 것(필요한 정보) / 다음에 할 일.
