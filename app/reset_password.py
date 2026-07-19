import hashlib
import json
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"


def hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 250_000
    )
    return digest.hex()


def main() -> None:
    if not CONFIG_PATH.exists():
        raise SystemExit("config.json does not exist. Run start.ps1 first.")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    password = secrets.token_urlsafe(14)
    salt = secrets.token_hex(16)
    config["password_salt"] = salt
    config["password_hash"] = hash_password(password, salt)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("New local remote desktop password:")
    print(password)
    print("Restart start.ps1 after resetting the password.")


if __name__ == "__main__":
    main()
