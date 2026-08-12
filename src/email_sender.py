import os
import json
import smtplib
from email.message import EmailMessage

# Настройки SMTP из переменных окружения
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

FROM_MAIL = SMTP_USER

def send_reply(to_email: str, subject: str, body: str):
    """Отправляет ответное письмо через SMTP Yandex."""
    reply_msg = EmailMessage()
    
    reply_msg["From"] = FROM_MAIL
    reply_msg["To"] = to_email
    reply_msg["Subject"] = subject
        
    reply_msg.set_content(body)
    
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(reply_msg)


def handler(event, context):
    """Точка входа для Cloud Function (принимает запрос от YaWL)."""
    
    if event.get("httpMethod") != "POST":
        return {"statusCode": 405, "body": f"Method {event.get('httpMethod')} Not Allowed"}

    try:
        body_raw = event.get("body", "")
        
        data = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
        
        
        to_email = data["to"]
        subject = data["subject"]
        body = data["body"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return {
            "statusCode": 400, 
            "body": json.dumps({"error": "Bad Request. JSON must contain 'to', 'subject', 'body'"})
        }

    try:
        send_reply(to_email, subject, body)
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "success"})
        }
    except Exception as e:
        print(f"SMTP error: {str(e)}")  # Логи в Cloud Logging
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal Server Error", "details": str(e)})
        }
