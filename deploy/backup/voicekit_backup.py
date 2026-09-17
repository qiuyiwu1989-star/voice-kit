"""voice-kit 每日备份：agent 文件 + 用户记忆 + 回流日志 + 租户表 +（有的话）Postgres → 本地滚动保留 + 上传 COS。

为什么要有：用户记忆和回流日志不在 git 里，只躺在服务器磁盘上（2026-09-17 盘点时发现没有任何备份）。

    sudo /opt/voicekit-backup/venv/bin/python /opt/voicekit-backup/voicekit_backup.py

配置读 /etc/voicekit-backup.env（root 600，不进仓库）：
    COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET / COS_REGION
    VOICEKIT_BACKUP_HOST     这台机器在 COS 里的目录名，例如 nanjing / shanghai
    VOICEKIT_AGENTS_DIR      默认 /opt/voicekit-svc/agents
    VOICEKIT_TENANTS         默认 /opt/voicekit-svc/tenants.json
    VOICEKIT_DATABASE_URL    可选；给了就 pg_dump
    VOICEKIT_BACKUP_DIR      默认 /var/backups/voicekit
    VOICEKIT_BACKUP_KEEP     本地保留天数，默认 14

COS 上的位置：<bucket>/backups/voicekit/<host>/<YYYYMMDD>/<文件>
恢复：下载 → tar xzf agents-*.tar.gz -C /opt/voicekit-svc/ ；gunzip -c db-*.sql.gz | psql "$VOICEKIT_DATABASE_URL"

**不备份任何 .env**：密钥不进备份包，丢了在控制台重新发。
"""
from __future__ import annotations

import gzip
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path


def load_env(path: str = "/etc/voicekit-backup.env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _skip_env(ti: tarfile.TarInfo):
    name = os.path.basename(ti.name)
    if name.endswith(".env") or name.startswith(".env") or "__pycache__" in ti.name:
        return None
    return ti


def build(out_dir: Path, stamp: str) -> list[Path]:
    made: list[Path] = []
    agents = Path(os.environ.get("VOICEKIT_AGENTS_DIR", "/opt/voicekit-svc/agents"))
    tenants = Path(os.environ.get("VOICEKIT_TENANTS", "/opt/voicekit-svc/tenants.json"))
    if agents.is_dir():
        f = out_dir / f"agents-{stamp}.tar.gz"
        with tarfile.open(f, "w:gz") as tar:
            tar.add(agents, arcname="agents", filter=_skip_env)
            if tenants.exists():
                tar.add(tenants, arcname="tenants.json")
        made.append(f)
    url = os.environ.get("VOICEKIT_DATABASE_URL", "")
    if url:
        f = out_dir / f"db-{stamp}.sql.gz"
        proc = subprocess.run(["pg_dump", "--no-owner", "--dbname", url], capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump 失败: {proc.stderr.decode(errors='ignore')[:300]}")
        with gzip.open(f, "wb") as g:
            g.write(proc.stdout)
        made.append(f)
    if not made:
        raise RuntimeError("什么都没备份：agents 目录不存在，也没配数据库")
    return made


def upload(files: list[Path], stamp_day: str) -> list[str]:
    from qcloud_cos import CosConfig, CosS3Client

    cfg = CosConfig(Region=os.environ["COS_REGION"], SecretId=os.environ["COS_SECRET_ID"],
                    SecretKey=os.environ["COS_SECRET_KEY"], Scheme="https")
    client = CosS3Client(cfg)
    bucket = os.environ["COS_BUCKET"]
    host = os.environ.get("VOICEKIT_BACKUP_HOST") or socket.gethostname()
    keys = []
    for f in files:
        key = f"backups/voicekit/{host}/{stamp_day}/{f.name}"
        client.upload_file(Bucket=bucket, Key=key, LocalFilePath=str(f), EnableMD5=True)
        head = client.head_object(Bucket=bucket, Key=key)
        if int(head.get("Content-Length", -1)) != f.stat().st_size:
            raise RuntimeError(f"上传后大小对不上: {key}")
        keys.append(key)
    return keys


def prune(out_dir: Path, keep_days: int) -> int:
    cutoff = time.time() - keep_days * 86400
    n = 0
    for f in out_dir.glob("*.gz"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            n += 1
    return n


def main() -> int:
    load_env(os.environ.get("VOICEKIT_BACKUP_ENV", "/etc/voicekit-backup.env"))
    out_dir = Path(os.environ.get("VOICEKIT_BACKUP_DIR", "/var/backups/voicekit"))
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    files = build(out_dir, stamp)
    for f in files:
        os.chmod(f, 0o600)
    keys = upload(files, stamp[:8]) if os.environ.get("COS_BUCKET") else []
    pruned = prune(out_dir, int(os.environ.get("VOICEKIT_BACKUP_KEEP", "14")))
    total = sum(f.stat().st_size for f in files)
    print(f"[voicekit-backup] {stamp} 本地 {len(files)} 个 {total/1024:.0f}KB · COS {len(keys)} 个 · 清理旧 {pruned} 个")
    for k in keys:
        print(f"  cos://{os.environ['COS_BUCKET']}/{k}")
    if os.environ.get("COS_BUCKET") and len(keys) != len(files):
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # cron 日志里要看得到原因
        print(f"[voicekit-backup] 失败: {e}", file=sys.stderr)
        sys.exit(1)
