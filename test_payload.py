import base64

payload = "9e35f0bf-313f-4953-950f-442f44e36c57|00487716-d76e-4d4c-ad27-097347a40c6c"

encoded = base64.urlsafe_b64encode(payload.encode()).decode()

print(encoded)

