import os

import requests
from dotenv import load_dotenv

from utils.http import request_with_retry
from utils.logging import get_logger
from utils.telegram import send_telegram_message

load_dotenv()
api_key = os.getenv("GRAPH_API_KEY")
PROTOCOL = "silo"
logger = get_logger(PROTOCOL)


def check_positions():
    first = 100  # Number of items to fetch per request
    skip = 0  # Start with the first set of results

    # Silo ID's to monitor
    silo_ids = [
        "0xea9961280b48fe521ece83f6cd8a7e9b2c4ffc2e",  # PENDLE, there is bad debt so here for test purposes
        "0x7bec832FF8060cD396645Ccd51E9E9B0E5d8c6e4",  # weETH
        "0x4a2bd8dcc2539e19cb97DF98EF5afC4d069d9e4C",  # ezETH
        "0x69eC552BE56E6505703f0C861c40039e5702037A",  # WBTC
        "0xA8897b4552c075e884BDB8e7b704eB10DB29BF0D",  # wstETH
        "0x601B76d37a2e06E971d3D63Cf16f41A44E306013",  # uniETH
        "0x0696E6808EE11a5750733a3d821F9bB847E584FB",  # ARB
        # add here
    ]
    silo_ids_string = ",".join([f'"{silo_id}"' for silo_id in silo_ids])

    while True:
        query = f"""
            query QueryPositions {{
              siloPositions(
                first: {first},
                skip: {skip},
                where: {{
                  silo_: {{id_in: [{silo_ids_string}]}},
                  riskFactor_gt: 0.9, # >1.0 means insolvent, very close to this value would mean "about to be liquidated"
                  riskScore_gt: 50000, # 50K is usually around 50k$ so a good value, imo
                  totalBorrowValue_gt: 0
                }},
                orderBy: riskFactor,
                orderDirection: desc,
              ) {{
                account {{
                  id
                }}
                silo {{
                  id
                  name
                  marketAssets: market {{
                    inputToken {{
                      symbol
                    }}
                  }}
                }}
                totalBorrowValue
                riskFactor
                riskScore
              }}
            }}
        """

        json_data = {
            "query": query,
            "operationName": "QueryPositions",
        }

        try:
            response = request_with_retry(
                "post",
                f"https://gateway-arbitrum.network.thegraph.com/api/{api_key}/subgraphs/id/2ufoztRpybsgogPVW6j9NTn1JmBWFYPKbP7pAabizADU",
                json=json_data,
            )
        except requests.RequestException as e:
            logger.error("Graph API query failed after retries: %s", e)
            send_telegram_message(f"Graph API query failed after retries: {e}", PROTOCOL, True)
            return

        response_data = response.json()
        if "errors" in response_data:
            logger.error("GraphQL error in response: %s", response_data["errors"])
            send_telegram_message(f"GraphQL error in response: {response_data['errors']}", PROTOCOL, True)
            return

        # Check if there are any positions returned
        positions = response_data["data"]["siloPositions"]
        if not positions:
            break

        # Process each position
        for position in positions:
            wallet_address = position["account"]["id"]
            input_token_symbol = position["silo"]["marketAssets"][0]["inputToken"]["symbol"]
            silo_name = position["silo"]["name"]
            silo_id = position["silo"]["id"]
            risk_factor = position["riskFactor"]
            risk_score = position["riskScore"]
            total_borrow_value = position["totalBorrowValue"]

            message = f"""
            High Risk Position Detected!
            Wallet Address: {wallet_address}
            Input Token Symbol: {input_token_symbol}
            Silo Name: {silo_name}
            Silo ID: {silo_id}
            Risk Factor: {risk_factor}
            Risk Score: {risk_score}
            Total Borrow Value: {total_borrow_value}
            """
            disable_notification = True
            if float(risk_factor) > 1:
                disable_notification = False
            send_telegram_message(message, PROTOCOL, disable_notification)

        # Increment the skip value to fetch the next set of results
        skip += first


def main():
    logger.info("Checking positions in Arbitrum")
    check_positions()


if __name__ == "__main__":
    main()
