"""Готовит платный плагин к продаже: вписывает адрес сервера, открытый ключ и ID товара, по возможности
запутывает код PyArmor и кладёт результат в license_server/ — магазин отправит его покупателю после оплаты.

    python tools/make_paid_plugin.py МОЙ_ПЛАГИН.py ID_ТОВАРА http://IP:ПОРТ

Открытый ключ берётся из license_server/data/signing_key.b64 (сервер должен быть хотя бы раз запущен у вас
или скачайте папку data с хостинга).
"""
import base64
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LS = ROOT / "license_server"


def public_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = base64.b64decode((LS / "data" / "signing_key.b64").read_text().strip())
    pk = Ed25519PrivateKey.from_private_bytes(raw).public_key()
    return base64.b64encode(pk.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    src, pid, server = Path(sys.argv[1]), sys.argv[2], sys.argv[3].rstrip("/")
    text = src.read_text(encoding="utf-8")
    text = re.sub(r'^PLUGIN_ID = .*$', f'PLUGIN_ID = "{pid}"', text, flags=re.M)
    text = re.sub(r'^SERVER = .*$', f'SERVER = "{server}"', text, flags=re.M)
    text = re.sub(r'^PUBKEY = .*$', f'PUBKEY = "{public_key()}"', text, flags=re.M)
    out_dir = ROOT / "build"
    out_dir.mkdir(exist_ok=True)
    plain = out_dir / src.name
    plain.write_text(text, encoding="utf-8")
    print("✅ Подставил ID, адрес и ключ:", plain)
    if shutil.which("pyarmor"):
        subprocess.run(["pyarmor", "gen", "-O", str(out_dir / "dist"), str(plain)], check=False)
        prot = out_dir / "dist" / src.name
        if prot.exists():
            print("✅ Код запутан PyArmor:", prot)
            print("   Покупателю нужен этот файл И папка pyarmor_runtime_* рядом с ним (заархивируйте вместе).")
    else:
        print("⚠️ PyArmor не установлен — файл НЕ защищён. Установите: pip install pyarmor и запустите снова.")
    dst = LS / src.name
    shutil.copy(plain if not (out_dir / "dist" / src.name).exists() else out_dir / "dist" / src.name, dst)
    print(f"📦 Скопировал в {dst}. В license_server/data/config.json у товара «{pid}» укажите \"file\": \"{src.name}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
