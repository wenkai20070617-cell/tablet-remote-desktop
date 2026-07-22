from __future__ import annotations

import secrets

from app.config import CONFIG_PATH, hash_password, load_config, save_config


def main() -> None:
    config, _ = load_config()
    password = secrets.token_urlsafe(14)
    salt = secrets.token_hex(16)
    config["password_salt"] = salt
    config["password_hash"] = hash_password(password, salt)
    save_config(config)
    print("New local remote desktop password:")
    print(password)
    print(f"Configuration: {CONFIG_PATH}")
    print("Restart Tablet Remote Desktop after resetting the password.")


if __name__ == "__main__":
    main()