#!/usr/bin/env python3
"""
Tesla Dashcam Decryptor – decryption service (scheduler).

The container does NOT communicate with Tesla. FEKs arrive via the browser
bookmarklet and are stored by the web viewer in keys.json (/data). This loop
periodically decrypts all clips for which a FEK is available.

  python main.py run --src <ENC_DIR> --out <DEC_DIR> [--delete] [--interval N]
"""
import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline
import keybridge


def cmd_run(args):
    def once():
        keys = keybridge.load_keys()
        r = pipeline.decrypt_pending(args.src, args.out, keys,
                                     delete_originals=args.delete)
        print(f"[{time.strftime('%H:%M:%S')}] pending={r['pending']} "
              f"with_key={r['had_keys']} decrypted={r['decrypted']} "
              f"errors={r['errors']}", flush=True)

    if args.interval:
        print(f"Service mode: every {args.interval}s", flush=True)
        while True:
            try:
                once()
            except Exception as e:
                print("[ERROR]", e, flush=True)
            time.sleep(args.interval)
    else:
        once()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--src", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--delete", action="store_true")
    r.add_argument("--interval", type=int, default=0)
    a = p.parse_args()
    if a.cmd == "run":
        cmd_run(a)
