import datetime
import locale
import os

import requests

from utils.logging import get_logger

logger = get_logger("bad-debt")

locale.setlocale(locale.LC_ALL, "en_US.UTF-8")


def get_data():
    url = os.environ["DATA_URL"]
    response = requests.get(url)

    if response.status_code == 200:
        data = response.json()
    else:
        raise Exception(f"Failed to get data: {response.status_code} - {response.text}")

    total_bad_debt = int(data["total"]) * -1
    decimals = int(data["decimals"])
    updated = int(data["updated"])
    tvl = int(data["tvl"])
    deposits = int(data["deposits"])
    borrows = int(data["borrows"])

    # ratio of bad debt to total value locked
    ratio_of_bad_debt = round(total_bad_debt / tvl * 100, 4)
    date = datetime.datetime.fromtimestamp(updated)
    debt = locale.currency(total_bad_debt / 10**decimals, grouping=True)
    tvl = locale.currency(tvl / 10**decimals, grouping=True)
    deposits = locale.currency(deposits / 10**decimals, grouping=True)
    borrows = locale.currency(borrows / 10**decimals, grouping=True)
    threshold = int(os.getenv("DEBT_THRESHOLD", 0))
    threshold_ratio = float(os.getenv("DEBT_THRESHOLD_RATIO", 100))
    protocol = os.getenv("PROTOCOL", "")

    message = f"⚠️ {protocol} Bad Debt ratio: {ratio_of_bad_debt}% at {date} ⚠️\nDebt: {debt}\nTVL: {tvl}\nDeposits: {deposits}\nBorrows: {borrows}"
    logger.info("%s", message)

    # Check if data is older than 1 day
    now = datetime.datetime.now()
    data_age = now - date
    is_data_old = data_age.total_seconds() > 24 * 60 * 60  # 1 day in seconds

    if is_data_old:
        old_data_message = f"🚨 {protocol} Data is stale! Last updated: {date} ({data_age.days} days, {data_age.seconds // 3600} hours ago)"
        logger.warning("Data is older than 1 day, sending telegram message...")
        send_telegram_message(old_data_message)

    if (threshold > 0 and total_bad_debt / 10**decimals > threshold) or ratio_of_bad_debt > threshold_ratio:
        accounts = accounts_with_bad_debt(data)
        message += f"\n{accounts}"
        send_telegram_message(message)
    else:
        logger.info("Thresholds not exceeded, no message sent")


def send_telegram_message(message):
    logger.info("Sending telegram message...")
    logger.info("%s", message)
    bot_token = os.getenv("BAD_DEBT_TELEGRAM_TOKEN")
    chat_id = os.getenv("BAD_DEBT_TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    params = {"chat_id": chat_id, "text": message}
    response = requests.get(url, params=params)
    if response.status_code != 200:
        raise Exception(f"Failed to send telegram message: {response.status_code} - {response.text}")


def accounts_with_bad_debt(data) -> str:
    # Sort users by bad debt (most negative first)
    sorted_users = sorted(data["users"], key=lambda x: int(x["badDebt"]))
    # Get top 5 accounts (or all if less than 5)
    top_accounts = sorted_users[:5]
    decimals = int(data["decimals"])

    # Format the message
    message = f"\nTop {len(top_accounts)} accounts with bad debt:"
    for user in top_accounts:
        # Convert bad debt to USD with proper formatting using locale
        bad_debt_usd = abs(int(user["badDebt"])) / 10**decimals
        formatted_debt = locale.currency(bad_debt_usd, grouping=True)
        message += f"\n- {user['user']} = {formatted_debt}"

    return message


if __name__ == "__main__":
    get_data()
