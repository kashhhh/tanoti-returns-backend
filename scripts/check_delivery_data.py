"""Read-only Shopify delivery audit. Prints no credentials or customer details."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from flask import Flask
import requests
from app.config import Config
from app.services import shopify_client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--created-on", required=True)
    args = parser.parse_args()
    app = Flask(__name__)
    app.config.from_object(Config)
    with app.app_context():
        response = requests.get(f"{shopify_client._base_url()}/orders.json",
            headers=shopify_client._headers(), params={"status": "any", "limit": 250,
                "created_at_min": args.created_on + "T00:00:00+05:30",
                "created_at_max": args.created_on + "T23:59:59+05:30"}, timeout=20)
        response.raise_for_status()
        orders = response.json().get("orders", [])
        fulfillments = [f for o in orders for f in o.get("fulfillments", []) if f.get("id")]
        print(json.dumps({"orders_on_date": len(orders), "has_more_pages": bool(response.links.get("next")),
                          "fulfillments": len(fulfillments)}), flush=True)
        if not fulfillments:
            return
        result = shopify_client.graphql('''query($ids: [ID!]!) {
          nodes(ids: $ids) { ... on Fulfillment { id deliveredAt displayStatus
            events(first: 50, reverse: true) { nodes { status happenedAt } pageInfo { hasNextPage } }
          } }
        }''', {"ids": [f"gid://shopify/Fulfillment/{f['id']}" for f in fulfillments[:100]]})
        nodes = {n["id"].rsplit("/", 1)[-1]: n for n in result["nodes"] if n}
        print(json.dumps([{"order_number": o.get("order_number"), "order_created_at": o.get("created_at"),
            "fulfillments": [{"created_at": f.get("created_at"), "shipment_status": f.get("shipment_status"),
                "delivery": nodes.get(str(f["id"]))} for f in o.get("fulfillments", []) if f.get("id")]}
            for o in orders], indent=2))


if __name__ == "__main__":
    main()
