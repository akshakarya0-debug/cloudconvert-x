"""Pengelolaan akun CloudConvert-X (dijalankan admin di dalam kontainer api).

  docker compose exec api python manage.py tambah-pengguna budi@contoh.com --nama "Budi"
  docker compose exec api python manage.py daftar
  docker compose exec api python manage.py ganti-password budi@contoh.com
  docker compose exec api python manage.py nonaktifkan budi@contoh.com
  docker compose exec api python manage.py aktifkan budi@contoh.com

Kata sandi selalu diminta lewat prompt (tidak pernah lewat argumen perintah).
"""
import argparse
import getpass
import os
import sys
import time

import redis

import auth


def get_redis():
    host, port = os.environ.get("REDIS_ADDR", "redis:6379").split(":")
    return redis.Redis(host=host, port=int(port), decode_responses=True)


def ask_password() -> str:
    pw = getpass.getpass("Kata sandi: ")
    if pw != getpass.getpass("Ulangi kata sandi: "):
        sys.exit("Kata sandi tidak sama.")
    return pw


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="manage.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("tambah-pengguna", help="buat akun baru")
    a.add_argument("email")
    a.add_argument("--nama", default="")
    sub.add_parser("daftar", help="tampilkan semua akun")
    for name, hlp in (("ganti-password", "ganti kata sandi (semua sesi dicabut)"),
                      ("nonaktifkan", "blokir akun (semua sesi dicabut)"),
                      ("aktifkan", "aktifkan kembali akun")):
        sub.add_parser(name, help=hlp).add_argument("email")
    args = p.parse_args(argv)

    auth.init_db()

    try:
        if args.cmd == "tambah-pengguna":
            uid = auth.create_user(args.email, args.nama, ask_password())
            print(f"Akun dibuat: {auth.norm_email(args.email)} (id {uid})")
        elif args.cmd == "daftar":
            users = auth.list_users()
            if not users:
                print("Belum ada akun. Buat dengan: python manage.py tambah-pengguna <email>")
            for u in users:
                dibuat = time.strftime("%Y-%m-%d", time.localtime(u["created_at"]))
                print(f"{u['id']:>3}  {u['email']:<35} {u['name']:<20} "
                      f"{'aktif' if u['active'] else 'NONAKTIF':<9} {dibuat}")
        elif args.cmd == "ganti-password":
            uid = auth.set_password(args.email, ask_password())
            if uid is None:
                sys.exit("Email tidak ditemukan.")
            n = auth.revoke_user_sessions(get_redis(), uid)
            print(f"Kata sandi diganti. {n} sesi dicabut.")
        elif args.cmd in ("nonaktifkan", "aktifkan"):
            aktif = args.cmd == "aktifkan"
            uid = auth.set_active(args.email, aktif)
            if uid is None:
                sys.exit("Email tidak ditemukan.")
            if aktif:
                print("Akun diaktifkan.")
            else:
                n = auth.revoke_user_sessions(get_redis(), uid)
                print(f"Akun dinonaktifkan. {n} sesi dicabut.")
    except ValueError as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
