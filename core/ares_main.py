from datetime import datetime
from memory import MemoryManager


def main() -> None:
    mem = MemoryManager()
    birth = mem.ensure_birth_memory()

    stats = mem.get_stats()

    # Pretty print birth date (local time from timestamp)
    birth_dt = datetime.fromisoformat(birth.timestamp.replace("Z", "+00:00"))

    print("=" * 40)
    print("🤖  ARES ONLINE")
    print("=" * 40)
    print(f"My first memory is: {birth.content}")
    print(f"Birth time (UTC): {birth_dt}")
    print()
    print(f"I currently remember {stats['total_memories']} things.")
    print("Memories by kind:", stats["by_kind"])
    print("=" * 40)


if __name__ == "__main__":
    main()
