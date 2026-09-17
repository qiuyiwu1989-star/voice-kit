"""后台管理员密码：生成哈希 / 校验哈希。只用标准库 hashlib。

服务器上只存哈希，不存明文（docs/后台登录.md）：

    python -m voice_kit.admin_password            # 交互式输两次
    printf '%s\n' "$PW" | python -m voice_kit.admin_password --stdin

输出一行 `VOICEKIT_ADMIN_PASSWORD_HASH=...`，贴进 /etc/voicekit-svc.env。

哈希格式（分隔符用冒号，不用 `$`——免得 shell / docker-compose 把它当变量展开吃掉）：
    scrypt:<N>:<r>:<p>:<salt>:<dk>             默认
    pbkdf2_sha256:<iterations>:<salt>:<dk>      兜底（个别 Python 没编进 scrypt 时）
salt / dk 是不带填充的 urlsafe base64。
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import os
import sys

SCRYPT_N, SCRYPT_R, SCRYPT_P, DKLEN = 2 ** 14, 8, 1, 32
PBKDF2_ITER = 600_000


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, algo: str = "scrypt", *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("密码不能为空")
    salt = salt or os.urandom(16)
    pw = password.encode("utf-8")
    if algo == "scrypt":
        dk = hashlib.scrypt(pw, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN)
        return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{_b64(salt)}:{_b64(dk)}"
    if algo == "pbkdf2_sha256":
        dk = hashlib.pbkdf2_hmac("sha256", pw, salt, PBKDF2_ITER, dklen=DKLEN)
        return f"pbkdf2_sha256:{PBKDF2_ITER}:{_b64(salt)}:{_b64(dk)}"
    raise ValueError(f"不认识的算法 {algo}")


def verify_password(password: str, encoded: str) -> bool:
    """哈希格式不对一律 False，不抛——配错了就是登不进，不能变成放行。"""
    try:
        parts = (encoded or "").strip().split(":")
        pw = (password or "").encode("utf-8")
        if parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt, want = _unb64(parts[4]), _unb64(parts[5])
            if n < 2 ** 10 or not want:
                return False
            got = hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=len(want), maxmem=256 * 1024 * 1024)
        elif parts[0] == "pbkdf2_sha256" and len(parts) == 4:
            it, salt, want = int(parts[1]), _unb64(parts[2]), _unb64(parts[3])
            if it < 10_000 or not want:
                return False
            got = hashlib.pbkdf2_hmac("sha256", pw, salt, it, dklen=len(want))
        else:
            return False
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError, MemoryError):
        return False


def is_valid_hash(encoded: str) -> bool:
    parts = (encoded or "").strip().split(":")
    return (parts[0] == "scrypt" and len(parts) == 6) or (parts[0] == "pbkdf2_sha256" and len(parts) == 4)


def _weak(password: str) -> bool:
    return len(password) < 12 or password.isdigit() or password.isalpha()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m voice_kit.admin_password",
                                 description="生成后台管理员密码哈希（VOICEKIT_ADMIN_PASSWORD_HASH）")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读第一行作为密码（给运维管道用）")
    ap.add_argument("--algo", choices=["scrypt", "pbkdf2_sha256"], default="scrypt")
    a = ap.parse_args(argv)
    if a.stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
    else:
        pw = getpass.getpass("新密码：")
        if getpass.getpass("再输一次：") != pw:
            print("两次不一样，没生成。", file=sys.stderr)
            return 1
    if not pw:
        print("密码是空的，没生成。", file=sys.stderr)
        return 1
    if _weak(pw):
        print("提醒：这个密码偏弱（少于 12 位或只有数字/字母），上线后建议换强密码。", file=sys.stderr)
    print(f"VOICEKIT_ADMIN_PASSWORD_HASH={hash_password(pw, a.algo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
