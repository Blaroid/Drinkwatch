"""The moderation queue for the one interface an operator actually has: a shell.

The admin API can list pending products, approve or reject one, and propose or
confirm cross-chain product links, but every one of those was a curl invocation
with a JSON body. This is that vocabulary as a program: the same endpoints, with
the evidence printed next to each decision. It never imports ``app`` so the test
suite can ignore it, and it talks to the running API so a decision takes the
same path (and the same ``price_daily`` side effects) as the Android client.

    python scripts/review.py queue
    python scripts/review.py approve monster-smoke-test-500ml --size-ml 500
    python scripts/review.py reject some-product --apply
    python scripts/review.py links --brand Monster
    python scripts/review.py confirm monster-ultra-500ml monster-ultra-500ml-x4

Writes are dry runs unless ``--apply`` is passed: the queue is read live, but a
typo in a slug should not change production. Base URL and token come from
``--base`` / ``--token`` or ``PUBLIC_BASE_URL`` / ``ADMIN_TOKEN``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def _client(args: argparse.Namespace) -> httpx.Client:
    if not args.token:
        print("ADMIN_TOKEN (or --token) is required for /v1/admin/*", file=sys.stderr)
        raise SystemExit(2)
    return httpx.Client(
        base_url=args.base.rstrip("/"),
        headers={"X-Admin-Token": args.token},
        timeout=30.0,
    )


def _get(client: httpx.Client, path: str, params: dict | None = None) -> object:
    response = client.get(path, params=params)
    if response.status_code >= 400:
        print(f"GET {path} -> {response.status_code}: {response.text[:300]}", file=sys.stderr)
        raise SystemExit(1)
    return response.json()


def _write(
    args: argparse.Namespace, client: httpx.Client, method: str, path: str, payload: dict
) -> object | None:
    if not args.apply:
        print(f"dry-run: {method} {str(client.base_url).rstrip('/')}{path}")
        print(json.dumps(payload, indent=2, default=str))
        print("nothing written; re-run with --apply")
        return None
    response = client.request(method, path, json=payload)
    if response.status_code >= 400:
        print(f"{method} {path} -> {response.status_code}: {response.text[:300]}", file=sys.stderr)
        raise SystemExit(1)
    body = response.json()
    print(json.dumps(body, indent=2, default=str))
    return body


def _dollars(cents: int | None) -> str:
    return "-" if cents is None else f"${cents / 100:.2f}"


def _when(value: str | None) -> str:
    return (value or "")[:19].replace("T", " ")


def cmd_queue(args: argparse.Namespace, client: httpx.Client) -> int:
    pending = _get(client, "/v1/admin/products/pending", {"limit": args.limit})
    flagged = _get(
        client, "/v1/admin/observations", {"status": "needs_review", "limit": args.limit}
    )

    print(f"Pending products ({len(pending)})")
    for product in pending:
        size = f"{product['size_ml']}ml" if product.get("size_ml") else "size unknown"
        pack = f" x{product['pack_count']}" if (product.get("pack_count") or 1) > 1 else ""
        print(
            f"\n  {product['slug']}\n"
            f"    {product['brand']} {product['name']} - {size}{pack}"
            f" - barcode {product.get('barcode') or 'none'} - queued {_when(product.get('created_at'))}"
        )
        if not product["sightings"]:
            print("    no crowd sighting attached")
        for sighting in product["sightings"]:
            where = sighting.get("store") or "chain-wide"
            suburb = f" ({sighting['suburb']})" if sighting.get("suburb") else ""
            note = f" note={sighting['note']!r}" if sighting.get("note") else ""
            photo = ""
            if sighting.get("photo_token"):
                photo = f" photo=/v1/photos/{sighting['photo_token']}"
            elif sighting.get("photo_url"):
                photo = f" photo={sighting['photo_url']}"
            print(
                f"    obs {sighting['id']}  {sighting['status']:<12} {_dollars(sighting['price_cents']):>8}"
                f"  {where}{suburb}  device={sighting['device_id']}{note}{photo}"
            )
        print(
            f"    -> python scripts/review.py approve {product['slug']}"
            f" --size-ml {product.get('size_ml') or 500} --apply"
        )

    print(f"\nObservations needing review ({len(flagged)})")
    for row in flagged:
        print(
            f"  obs {row['id']}  {row['product_label']} ({row['product_slug']})"
            f"  {_dollars(row['price_cents'])}  {row.get('store') or 'chain-wide'}"
            f"  device={row.get('device') or '-'}"
        )
        print(
            f"    -> python scripts/review.py observation {row['id']} accepted --apply"
            f"   (or rejected)"
        )
    if not pending and not flagged:
        print("  nothing waiting")
    return 0


def cmd_approve(args: argparse.Namespace, client: httpx.Client) -> int:
    payload: dict = {}
    if args.caffeine is not None:
        payload["caffeine_mg_per_100ml"] = args.caffeine
    if args.size_ml is not None:
        payload["size_ml"] = args.size_ml
    if args.pack_count is not None:
        payload["pack_count"] = args.pack_count
    if args.merge_into:
        payload["merge_into_slug"] = args.merge_into
    _write(args, client, "POST", f"/v1/admin/products/{args.slug}/approve", payload)
    return 0


def cmd_reject(args: argparse.Namespace, client: httpx.Client) -> int:
    _write(args, client, "POST", f"/v1/admin/products/{args.slug}/reject", {})
    return 0


def cmd_links(args: argparse.Namespace, client: httpx.Client) -> int:
    params: dict = {"min_score": args.min_score, "limit": args.limit}
    if args.brand:
        params["brand"] = args.brand
    suggestions = _get(client, "/v1/admin/products/suggestions", params)
    print(f"Link suggestions ({len(suggestions)})")
    for item in suggestions:
        left, right = item["left"], item["right"]
        print(
            f"\n  score {item['score']:.2f}  {item['size_ml']}ml x{item['pack_count']}\n"
            f"    {left['slug']:<45} chains={','.join(left['chains']) or '-'}\n"
            f"    {right['slug']:<45} chains={','.join(right['chains']) or '-'}"
        )
        print(f"    -> python scripts/review.py confirm {left['slug']} {right['slug']} --apply")
    if not suggestions:
        print("  none above the score threshold")
    return 0


def cmd_confirm(args: argparse.Namespace, client: httpx.Client) -> int:
    _write(
        args,
        client,
        "POST",
        f"/v1/admin/products/{args.slug}/link",
        {"target_slug": args.target},
    )
    return 0


def cmd_unlink(args: argparse.Namespace, client: httpx.Client) -> int:
    _write(
        args,
        client,
        "POST",
        f"/v1/admin/products/{args.slug}/link",
        {"target_slug": args.target, "unlink": True},
    )
    return 0


def cmd_observation(args: argparse.Namespace, client: httpx.Client) -> int:
    _write(
        args,
        client,
        "POST",
        f"/v1/admin/observations/{args.observation_id}/status",
        {"status": args.status},
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8788"),
        help="API base URL (default: PUBLIC_BASE_URL or http://127.0.0.1:8788)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("ADMIN_TOKEN", ""),
        help="admin token (default: ADMIN_TOKEN)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    # argparse only accepts a parent flag after the subcommand when the
    # subcommand declares it too, so `approve SLUG --apply` works as written in
    # the help text instead of forcing `--apply approve SLUG`.
    write = argparse.ArgumentParser(add_help=False)
    write.add_argument(
        "--apply",
        action="store_true",
        help="perform the write; without it the command is a dry run",
    )

    queue = sub.add_parser("queue", help="pending products with their crowd sightings")
    queue.add_argument("--limit", type=int, default=100)
    queue.set_defaults(func=cmd_queue)

    approve = sub.add_parser("approve", help="confirm a crowd-submitted product", parents=[write])
    approve.add_argument("slug")
    approve.add_argument("--caffeine", type=float, help="mg per 100 mL from the label")
    approve.add_argument("--size-ml", type=int, dest="size_ml")
    approve.add_argument("--pack-count", type=int, dest="pack_count")
    approve.add_argument("--merge-into", dest="merge_into", help="canonical product slug")
    approve.set_defaults(func=cmd_approve)

    reject = sub.add_parser("reject", help="reject a crowd-submitted product", parents=[write])
    reject.add_argument("slug")
    reject.set_defaults(func=cmd_reject)

    links = sub.add_parser("links", help="propose cross-chain product links")
    links.add_argument("--brand")
    links.add_argument("--min-score", type=float, default=0.34, dest="min_score")
    links.add_argument("--limit", type=int, default=100)
    links.set_defaults(func=cmd_links)

    confirm = sub.add_parser("confirm", help="link SLUG into TARGET's canonical group", parents=[write])
    confirm.add_argument("slug")
    confirm.add_argument("target")
    confirm.set_defaults(func=cmd_confirm)

    unlink = sub.add_parser("unlink", help="detach SLUG from its canonical group", parents=[write])
    unlink.add_argument("slug")
    unlink.add_argument("--target", required=True, help="any existing product slug")
    unlink.set_defaults(func=cmd_unlink)

    observation = sub.add_parser("observation", help="set one observation's moderation status", parents=[write])
    observation.add_argument("observation_id", type=int)
    observation.add_argument("status", choices=("accepted", "needs_review", "rejected"))
    observation.set_defaults(func=cmd_observation)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with _client(args) as client:
        return args.func(args, client)


if __name__ == "__main__":
    raise SystemExit(main())
