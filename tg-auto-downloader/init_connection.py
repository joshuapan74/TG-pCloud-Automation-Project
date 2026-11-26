"""Optional CLI login helper (for headless/terminal setup; GUI login is built into main.py --gui)."""

import getpass
from telethon import TelegramClient

from file_service import FileService


def main():
    file_service = FileService()
    api_id, api_hash = file_service.get_credentials()

    if not api_id or not api_hash:
        print("=== Telegram API 憑證設定 ===")
        print("請前往 https://my.telegram.org 取得您的 API ID 與 API Hash")
        raw_id = input("請輸入 TG_API_ID: ").strip()
        raw_hash = getpass.getpass("請輸入 TG_API_HASH (輸入不顯示): ").strip()
        ok, msg = file_service.set_credentials(raw_id, raw_hash)
        if not ok:
            print(f"[-] {msg}")
            return
        api_id, api_hash = file_service.get_credentials()

    print(f"[*] Session 將儲存於: {file_service.get_session_base_path()}.session")
    client = TelegramClient(file_service.get_session_base_path(), api_id, api_hash)

    async def run():
        await client.start()
        me = await client.get_me()
        print("\n" + "=" * 40)
        print("[+] 成功連線至 Telegram！")
        print(f"    帳號名稱: {me.first_name} {me.last_name or ''}".strip())
        print(f"    使用者名稱: @{me.username or '無'}")
        print("=" * 40)

    with client:
        client.loop.run_until_complete(run())


if __name__ == "__main__":
    main()
