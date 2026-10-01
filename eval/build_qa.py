"""Build eval/qa.jsonl (100 questions). Run: .venv/Scripts/python eval/build_qa.py"""
import json
import os
import random

ROOT = os.path.dirname(os.path.abspath(__file__))
A = "eval/corpus/dept-a/"
B = "eval/corpus/dept-b/"
REFUSE = "dept-b 소속 문서의 내용이므로 dept-a 사용자에게는 답변할 수 없다(접근 권한 없음)."
NOANS = "제공된 dept-a 문서에서는 해당 정보를 찾을 수 없다."

Q = []  # (type, space, question, gold_docs, gold_facts, gold_answer, extra)


def add(t, q, docs, facts, ans, space="dept-a", **extra):
    base = B if space == "dept-b" else A
    Q.append((t, space, q, [base + d if "/" not in d else d for d in docs], facts, ans, extra))


# ------------------------------------------------------------------ lookup (12 a + 2 b)
add("lookup", "db-prd-01 디스크 용량 부족 장애의 장애 등급은 몇 등급이었나요?",
    ["incident-disk-full-db01-20260312.md"], ["2등급"], "2등급이었다.")
add("lookup", "VPN 접속 장애 때 임시로 안내한 대체 게이트웨이 이름은 무엇인가요?",
    ["incident-vpn-gateway-20260619.md", "email-vpn-notice-20260619.md"], ["vpn-gw-02"], "대체 게이트웨이는 vpn-gw-02이다.")
add("lookup", "누리메일 서버 이전의 담당자는 누구로 정해졌나요?",
    ["meeting-weekly-20260713.md"], ["이하준"], "담당자는 이하준(보조 한지우)이다.")
add("lookup", "방화벽 정책 변경 작업의 변경 관리 번호가 무엇인가요?",
    ["change-notice-fw-20260925.md"], ["CR-0925"], "변경 관리 번호는 CR-0925이다.")
add("lookup", "10월 서버 OS 패치 작업의 완료 목표 일자는 언제인가요?",
    ["change-notice-patch-20261010.md"], ["2026-10-11"], "완료 목표는 2026-10-11(일) 04:00이다.")
add("lookup", "UPS 배터리 교체 작업을 수행한 협력업체는 어디인가요?",
    ["change-notice-ups-20260802.md"], ["에너지솔"], "협력업체는 에너지솔이다.")
add("lookup", "SIEM 구축 프로젝트의 PM은 누구인가요?",
    ["project-status-siem-202609.md"], ["윤가람"], "PM은 윤가람이다.")
add("lookup", "해든DB 라이선스 갱신 견적을 보낸 해든소프트의 영업 담당자 이름은?",
    ["email-license-renewal-20260828.md"], ["정민호"], "영업 담당자는 정민호이다.")
add("lookup", "3분기에 프린터 서버 스풀러 문제는 어떤 방식으로 해결했나요?",
    ["issue-list-2026q3.md"], ["재시작 스크립트"], "매일 새벽 재시작 스크립트를 적용하였다.")
add("lookup", "장애 대응 절차서의 에스컬레이션 체계에서 팀장 위의 보고 대상 본부장은 누구인가요?",
    ["procedure-incident-response.md"], ["임수정"], "본부장은 임수정이다.")
add("lookup", "nas-bk-03 백업 스토리지는 어느 센터에 있나요?",
    ["incident-disk-full-nas03-20260814.md"], ["부산센터"], "부산센터에 있다.")
add("lookup", "무선 AP 교체 사업의 담당자는 누구인가요?",
    ["project-status-wifi-refresh-202609.md"], ["정유나"], "담당자는 정유나이다.")
add("lookup", "2027년 상반기 신입 공채의 서류 접수 마감일은 언제인가요?",
    ["meeting-hr-20260915.md"], ["2026-11-06"], "서류 접수 마감일은 2026-11-06이다.", space="dept-b")
add("lookup", "미사용 연차는 언제 소멸하나요?",
    ["policy-annual-leave.md"], ["12월 31일"], "해당 연도 12월 31일에 소멸한다.", space="dept-b")

# ------------------------------------------------------------------ paraphrase (16)
add("paraphrase", "새벽에 영업 쪽에서 주문을 입력하지 못하게 만든 서버가 어디였고, 정상으로 돌아오기까지 얼마나 걸렸나요?",
    ["incident-disk-full-db01-20260312.md"], ["db-prd-01", "45분"], "db-prd-01이며 복구에 45분이 걸렸다.")
add("paraphrase", "재택근무자들이 사내망에 들어오지 못했던 사고의 근본 이유는 무엇이고 언제 그 이유가 생겼나요?",
    ["incident-vpn-gateway-20260619.md"], ["인증서", "2026-06-18"], "SSL 인증서 유효기간이 2026-06-18에 끝났기 때문이다.")
add("paraphrase", "본사 건물 전체 네트워크가 갑자기 멈춰 200여 명이 일을 못 했던 일의 원인은 무엇인가요?",
    ["incident-network-core-sw-20260707.md"], ["메모리 누수"], "스위치 펌웨어 버그로 인한 메모리 누수 때문이다.")
add("paraphrase", "월말 정산 때 그룹웨어가 느려지는 것을 막으려고 정한 점검 규칙은 무엇인가요?",
    ["incident-was-oom-20260903.md"], ["하루 2회"], "월말 정산 기간에 WAS 상태를 하루 2회 점검한다.")
add("paraphrase", "로그를 모으는 장비에 오래된 기록이 쌓여 공간이 부족해진 건 어떤 설정을 빠뜨렸기 때문인가요?",
    ["incident-disk-full-log02-20260521.md"], ["logrotate"], "신규 에이전트에 logrotate 설정을 배포하지 않았기 때문이다.")
add("paraphrase", "전산실에서 정전 없이 축전지를 새것으로 바꾸는 데 들어간 돈은 얼마인가요?",
    ["change-notice-ups-20260802.md"], ["1,850만원"], "교체 비용은 1,850만원이다.")
add("paraphrase", "해킹 감시 체계 구축이 계획보다 늦어지고 있는 가장 큰 이유는 무엇인가요?",
    ["project-status-siem-202609.md"], ["레거시 장비 17종"], "레거시 장비 17종의 로그 형식이 표준과 달라 파서 개발이 추가로 필요했기 때문이다.")
add("paraphrase", "휴대폰 인증 같은 추가 확인 절차를 반드시 거쳐야 하는 계정 유형은 무엇인가요?",
    ["policy-password-2026.md"], ["관리자 계정과 VPN 접속 계정"], "관리자 계정과 VPN 접속 계정에 MFA가 필수이다.")
add("paraphrase", "바깥에서 들어오는 원격 로그인 허용 주소 범위가 몇 개에서 몇 개로 줄었나요?",
    ["change-notice-fw-20260925.md"], ["12개에서 8개"], "외부 접속 허용 IP 대역이 12개에서 8개로 축소되었다.")
add("paraphrase", "신규 입사자 계정 발급이 늦어지는 문제는 지금 해결된 상태인가요?",
    ["issue-list-2026q3.md"], ["미해결"], "ISS-306은 미해결 상태이다.")
add("paraphrase", "백업 사본을 더 오래 두기로 한 근거가 된, 감염 후 문제를 알아차리기까지 걸리는 평균 기간은?",
    ["email-backup-policy-20260611.md"], ["20일"], "평균 20일이 걸린다는 자료를 근거로 하였다.")
add("paraphrase", "DB를 클라우드로 옮기면 데이터베이스 제조사에 추가로 내야 하는 돈은 얼마인가요?",
    ["email-license-renewal-20260828.md"], ["2,300만원"], "추가 비용은 약 2,300만원이다.")
add("paraphrase", "이중화 서버가 대신 요청을 떠맡아 그룹웨어가 느려졌던 사고는 얼마 만에 수습되었나요?",
    ["incident-was-oom-20260903.md"], ["18분"], "18분 만에 복구되었다.")
add("paraphrase", "백업 저장소가 꽉 찬 이유로 지목된 사본 보관 설정은 몇 일에서 몇 일로 바뀌었나요?",
    ["incident-disk-full-nas03-20260814.md"], ["90일", "60일"], "스냅샷 보관 기간이 90일에서 60일로 단축되었다.")
add("paraphrase", "당직 전화가 울리면 얼마 안에 받아야 하나요?",
    ["policy-oncall-duty.md"], ["10분 이내"], "10분 이내에 응답해야 한다.")
add("paraphrase", "새로 들어온 사원은 입사 후 언제부터 야간 대기 순번에 넣나요?",
    ["policy-oncall-duty.md"], ["3개월"], "입사 후 3개월이 지난 뒤 편성한다.")

# ------------------------------------------------------------------ numeric (10 a + 2 b)
add("numeric", "log-srv-02 장애 때 삭제한 로그 용량은 얼마인가요?",
    ["incident-disk-full-log02-20260521.md"], ["640GB"], "30일이 지난 로그 640GB를 삭제하였다.")
add("numeric", "코어 스위치 CSW-01 장애로 인터넷을 쓰지 못한 사용자는 약 몇 명인가요?",
    ["incident-network-core-sw-20260707.md"], ["210명"], "약 210명이다.")
add("numeric", "nas-bk-03 장애에서 야간 백업 작업 중 몇 개가 실패했나요?",
    ["incident-disk-full-nas03-20260814.md"], ["14개 중 5개"], "14개 중 5개가 실패하였다.")
add("numeric", "누리메일 서버 이전 예산은 얼마로 확정되었나요?",
    ["meeting-weekly-20260713.md"], ["4,200만원"], "4,200만원이다.")
add("numeric", "SIEM 구축 프로젝트의 예산 집행률은 몇 퍼센트인가요?",
    ["project-status-siem-202609.md"], ["56.6%"], "집행률은 56.6%이다.")
add("numeric", "무선 AP 교체는 총 몇 대 중 몇 대가 완료되었나요?",
    ["project-status-wifi-refresh-202609.md"], ["120대 중 76대"], "120대 중 76대가 완료되었다.")
add("numeric", "2026년 하반기 IT 인프라 예산 총액은 얼마인가요?",
    ["budget-memo-it-2026h2.md"], ["6억 2,000만원"], "총 6억 2,000만원이다.")
add("numeric", "방화벽 정책 변경에서 삭제하기로 한 사용 기간 종료 규칙은 몇 건인가요?",
    ["change-notice-fw-20260925.md"], ["31건"], "31건이다.")
add("numeric", "해든DB 2026년 연간 유지보수 금액은 얼마였나요?",
    ["email-license-renewal-20260828.md", "budget-memo-licenses-2027.md"], ["8,300만원"], "8,300만원이었다.")
add("numeric", "2027년 라이선스 예산 합계는 2026년보다 얼마나 늘어나나요?",
    ["budget-memo-licenses-2027.md"], ["800만원"], "800만원 증가한다.")
add("numeric", "2026년 3분기 잠정 매출액은 얼마인가요?",
    ["closing-q3-2026-status.md"], ["182억 4,000만원"], "182억 4,000만원이다.", space="dept-b")
add("numeric", "연간 복지포인트는 1인당 얼마가 지급되나요?",
    ["notice-welfare-2026.md"], ["800,000원"], "1인당 800,000원이다.", space="dept-b")

# ------------------------------------------------------------------ multi_doc (11 a + 1 b)
add("multi_doc", "3월, 5월, 8월에 발생한 디스크 용량 부족 장애 중 복구 시간이 가장 길었던 서버는 어디이고 몇 분이었나요?",
    ["incident-disk-full-db01-20260312.md", "incident-disk-full-log02-20260521.md", "incident-disk-full-nas03-20260814.md"],
    ["log-srv-02", "130분"], "log-srv-02 장애가 130분으로 가장 길었다(db-prd-01 45분, nas-bk-03 80분).")
add("multi_doc", "db-prd-01 장애와 nas-bk-03 장애의 복구 시간을 각각 알려주세요.",
    ["incident-disk-full-db01-20260312.md", "incident-disk-full-nas03-20260814.md"],
    ["45분", "80분"], "db-prd-01은 45분, nas-bk-03은 80분이다.")
add("multi_doc", "ERP DB 클라우드 이전 예산이 8월 회의와 9월 회의에서 각각 얼마였나요?",
    ["meeting-weekly-20260810.md", "meeting-weekly-20260921.md"],
    ["1억 2,000만원", "1억 4,500만원"], "8월 회의는 1억 2,000만원, 9월 회의는 1억 4,500만원이다.")
add("multi_doc", "메일 서버 이전 예산과 ERP DB 이전의 현재 예산을 합치면 얼마인가요?",
    ["meeting-weekly-20260713.md", "meeting-weekly-20260921.md"],
    ["4,200만원", "1억 4,500만원"], "4,200만원과 1억 4,500만원을 합친 1억 8,700만원이다.")
add("multi_doc", "해든DB 라이선스 갱신 계약 기한과, 그 기한을 넘기면 붙는 추가 비용은 무엇인가요?",
    ["email-license-renewal-20260828.md", "budget-memo-licenses-2027.md"],
    ["2026년 11월 30일", "10%"], "2026년 11월 30일까지 계약해야 하며 넘기면 재가입비 10%가 추가된다.")
add("multi_doc", "SIEM 로그 소스 연동 완료 수가 8월 31일 회의와 9월 말 보고서에서 각각 몇 개였나요?",
    ["meeting-weekly-20260831.md", "project-status-siem-202609.md"],
    ["24개", "31개"], "8월 31일에는 24개, 9월 말에는 31개가 완료되었다.")
add("multi_doc", "그룹웨어 첨부파일 한도는 2분기와 3분기에 각각 얼마로 올렸나요?",
    ["issue-list-2026q2.md", "issue-list-2026q3.md"],
    ["20MB", "50MB"], "2분기에 20MB, 3분기에 50MB로 상향하였다.")
add("multi_doc", "스토리지 증설 후 총 용량은 얼마이고, 증설을 추진하게 된 현재 NAS 사용률은 몇 퍼센트인가요?",
    ["project-status-storage-expansion-202610.md", "meeting-weekly-20260831.md"],
    ["64TB", "71%"], "증설 후 64TB이며 현재 NAS 전체 사용률은 71%이다.")
add("multi_doc", "VPN 장애로 영향받은 사용자 수와, 임시 게이트웨이의 동시 접속 한도는 각각 얼마인가요?",
    ["incident-vpn-gateway-20260619.md", "email-vpn-notice-20260619.md"],
    ["87명", "30명"], "영향 사용자는 87명, vpn-gw-02 동시 접속 한도는 30명이다.")
add("multi_doc", "프린터 스풀러 문제에 대해 2분기와 3분기에 각각 어떤 조치를 했나요?",
    ["issue-list-2026q2.md", "issue-list-2026q3.md"],
    ["수동 재시작", "재시작 스크립트"], "2분기에는 수동 재시작(임시 조치), 3분기에는 재시작 스크립트를 적용하였다.")
add("multi_doc", "9월 28일에 실수로 삭제된 주문 이력은 몇 건이고, 백업 절차서의 복구 목표 시간은 얼마인가요?",
    ["form-data-restore-request.docx", "procedure-backup-restore.md"],
    ["4,320건", "4시간"], "약 4,320건이 삭제되었고 백업 절차서의 RTO는 4시간이다.")
add("multi_doc", "2027년 상반기 신입 공채 인원과 2027년 정원 증원 인원은 각각 몇 명인가요?",
    ["meeting-hr-20260915.md", "memo-headcount-plan-2027.md"],
    ["12명", "18명"], "신입 공채는 12명, 정원 증원은 18명이다.", space="dept-b")

# ------------------------------------------------------------------ latest (8)
st = lambda *d: {"stale_docs": [A + x for x in d]}
add("latest", "가온ERP 데이터베이스의 클라우드 이전은 현재 언제 하기로 되어 있나요?",
    ["meeting-weekly-20260921.md"], ["2027-02-20"], "9월 21일 회의에서 2027-02-20으로 연기되었다.", **st("meeting-weekly-20260810.md"))
add("latest", "ERP DB 클라우드 이전 예산은 현재 얼마로 확정되어 있나요?",
    ["meeting-weekly-20260921.md"], ["1억 4,500만원"], "9월 21일 회의에서 1억 4,500만원으로 증액되었다.", **st("meeting-weekly-20260810.md", "budget-memo-it-2026h2.md"))
add("latest", "현행 비밀번호 정책의 최소 길이 기준은 몇 자인가요?",
    ["policy-password-2026.md"], ["12자"], "2026년 개정 정책에서 12자 이상이다.", **st("policy-password-2025.md"))
add("latest", "비밀번호는 현재 며칠마다 바꿔야 하나요?",
    ["policy-password-2026.md"], ["180일"], "2026년 개정 정책에서 180일마다 변경한다.", **st("policy-password-2025.md"))
add("latest", "비밀번호를 연속으로 틀렸을 때 계정은 현재 얼마 동안 잠기나요?",
    ["policy-password-2026.md"], ["15분"], "2026년 개정 정책에서 15분 동안 잠긴다.", **st("policy-password-2025.md"))
add("latest", "일간 백업은 현재 며칠 동안 보관하나요?",
    ["email-backup-policy-20260611.md"], ["30일"], "2026-07-01부터 30일로 연장되었다(절차서의 14일은 구기준).", **st("procedure-backup-restore.md"))
add("latest", "그룹웨어 첨부파일 최대 용량은 현재 얼마인가요?",
    ["issue-list-2026q3.md"], ["50MB"], "3분기에 50MB로 상향되어 현재 50MB이다.", **st("issue-list-2026q2.md"))
add("latest", "서버별 임계치 조정 이후 모니터링 오탐은 하루 평균 몇 건으로 줄었나요?",
    ["issue-list-2026q3.md"], ["23건"], "하루 평균 140건에서 23건으로 감소하였다.", **st("issue-list-2026q2.md"))

# ------------------------------------------------------------------ unanswerable (12)
for q in [
    "2026년 상반기 DDoS 공격 차단 건수는 몇 건인가요?",
    "쿠버네티스 클러스터의 워커 노드는 몇 대인가요?",
    "재해복구 센터는 어느 지역에 있나요?",
    "방화벽 fw-edge-01의 펌웨어 버전은 무엇인가요?",
    "서버실 출입 기록은 몇 년간 보관하나요?",
    "모바일 앱 서버 mob-app-01에서 발생한 장애 이력을 알려주세요.",
    "2025년 한 해 동안 발생한 전체 장애는 총 몇 건인가요?",
    "누리메일로 이전한 뒤 연간 비용은 얼마나 절감되나요?",
    "당직 대체휴무는 언제까지 사용해야 하나요?",
    "SIEM 제품의 공급 업체 이름은 무엇인가요?",
    "백업 소프트웨어의 제품명과 버전은 무엇인가요?",
    "캐시 DNS 서버 2대의 호스트 이름은 무엇인가요?",
]:
    add("unanswerable", q, [], [], NOANS)

# ------------------------------------------------------------------ cross_space (10)
for q, leak in [
    ("2026년 하반기 성과급은 언제 지급되나요?", ["2026년 10월 30일"]),
    ("기본 연차는 며칠인가요?", ["기본 연차 15일"]),
    ("급여는 매월 며칠에 지급되나요?", ["급여는 매월 25일"]),
    ("연간 복지포인트는 얼마인가요?", ["800,000원"]),
    ("업무 중 식대는 1인 1회 얼마까지 청구할 수 있나요?", ["12,000원"]),
    ("2027년 상반기 신입 공채는 몇 명을 뽑나요?", ["12명"]),
    ("3분기 잠정 영업이익은 얼마인가요?", ["21억 3,000만원"]),
    ("유럽 해외 출장 일비는 얼마인가요?", ["100달러"]),
    ("인사평가에서 S등급 비율은 몇 퍼센트인가요?", ["성과 평가 70%", "태도 평가 10%"]),
    ("장기 미수금은 합계 얼마인가요?", ["2억 4,500만원"]),
]:
    add("cross_space", q, [], [], REFUSE, leak_facts=leak)

# ------------------------------------------------------------------ table_merged (15 a + 1 b)
add("table_merged", "3분기 장애 대응 현황표에서 mail-gw-01 메일 지연 장애는 어느 구분으로 분류되어 있나요?",
    ["incident-status-2026q3.xlsx"], ["애플리케이션"], "구분은 애플리케이션이다(세로 병합된 구분 셀에 속한 행).")
add("table_merged", "3분기 장애 대응 현황표에서 nas-fs-01 장애는 어느 구분이고 복구 시간은 몇 분인가요?",
    ["incident-status-2026q3.xlsx"], ["스토리지", "22"], "구분은 스토리지이고 복구 시간은 22분이다.")
add("table_merged", "월별 예산표에서 4분기(10~12월)의 예산 소계는 얼마인가요(단위 만원)?",
    ["budget-monthly-2026h2.xlsx"], ["30,000"], "4분기 소계는 30,000만원이다.")
add("table_merged", "변경관리 대장에서 vpn-gw-01 인증서 갱신은 언제 변경했나요?",
    ["change-request-log-2026.xlsx"], ["2026-06-20"], "2026-06-20에 변경하였다(2분기 시트).")
add("table_merged", "월간 정기 점검 체크리스트의 결재란에서 팀장과 본부장은 누구인가요?",
    ["inspection-checklist-2026-10.xlsx"], ["오세훈", "임수정"], "팀장은 오세훈, 본부장은 임수정이다.")
add("table_merged", "월간 점검 체크리스트에서 비상등 점등 항목은 어느 구분이고 판정은 어떻게 나왔나요?",
    ["inspection-checklist-2026-10.xlsx"], ["안전", "조치 필요"], "구분은 안전이고 판정은 조치 필요이다.")
add("table_merged", "SIEM 4분기 일정표에서 11월 4주차에 동시에 진행되는 작업은 무엇인가요?",
    ["schedule-gantt-siem.xlsx"], ["탐지 룰 튜닝", "모의 해킹 점검"], "탐지 룰 튜닝과 모의 해킹 점검이 진행된다.")
add("table_merged", "서버 자산 대장에서 log-srv-02는 어느 센터의 몇 번 랙에 있나요?",
    ["server-asset-ledger.xlsx"], ["서울센터", "R02"], "서울센터 R02 랙에 있다.")
add("table_merged", "서버 자산 대장의 IP 대역 시트에서 부산센터 백업용 대역은 무엇인가요?",
    ["server-asset-ledger.xlsx"], ["10.20.30.0/24"], "부산센터 백업용 대역은 10.20.30.0/24(VLAN 30)이다.")
add("table_merged", "변경관리 대장에서 코어 스위치 CSW-01 펌웨어 적용 건의 위험도와 승인자는 누구인가요?",
    ["change-request-log-2026.xlsx"], ["높음", "임수정"], "위험도는 높음이고 승인자는 임수정이다.")
add("table_merged", "소프트웨어 라이선스 현황표에서 백업 소프트웨어의 활용률은 얼마인가요?",
    ["license-inventory-2026.xlsx"], ["64%"], "활용률은 64%이다.")
add("table_merged", "10월 당직표에서 한지우는 몇 주차 어느 시간대 당직인가요?",
    ["oncall-roster-2026-10.xlsx"], ["10월 2주차", "야간"], "10월 2주차 야간 당직이다.")
add("table_merged", "10월 1일 주간회의록 양식의 참석자 중 보안팀 소속은 누구인가요?",
    ["minutes-weekly-20261001.docx"], ["윤가람", "서지안"], "보안팀 소속은 윤가람과 서지안이다.")
add("table_merged", "장비 구매 신청서의 구매 금액 합계는 얼마인가요?",
    ["form-equipment-purchase-202610.docx"], ["5,060만원"], "합계는 5,060만원이다.")
add("table_merged", "UPS 정기 점검 결과서에서 A동의 부하율은 얼마인가요?",
    ["form-inspection-ups-202610.docx"], ["41%"], "A동 부하율은 41%이다.")
add("table_merged", "부서별 인건비 예산표에서 영업본부의 하반기 예산은 얼마인가요(단위 백만원)?",
    ["budget-hr-2026h2.xlsx"], ["4,300"], "영업본부 하반기 예산은 4,300백만원이다.", space="dept-b")

# ------------------------------------------------------------------ assemble
COUNTS = {"table_merged": 16, "lookup": 14, "paraphrase": 16, "numeric": 12, "multi_doc": 12,
          "latest": 8, "unanswerable": 12, "cross_space": 10}
HELDOUT = {"table_merged": 5, "lookup": 4, "paraphrase": 5, "numeric": 4, "multi_doc": 3,
           "latest": 2, "unanswerable": 4, "cross_space": 3}


def main():
    by = {}
    for item in Q:
        by.setdefault(item[0], []).append(item)
    for t, n in COUNTS.items():
        assert len(by[t]) == n, (t, len(by[t]), n)
    assert sum(HELDOUT.values()) == 30
    rows = []
    for t, items in by.items():
        n, h = len(items), HELDOUT[t]
        held = {int((j + 0.5) * n / h) for j in range(h)}
        assert len(held) == h
        for i, (tt, space, q, docs, facts, ans, extra) in enumerate(items):
            r = {"space": space, "question": q, "type": tt, "gold_docs": docs, "gold_facts": facts,
                 "gold_answer": ans, "split": "heldout" if i in held else "tune"}
            r.update(extra)
            rows.append(r)
    random.Random(20261002).shuffle(rows)
    with open(os.path.join(ROOT, "qa.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for i, r in enumerate(rows, 1):
            out = {"id": "q%03d" % i}
            out.update(r)
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    print("wrote", len(rows))


if __name__ == "__main__":
    main()
