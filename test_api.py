import sys
import os
import requests

login_res = requests.post("http://127.0.0.1:8001/auth/direct-login", json={"email": "jay.chaudhari@niveshaay.com"})
token = login_res.json()["token"]
headers = {"Authorization": f"Bearer {token}"}

# Test POST /api/simulator/calculate-return
print("Testing calculate-return:")
res = requests.post(
    "http://127.0.0.1:8001/api/simulator/calculate-return",
    headers=headers,
    json={"holdings": [], "sips": []}
)
print("Status:", res.status_code)
print("Response:", res.text)

# Test POST sips
print("\nTesting POST sip:")
res = requests.post(
    "http://127.0.0.1:8001/api/simulator/NIA-Sim/sips",
    headers=headers,
    json={"sip_date": "2026-04-01", "amount": 100000}
)
print("Status:", res.status_code)
print("Response:", res.text)

# Test GET audit-log
print("\nTesting GET audit-log:")
res = requests.get(
    "http://127.0.0.1:8001/api/admin/audit-log",
    headers=headers
)
print("Status:", res.status_code)
# print("Response:", res.text[:200])

