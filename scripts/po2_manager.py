#!/usr/bin/env python3
"""CLI utility to manage, inspect, and export Po2Bot chat dialogues and registration status.

Usage:
    python po2_manager.py users [--search SEARCH] [--limit LIMIT]
    python po2_manager.py dialog <user_id> [--limit LIMIT]
    python po2_manager.py stats
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

DEFAULT_DB = os.path.expanduser(
    os.getenv("PO2BOT_DB_PATH", "/root/services/po2bot/data/bot_support.db")
)


def get_db_connection(db_path: str):
    if not os.path.exists(db_path):
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def list_users(args):
    conn = get_db_connection(args.db)
    cursor = conn.cursor()
    query = """
        SELECT u.user_id, u.first_name, u.last_name, u.username, u.phone, u.apartment, u.user_type,
               u.last_message_text, u.last_message_at, COUNT(m.id) as message_count
        FROM chat_users u
        LEFT JOIN chat_messages m ON u.user_id = m.user_id
    """
    params = []
    if args.search:
        s = f"%{args.search.strip()}%"
        query += " WHERE (u.first_name LIKE ? OR u.last_name LIKE ? OR u.username LIKE ? OR u.phone LIKE ? OR u.apartment LIKE ? OR CAST(u.user_id AS TEXT) LIKE ?)"
        params.extend([s, s, s, s, s, s])

    query += " GROUP BY u.user_id ORDER BY COALESCE(u.last_message_at, u.updated_at) DESC LIMIT ? OFFSET ?"
    params.extend([args.limit, args.offset])

    cursor.execute(query, params)
    rows = cursor.fetchall()

    if not rows:
        print("No users found.")
        return

    print(f"{'User ID':<12} | {'Name':<20} | {'Apt':<6} | {'Role':<12} | {'Phone':<14} | {'Msgs':<5} | {'Last Active':<19}")
    print("-" * 105)
    for r in rows:
        name = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or (r['username'] or 'N/A')
        print(
            f"{r['user_id']:<12} | {name[:20]:<20} | {str(r['apartment'] or '-'):<6} | "
            f"{str(r['user_type'] or '-'):<12} | {str(r['phone'] or '-'):<14} | {r['message_count']:<5} | "
            f"{str(r['last_message_at'] or '-'):<19}"
        )


def show_dialog(args):
    conn = get_db_connection(args.db)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM chat_users WHERE user_id = ?", (args.user_id,))
    user = cursor.fetchone()

    if user:
        name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip() or user['username'] or 'Unknown'
        print(f"=== Dialogue for {name} (ID: {user['user_id']}, Apt: {user['apartment'] or '-'}, Phone: {user['phone'] or '-'}) ===")
    else:
        print(f"=== Dialogue for User ID: {args.user_id} ===")

    cursor.execute(
        """
        SELECT id, direction, sender_type, sender_name, text, media_type, media_path, created_at
        FROM chat_messages
        WHERE user_id = ?
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (args.user_id, args.limit),
    )
    rows = cursor.fetchall()
    if not rows:
        print("No messages found.")
        return

    for r in rows:
        direction_mark = ">>>" if r["direction"] == "in" else "<<<"
        sender = r["sender_name"] or r["sender_type"]
        media_tag = f" [{r['media_type']}]" if r["media_type"] and r["media_type"] != "text" else ""
        print(f"[{r['created_at']}] {direction_mark} {sender}{media_tag}:")
        text = r["text"] or ""
        for line in text.splitlines():
            print(f"    {line}")
        if r["media_path"]:
            print(f"    [Media: {r['media_path']}]")
        print()


def show_stats(args):
    conn = get_db_connection(args.db)
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM chat_users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT count(*) FROM chat_messages")
    total_msgs = cursor.fetchone()[0]
    cursor.execute("SELECT count(*) FROM chat_messages WHERE media_type IN ('photo', 'document')")
    total_docs = cursor.fetchone()[0]

    print("=== Po2Bot Database Statistics ===")
    print(f"Total Users Tracked:    {total_users}")
    print(f"Total Messages Logged:  {total_msgs}")
    print(f"Document Uploads:       {total_docs}")


def main():
    parser = argparse.ArgumentParser(description="Po2Bot Dialogue & Verification Manager")
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to bot_support.db SQLite file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # users
    p_users = subparsers.add_parser("users", help="List registered chat users")
    p_users.add_argument("--search", "-s", help="Filter by name, phone, apartment, or user_id")
    p_users.add_argument("--limit", "-l", type=int, default=50, help="Max users to display")
    p_users.add_argument("--offset", type=int, default=0, help="Query offset")
    p_users.set_defaults(func=list_users)

    # dialog
    p_dialog = subparsers.add_parser("dialog", help="Show chronological dialogue messages for user")
    p_dialog.add_argument("user_id", type=int, help="Target Telegram User ID")
    p_dialog.add_argument("--limit", "-l", type=int, default=100, help="Max messages to fetch")
    p_dialog.set_defaults(func=show_dialog)

    # stats
    p_stats = subparsers.add_parser("stats", help="Show database overview statistics")
    p_stats.set_defaults(func=show_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
