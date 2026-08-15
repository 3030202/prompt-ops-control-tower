import unittest
import urllib.request
import urllib.parse
import json

class TestSend(unittest.TestCase):
    def test_send_message(self):
        token = "8898448488:AAHYi6L1UKQg5O7xj_LkpPH_xX7dqXBx7QY"
        chat_id = "@desp0tat"
        
        text = """🔑 <b>DAILY PASS // КОД ДНЯ: <code>0942</code></b>

📅 Дата: <b>2026-08-15</b> (MSK)
📊 В каталоге: <b>актуальные системные промпты, скиллы и операционные пайплайны</b>

Введите 4-значный код <code>0942</code> в веб-интерфейсе для полного доступа к каталогу, экспорту и AI-аналитике.

🌐 <b>TUI Каталог:</b> https://8.0x101.lol
⚡ <b>Lite витрина:</b> https://8.0x101.lol/lite

#DailyPass #PromptOps #AI"""

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode())
                print("Success:", result)
        except Exception as e:
            print("Error:", str(e))
            if hasattr(e, 'read'):
                print("Response:", e.read().decode())

if __name__ == "__main__":
    unittest.main()
