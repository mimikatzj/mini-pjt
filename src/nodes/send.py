"""send_email 노드 (배치 모드 9단계): compose_email이 만든 메일을 Gmail SMTP로 보낸다. LLM 호출 없음.

- dry-run이면 보내지 않는다 (status: skipped). 미리보기는 main.py가 tmp/preview.html로 저장한다
- 발송 실패는 예외로 실행을 멈추지 않고 status: failed로 남긴다. 다음 노드 분기에서 저장(commit_records)을 건너뛴다
  (SERVICE.md 8장 10단계: 발송에 성공한 뒤에만 기록 저장)
- trace에는 받는 사람 수만 남긴다 (메일 주소는 개인정보라 trace 파일에 쓰지 않는다)
"""

from src.tools import send_mail


def send_email(state: dict) -> dict:
    mail = state.get("mail") or {}
    if state.get("dry_run", True):
        sent = {"status": "skipped", "to": [], "error": ""}
    else:
        try:
            to = send_mail(mail["subject"], mail["html"], mail["text"])
            sent = {"status": "sent", "to": to, "error": ""}
        except Exception as e:  # SMTP 인증·연결 오류 등. 오류 종류와 메시지만 남긴다 (비밀번호는 메시지에 없음)
            sent = {"status": "failed", "to": [], "error": f"{type(e).__name__}: {e}"}
    return {"sent": sent,
            "trace": [{"step": "send_email", "input": {"subject": mail.get("subject", ""), "dry_run": state.get("dry_run", True)},
                       "output": {"status": sent["status"], "recipients": len(sent["to"]), "error": sent["error"]}}]}
