import json
import os
import sys
import boto3

config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "application", "config.json")


def load_existing_config():
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_aws_defaults():
    session = boto3.Session()
    region = session.region_name or "us-west-2"

    sts = boto3.client("sts")
    response = sts.get_caller_identity()
    account_id = response["Account"]

    return region, account_id


def prompt(label, default=None):
    if default:
        value = input(f"{label} [{default}]: ").strip()
        return value if value else default
    else:
        value = input(f"{label}: ").strip()
        return value


def main():
    print("=== Update config.json ===\n")

    config = load_existing_config()

    try:
        print("Fetching AWS account information...")
        region, account_id = get_aws_defaults()
    except Exception as e:
        print(f"Failed to fetch AWS information: {e}", file=sys.stderr)
        region = config.get("region", "us-west-2")
        account_id = config.get("accountId", "")

    config["region"] = region
    config["accountId"] = account_id
    config.setdefault("projectName", "power-trade")

    print(f"  region   : {region}")
    print(f"  accountId: {account_id}\n")

    config["s3_bucket"] = prompt("S3 bucket name", default=config.get("s3_bucket"))
    config["knowledge_base_id"] = prompt("Knowledge Base ID", default=config.get("knowledge_base_id"))
    config["tavily_api_key"] = prompt("Tavily API Key", default=config.get("tavily_api_key"))

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"\nSaved: {config_path}")
    print(json.dumps(config, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
