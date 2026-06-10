"""One-off recovery: deliver the two Yearn timelock alerts that were lost on 2026-05-28 09:39 UTC.

Both messages were captured from the LOG_LEVEL=DEBUG dry-run of
`timelock_alerts.py --no-cache --since-seconds 172800 --protocol YEARN_TIMELOCK`.
The only edit vs. what the script produced is `YEARN\\_TIMELOCK` instead of `YEARN_TIMELOCK` —
the unescaped underscore was opening a Markdown V1 italic that never closed, causing the original 400.

Sent in a single chunk joined by '\\n\\n---\\n\\n' to match what process_events would have built.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

# Mainnet — scheduled 2026-05-27 17:55 UTC, op 0x5dac358a2f25b7148e
MAINNET = r"""⏰ *TIMELOCK: New Operation Scheduled*
🅿️ Protocol: YEARN\_TIMELOCK
📋 Yearn TimelockController: [0x88ba032be87d5ef1fbe87336b7090767f367bf73](https://etherscan.io/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73)
🔗 Chain: Mainnet
⏳ Delay: 7d
--- Call 0 ---
🎯 Target: [0x696d02db93291651ed510704c9b286841d506987](https://etherscan.io/address/0x696d02db93291651ed510704c9b286841d506987)
📝 Function: `add_strategy(address)`
    ├ address: `0x908244B6ef0e52911a380a5454aEC0743598Fb20`
--- Call 1 ---
🎯 Target: [0x696d02db93291651ed510704c9b286841d506987](https://etherscan.io/address/0x696d02db93291651ed510704c9b286841d506987)
📝 Function: `update_max_debt_for_strategy(address,uint256)`
    ├ address: `0x908244B6ef0e52911a380a5454aEC0743598Fb20`
    ├ uint256: `100000000000000`

🤖 *AI Summary:*
Adds a new CCTPStrategy to the Yearn V3 yvUSD vault and caps its debt at 100,000 USDC (6-decimal). Strategy inclusion without a prior safety review is routine for Yearn, and the max debt limit is moderate. No funds move immediately; the strategy becomes available for future deposits. LOW
🔗 Tx: [0xa6e8a54c3ff514951bca921cc38af55278980937816e5d04cd2d88fcf406199c](https://etherscan.io/tx/0xa6e8a54c3ff514951bca921cc38af55278980937816e5d04cd2d88fcf406199c)"""

# Base — scheduled 2026-05-27 18:04 UTC, op 0xe8c04f74b9b8bd6687
BASE = r"""⏰ *TIMELOCK: New Operation Scheduled*
🅿️ Protocol: YEARN\_TIMELOCK
📋 Yearn TimelockController: [0x88ba032be87d5ef1fbe87336b7090767f367bf73](https://basescan.org/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73)
🔗 Chain: Base
⏳ Delay: 1d
🎯 Target: [0x88ba032be87d5ef1fbe87336b7090767f367bf73](https://basescan.org/address/0x88ba032be87d5ef1fbe87336b7090767f367bf73)
📝 Function: `updateDelay(uint256)`
    ├ uint256: `604800`

🤖 *AI Summary:*
Sets the minimum timelock delay to 604800 seconds (7 days). This applies to all future scheduled operations, slowing the pace at which any governance action can be executed. No assets are moved and no roles are changed. LOW
🔗 Tx: [0x7c03e52b297ffc6d73ddcc7ed14621295f76d058f82d96b734185c182b717a7a](https://basescan.org/tx/0x7c03e52b297ffc6d73ddcc7ed14621295f76d058f82d96b734185c182b717a7a)"""

bot_token = os.environ["TELEGRAM_BOT_TOKEN_DEFAULT"]
chat_id = os.environ["TELEGRAM_CHAT_ID_TOPICS"]
topic_id = int(os.environ["TELEGRAM_TOPIC_ID_YEARN_TIMELOCK"])
url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

# Only send the Base one now — Mainnet already landed via the earlier diagnostic.
payload = {
    "chat_id": chat_id,
    "text": BASE,
    "parse_mode": "Markdown",
    "message_thread_id": topic_id,
}
r = requests.post(url, json=payload, timeout=10)
print(f"Base alert: status={r.status_code}")
ok = r.json().get("ok", False)
print(f"  ok={ok}")
if not ok:
    print(f"  body={r.text}")
else:
    print(f"  message_id={r.json()['result']['message_id']}")
