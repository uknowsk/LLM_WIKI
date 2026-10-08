# 사내 LLM Wiki

회사 문서(회의록, 이슈, 메일 .eml, docx/xlsx/pdf)를 LLM 이 연관된 Markdown 위키(Obsidian 호환)로 컴파일하고,
권한이 있는 사람만 자연어로 질문해 출처 인용 답변을 받는 사내 전용 지식창고. Python, 코어는 표준 라이브러리만 사용.

## 문서
- `docs/ONSITE-HANDOFF.md` : 사내 이관 런북 (완료/미완료, 설치 순서, Gauss/LM Studio 설정, SAML/OCR, 트러블슈팅)
- `docs/DEV-GUIDE.md` : 아키텍처, 데이터 구조, 확장 방법, 테스트/코드 규칙, 결정 로그
- `docs/PLAN-company-llm-wiki.md` : 기획과 확정 요구사항
- `config/env.example` : 모든 환경변수 (복사해서 `.env` 로 사용, 커밋 금지)
- `CLAUDE.md` : Claude Code 세션용 규칙 (상단 섹션)

## 빠른 시작 (Windows, PowerShell)
```
scripts\setup-venv.ps1 -IndexUrl <사내 pip 미러>   # .venv + pip install -e .[dev,prod]
copy config\env.example .env                        # 값 채우기 (비밀은 *_FILE 로)
scripts\run-doctor.ps1 -SkipLlm -SkipEmbed          # 자가진단 (이후 전체 진단)
scripts\run-tests.ps1                               # 테스트
scripts\run-web.ps1 ; scripts\run-pipeline.ps1      # 웹 서버 / inbox 파이프라인
```
운영 서비스 등록: `scripts\install-service-taskscheduler.ps1 -WhatIf` 부터. WSL/Docker 는 사용하지 않는다.

## 두뇌(LLM) 선택과 자동 전환 (Claude / Gemini / Gauss / 로컬)

위키 엔진이 호출하는 LLM 은 프로필로 고르거나 자동 전환시킬 수 있다. `config\brains.example.json` 을 `config\brains.json` 으로 복사해 채우고(키는 파일 경로만),
`WIKI_BRAIN=auto`(비용이 낮은 순서로, 오류·한도 초과면 다음 두뇌로) 또는 `WIKI_BRAIN=gemini`(그 두뇌만) 로 실행한다.
**데이터 등급 규칙**이 핵심이다: `WIKI_BRAIN_DATA=company|personal|private|public`(기본 company) 인 실행은 그 등급이 허용된 두뇌로만 가고, 허용되지 않은 두뇌(예: 사내 문서를 Gemini 로)는 폴백으로도 절대 쓰지 않는다.
확인: `python -m llmwiki.brains list`(네트워크 없음) / `probe <이름>`(실제 호출 1회). 실제 Gemini·Claude·Gauss 호출은 이 PC에서 검증하지 못했다(요청 형태와 클라이언트 생성만 자동 테스트).
작업하는 코딩 에이전트(Claude Code, Gemini CLI 등)는 별개다: 어느 도구를 실행하느냐로 정해지며, 저장소 지침은 `CLAUDE.md`/`GEMINI.md` 로 같다.

## 세컨드 브레인 (옵시디언 볼트: 이 폴더를 옵시디언에서 열기)

Karpathy 의 LLM Wiki 방식: 사람이 원본을 넣고, AI 가 위키를 만들고 유지하고, 사람은 읽고 질문한다. 위의 사내 LLM Wiki(권한 관리되는 시스템)와는 별개의 **개인용** 지식 폴더다.

| 폴더 | 역할 | 규칙 |
|---|---|---|
| `10_raw_data/` | 원본(회의록, 고객 CS, 일기, 수집 텍스트) | 저장 후 수정/삭제 금지. 파일명 `YYYY-MM-DD_제목.md` |
| `20_ai_wiki/` | AI 가 요약·재구성한 지식 문서 | 서로 `[[문서명]]` 로 연결, `_index.md`(목록)·`_log.md`(이력) 유지 |
| `30_outputs/` | 최종 산출물(블로그, 대본, 사업계획서, 뉴스레터 등) | 파일명 `YYYY-MM-DD_제목.md`, 근거 위키는 `based_on` 에 링크 |

**사용법**: Claude Code 에서 이 폴더를 열고
1. 정보를 주면 -> "이거 저장하고 위키에 반영해줘" (원본이 `10_raw_data/` 에 먼저 저장되고 위키가 갱신된다)
2. 질문하면 -> 위키를 근거로 `[[문서명]]` 을 밝혀 답한다 ("내 위키에서 X 에 대해 뭘 알고 있어?")
3. 산출물 지시 -> "위키를 바탕으로 뉴스레터 써줘" (`30_outputs/` 에 저장)
4. 가끔 -> "위키 점검해줘" (깨진 링크, 낡은 주장, 모순, 고아 문서 보고)

5. 점검 -> `python -m llmwiki.vault_lint` (루트에서 `PYTHONPATH=src`): 깨진 링크, 인덱스 불일치, **위키 숫자·날짜가 원본에 있는지**, 원본 변경·삭제, 미처리 원본, 고아·묵은 초안, 개인(private) 문서의 외부 산출물 사용을 찾는다. 원본을 새로 넣은 뒤에는 `--accept` 로 보호 기준선을 갱신한다.

각 폴더의 `_templates/` 에 양식이 있다. 운영 규칙 전문은 `CLAUDE.md` 의 "세컨드 브레인" 절.
**옵시디언 설정(미검증, 이 PC에서 옵시디언을 열어 보지 않음)**: 이 루트 폴더를 보관소로 열고, 설정 > 파일 및 링크에서 "위키링크 사용"을 켜고 새 링크 형식을 "가능한 최단 경로"로 한다. `.obsidian/` 은 git 에서 제외된다.
**백업과 정기 점검**: 세컨드 브레인 폴더는 git 이력이 없어서(개인 내용이라 커밋하지 않음) `python -m llmwiki.vault_backup backup` 으로 백업 폴더에 zip 스냅샷을 만든다(기본 저장소 옆 `second-brain-backup`, 내용이 같으면 건너뜀, 30개 보관, 복원은 `restore <zip> --into <빈 폴더>`). `scripts\install-vault-maintenance-task.ps1` 이 매일 21:00 에 백업 + 점검(`vault_lint`)을 하는 작업 스케줄러 작업을 등록하고, 결과는 백업 폴더의 `last-maintenance.txt` 에서 본다(주기 변경: `-Time 07:30`, 제거: `-Uninstall`). 모델을 호출하지 않아 비용이 없다. 백업 폴더도 평문이므로 본인만 읽게 둔다.
**주의**: 이 세 폴더의 내용은 git 에 커밋되지 않는다(템플릿만 추적). 사내 문서/비밀/개인정보는 넣지 않는다 — 사내 문서는 사내 위키 파이프라인으로.
