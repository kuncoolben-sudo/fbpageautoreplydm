"""
บอทเพจเฟสบุ๊ค: เมื่อมีคนคอมเม้นต์คำว่า "สนใจ" ใต้โพสต์
  1) ตอบกลับคอมเม้นต์นั้นอัตโนมัติ
  2) ส่งข้อความส่วนตัว (Private Reply) ไปหาคนคอมเม้นต์

ติดตั้ง:  pip install flask requests gunicorn
รันทดสอบ: python fb_comment_bot.py
รันจริง:  gunicorn -w 1 -b 0.0.0.0:8000 fb_comment_bot:app

ตัวแปรสภาพแวดล้อมที่ต้องตั้ง:
  PAGE_ID            ไอดีของเพจ
  PAGE_ACCESS_TOKEN  Page Access Token (แบบ long-lived)
  APP_SECRET         App Secret ของแอป (ใช้ตรวจลายเซ็น webhook)
  VERIFY_TOKEN       ข้อความอะไรก็ได้ที่คุณตั้งเอง ใช้ตอนยืนยัน webhook
  GRAPH_VERSION      (ไม่บังคับ) เช่น v21.0 ควรเช็กเวอร์ชันล่าสุดของ Meta
"""

import hashlib
import hmac
import logging
import os
import threading

import requests
from flask import Flask, abort, request

# ---------- ตั้งค่า ----------
PAGE_ID = os.environ["PAGE_ID"]
PAGE_ACCESS_TOKEN = os.environ["PAGE_ACCESS_TOKEN"]
APP_SECRET = os.environ["APP_SECRET"]
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v21.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

KEYWORDS = ["สนใจ"]  # เพิ่มคำได้ เช่น ["สนใจ", "ราคา", "interested"]

COMMENT_REPLY = "ขอบคุณที่สนใจนะคะ ทางเพจส่งรายละเอียดไปให้ทางข้อความส่วนตัวแล้วค่ะ 😊"
PRIVATE_MESSAGE = (
    "สวัสดีค่ะ ขอบคุณที่สนใจสินค้า/บริการของเรานะคะ\n"
    "รายละเอียดเพิ่มเติม: https://example.com\n"
    "หากมีคำถามพิมพ์ถามในแชทนี้ได้เลยค่ะ"
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fb-bot")

app = Flask(__name__)

# กันตอบซ้ำ (Meta อาจส่ง event เดิมมาหลายครั้ง)
# หมายเหตุ: เก็บในหน่วยความจำ ถ้ารีสตาร์ทจะหาย ถ้าจริงจังควรเก็บลง DB/Redis
_processed = set()
_lock = threading.Lock()


# ---------- ยืนยัน webhook (Meta เรียกครั้งแรกตอนตั้งค่า) ----------
@app.get("/webhook")
def verify():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge", ""), 200
    abort(403)


# ---------- รับ event ----------
@app.post("/webhook")
def receive():
    raw = request.get_data()
    if not _valid_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        abort(403)

    payload = request.get_json(silent=True) or {}
    if payload.get("object") == "page":
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                # ทำงานใน thread แยก เพื่อตอบ 200 ให้ Meta ทันที
                threading.Thread(
                    target=_handle_change, args=(change,), daemon=True
                ).start()
    return "EVENT_RECEIVED", 200


def _valid_signature(raw: bytes, header: str) -> bool:
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


# ---------- ตรรกะหลัก ----------
def _handle_change(change: dict):
    try:
        if change.get("field") != "feed":
            return
        v = change.get("value", {})
        if v.get("item") != "comment" or v.get("verb") != "add":
            return

        comment_id = v.get("comment_id")
        message = (v.get("message") or "").strip()
        author_id = (v.get("from") or {}).get("id")

        # ข้ามคอมเม้นต์ของเพจเอง (กันบอทตอบวนลูป)
        if not comment_id or author_id == PAGE_ID:
            return
        # ตอบเฉพาะคอมเม้นต์หลัก ไม่ตอบคอมเม้นต์ที่เป็นการตอบกลับ
        if v.get("parent_id") != v.get("post_id"):
            return
        if not any(k.lower() in message.lower() for k in KEYWORDS):
            return

        with _lock:
            if comment_id in _processed:
                return
            _processed.add(comment_id)

        log.info("keyword hit on comment %s", comment_id)
        reply_to_comment(comment_id, COMMENT_REPLY)
        send_private_reply(comment_id, PRIVATE_MESSAGE)
    except Exception:
        log.exception("failed to handle change")


def reply_to_comment(comment_id: str, text: str):
    r = requests.post(
        f"{GRAPH}/{comment_id}/comments",
        data={"message": text, "access_token": PAGE_ACCESS_TOKEN},
        timeout=15,
    )
    _log_result("comment reply", r)


def send_private_reply(comment_id: str, text: str):
    r = requests.post(
        f"{GRAPH}/{PAGE_ID}/messages",
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={"recipient": {"comment_id": comment_id}, "message": {"text": text}},
        timeout=15,
    )
    _log_result("private reply", r)


def _log_result(label: str, r: requests.Response):
    if r.ok:
        log.info("%s ok: %s", label, r.text)
    else:
        log.error("%s failed %s: %s", label, r.status_code, r.text)


if __name__ == "__main__":
    app.run(port=8000)
